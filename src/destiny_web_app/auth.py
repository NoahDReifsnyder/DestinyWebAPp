"""Bungie OAuth routes and durable authentication middleware."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
from dataclasses import replace
from datetime import UTC, datetime
from html import escape
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from destiny_web_app.app_keys import (
    AUTH_SESSION_KEY,
    AUTH_WARNING_KEY,
    BUNGIE_CLIENT_KEY,
    DATABASE_KEY,
    SETTINGS_KEY,
    TOKEN_REFRESH_LOCKS_KEY,
)
from destiny_web_app.bungie import (
    BungieAuthenticationRejected,
    BungieError,
    BungieTemporarilyUnavailable,
)
from destiny_web_app.config import ConfigurationError
from destiny_web_app.database import required_string
from destiny_web_app.ui import reset_render_request, set_render_request


LOGGER = logging.getLogger(__name__)
OAUTH_STATE_COOKIE = "bungie_oauth_state"
OAUTH_STATE_MAX_AGE = 10 * 60
AUTH_FREE_PATHS = {
    "/health",
    "/auth/bungie/login",
    "/redirect",
    "/auth/bungie/callback",
}
CSRF_FIELD = "_csrf"


@web.middleware
async def authentication_middleware(
    request: web.Request,
    handler,
) -> web.StreamResponse:
    if request.path.startswith("/static/") or request.path in AUTH_FREE_PATHS:
        return await handler(request)

    settings = request.app[SETTINGS_KEY]
    database = request.app[DATABASE_KEY]
    bungie = request.app[BUNGIE_CLIENT_KEY]
    raw_session_id = request.cookies.get(settings.session_cookie_name)
    clear_cookie = False
    renew_cookie_max_age: int | None = None

    if raw_session_id:
        authenticated = await asyncio.to_thread(
            database.get_session,
            raw_session_id,
        )
        if authenticated is None:
            clear_cookie = True
        elif authenticated.token.refresh_is_expired:
            await asyncio.to_thread(database.delete_session, raw_session_id)
            clear_cookie = True
        elif authenticated.token.access_needs_refresh:
            locks = request.app[TOKEN_REFRESH_LOCKS_KEY]
            lock = locks.setdefault(
                authenticated.bungie_membership_id,
                asyncio.Lock(),
            )
            async with lock:
                # Another request may have rotated the refresh token while this
                # one was waiting. Reload before deciding whether to refresh.
                authenticated = await asyncio.to_thread(
                    database.get_session,
                    raw_session_id,
                )
                if authenticated is None:
                    clear_cookie = True
                elif authenticated.token.refresh_is_expired:
                    await asyncio.to_thread(
                        database.delete_session,
                        raw_session_id,
                    )
                    clear_cookie = True
                    authenticated = None
                elif authenticated.token.access_needs_refresh:
                    try:
                        payload = await bungie.refresh_access_token(
                            authenticated.token.refresh_token
                        )
                        refreshed_token = await asyncio.to_thread(
                            database.update_tokens,
                            authenticated.bungie_membership_id,
                            payload,
                            previous_refresh_token=(
                                authenticated.token.refresh_token
                            ),
                        )
                        authenticated = replace(
                            authenticated,
                            token=refreshed_token,
                            expires_at=refreshed_token.refresh_expires_at,
                        )
                        renew_cookie_max_age = max(
                            0,
                            int(
                                (
                                    refreshed_token.refresh_expires_at
                                    - datetime.now(UTC)
                                ).total_seconds()
                            ),
                        )
                        LOGGER.info(
                            "Refreshed Bungie access token for membership %s",
                            authenticated.bungie_membership_id,
                        )
                    except BungieAuthenticationRejected:
                        latest = await asyncio.to_thread(
                            database.get_session,
                            raw_session_id,
                        )
                        if (
                            latest is not None
                            and latest.token.refreshed_at
                            > authenticated.token.refreshed_at
                        ):
                            authenticated = latest
                            renew_cookie_max_age = max(
                                0,
                                int(
                                    (
                                        latest.expires_at
                                        - datetime.now(UTC)
                                    ).total_seconds()
                                ),
                            )
                        else:
                            await asyncio.to_thread(
                                database.delete_session,
                                raw_session_id,
                            )
                            clear_cookie = True
                            authenticated = None
                    except BungieTemporarilyUnavailable:
                        request[AUTH_WARNING_KEY] = (
                            "Bungie is temporarily unavailable. Your saved "
                            "sign-in session is still available and refresh "
                            "will be retried."
                        )
                    except ConfigurationError:
                        request[AUTH_WARNING_KEY] = (
                            "Bungie OAuth configuration is unavailable. Your "
                            "saved session remains stored."
                        )
                    except (BungieError, ValueError) as error:
                        LOGGER.warning(
                            "Bungie token refresh could not be completed: %s",
                            error,
                        )
                        request[AUTH_WARNING_KEY] = (
                            "The Bungie token refresh could not be completed. "
                            "Your saved session remains stored for another "
                            "attempt."
                        )

        if authenticated is not None:
            request[AUTH_SESSION_KEY] = authenticated

    raised_response: web.HTTPException | None = None
    render_request_token = set_render_request(request)
    try:
        response = await handler(request)
    except web.HTTPException as error:
        response = error
        raised_response = error
    finally:
        reset_render_request(render_request_token)
    if clear_cookie:
        response.del_cookie(
            settings.session_cookie_name,
            path="/",
            secure=True,
            httponly=True,
            samesite="Lax",
        )
    elif renew_cookie_max_age is not None and raw_session_id:
        response.set_cookie(
            settings.session_cookie_name,
            raw_session_id,
            max_age=renew_cookie_max_age,
            path="/",
            secure=True,
            httponly=True,
            samesite="Lax",
        )
    if raised_response is not None:
        raise raised_response
    return response


async def begin_login(request: web.Request) -> web.StreamResponse:
    settings = request.app[SETTINGS_KEY]
    bungie = request.app[BUNGIE_CLIENT_KEY]
    try:
        settings.require_bungie()
    except ConfigurationError as error:
        return auth_error_response(str(error), status=503)

    state = secrets.token_urlsafe(32)
    response = web.HTTPFound(bungie.authorization_url(state))
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        state,
        max_age=OAUTH_STATE_MAX_AGE,
        path="/",
        secure=True,
        httponly=True,
        samesite="Lax",
    )
    return response


async def oauth_callback(request: web.Request) -> web.StreamResponse:
    settings = request.app[SETTINGS_KEY]
    bungie = request.app[BUNGIE_CLIENT_KEY]
    database = request.app[DATABASE_KEY]
    expected_state = request.cookies.get(OAUTH_STATE_COOKIE)
    returned_state = request.query.get("state")

    if (
        not expected_state
        or not returned_state
        or not secrets.compare_digest(expected_state, returned_state)
    ):
        return clear_state_cookie(
            auth_error_response(
                "The Bungie sign-in state was missing or invalid. Please try again.",
                status=400,
            )
        )

    if denial := request.query.get("error"):
        description = request.query.get("error_description") or denial
        return clear_state_cookie(
            auth_error_response(
                f"Bungie sign-in was not completed: {description}",
                status=400,
            )
        )

    code = request.query.get("code")
    if not code:
        return clear_state_cookie(
            auth_error_response(
                "Bungie did not return an authorization code.",
                status=400,
            )
        )

    try:
        token_payload = await bungie.exchange_code(code)
        bungie_membership_id = required_string(
            token_payload,
            "membership_id",
        )
        membership_data = await bungie.get_current_memberships(
            required_string(token_payload, "access_token")
        )
        display_name = extract_display_name(
            membership_data,
            bungie_membership_id,
        )
        raw_session_id, max_age = await asyncio.to_thread(
            database.save_login,
            bungie_membership_id,
            display_name,
            membership_data,
            token_payload,
        )
    except BungieAuthenticationRejected as error:
        return clear_state_cookie(auth_error_response(str(error), status=401))
    except BungieTemporarilyUnavailable as error:
        return clear_state_cookie(auth_error_response(str(error), status=503))
    except (BungieError, ValueError) as error:
        LOGGER.warning("Bungie OAuth callback failed: %s", error)
        return clear_state_cookie(auth_error_response(str(error), status=502))

    response = web.HTTPFound("/")
    response.headers.update(
        {
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
        }
    )
    response.set_cookie(
        settings.session_cookie_name,
        raw_session_id,
        max_age=max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="Lax",
    )
    return clear_state_cookie(response)


async def logout(request: web.Request) -> web.StreamResponse:
    settings = request.app[SETTINGS_KEY]
    raw_session_id = request.cookies.get(settings.session_cookie_name)
    if raw_session_id and request.get(AUTH_SESSION_KEY) is not None:
        require_csrf(request, await request.post())
    if raw_session_id:
        await asyncio.to_thread(
            request.app[DATABASE_KEY].delete_session,
            raw_session_id,
        )

    response = web.HTTPSeeOther("/")
    response.del_cookie(
        settings.session_cookie_name,
        path="/",
        secure=True,
        httponly=True,
        samesite="Lax",
    )
    return response


async def auth_status(request: web.Request) -> web.Response:
    authenticated = request.get(AUTH_SESSION_KEY)
    warning = request.get(AUTH_WARNING_KEY)
    if authenticated is None:
        return web.json_response(
            {
                "authenticated": False,
                "message": "No active saved sign-in session.",
            },
            headers={"Cache-Control": "no-store"},
        )

    return web.json_response(
        {
            "authenticated": True,
            "bungie_name": authenticated.display_name,
            "session_storage": "sqlite",
            "access_expires_at": authenticated.token.access_expires_at.isoformat(),
            "refresh_expires_at": authenticated.token.refresh_expires_at.isoformat(),
            "last_token_refresh_at": authenticated.token.refreshed_at.isoformat(),
            "warning": warning,
        },
        headers={"Cache-Control": "no-store"},
    )


def extract_display_name(
    membership_data: dict[str, Any],
    bungie_membership_id: str,
) -> str:
    bungie_user = membership_data.get("bungieNetUser")
    if isinstance(bungie_user, dict):
        for field in ("uniqueName", "displayName"):
            value = bungie_user.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()

    memberships = membership_data.get("destinyMemberships")
    if isinstance(memberships, list):
        for membership in memberships:
            if not isinstance(membership, dict):
                continue
            global_name = membership.get("bungieGlobalDisplayName")
            global_code = membership.get("bungieGlobalDisplayNameCode")
            if isinstance(global_name, str) and global_name.strip():
                if isinstance(global_code, int):
                    return f"{global_name.strip()}#{global_code:04d}"
                return global_name.strip()

    return f"Bungie member {bungie_membership_id}"


def auth_error_response(message: str, *, status: int) -> web.Response:
    safe_message = escape(message)
    html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Sign-in problem</title>
  </head>
  <body>
    <main>
      <h1>Sign-in problem</h1>
      <p>{safe_message}</p>
      <p><a href="/">Return home</a></p>
    </main>
  </body>
</html>"""
    return web.Response(
        text=html,
        status=status,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


def clear_state_cookie(response: web.StreamResponse) -> web.StreamResponse:
    response.del_cookie(
        OAUTH_STATE_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="Lax",
    )
    return response


def csrf_token(request: web.Request, action_path: str) -> str:
    raw_session_id = request.cookies.get(
        request.app[SETTINGS_KEY].session_cookie_name
    )
    if not raw_session_id or request.get(AUTH_SESSION_KEY) is None:
        return ""
    nonce = secrets.token_urlsafe(18)
    signature = csrf_signature(
        raw_session_id,
        action_path,
        nonce,
    )
    return f"{nonce}.{signature}"


def csrf_signature(
    raw_session_id: str,
    action_path: str,
    nonce: str,
) -> str:
    return hmac.new(
        raw_session_id.encode("utf-8"),
        f"destiny-csrf:{action_path}:{nonce}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def csrf_input(request: web.Request, action_path: str) -> str:
    token = csrf_token(request, action_path)
    return (
        f'<input type="hidden" name="{CSRF_FIELD}" '
        f'value="{escape(token)}">'
    )


def require_csrf(
    request: web.Request,
    form: Any,
) -> None:
    supplied = form.get(CSRF_FIELD) or request.headers.get("X-CSRF-Token")
    raw_session_id = request.cookies.get(
        request.app[SETTINGS_KEY].session_cookie_name
    )
    try:
        nonce, signature = supplied.rsplit(".", 1)
    except (AttributeError, ValueError):
        nonce = signature = ""
    expected = (
        csrf_signature(raw_session_id, request.path, nonce)
        if raw_session_id and 10 <= len(nonce) <= 100
        else ""
    )
    if not expected or not hmac.compare_digest(expected, signature):
        raise web.HTTPForbidden(
            text="The form expired or did not originate from this session."
        )

    origin = request.headers.get("Origin")
    configured_origin = canonical_origin(
        request.app[SETTINGS_KEY].bungie_redirect_uri,
        allow_path=True,
    )
    # Some embedded development browsers incorrectly send an absolute URL
    # here instead of the RFC origin serialization. Origin identity is still
    # only scheme, host, and effective port, so safely ignore any path.
    supplied_origin = canonical_origin(origin, allow_path=True)
    request_origin = canonical_origin(
        f"{request.scheme}://{request.host}",
        allow_path=True,
    )
    expected_origins = {
        candidate
        for candidate in (configured_origin, request_origin)
        if candidate is not None
    }
    origin_mismatch = bool(
        origin
        and (
        supplied_origin is None
        or supplied_origin not in expected_origins
        )
    )
    fetch_site = request.headers.get("Sec-Fetch-Site", "").lower()
    if fetch_site == "cross-site":
        raise web.HTTPForbidden(
            text="The request came from a different site."
        )
    allow_opaque_development_origin = (
        origin == "null"
        and request.app[SETTINGS_KEY].environment.lower() == "development"
    )
    if origin_mismatch and not allow_opaque_development_origin:
        LOGGER.warning(
            "Rejected form origin %r; expected one of %r",
            origin,
            sorted(expected_origins),
        )
        raise web.HTTPForbidden(
            text="The request origin did not match this application."
        )
    if allow_opaque_development_origin:
        LOGGER.info(
            "Allowed opaque development origin after valid CSRF token"
        )


def canonical_origin(
    value: Any,
    *,
    allow_path: bool = False,
) -> tuple[str, str, int] | None:
    """Return a comparable web origin without trusting reconstructed headers."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (not allow_path and parsed.path not in {"", "/"})
        or (not allow_path and (parsed.query or parsed.fragment))
    ):
        return None
    effective_port = port or (443 if parsed.scheme == "https" else 80)
    return (
        parsed.scheme.lower(),
        parsed.hostname.rstrip(".").lower(),
        effective_port,
    )
