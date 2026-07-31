"""Read-only weapon vault-cleaner routes."""

from __future__ import annotations

import asyncio
import logging
from html import escape
from pathlib import Path
from string import Template
from typing import Any
from urllib.parse import urlencode

from aiohttp import web

from destiny_web_app.app_keys import (
    AUTH_SESSION_KEY,
    DATABASE_KEY,
    MANIFEST_SERVICE_KEY,
    WEAPON_CLEANER_SERVICE_KEY,
    WEAPON_ORGANIZER_SERVICE_KEY,
)
from destiny_web_app.auth import csrf_input, require_csrf
from destiny_web_app.bungie import BungieError
from destiny_web_app.cleaner import (
    WEAPON_RULESET_VERSION,
    WeaponCleanerError,
)
from destiny_web_app.inventory_routes import icon_url
from destiny_web_app.inventory import InventoryDataError
from destiny_web_app.manifest import ManifestError
from destiny_web_app.organizer import WeaponOrganizerError


TEMPLATE_ROOT = Path(__file__).with_name("templates")
LOGGER = logging.getLogger(__name__)


async def weapon_cleaner_page(request: web.Request) -> web.Response:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPSeeOther("/")
    analysis, inventory = await asyncio.gather(
        asyncio.to_thread(
            request.app[WEAPON_CLEANER_SERVICE_KEY].latest,
            authenticated.bungie_membership_id,
        ),
        asyncio.to_thread(
            request.app[DATABASE_KEY].load_active_inventory,
            authenticated.bungie_membership_id,
        ),
    )
    manifest_status = request.app[MANIFEST_SERVICE_KEY].status()
    current = bool(
        analysis
        and inventory
        and analysis["snapshot_id"] == inventory["snapshot"]["snapshot_id"]
        and analysis["manifest_version"] == manifest_status.get("version")
        and analysis["ruleset_version"] == WEAPON_RULESET_VERSION
    )
    body = render_analysis(analysis, current=current)
    notice = request.query.get("notice", "")
    error = request.query.get("error", "")
    html = render_template(
        "weapon_cleaner.html",
        guardian_name=escape(authenticated.display_name),
        notice=message_html(notice, "success"),
        error=message_html(error, "error"),
        current_warning=(
            ""
            if current or analysis is None
            else (
                '<div class="notice warning">This analysis is retained as '
                "evidence, but it does not match the active inventory or "
                "current manifest/ruleset. Run it again before acting.</div>"
            )
        ),
        stale_warning=(
            '<div class="notice warning">The analyzed inventory snapshot was '
            "stale or had a failed refresh. Synchronize before a final "
            "dismantle review.</div>"
            if analysis
            and analysis["result"]["summary"]["snapshot_status"] != "fresh"
            else ""
        ),
        analyze_csrf=csrf_input(request, "/cleaner/weapons/analyze"),
        organize_csrf=csrf_input(request, "/cleaner/weapons/organize"),
        body=body,
    )
    response = web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )
    response.enable_compression()
    return response


async def analyze_weapons(request: web.Request) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before analyzing inventory.")
    try:
        require_csrf(request, await request.post())
        await asyncio.to_thread(
            request.app[WEAPON_CLEANER_SERVICE_KEY].analyze,
            authenticated.bungie_membership_id,
        )
    except web.HTTPForbidden as error:
        query = urlencode(
            {"error": error.text or "The analysis form was rejected."}
        )
    except (WeaponCleanerError, ManifestError, ValueError) as error:
        query = urlencode({"error": str(error)})
    except Exception:
        LOGGER.exception("Unexpected weapon-cleaner analysis failure")
        query = urlencode(
            {"error": "The weapon analysis could not be completed."}
        )
    else:
        query = urlencode(
            {"notice": "Weapon coverage analysis completed."}
        )
    raise web.HTTPSeeOther(f"/cleaner/weapons?{query}")


async def organize_weapons(request: web.Request) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before organizing weapons.")
    try:
        require_csrf(request, await request.post())
        request.app[WEAPON_ORGANIZER_SERVICE_KEY].start(
            bungie_membership_id=authenticated.bungie_membership_id,
            access_token=authenticated.token.access_token,
        )
    except web.HTTPForbidden as error:
        return web.json_response(
            {"error": error.text or "The organization form was rejected."},
            status=403,
        )
    except (
        BungieError,
        InventoryDataError,
        WeaponOrganizerError,
        WeaponCleanerError,
        ManifestError,
    ) as error:
        return web.json_response({"error": str(error)}, status=409)
    except Exception:
        LOGGER.exception("Unexpected weapon organization failure")
        return web.json_response(
            {"error": "Weapon organization could not be started."},
            status=500,
        )
    return web.json_response({"status": "started"}, status=202)


async def weapon_organization_status(request: web.Request) -> web.Response:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before viewing progress.")
    progress = request.app[WEAPON_ORGANIZER_SERVICE_KEY].progress(
        authenticated.bungie_membership_id
    )
    if progress is None:
        raise web.HTTPNotFound(text="No weapon organization job exists.")
    return web.json_response(progress, headers={"Cache-Control": "no-store"})


def render_analysis(
    analysis: dict[str, Any] | None,
    *,
    current: bool,
) -> str:
    if analysis is None:
        return """
<section class="empty-state">
  <p class="eyebrow">No analysis yet</p>
  <h2>Find the smallest useful weapon collection.</h2>
  <p>The analyzer preserves every unique gameplay option in every barrel,
  magazine, trait, origin, and weapon-specific column.</p>
</section>"""

    result = analysis["result"]
    summary = result["summary"]
    perk_definitions = result.get("perk_definitions", {})
    candidate_groups = [
        group for group in result["groups"] if group["candidate_count"] > 0
    ]
    cards = "".join(
        render_weapon_group(
            group,
            perk_definitions,
            open_group=index == 0,
        )
        for index, group in enumerate(candidate_groups)
    )
    if not cards:
        cards = """
<section class="empty-state">
  <p class="eyebrow">No candidates</p>
  <h2>Every analyzed vault copy currently contributes unique coverage.</h2>
</section>"""
    return f"""
<section class="analysis-summary">
  <div><strong>{summary["candidate_count"]}</strong><span>Review candidates</span></div>
  <div><strong>{summary["candidate_group_count"]}</strong><span>Weapon groups</span></div>
  <div><strong>{summary["weapon_count"]}</strong><span>Weapons scanned</span></div>
  <div><strong>{summary["incomplete_group_count"]}</strong><span>Incomplete groups</span></div>
</section>
<section class="method-card">
  <p class="eyebrow">Coverage rule</p>
  <h2>Keep every unique option, once.</h2>
  <p>Each selectable gameplay plug is tagged by its socket column. The retained
  set covers every observed barrel, magazine, trait, origin trait, and
  weapon-specific option. Cross-column combinations are not treated as unique
  requirements.</p>
  <p class="perk-help">Hover a perk—or focus it with the keyboard—to see its
  official Bungie description. Perk images, names, and descriptions are saved
  from the same manifest version recorded with this analysis.</p>
  <p>Only unlocked, non-crafted, non-Exotic vault copies that are absent from
  every stored in-game loadout can become candidates. Masterworked copies win
  ties when several minimum sets exist.</p>
  <dl>
    <dt>Snapshot</dt><dd>{escape(summary["snapshot_fetched_at"])}</dd>
    <dt>Manifest</dt><dd>{escape(analysis["manifest_version"])}</dd>
    <dt>Rules</dt><dd>{escape(analysis["ruleset_version"])}</dd>
    <dt>Loadout references</dt><dd>{summary["loadout_reference_count"]} stored;
    {summary["unresolved_loadout_reference_count"]} currently unresolved</dd>
    <dt>Current</dt><dd>{"Yes" if current else "No"}</dd>
  </dl>
</section>
<section class="weapon-list">{cards}</section>"""


def render_weapon_group(
    group: dict[str, Any],
    perk_definitions: dict[str, Any],
    *,
    open_group: bool,
) -> str:
    image = ""
    if asset_url := icon_url(group.get("icon")):
        image = f'<img src="{escape(asset_url)}" alt="">'
    columns = "".join(
        f'<span>{escape(column["label"])}</span>'
        for column in group["column_labels"]
    )
    candidates = "".join(
        render_weapon_item(item, perk_definitions)
        for item in group["items"]
        if item["decision"] == "candidate"
    )
    retained = "".join(
        render_weapon_item(item, perk_definitions)
        for item in group["items"]
        if item["decision"] != "candidate"
    )
    return f"""
<details class="weapon-card"{" open" if open_group else ""}>
  <summary>
    <span class="weapon-icon">{image or "◇"}</span>
    <span class="weapon-title">
      <small>{escape(group["tier"])} · {escape(group["type"])}</small>
      <strong>{escape(group["name"])}</strong>
      <span>{group["owned_count"]} owned · {group["option_count"]} unique
      column options</span>
    </span>
    <span class="weapon-result">
      <strong>{group["candidate_count"]}</strong><small>Review</small>
    </span>
  </summary>
  <div class="weapon-body">
    <div class="column-list">{columns}</div>
    <section>
      <h3>Review candidates</h3>
      <p class="section-copy">Every option on these copies is represented by
      the retained set.</p>
      <div class="copy-grid">{candidates}</div>
    </section>
    <details class="retained">
      <summary>Show {group["keep_count"]} retained copies</summary>
      <div class="copy-grid">{retained}</div>
    </details>
  </div>
</details>"""


def render_weapon_item(
    item: dict[str, Any],
    perk_definitions: dict[str, Any],
) -> str:
    labels = []
    if item["locked"]:
        labels.append("Locked")
    if item["crafted"]:
        labels.append("Crafted")
    if item["masterworked"]:
        labels.append("Masterworked")
    labels.extend(item["reasons"])
    badges = "".join(f"<span>{escape(label)}</span>" for label in dict.fromkeys(labels))
    columns = "".join(
        render_roll_column(item, column, perk_definitions)
        for column in item["columns"]
    )
    evidence = (
        f"All {item['coverage_count']} column options are covered elsewhere."
        if item["decision"] == "candidate"
        else (
            "Protected: " + ", ".join(item["reasons"])
            if item["reasons"]
            else "Selected by the minimum coverage calculation."
        )
    )
    return f"""
<article class="weapon-copy decision-{item["decision"]}">
  <header>
    <div>
      <strong>{escape(item["instance_suffix"])}</strong>
      <span>{escape(item["source"])} · Power {item["power"] or "—"}</span>
    </div>
    <span class="decision">{escape(item["decision"])}</span>
  </header>
  <div class="badges">{badges}</div>
  <div class="roll-columns">{columns}</div>
  <p>{escape(evidence)}</p>
</article>"""


def render_roll_column(
    item: dict[str, Any],
    column: dict[str, Any],
    perk_definitions: dict[str, Any],
) -> str:
    plugs = "".join(
        render_perk(
            resolve_perk_display(plug, perk_definitions),
            tooltip_id=(
                f"perk-{item['item_row_id']}-{column['index']}-"
                f"{plug_hash(plug)}"
            ),
        )
        for plug in column["plugs"]
    )
    return f"""
<div class="roll-column">
  <strong>{escape(column["label"])}</strong>
  <div class="perk-grid">{plugs}</div>
</div>"""


def resolve_perk_display(
    plug: Any,
    perk_definitions: dict[str, Any],
) -> dict[str, Any]:
    if isinstance(plug, dict):
        return plug
    definition = perk_definitions.get(str(plug))
    if isinstance(definition, dict):
        return definition
    return {"hash": plug, "name": f"Plug {plug}"}


def plug_hash(plug: Any) -> Any:
    return plug.get("hash", "unknown") if isinstance(plug, dict) else plug


def render_perk(plug: dict[str, Any], *, tooltip_id: str) -> str:
    name = str(plug.get("name") or f"Plug {plug.get('hash', '')}")
    description = str(
        plug.get("description")
        or "No official Bungie description is available."
    )
    source = str(plug.get("source") or "Bungie manifest")
    asset_url = icon_url(plug.get("icon"))
    image = (
        f'<img src="{escape(asset_url)}" alt="" loading="lazy">'
        if asset_url
        else '<span class="perk-icon-fallback" aria-hidden="true">◇</span>'
    )
    safe_id = escape(tooltip_id)
    return f"""
<span class="perk-chip" tabindex="0" aria-describedby="{safe_id}">
  <span class="perk-icon">{image}</span>
  <span class="perk-name">{escape(name)}</span>
  <span class="perk-tooltip" id="{safe_id}" role="tooltip">
    <strong>{escape(name)}</strong>
    <span>{escape(description)}</span>
    <small>{escape(source)}</small>
  </span>
</span>"""


def message_html(message: str, css_class: str) -> str:
    if not message:
        return ""
    return f'<div class="notice {css_class}">{escape(message)}</div>'


def render_template(template_name: str, **values: str) -> str:
    template = Template(
        (TEMPLATE_ROOT / template_name).read_text(encoding="utf-8")
    )
    return template.substitute(values)
