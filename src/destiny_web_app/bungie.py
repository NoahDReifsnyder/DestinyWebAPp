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


class BungieActionError(BungieError):
    """A Destiny write failed with structured durable evidence."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        error_code: int | None = None,
        error_status: str = "",
        throttle_seconds: float = 0,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.error_code = error_code
        self.error_status = error_status
        self.throttle_seconds = throttle_seconds
        self.transient = transient


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
    TRANSFER_ITEM_URL = (
        "https://www.bungie.net/Platform/Destiny2/Actions/Items/TransferItem/"
    )
    EQUIP_ITEMS_URL = (
        "https://www.bungie.net/Platform/Destiny2/Actions/Items/EquipItems/"
    )
    INSERT_SOCKET_PLUG_FREE_URL = (
        "https://www.bungie.net/Platform/Destiny2/Actions/Items/"
        "InsertSocketPlugFree/"
    )
    SNAPSHOT_LOADOUT_URL = (
        "https://www.bungie.net/Platform/Destiny2/Actions/Loadouts/"
        "SnapshotLoadout/"
    )
    UPDATE_LOADOUT_IDENTIFIERS_URL = (
        "https://www.bungie.net/Platform/Destiny2/Actions/Loadouts/"
        "UpdateLoadoutIdentifiers/"
    )
    CLEAR_LOADOUT_URL = (
        "https://www.bungie.net/Platform/Destiny2/Actions/Loadouts/ClearLoadout/"
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

    async def transfer_item(
        self,
        access_token: str,
        *,
        item_instance_id: str,
        item_hash: int,
        character_id: str,
        membership_type: int,
        transfer_to_vault: bool,
    ) -> dict[str, Any]:
        """Transfer one exact item between a character and the vault."""

        return await self._post_destiny_action(
            self.TRANSFER_ITEM_URL,
            access_token,
            {
                "itemReferenceHash": item_hash,
                "stackSize": 1,
                "transferToVault": transfer_to_vault,
                "itemId": item_instance_id,
                "characterId": character_id,
                "membershipType": membership_type,
            },
            label="transferring an item",
        )

    async def equip_items(
        self,
        access_token: str,
        *,
        item_instance_ids: list[str],
        character_id: str,
        membership_type: int,
    ) -> dict[str, Any]:
        """Equip exact items and validate every returned per-item status."""

        result = await self._post_destiny_action(
            self.EQUIP_ITEMS_URL,
            access_token,
            {
                "itemIds": item_instance_ids,
                "characterId": character_id,
                "membershipType": membership_type,
            },
            label="equipping loadout items",
        )
        response = result.get("response")
        rows = response.get("equipResults") if isinstance(response, dict) else None
        if not isinstance(rows, list):
            raise BungieActionError(
                "Bungie returned no per-item equip results.",
                http_status=result.get("http_status"),
                error_code=result.get("error_code"),
                error_status="MissingEquipResults",
            )
        statuses = {
            str(row.get("itemInstanceId")): row.get("equipStatus")
            for row in rows
            if isinstance(row, dict)
        }
        failed = [
            (item_id, statuses.get(item_id))
            for item_id in item_instance_ids
            if statuses.get(item_id) != 1
        ]
        if failed:
            detail = ", ".join(
                f"{item_id} (equipStatus {status})"
                for item_id, status in failed
            )
            raise BungieActionError(
                f"Bungie did not equip: {detail}.",
                http_status=result.get("http_status"),
                error_code=(failed[0][1] if isinstance(failed[0][1], int) else None),
                error_status="EquipItemFailed",
            )
        result["message"] = (
            f"Bungie reported successful equip status for "
            f"{len(item_instance_ids)} item(s)."
        )
        result["equip_results"] = rows
        return result

    async def insert_socket_plug_free(
        self,
        access_token: str,
        *,
        item_instance_id: str,
        socket_index: int,
        plug_hash: int,
        character_id: str,
        membership_type: int,
    ) -> dict[str, Any]:
        """Insert one server-reported free socket plug."""

        return await self._post_destiny_action(
            self.INSERT_SOCKET_PLUG_FREE_URL,
            access_token,
            {
                "itemId": item_instance_id,
                "plug": {
                    "socketIndex": socket_index,
                    "socketArrayType": 0,
                    "plugItemHash": plug_hash,
                },
                "characterId": character_id,
                "membershipType": membership_type,
            },
            label="inserting a free loadout plug",
        )

    async def snapshot_loadout(
        self,
        access_token: str,
        *,
        loadout_index: int,
        character_id: str,
        membership_type: int,
    ) -> dict[str, Any]:
        """Snapshot currently equipped state into one zero-based game slot."""

        return await self._post_destiny_action(
            self.SNAPSHOT_LOADOUT_URL,
            access_token,
            {
                # Bungie's endpoint rejects the request if these nullable
                # members of DestinyLoadoutUpdateActionRequest are omitted.
                "colorHash": None,
                "iconHash": None,
                "nameHash": None,
                "loadoutIndex": loadout_index,
                "characterId": character_id,
                "membershipType": membership_type,
            },
            label="snapshotting an in-game loadout",
        )

    async def update_loadout_identifiers(
        self,
        access_token: str,
        *,
        loadout_index: int,
        character_id: str,
        membership_type: int,
        name_hash: int | None,
        icon_hash: int | None,
        color_hash: int | None,
    ) -> dict[str, Any]:
        """Update the constrained Bungie name, icon, and color identifiers."""

        return await self._post_destiny_action(
            self.UPDATE_LOADOUT_IDENTIFIERS_URL,
            access_token,
            {
                "colorHash": color_hash,
                "iconHash": icon_hash,
                "nameHash": name_hash,
                "loadoutIndex": loadout_index,
                "characterId": character_id,
                "membershipType": membership_type,
            },
            label="updating loadout identifiers",
        )

    async def clear_loadout(
        self,
        access_token: str,
        *,
        loadout_index: int,
        character_id: str,
        membership_type: int,
    ) -> dict[str, Any]:
        """Clear one zero-based Bungie loadout slot."""

        return await self._post_destiny_action(
            self.CLEAR_LOADOUT_URL,
            access_token,
            {
                "loadoutIndex": loadout_index,
                "characterId": character_id,
                "membershipType": membership_type,
            },
            label="clearing an in-game loadout",
        )

    async def _post_destiny_action(
        self,
        url: str,
        access_token: str,
        body: dict[str, Any],
        *,
        label: str,
    ) -> dict[str, Any]:
        """Send one action and retain HTTP/envelope/throttle evidence."""

        session = await self._get_session()
        try:
            async with session.post(
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "X-API-Key": self.settings.bungie_api_key,
                },
            ) as response:
                payload = await read_json(response)
                http_status = response.status
        except (aiohttp.ClientError, TimeoutError) as error:
            raise BungieActionError(
                f"Could not reach Bungie while {label}.", transient=True
            ) from error
        error_code = payload.get("ErrorCode")
        error_status = str(payload.get("ErrorStatus") or "")
        message = str(payload.get("Message") or "")
        raw_throttle = payload.get("ThrottleSeconds")
        throttle = (
            float(raw_throttle)
            if isinstance(raw_throttle, (int, float))
            and not isinstance(raw_throttle, bool)
            and raw_throttle > 0
            else 0.0
        )
        if http_status == 401:
            raise BungieAuthenticationRejected(
                f"Bungie rejected the access token while {label}."
            )
        if http_status >= 400 or error_code != 1:
            raise BungieActionError(
                message or error_status or f"Bungie rejected {label}.",
                http_status=http_status,
                error_code=error_code if isinstance(error_code, int) else None,
                error_status=error_status,
                throttle_seconds=throttle,
                transient=(http_status == 429 or throttle > 0),
            )
        return {
            "http_status": http_status,
            "error_code": error_code,
            "error_status": error_status,
            "message": message,
            "throttle_seconds": throttle,
            "response": payload.get("Response"),
        }

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
