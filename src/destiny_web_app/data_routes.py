"""Authenticated inventory synchronization and database-status routes."""

from __future__ import annotations

import asyncio
import logging
from html import escape
from pathlib import Path
from string import Template
from typing import Any

from aiohttp import web

from destiny_web_app.app_keys import (
    AUTH_SESSION_KEY,
    DATABASE_KEY,
    INVENTORY_SERVICE_KEY,
    SETTINGS_KEY,
)
from destiny_web_app.bungie import BungieAuthenticationRejected, BungieError
from destiny_web_app.auth import csrf_input, require_csrf
from destiny_web_app.inventory import InventoryDataError
from destiny_web_app.ui import render_header


LOGGER = logging.getLogger(__name__)
TEMPLATE_ROOT = Path(__file__).with_name("templates")
CLASS_NAMES = {
    0: "Titan",
    1: "Hunter",
    2: "Warlock",
    3: "Unknown",
}


async def data_status(request: web.Request) -> web.Response:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPSeeOther("/")

    database = request.app[DATABASE_KEY]
    status = await asyncio.to_thread(
        database.inventory_status,
        authenticated.bungie_membership_id,
    )
    result = request.query.get("result")
    notice = {
        "synced": "A complete inventory snapshot was fetched and saved.",
        "cache": "The saved inventory was still fresh, so no Bungie request was needed.",
        "stale": "The saved inventory was marked stale for verification.",
        "failed": (
            "A controlled refresh failure was recorded. The prior complete "
            "snapshot remains active."
        ),
    }.get(result, "")
    error = request.query.get("error", "")

    html = render_status_template(
        status=status,
        bungie_name=authenticated.display_name,
        environment=request.app[SETTINGS_KEY].environment,
        notice=notice,
        error=error,
        csrf_sync=csrf_input(request, "/data/inventory/sync"),
        csrf_stale=csrf_input(request, "/data/inventory/mark-stale"),
        csrf_failure=csrf_input(
            request,
            "/data/inventory/simulate-failure",
        ),
    )
    return web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


async def data_status_json(request: web.Request) -> web.Response:
    authenticated = require_authenticated(request)
    status = await asyncio.to_thread(
        request.app[DATABASE_KEY].inventory_status,
        authenticated.bungie_membership_id,
    )
    return web.json_response(
        {
            "bungie_name": authenticated.display_name,
            **status,
        },
        headers={"Cache-Control": "no-store"},
    )


async def synchronize_inventory(request: web.Request) -> web.StreamResponse:
    authenticated = require_authenticated(request)
    form = await request.post()
    require_csrf(request, form)
    force = form.get("force") == "true"
    return_to = local_return_path(str(form.get("return_to") or ""))
    service = request.app[INVENTORY_SERVICE_KEY]
    try:
        result = await service.synchronize(
            bungie_membership_id=authenticated.bungie_membership_id,
            access_token=authenticated.token.access_token,
            force=force,
        )
    except BungieAuthenticationRejected as error:
        return redirect_with_error(str(error), return_to)
    except (BungieError, InventoryDataError, LookupError) as error:
        LOGGER.warning("Inventory synchronization failed: %s", error)
        return redirect_with_error(str(error), return_to)
    except Exception:
        LOGGER.exception("Unexpected inventory synchronization failure")
        return redirect_with_error(
            "Inventory synchronization failed. The previous complete snapshot "
            "was preserved.",
            return_to,
        )

    result_name = "cache" if result.used_cache else "synced"
    raise web.HTTPSeeOther(add_query(return_to, result=result_name))


async def mark_inventory_stale(request: web.Request) -> web.StreamResponse:
    authenticated = require_authenticated(request)
    require_development(request)
    require_csrf(request, await request.post())
    changed = await asyncio.to_thread(
        request.app[DATABASE_KEY].mark_inventory_stale,
        authenticated.bungie_membership_id,
    )
    if not changed:
        return redirect_with_error("There is no saved inventory to mark stale.")
    raise web.HTTPSeeOther("/data/status?result=stale")


async def simulate_inventory_failure(
    request: web.Request,
) -> web.StreamResponse:
    authenticated = require_authenticated(request)
    require_development(request)
    require_csrf(request, await request.post())
    await request.app[INVENTORY_SERVICE_KEY].simulate_failed_refresh(
        authenticated.bungie_membership_id
    )
    raise web.HTTPSeeOther("/data/status?result=failed")


def require_authenticated(request: web.Request):
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(
            text="Sign in with Bungie before accessing saved user data."
        )
    return authenticated


def require_development(request: web.Request) -> None:
    if request.app[SETTINGS_KEY].environment != "development":
        raise web.HTTPNotFound()


def local_return_path(value: str) -> str:
    return value if value.startswith("/") and not value.startswith("//") else "/data/status"


def add_query(path: str, **values: str) -> str:
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parsed = urlsplit(path)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(values)
    return urlunsplit(("", "", parsed.path, urlencode(query), parsed.fragment))


def redirect_with_error(message: str, return_to: str = "/data/status") -> web.HTTPSeeOther:
    # The status page escapes this value before rendering. Keeping this short
    # also avoids putting Bungie's full response content into a URL.
    return web.HTTPSeeOther(
        add_query(
            local_return_path(return_to),
            error=" ".join(message.split())[:300],
        )
    )


def render_status_template(
    *,
    status: dict[str, Any],
    bungie_name: str,
    environment: str,
    notice: str,
    error: str,
    csrf_sync: str,
    csrf_stale: str,
    csrf_failure: str,
) -> str:
    characters = status.get("characters") or []
    character_rows = "".join(character_row(character) for character in characters)
    if not character_rows:
        character_rows = (
            '<tr><td colspan="6" class="empty">'
            "No character snapshot saved yet.</td></tr>"
        )

    resources = status.get("resources") or []
    resource_rows = "".join(resource_row(resource) for resource in resources)
    if not resource_rows:
        resource_rows = (
            '<tr><td colspan="4" class="empty">No cached resources saved yet.</td></tr>'
        )

    development_controls = ""
    if environment == "development":
        development_controls = """
<section class="card">
  <h2>Development verification controls</h2>
  <p>These controls change freshness metadata only. They do not delete the
  active inventory snapshot.</p>
  <div class="actions">
    <form method="post" action="/data/inventory/mark-stale">
      {csrf_stale}
      <button class="secondary" type="submit">Mark inventory stale</button>
    </form>
    <form method="post" action="/data/inventory/simulate-failure">
      {csrf_failure}
      <button class="danger" type="submit">Simulate failed refresh</button>
    </form>
  </div>
</section>"""

    sync_status = str(status.get("sync_status") or "missing")
    total = status.get("total_item_count") or 0
    vault = status.get("vault_item_count") or 0
    profile_items = status.get("profile_item_count") or 0
    carried = status.get("character_inventory_count") or 0
    equipped = status.get("equipped_item_count") or 0
    postmaster = status.get("postmaster_item_count") or 0
    expected_total = vault + profile_items + carried + equipped + postmaster
    totals_match = total == expected_total
    migration = status.get("last_migration") or {}

    template = Template(
        (TEMPLATE_ROOT / "data_status.html").read_text(encoding="utf-8")
    )
    return template.substitute(
        header=render_header("data_status.html", {"bungie_name": escape(bungie_name)}),
        bungie_name=escape(bungie_name),
        sync_status=escape(sync_status),
        status_class=status_css_class(sync_status),
        notice=message_html(notice, "notice"),
        error=message_html(error, "error"),
        schema_version=escape(str(status.get("schema_version", 0))),
        expected_schema_version=escape(
            str(status.get("expected_schema_version", 0))
        ),
        last_migration=escape(
            f"{migration.get('name', 'None')} at "
            f"{migration.get('applied_at', 'unknown')}"
        ),
        integrity=escape(str(status.get("integrity", "unknown"))),
        database_size=escape(format_bytes(status.get("database_bytes", 0))),
        read_duration=escape(str(status.get("read_duration_ms", "—"))),
        destiny_membership=escape(
            str(status.get("destiny_membership_id") or "Not synchronized")
        ),
        membership_type=escape(
            str(status.get("membership_type") or "Not synchronized")
        ),
        destiny_display_name=escape(
            str(status.get("destiny_display_name") or "Not synchronized")
        ),
        snapshot_id=escape(str(status.get("snapshot_id") or "None")),
        source_minted_at=escape(
            str(status.get("source_minted_at") or "Not available")
        ),
        last_attempt=escape(str(status.get("last_attempt_at") or "Never")),
        last_success=escape(str(status.get("last_success_at") or "Never")),
        stale_at=escape(str(status.get("stale_at") or "Not available")),
        api_fetch_count=escape(str(status.get("api_fetch_count") or 0)),
        cache_hit_count=escape(str(status.get("cache_hit_count") or 0)),
        last_error=escape(str(status.get("last_error") or "None")),
        total_items=escape(str(total)),
        vault_items=escape(str(vault)),
        profile_items=escape(str(profile_items)),
        carried_items=escape(str(carried)),
        equipped_items=escape(str(equipped)),
        postmaster_items=escape(str(postmaster)),
        calculated_total=escape(str(expected_total)),
        totals_result="PASS" if totals_match else "FAIL",
        totals_class="pass" if totals_match else "fail",
        duplicate_instances=escape(
            str(status.get("duplicate_instance_count") or 0)
        ),
        broken_owners=escape(
            str(status.get("broken_owner_reference_count") or 0)
        ),
        character_rows=character_rows,
        resource_rows=resource_rows,
        development_controls=development_controls,
        csrf_sync=csrf_sync,
    )


def character_row(character: dict[str, Any]) -> str:
    class_type = character.get("class_type")
    class_name = CLASS_NAMES.get(class_type, f"Class {class_type}")
    return f"""
<tr>
  <td>{escape(class_name)}</td>
  <td><code>{escape(str(character.get("character_id")))}</code></td>
  <td>{escape(str(character.get("light") or "—"))}</td>
  <td>{escape(str(character.get("carried_count") or 0))}</td>
  <td>{escape(str(character.get("equipped_count") or 0))}</td>
  <td>{escape(str(character.get("postmaster_count") or 0))}</td>
</tr>"""


def resource_row(resource: dict[str, Any]) -> str:
    return f"""
<tr>
  <td>{escape(str(resource.get("resource_type")))}</td>
  <td><code>{escape(str(resource.get("resource_key")))}</code></td>
  <td>{escape(str(resource.get("status")))}</td>
  <td>{escape(str(resource.get("fetched_at")))}</td>
</tr>"""


def message_html(message: str, css_class: str) -> str:
    if not message:
        return ""
    return f'<p class="{css_class}">{escape(message)}</p>'


def status_css_class(status: str) -> str:
    return {
        "fresh": "fresh",
        "stale": "stale",
        "refresh_failed": "failed",
    }.get(status, "missing")


def format_bytes(value: Any) -> str:
    if not isinstance(value, int):
        return "Unknown"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if amount < 1024 or unit == "GiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} GiB"
