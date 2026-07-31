"""Bungie OAuth and authenticated API client."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

import aiohttp

from destiny_web_app.config import Settings


LOGGER = logging.getLogger(__name__)
MAX_MANIFEST_DOWNLOAD_BYTES = 1024 * 1024 * 1024


class BungieError(RuntimeError):
    """Base error for a failed Bungie request."""


class BungieAuthenticationRejected(BungieError):
    """The authorization grant or refresh token is no longer valid."""


class BungieTemporarilyUnavailable(BungieError):
    """Bungie or the network could not service the request."""


class BungieClient:
    AUTHORIZATION_URL = "https://www.bungie.net/en/OAuth/Authorize"
    TOKEN_URL = "https://www.bungie.net/Platform/App/OAuth/token/"
    CURRENT_MEMBERSHIPS_URL = (
        "https://www.bungie.net/Platform/User/GetMembershipsForCurrentUser/"
    )
    PROFILE_URL = (
        "https://www.bungie.net/Platform/Destiny2/"
        "{membership_type}/Profile/{membership_id}/"
    )
    MANIFEST_URL = "https://www.bungie.net/Platform/Destiny2/Manifest/"
    SET_LOCK_STATE_URL = (
        "https://www.bungie.net/Platform/Destiny2/Actions/Items/SetLockState/"
    )
    BUNGIE_ROOT = "https://www.bungie.net"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=90, connect=15),
                raise_for_status=False,
            )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
        self._session = None

    def authorization_url(self, state: str) -> str:
        self.settings.require_bungie()
        query = urlencode(
            {
                "client_id": self.settings.bungie_client_id,
                "response_type": "code",
                "state": state,
            }
        )
        return f"{self.AUTHORIZATION_URL}?{query}"

    async def exchange_code(self, code: str) -> dict[str, Any]:
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
            }
        )

    async def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        return await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )

    async def get_current_memberships(
        self,
        access_token: str,
    ) -> dict[str, Any]:
        session = await self._get_session()
        try:
            async with session.get(
                self.CURRENT_MEMBERSHIPS_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "X-API-Key": self.settings.bungie_api_key,
                },
            ) as response:
                payload = await read_json(response)
        except (aiohttp.ClientError, TimeoutError) as error:
            raise BungieTemporarilyUnavailable(
                "Could not reach Bungie to load the signed-in user."
            ) from error

        if response.status == 401:
            raise BungieAuthenticationRejected(
                "Bungie rejected the access token."
            )
        if response.status == 429 or response.status >= 500:
            raise BungieTemporarilyUnavailable(
                "Bungie is temporarily unavailable."
            )
        if response.status >= 400:
            raise BungieError(
                f"Bungie membership request failed with HTTP {response.status}."
            )

        error_code = payload.get("ErrorCode")
        if error_code != 1:
            message = payload.get("Message") or payload.get("ErrorStatus")
            raise BungieError(message or "Bungie returned an API error.")

        membership_data = payload.get("Response")
        if not isinstance(membership_data, dict):
            raise BungieError("Bungie returned invalid membership data.")
        return membership_data

    async def get_profile(
        self,
        access_token: str,
        *,
        membership_type: int,
        membership_id: str,
        components: tuple[int, ...],
    ) -> dict[str, Any]:
        """Fetch an authenticated Destiny profile with explicit components."""
        session = await self._get_session()
        url = self.PROFILE_URL.format(
            membership_type=membership_type,
            membership_id=membership_id,
        )
        try:
            async with session.get(
                url,
                params={"components": ",".join(map(str, components))},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "X-API-Key": self.settings.bungie_api_key,
                },
            ) as response:
                payload = await read_json(response)
        except (aiohttp.ClientError, TimeoutError) as error:
            raise BungieTemporarilyUnavailable(
                "Could not reach Bungie to synchronize the inventory."
            ) from error

        if response.status == 401:
            raise BungieAuthenticationRejected(
                "Bungie rejected the access token during inventory sync."
            )
        if response.status == 429 or response.status >= 500:
            raise BungieTemporarilyUnavailable(
                "Bungie is temporarily unavailable or throttling requests."
            )
        if response.status >= 400:
            raise BungieError(
                f"Bungie profile request failed with HTTP {response.status}."
            )

        error_code = payload.get("ErrorCode")
        if error_code != 1:
            message = payload.get("Message") or payload.get("ErrorStatus")
            throttle_seconds = payload.get("ThrottleSeconds")
            if isinstance(throttle_seconds, int) and throttle_seconds > 0:
                raise BungieTemporarilyUnavailable(
                    message or "Bungie asked the application to retry later."
                )
            raise BungieError(message or "Bungie returned a profile API error.")

        profile = payload.get("Response")
        if not isinstance(profile, dict):
            raise BungieError("Bungie returned invalid profile data.")
        return profile

    async def set_item_lock_state(
        self,
        access_token: str,
        *,
        item_instance_id: str,
        character_id: str,
        membership_type: int,
        locked: bool,
    ) -> int:
        """Set one instanced item's lock state and return Bungie's throttle."""
        session = await self._get_session()
        try:
            async with session.post(
                self.SET_LOCK_STATE_URL,
                json={
                    "state": locked,
                    "itemId": item_instance_id,
                    "characterId": character_id,
                    "membershipType": membership_type,
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "X-API-Key": self.settings.bungie_api_key,
                },
            ) as response:
                payload = await read_json(response)
        except (aiohttp.ClientError, TimeoutError) as error:
            raise BungieTemporarilyUnavailable(
                "Could not reach Bungie while changing an item lock."
            ) from error

        if response.status == 401:
            raise BungieAuthenticationRejected(
                "Bungie rejected the access token while changing item locks."
            )
        if response.status == 429 or response.status >= 500:
            raise BungieTemporarilyUnavailable(
                "Bungie is temporarily unavailable or throttling item actions."
            )
        if response.status >= 400:
            raise BungieError(
                f"Bungie lock request failed with HTTP {response.status}."
            )
        if payload.get("ErrorCode") != 1:
            message = payload.get("Message") or payload.get("ErrorStatus")
            throttle = payload.get("ThrottleSeconds")
            if isinstance(throttle, int) and throttle > 0:
                raise BungieTemporarilyUnavailable(
                    message or "Bungie asked the application to retry later."
                )
            raise BungieError(message or "Bungie rejected an item lock action.")
        throttle = payload.get("ThrottleSeconds")
        return throttle if isinstance(throttle, int) and throttle > 0 else 0

    async def get_manifest_metadata(self) -> dict[str, Any]:
        session = await self._get_session()
        try:
            async with session.get(
                self.MANIFEST_URL,
                headers={"X-API-Key": self.settings.bungie_api_key},
            ) as response:
                payload = await read_json(response)
        except (aiohttp.ClientError, TimeoutError) as error:
            raise BungieTemporarilyUnavailable(
                "Could not reach Bungie to check the Destiny manifest."
            ) from error

        if response.status == 429 or response.status >= 500:
            raise BungieTemporarilyUnavailable(
                "Bungie is temporarily unavailable or throttling requests."
            )
        if response.status >= 400:
            raise BungieError(
                f"Bungie manifest request failed with HTTP {response.status}."
            )
        if payload.get("ErrorCode") != 1:
            message = payload.get("Message") or payload.get("ErrorStatus")
            raise BungieError(message or "Bungie returned a manifest API error.")
        manifest = payload.get("Response")
        if not isinstance(manifest, dict):
            raise BungieError("Bungie returned invalid manifest metadata.")
        return manifest

    async def download_manifest_content(
        self,
        content_path: str,
        destination: Path,
    ) -> None:
        parsed_path = urlsplit(content_path)
        if (
            not content_path.startswith("/")
            or content_path.startswith("//")
            or parsed_path.scheme
            or parsed_path.netloc
        ):
            raise BungieError("Bungie returned an invalid manifest content path.")
        session = await self._get_session()
        url = urljoin(self.BUNGIE_ROOT, content_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=600, connect=15),
            ) as response:
                if response.status == 429 or response.status >= 500:
                    raise BungieTemporarilyUnavailable(
                        "Bungie is temporarily unavailable while downloading "
                        "the manifest."
                    )
                if response.status >= 400:
                    raise BungieError(
                        "Bungie manifest download failed with HTTP "
                        f"{response.status}."
                    )
                content_length = response.content_length
                if (
                    content_length is not None
                    and content_length > MAX_MANIFEST_DOWNLOAD_BYTES
                ):
                    raise BungieError(
                        "The Destiny manifest download is unexpectedly large."
                    )
                downloaded = 0
                with destination.open("wb") as output:
                    async for chunk in response.content.iter_chunked(1024 * 256):
                        downloaded += len(chunk)
                        if downloaded > MAX_MANIFEST_DOWNLOAD_BYTES:
                            raise BungieError(
                                "The Destiny manifest download is "
                                "unexpectedly large."
                            )
                        output.write(chunk)
        except (aiohttp.ClientError, TimeoutError) as error:
            raise BungieTemporarilyUnavailable(
                "The Destiny manifest download was interrupted."
            ) from error

    async def _token_request(
        self,
        data: dict[str, str],
    ) -> dict[str, Any]:
        self.settings.require_bungie()
        session = await self._get_session()
        try:
            async with session.post(
                self.TOKEN_URL,
                data=data,
                auth=aiohttp.BasicAuth(
                    self.settings.bungie_client_id,
                    self.settings.bungie_client_secret,
                ),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as response:
                payload = await read_json(response)
        except (aiohttp.ClientError, TimeoutError) as error:
            raise BungieTemporarilyUnavailable(
                "Could not reach Bungie's authentication service."
            ) from error

        if response.status in (400, 401):
            error_name = payload.get("error")
            description = payload.get("error_description")
            LOGGER.warning(
                "Bungie rejected an OAuth request: %s",
                error_name or response.status,
            )
            raise BungieAuthenticationRejected(
                description or "Bungie rejected the authorization."
            )
        if response.status >= 500:
            raise BungieTemporarilyUnavailable(
                "Bungie's authentication service is temporarily unavailable."
            )
        if response.status >= 400:
            raise BungieError(
                f"Bungie authentication failed with HTTP {response.status}."
            )
        if not isinstance(payload.get("access_token"), str):
            raise BungieError("Bungie returned an invalid token response.")
        return payload

    async def _get_session(self) -> aiohttp.ClientSession:
        await self.start()
        assert self._session is not None
        return self._session


async def read_json(response: aiohttp.ClientResponse) -> dict[str, Any]:
    try:
        payload = await response.json()
    except (aiohttp.ContentTypeError, ValueError) as error:
        raise BungieError(
            f"Bungie returned a non-JSON response with HTTP {response.status}."
        ) from error
    if not isinstance(payload, dict):
        raise BungieError("Bungie returned an invalid JSON response.")
    return payload
