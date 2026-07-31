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
    sets = {}
    for set_hash in catalog:
        prefix = f"set_{set_hash}"
        sets[str(set_hash)] = {
            "interested": form.get(f"{prefix}_interested") == "1",
            "source": str(form.get(f"{prefix}_source") or "nonraid"),
            "two_piece": form.get(f"{prefix}_two_piece") == "1",
            "four_piece": form.get(f"{prefix}_four_piece") == "1",
            "primary": selected_stats(form, f"{prefix}_primary"),
            "secondary": selected_stats(form, f"{prefix}_secondary"),
            "tertiary": selected_stats(form, f"{prefix}_tertiary"),
        }
    return {
        "source_mode": str(form.get("source_mode") or "same"),
        "tuning_mode": str(form.get("tuning_mode") or "preferred"),
        "excluded_stats": ["Health"],
        "sets": sets,
    }


def selected_stats(form: Any, prefix: str) -> list[str]:
    return [
        name
        for name in ARMOR_STAT_NAMES
        if name != "Health" and form.get(f"{prefix}_{field_name(name)}") == "1"
    ]


def render_policy_form(request: web.Request, context: dict[str, Any]) -> str:
    policy = context["policy"]
    cards = "".join(
        render_set_policy(
            set_hash,
            catalog,
            policy["sets"][str(set_hash)],
        )
        for set_hash, catalog in sorted(
            context["set_catalog"].items(),
            key=lambda row: row[1]["name"].lower(),
        )
    )
    return f"""
<form class="armor-policy" method="post" action="/cleaner/armor/policy">
  {csrf_input(request, "/cleaner/armor/policy")}
  <header class="policy-heading">
    <div><p class="eyebrow">Saved policy</p><h2>Armor sets and intrinsic priorities</h2></div>
    <button type="submit">Save settings &amp; analyze</button>
  </header>
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
    must be Tier 5, and any intrinsic Health position is tagged for review.</p>
  </div>
  <div class="set-policy-list">{cards}</div>
  <button type="submit">Save settings &amp; analyze</button>
</form>"""


def render_set_policy(
    set_hash: int,
    catalog: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    prefix = f"set_{set_hash}"
    bonuses = "".join(
        bonus_choice(prefix, count, catalog["bonuses"].get(str(count)), policy)
        for count in (2, 4)
        if catalog["bonuses"].get(str(count))
    )
    positions = "".join(
        stat_position(prefix, position, policy[position])
        for position in ("primary", "secondary", "tertiary")
    )
    return f"""
<details class="set-policy">
  <summary>
    <strong>{escape(catalog["name"])}</strong>
    <span>{len(catalog["members"])} definitions · {escape(policy["source"])}</span>
  </summary>
  <div class="set-policy-body">
    <div class="set-controls">
      {checkbox(f"{prefix}_interested", "Analyze and retain useful rolls", policy["interested"])}
      <label>Source
        <select name="{prefix}_source">
          {option("raid", "Raid", policy["source"])}
          {option("nonraid", "Non-raid", policy["source"])}
        </select>
      </label>
      <div class="bonus-choices">{bonuses or '<span>No set bonus in the manifest</span>'}</div>
    </div>
    <div class="stat-priority-grid">{positions}</div>
  </div>
</details>"""


def bonus_choice(
    prefix: str,
    count: int,
    bonus: dict[str, Any] | None,
    policy: dict[str, Any],
) -> str:
    if not bonus:
        return ""
    key = "two_piece" if count == 2 else "four_piece"
    asset = icon_url(bonus.get("icon"))
    image = f'<img src="{escape(asset)}" alt="">' if asset else "◇"
    return f"""
<label class="bonus-choice">
  <input type="checkbox" name="{prefix}_{key}" value="1"{' checked' if policy[key] else ''}>
  <span>{count}-piece</span>
  <span class="perk-chip bonus-perk" tabindex="0">
    <span class="perk-icon">{image}</span>
    <span class="perk-name">{escape(bonus["name"])}</span>
    <span class="perk-tooltip" role="tooltip">
      <strong>{escape(bonus["name"])}</strong>
      <span>{escape(bonus["description"])}</span>
      <small>Bungie manifest</small>
    </span>
  </span>
</label>"""


def stat_position(prefix: str, position: str, selected: list[str]) -> str:
    choices = "".join(
        checkbox(
            f"{prefix}_{position}_{field_name(name)}",
            name,
            name in selected,
        )
        for name in ARMOR_STAT_NAMES
        if name != "Health"
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
  <h2>Tier 5, chosen intrinsic rolls, and selected set coverage.</h2>
  <p>Exotics and exact-instance manual keeps are retained. Lock state,
  equipped location, and loadout references do not protect legendary armor.
  Selected two-/four-piece obligations can promote a non-Health Tier 5 piece
  when another slot is needed.</p>
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
    intrinsic = " / ".join(item["intrinsic_stats"]) or "Unresolved"
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
    badges = [f"Tier {item['gear_tier'] or '—'}", item["class_name"], item["slot"]]
    if item["locked"]:
        badges.append("Locked")
    if item["source_kind"] == "equipped":
        badges.append("Equipped")
    return f"""
<article class="weapon-copy armor-copy decision-{item["decision"]}">
  <header>
    <span class="armor-item-icon">{image}</span>
    <div><strong>{escape(item["name"])}</strong><span>{escape(item["instance_suffix"])} · {escape(item["rarity"])}</span></div>
    <span class="decision">{escape(item["decision"])}</span>
  </header>
  <div class="badges">{''.join(f'<span>{escape(value)}</span>' for value in badges)}</div>
  <p><strong>Intrinsic:</strong> {escape(intrinsic)} · <strong>Tuned:</strong> {escape(item["tuned_stat"] or "Unknown")}</p>
  <div class="armor-stats">{stats}</div>
  <p>{escape(item["reason"])}</p>
  {keep_form}
</article>"""


def render_template(template_name: str, **values: str) -> str:
    return Template(
        (TEMPLATE_ROOT / template_name).read_text(encoding="utf-8")
    ).substitute(values)
