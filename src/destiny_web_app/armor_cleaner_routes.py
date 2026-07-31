"""Saved-policy Armor 3.0 cleaner routes."""

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
    ARMOR_CLEANER_SERVICE_KEY,
    ARMOR_ORGANIZER_SERVICE_KEY,
    AUTH_SESSION_KEY,
    DATABASE_KEY,
    MANIFEST_SERVICE_KEY,
)
from destiny_web_app.armor_cleaner import (
    ARMOR_STAT_NAMES,
    DEFAULT_ACCEPTED_STATS,
    ArmorCleanerError,
)
from destiny_web_app.auth import csrf_input, require_csrf
from destiny_web_app.bungie import BungieError
from destiny_web_app.cleaner_routes import message_html
from destiny_web_app.inventory import InventoryDataError
from destiny_web_app.inventory_routes import icon_url
from destiny_web_app.manifest import ManifestError
from destiny_web_app.organizer import WeaponOrganizerError


TEMPLATE_ROOT = Path(__file__).with_name("templates")
LOGGER = logging.getLogger(__name__)


async def armor_cleaner_page(request: web.Request) -> web.Response:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPSeeOther("/")
    service = request.app[ARMOR_CLEANER_SERVICE_KEY]
    try:
        context, analysis, inventory = await asyncio.gather(
            asyncio.to_thread(
                service.policy_context,
                authenticated.bungie_membership_id,
            ),
            asyncio.to_thread(
                service.latest,
                authenticated.bungie_membership_id,
            ),
            asyncio.to_thread(
                request.app[DATABASE_KEY].load_active_inventory,
                authenticated.bungie_membership_id,
            ),
        )
    except (ArmorCleanerError, ManifestError, ValueError) as error:
        context = None
        analysis = None
        inventory = None
        load_error = str(error)
    else:
        load_error = ""
    manifest_status = request.app[MANIFEST_SERVICE_KEY].status()
    current = bool(
        analysis
        and inventory
        and analysis["snapshot_id"] == inventory["snapshot"]["snapshot_id"]
        and analysis["manifest_version"] == manifest_status.get("version")
    )
    html = render_template(
        "armor_cleaner.html",
        guardian_name=escape(authenticated.display_name),
        notice=message_html(request.query.get("notice", ""), "success"),
        error=message_html(request.query.get("error", "") or load_error, "error"),
        current_warning=(
            ""
            if current or analysis is None
            else (
                '<div class="notice warning">This result does not match the '
                "active inventory, manifest, or saved policy. Analyze again "
                "before applying lock states.</div>"
            )
        ),
        stale_warning=(
            '<div class="notice warning">The analyzed inventory was not fresh. '
            "Refresh it before applying lock states.</div>"
            if analysis
            and analysis["result"]["summary"]["snapshot_status"] != "fresh"
            else ""
        ),
        policy_form=(
            render_policy_form(request, context) if context is not None else ""
        ),
        body=render_armor_analysis(request, analysis, current=current),
        analyze_csrf=csrf_input(request, "/cleaner/armor/analyze"),
        organize_csrf=csrf_input(request, "/cleaner/armor/organize"),
    )
    response = web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )
    response.enable_compression()
    return response


async def save_armor_policy(request: web.Request) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before saving armor settings.")
    try:
        form = await request.post()
        require_csrf(request, form)
        service = request.app[ARMOR_CLEANER_SERVICE_KEY]
        context = await asyncio.to_thread(
            service.policy_context,
            authenticated.bungie_membership_id,
        )
        policy = policy_from_form(form, context["set_catalog"])
        await asyncio.to_thread(
            service.save_policy,
            authenticated.bungie_membership_id,
            policy,
        )
        await asyncio.to_thread(
            service.analyze,
            authenticated.bungie_membership_id,
        )
    except web.HTTPForbidden as error:
        query = urlencode({"error": error.text or "The policy form was rejected."})
    except (ArmorCleanerError, ManifestError, ValueError) as error:
        query = urlencode({"error": str(error)})
    except Exception:
        LOGGER.exception("Unexpected armor policy failure")
        query = urlencode({"error": "The armor settings could not be saved."})
    else:
        query = urlencode({"notice": "Armor settings saved and analysis updated."})
    raise web.HTTPSeeOther(f"/cleaner/armor?{query}")


async def analyze_armor(request: web.Request) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before analyzing armor.")
    try:
        require_csrf(request, await request.post())
        await asyncio.to_thread(
            request.app[ARMOR_CLEANER_SERVICE_KEY].analyze,
            authenticated.bungie_membership_id,
        )
    except web.HTTPForbidden as error:
        query = urlencode({"error": error.text or "The analysis form was rejected."})
    except (ArmorCleanerError, ManifestError, ValueError) as error:
        query = urlencode({"error": str(error)})
    except Exception:
        LOGGER.exception("Unexpected armor-cleaner analysis failure")
        query = urlencode({"error": "The armor analysis could not be completed."})
    else:
        query = urlencode({"notice": "Armor analysis completed."})
    raise web.HTTPSeeOther(f"/cleaner/armor?{query}")


async def set_armor_manual_keep(request: web.Request) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before changing armor decisions.")
    try:
        form = await request.post()
        require_csrf(request, form)
        instance_id = str(form.get("item_instance_id") or "")
        if not instance_id.isdecimal() or int(instance_id) <= 0:
            raise ValueError("The armor instance is invalid.")
        keep = form.get("keep") == "1"
        await asyncio.to_thread(
            request.app[ARMOR_CLEANER_SERVICE_KEY].set_manual_keep,
            authenticated.bungie_membership_id,
            instance_id,
            keep=keep,
        )
    except web.HTTPForbidden as error:
        query = urlencode({"error": error.text or "The keep form was rejected."})
    except (ArmorCleanerError, ManifestError, ValueError) as error:
        query = urlencode({"error": str(error)})
    except Exception:
        LOGGER.exception("Unexpected armor manual-keep failure")
        query = urlencode({"error": "The armor decision could not be saved."})
    else:
        query = urlencode(
            {"notice": "Armor marked to keep." if keep else "Manual keep removed."}
        )
    raise web.HTTPSeeOther(f"/cleaner/armor?{query}")


async def organize_armor(request: web.Request) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before organizing armor.")
    try:
        require_csrf(request, await request.post())
        request.app[ARMOR_ORGANIZER_SERVICE_KEY].start(
            bungie_membership_id=authenticated.bungie_membership_id,
            access_token=authenticated.token.access_token,
        )
    except web.HTTPForbidden as error:
        return web.json_response(
            {"error": error.text or "The organization form was rejected."},
            status=403,
        )
    except (
        ArmorCleanerError,
        BungieError,
        InventoryDataError,
        ManifestError,
        WeaponOrganizerError,
    ) as error:
        return web.json_response({"error": str(error)}, status=409)
    except Exception:
        LOGGER.exception("Unexpected armor organization failure")
        return web.json_response(
            {"error": "Armor organization could not be started."},
            status=500,
        )
    return web.json_response({"status": "started"}, status=202)


async def armor_organization_status(request: web.Request) -> web.Response:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before viewing progress.")
    progress = request.app[ARMOR_ORGANIZER_SERVICE_KEY].progress(
        authenticated.bungie_membership_id
    )
    if progress is None:
        raise web.HTTPNotFound(text="No armor organization job exists.")
    return web.json_response(progress, headers={"Cache-Control": "no-store"})


def policy_from_form(form: Any, catalog: dict[int, dict[str, Any]]) -> dict[str, Any]:
    global_stats = selected_stats(form, "global_stats")
    global_interested = form.get("global_interested") == "1"
    global_include_raid = form.get("global_include_raid") == "1"
    preferred_tuning = selected_tuning_stats(form, "global_preferred_tuning")
    sets = {}
    for set_hash in catalog:
        catalog_entry = catalog[set_hash]
        default_source = catalog_entry.get("default_source", "nonraid")
        raid_filtered = (
            default_source == "raid"
            and not global_include_raid
        )
        prefix = f"set_{set_hash}"
        custom = form.get(f"{prefix}_custom") == "1"
        if custom:
            interested = form.get(f"{prefix}_interested") == "1"
            source = default_source
            primary = selected_stats(form, f"{prefix}_primary")
            secondary = selected_stats(form, f"{prefix}_secondary")
            tertiary = selected_stats(form, f"{prefix}_tertiary")
        else:
            interested = global_interested and not raid_filtered
            source = default_source
            primary = list(global_stats)
            secondary = list(global_stats)
            tertiary = list(global_stats)
        sets[str(set_hash)] = {
            "interested": interested,
            "source": source,
            "two_piece": False,
            "four_piece": False,
            "primary": primary,
            "secondary": secondary,
            "tertiary": tertiary,
        }
    return {
        "source_mode": str(form.get("source_mode") or "same"),
        "tuning_mode": str(form.get("tuning_mode") or "preferred"),
        "preferred_tuning": preferred_tuning,
        "excluded_stats": [],
        "sets": sets,
    }


def selected_stats(form: Any, prefix: str) -> list[str]:
    selected = [
        name
        for name in ARMOR_STAT_NAMES
        if form.get(f"{prefix}_{field_name(name)}") == "1"
    ]
    return selected or list(DEFAULT_ACCEPTED_STATS)


def selected_tuning_stats(form: Any, prefix: str) -> list[str]:
    return [
        name
        for name in ARMOR_STAT_NAMES
        if form.get(f"{prefix}_{field_name(name)}") == "1"
    ]


def infer_simple_defaults(
    policy: dict[str, Any],
    set_catalog: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    set_rows = [
        (
            set_hash,
            policy["sets"][str(set_hash)],
        )
        for set_hash in sorted(set_catalog)
        if str(set_hash) in policy.get("sets", {})
    ]
    if not set_rows:
        return {
            "interested": True,
            "include_raid": True,
            "stats": list(DEFAULT_ACCEPTED_STATS),
        }

    def uniform(key: str, fallback: Any) -> Any:
        first = set_rows[0][1][key]
        return (
            first
            if all(row[1][key] == first for row in set_rows)
            else fallback
        )

    stats = list(DEFAULT_ACCEPTED_STATS)
    first_primary = set_rows[0][1]["primary"]
    same_primary = all(row[1]["primary"] == first_primary for row in set_rows)
    same_secondary = all(
        row[1]["secondary"] == first_primary for row in set_rows
    )
    same_tertiary = all(
        row[1]["tertiary"] == first_primary for row in set_rows
    )
    if same_primary and same_secondary and same_tertiary:
        stats = list(first_primary)

    raid_rows = [
        row
        for set_hash, row in set_rows
        if set_catalog.get(set_hash, {}).get("default_source") == "raid"
    ]
    include_raid = all(row["interested"] for row in raid_rows)
    if not raid_rows:
        include_raid = True

    return {
        "interested": uniform("interested", True),
        "include_raid": include_raid,
        "stats": stats,
        "preferred_tuning": normalize_tuning_preferences(
            policy.get("preferred_tuning")
        ),
    }


def set_uses_simple_policy(
    set_hash: int,
    catalog: dict[str, Any],
    set_policy: dict[str, Any],
    simple_defaults: dict[str, Any],
) -> bool:
    simple_stats = simple_defaults["stats"]
    expected_interested = simple_defaults["interested"]
    if (
        catalog.get("default_source") == "raid"
        and not simple_defaults["include_raid"]
    ):
        expected_interested = False
    return (
        set_policy["interested"] == expected_interested
        and set_policy["source"] == catalog.get("default_source", "nonraid")
        and not set_policy.get("two_piece")
        and not set_policy.get("four_piece")
        and set_policy["primary"] == simple_stats
        and set_policy["secondary"] == simple_stats
        and set_policy["tertiary"] == simple_stats
    )


def render_simple_stat_picker(prefix: str, selected: list[str]) -> str:
    options = "".join(
        checkbox(
            f"{prefix}_{field_name(name)}",
            name,
            name in selected,
        )
        for name in ARMOR_STAT_NAMES
    )
    return f'<div class="simple-stat-picker">{options}</div>'


def normalize_tuning_preferences(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value in ARMOR_STAT_NAMES else []
    if not isinstance(value, list):
        return []
    return [name for name in ARMOR_STAT_NAMES if name in value]


def render_tuning_picker(prefix: str, selected: list[str]) -> str:
    options = "".join(
        checkbox(
            f"{prefix}_{field_name(name)}",
            name,
            name in selected,
        )
        for name in ARMOR_STAT_NAMES
    )
    return f'<div class="simple-stat-picker">{options}</div>'


def render_policy_form(request: web.Request, context: dict[str, Any]) -> str:
        policy = context["policy"]
        simple_defaults = infer_simple_defaults(policy, context["set_catalog"])
        cards = "".join(
                render_set_policy(
                        set_hash,
                        catalog,
                        policy["sets"][str(set_hash)],
                        simple_defaults,
                )
                for set_hash, catalog in sorted(
                        context["set_catalog"].items(),
                        key=lambda row: row[1]["name"].lower(),
                )
        )
        simple_stats = render_simple_stat_picker(
                "global_stats",
                simple_defaults["stats"],
        )
        tuning_preferences = render_tuning_picker(
            "global_preferred_tuning",
            simple_defaults["preferred_tuning"],
        )
        return f"""
<form class="armor-policy" method="post" action="/cleaner/armor/policy">
    {csrf_input(request, "/cleaner/armor/policy")}
    <header class="policy-heading">
        <div><p class="eyebrow">Saved policy</p><h2>Simple controls first, advanced per set</h2></div>
        <button type="submit">Save settings &amp; analyze</button>
    </header>
    <section class="policy-simple" data-role="simple-policy">
        <h3>Global armor defaults</h3>
        <p>Pick the stats you care about and decide whether raid armor should be included.
        Every armor set follows these values unless you turn on customization for a set.</p>
        <div class="policy-global simple-grid">
            {checkbox("global_interested", "Analyze and retain useful rolls", simple_defaults["interested"])}
            {checkbox("global_include_raid", "Include raid armor in analysis", simple_defaults["include_raid"])}
            <div class="simple-stats">
                <strong>Preferred tuning slots (optional)</strong>
                {tuning_preferences}
            </div>
            <div class="simple-stats">
                <strong>Stats to prioritize (applies to primary/secondary/tertiary)</strong>
                {simple_stats}
            </div>
        </div>
    </section>
    <div class="policy-global">
        <label>Source treatment
            <select name="source_mode">
                {option("same", "Treat raid and non-raid the same", policy["source_mode"])}
                {option("separate", "Preserve raid and non-raid separately", policy["source_mode"])}
            </select>
        </label>
        <label>Tuning alignment
            <select name="tuning_mode">
                {option("required", "Required", policy["tuning_mode"])}
                {option("preferred", "Preferred tie-breaker", policy["tuning_mode"])}
                {option("ignored", "Ignored", policy["tuning_mode"])}
            </select>
        </label>
        <p><strong>Hard rules:</strong> Exotics are always kept. Legendary armor
        must be Tier 5.</p>
    </div>
    <div class="set-policy-list">{cards}</div>
    <button type="submit">Save settings &amp; analyze</button>
</form>"""


def render_set_policy(
        set_hash: int,
        catalog: dict[str, Any],
        policy: dict[str, Any],
        simple_defaults: dict[str, Any],
) -> str:
        prefix = f"set_{set_hash}"
        uses_simple = set_uses_simple_policy(
                set_hash,
                catalog,
                policy,
                simple_defaults,
        )
        positions = "".join(
                stat_position(prefix, position, policy[position])
                for position in ("primary", "secondary", "tertiary")
        )
        return f"""
<details class="set-policy" data-prefix="{prefix}" data-raid="{'1' if catalog.get('default_source') == 'raid' else '0'}">
    <summary>
        <strong>{escape(catalog["name"])}</strong>
        <span>{len(catalog["members"])} definitions · {'Following global defaults' if uses_simple else 'Custom set policy'}</span>
    </summary>
    <div class="set-policy-body">
        <label class="check set-custom-toggle">
            <input type="checkbox" name="{prefix}_custom" value="1"{' checked' if not uses_simple else ''} data-role="set-custom-toggle">
            <span>Customize this armor set</span>
        </label>
        <div class="set-controls" data-role="set-advanced-controls">
            {checkbox(f"{prefix}_interested", "Analyze and retain useful rolls", policy["interested"])}
        </div>
        <div class="stat-priority-grid">{positions}</div>
    </div>
</details>"""


def stat_position(prefix: str, position: str, selected: list[str]) -> str:
    choices = "".join(
        checkbox(
            f"{prefix}_{position}_{field_name(name)}",
            name,
            name in selected,
        )
        for name in ARMOR_STAT_NAMES
    )
    return f"<fieldset><legend>{position.title()}</legend>{choices}</fieldset>"


def checkbox(name: str, label: str, checked: bool) -> str:
    return (
        f'<label class="check"><input type="checkbox" name="{escape(name)}" '
        f'value="1"{" checked" if checked else ""}><span>{escape(label)}</span></label>'
    )


def option(value: str, label: str, selected: str) -> str:
    return (
        f'<option value="{escape(value)}"'
        f'{" selected" if value == selected else ""}>{escape(label)}</option>'
    )


def field_name(value: str) -> str:
    return value.lower().replace(" ", "_")


def render_armor_analysis(
    request: web.Request,
    analysis: dict[str, Any] | None,
    *,
    current: bool,
) -> str:
    if analysis is None:
        return """
<section class="empty-state">
  <p class="eyebrow">No armor analysis yet</p>
  <h2>Save the set policy to build the first review.</h2>
  <p>The cleaner will preserve Tier 5 intrinsic combinations and selected set
  bonuses without using a total retention budget.</p>
</section>"""
    summary = analysis["result"]["summary"]
    groups = "".join(
        render_armor_group(request, group, open_group=index == 0)
        for index, group in enumerate(analysis["result"]["groups"])
        if group["candidate_count"] or group["keep_count"]
    )
    return f"""
<section class="analysis-summary">
  <div><strong>{summary["candidate_count"]}</strong><span>Review candidates</span></div>
  <div><strong>{summary["projected_space_recovered"]}</strong><span>Projected space</span></div>
  <div><strong>{summary["keep_count"]}</strong><span>Retained</span></div>
  <div><strong>{summary["armor_count"]}</strong><span>Armor scanned</span></div>
</section>
<section class="method-card">
  <p class="eyebrow">Armor 3.0 rules</p>
    <h2>Tier 5 and chosen intrinsic rolls.</h2>
  <p>Exotics and exact-instance manual keeps are retained. Lock state,
    equipped location, and loadout references do not protect legendary armor.
    Selected intrinsic positions and tuning alignment determine whether a piece
    is retained or marked for review.</p>
  <dl>
    <dt>Snapshot</dt><dd>{escape(summary["snapshot_fetched_at"])}</dd>
    <dt>Manifest</dt><dd>{escape(analysis["manifest_version"])}</dd>
    <dt>Rules</dt><dd>{escape(analysis["ruleset_version"])}</dd>
    <dt>Manual keeps</dt><dd>{summary["manual_keep_count"]}</dd>
    <dt>Current</dt><dd>{"Yes" if current else "No"}</dd>
  </dl>
</section>
<section class="weapon-list armor-list">{groups}</section>"""


def render_armor_group(
    request: web.Request,
    group: dict[str, Any],
    *,
    open_group: bool,
) -> str:
    candidates = "".join(
        render_armor_item(request, item)
        for item in group["items"]
        if item["decision"] == "candidate"
    )
    retained = "".join(
        render_armor_item(request, item)
        for item in group["items"]
        if item["decision"] == "keep"
    )
    coverage = "".join(
        (
            f'<div class="notice {"success" if row["met"] else "error"}">'
            f'{escape(row["class_name"])}: {len(row["retained_slots"])} distinct '
            f'slots retained for the selected {row["required"]}-piece bonus'
            f'{"." if row["met"] else "; the owned policy-compatible pieces cannot satisfy it."}'
            "</div>"
        )
        for row in group.get("coverage", [])
    )
    return f"""
<details class="weapon-card armor-set-result"{' open' if open_group else ''}>
  <summary>
    <span class="weapon-icon">◇</span>
    <span class="weapon-title"><small>{escape(group["source"])}</small>
      <strong>{escape(group["name"])}</strong>
      <span>{group["owned_count"]} owned · {group["keep_count"]} retained</span>
    </span>
    <span class="weapon-result"><strong>{group["candidate_count"]}</strong><small>Review</small></span>
  </summary>
  <div class="weapon-body">
    {coverage}
    <section><h3>Review candidates</h3><div class="copy-grid">{candidates or '<p class="section-copy">No candidates in this set.</p>'}</div></section>
    <details class="retained"><summary>Show {group["keep_count"]} retained pieces</summary><div class="copy-grid">{retained}</div></details>
  </div>
</details>"""


def render_armor_item(request: web.Request, item: dict[str, Any]) -> str:
    asset = icon_url(item.get("icon"))
    image = f'<img src="{escape(asset)}" alt="">' if asset else "◇"
    intrinsic_stats = item.get("intrinsic_stats", [])
    primary = intrinsic_stats[0] if len(intrinsic_stats) > 0 else "Unknown"
    secondary = intrinsic_stats[1] if len(intrinsic_stats) > 1 else "Unknown"
    tertiary = intrinsic_stats[2] if len(intrinsic_stats) > 2 else "Unknown"
    tuned_stat = item.get("tuned_stat") or "Unknown"
    set_name = item.get("set_name") or "Unknown set"
    archetype = item.get("archetype")
    archetype_name = (
        archetype.get("name")
        if isinstance(archetype, dict) and archetype.get("name")
        else "Unknown"
    )
    stats = "".join(
        f'<span><strong>{row["value"]}</strong>{escape(row["name"])}</span>'
        for row in item["stats"]
    )
    manual = item["reason"] == "Manual keep"
    keep_form = ""
    if item.get("item_instance_id") and (item["decision"] == "candidate" or manual):
        keep_form = f"""
<form class="manual-keep" method="post" action="/cleaner/armor/keep">
  {csrf_input(request, "/cleaner/armor/keep")}
  <input type="hidden" name="item_instance_id" value="{escape(item["item_instance_id"])}">
  <input type="hidden" name="keep" value="{'0' if manual else '1'}">
  <button type="submit">{'Return to review' if manual else 'Keep this item'}</button>
</form>"""

    comparison_link = ""
    if item.get("alternative_item_row_id"):
        target = int(item["alternative_item_row_id"])
        comparison_link = (
            '<p class="comparison-link">'
            f'<a href="#armor-item-{target}">View comparison piece</a>'
            "</p>"
        )

    return f"""
<article id="armor-item-{item['item_row_id']}" class="weapon-copy armor-copy decision-{item["decision"]}">
  <header>
    <span class="armor-item-icon">{image}</span>
    <div><strong>{escape(item["name"])}</strong><span>{escape(item["instance_suffix"])} · {escape(item["rarity"])}</span></div>
    <span class="decision">{escape(item["decision"])}</span>
  </header>
    <dl class="armor-focus-list">
        <dt>Primary</dt><dd>{escape(primary)}</dd>
        <dt>Secondary</dt><dd>{escape(secondary)}</dd>
        <dt>Tertiary</dt><dd>{escape(tertiary)}</dd>
        <dt>Tuning</dt><dd>{escape(tuned_stat)}</dd>
        <dt>Armor set</dt><dd>{escape(set_name)}</dd>
        <dt>Archetype</dt><dd>{escape(archetype_name)}</dd>
    </dl>
    <div class="armor-stats">{stats}</div>
  <p>{escape(item["reason"])}</p>
  {comparison_link}
  {keep_form}
</article>"""


def render_template(template_name: str, **values: str) -> str:
    return Template(
        (TEMPLATE_ROOT / template_name).read_text(encoding="utf-8")
    ).substitute(values)
