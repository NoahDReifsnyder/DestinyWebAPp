"""Accessible exact-item loadout builder routes."""

from __future__ import annotations

import asyncio
import logging
from html import escape
from pathlib import Path
from string import Template
from urllib.parse import urlencode

from aiohttp import web

from destiny_web_app.app_keys import (
    AUTH_SESSION_KEY,
    LOADOUT_FUNCTIONS_KEY,
    LOADOUT_MANAGER_SERVICE_KEY,
)
from destiny_web_app.auth import csrf_input, require_csrf
from destiny_web_app.bungie import BungieError
from destiny_web_app.inventory import InventoryDataError
from destiny_web_app.inventory_routes import icon_url
from destiny_web_app.loadout_freshness import ensure_fresh_loadout_snapshot
from destiny_web_app.loadout_manager import (
    CLASS_NAMES,
    GAMEPLAY_BUCKET_NAMES,
    LoadoutInspectionError,
)
from destiny_web_app.loadout_routes import parse_tags
from destiny_web_app.manifest import ManifestError


LOGGER = logging.getLogger(__name__)
TEMPLATE_ROOT = Path(__file__).with_name("templates")


async def loadout_builder_page(request: web.Request) -> web.Response:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPSeeOther("/")
    edit_id = str(request.query.get("edit") or "")
    set_id = str(request.query.get("set_id") or "")
    editing = None
    if edit_id:
        editing = await asyncio.to_thread(
            request.app[LOADOUT_MANAGER_SERVICE_KEY].saved_loadout,
            authenticated.bungie_membership_id,
            edit_id,
            include_archived=True,
        )
        if editing is None:
            raise web.HTTPNotFound()
        class_type = int(editing["character_class_type"])
    else:
        try:
            class_type = int(request.query.get("class", "0"))
        except ValueError:
            class_type = 0
    try:
        await ensure_fresh_loadout_snapshot(request, authenticated)
        catalog = await asyncio.to_thread(
            request.app[LOADOUT_MANAGER_SERVICE_KEY].builder_catalog,
            authenticated.bungie_membership_id,
            class_type=class_type,
        )
        icons = await asyncio.to_thread(
            request.app[LOADOUT_MANAGER_SERVICE_KEY].loadout_icons
        )
        if editing is not None:
            selected = {
                int(item["bucket_hash"]): item["item_instance_id"]
                for item in editing["items"]
            }
            for bucket in catalog["buckets"]:
                bucket["choices"].sort(
                    key=lambda choice: choice["instance_id"]
                    != selected.get(int(bucket["bucket_hash"]))
                )
    except (
        BungieError,
        InventoryDataError,
        LoadoutInspectionError,
        ManifestError,
        ValueError,
    ) as error:
        catalog = None
        icons = []
        error_html = notice(str(error), "error")
    else:
        error_html = ""
    html = render_template(
        "loadout_builder.html",
        guardian_name=escape(authenticated.display_name),
        notices=(
            notice(request.query.get("notice", ""), "success")
            + notice(request.query.get("error", ""), "error")
            + error_html
        ),
        class_tabs="" if editing else render_class_tabs(class_type),
        builder=(
            render_builder(
                request,
                catalog,
                editing=editing,
                set_id=set_id,
                icons=icons,
            ) if catalog is not None else ""
        ),
    )
    response = web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )
    response.enable_compression()
    return response


async def save_builder_loadout(request: web.Request) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before saving a loadout.")
    class_type = 0
    try:
        form = await request.post()
        require_csrf(request, form)
        class_type = int(str(form.get("class_type") or "-1"))
        await ensure_fresh_loadout_snapshot(
            request, authenticated, force=True
        )
        selections = {
            bucket_hash: str(form.get(f"bucket_{bucket_hash}") or "")
            for bucket_hash in GAMEPLAY_BUCKET_NAMES
        }
        saved = await asyncio.to_thread(
            request.app[LOADOUT_MANAGER_SERVICE_KEY].save_builder_loadout,
            authenticated.bungie_membership_id,
            class_type=class_type,
            selections=selections,
            name=str(form.get("name") or ""),
            description=str(form.get("description") or ""),
            tags=parse_tags(form.get("tags")),
            cover_icon_hash=int(str(form.get("cover_icon_hash") or "0")),
            loadout_id=str(form.get("loadout_id") or "") or None,
            set_id=str(form.get("set_id") or "") or None,
        )
    except web.HTTPForbidden as error:
        failure = error.text or "The builder form was rejected."
    except (
        BungieError,
        InventoryDataError,
        LoadoutInspectionError,
        ManifestError,
        ValueError,
    ) as error:
        failure = str(error)
    except Exception:
        LOGGER.exception("Unexpected builder save failure")
        failure = "The builder loadout could not be saved."
    else:
        query = urlencode({"notice": "Complete builder loadout saved."})
        raise web.HTTPSeeOther(
            f"/loadouts/saved/{saved['loadout_id']}?{query}"
        )
    query = urlencode({"class": class_type, "error": failure})
    raise web.HTTPSeeOther(f"/loadouts/builder?{query}")


async def loadout_builder_items(request: web.Request) -> web.Response:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before browsing builder items.")
    try:
        class_type = int(request.query.get("class", "-1"))
        bucket_hash = int(request.query.get("bucket", "0"))
        offset = max(0, int(request.query.get("offset", "0")))
        query = " ".join(request.query.get("q", "").split()).lower()[:100]
        await ensure_fresh_loadout_snapshot(request, authenticated)
        catalog = await asyncio.to_thread(
            request.app[LOADOUT_MANAGER_SERVICE_KEY].builder_catalog,
            authenticated.bungie_membership_id,
            class_type=class_type,
        )
        bucket = next(
            row for row in catalog["buckets"]
            if int(row["bucket_hash"]) == bucket_hash
        )
    except (
        BungieError,
        InventoryDataError,
        LoadoutInspectionError,
        ManifestError,
        StopIteration,
        ValueError,
    ) as error:
        return web.json_response({"error": str(error)}, status=400)
    choices = bucket["choices"]
    if query:
        choices = [
            choice for choice in choices
            if query in " ".join(
                (
                    choice["name"], choice["type"], choice["location"],
                    choice["instance_id"],
                )
            ).lower()
        ]
    page_size = 24
    page = choices[offset:offset + page_size]
    next_offset = offset + len(page)
    return web.json_response(
        {
            "html": "".join(
                render_choice(bucket_hash, choice) for choice in page
            ),
            "total": len(choices),
            "next_offset": next_offset,
            "has_more": next_offset < len(choices),
        },
        headers={"Cache-Control": "no-store"},
    )


def render_class_tabs(selected: int) -> str:
    return "".join(
        f'<a class="builder-class{" active" if value == selected else ""}" '
        f'href="/loadouts/builder?class={value}">{escape(name)}</a>'
        for value, name in CLASS_NAMES.items()
        if value in (0, 1, 2)
    )


def render_builder(
    request: web.Request,
    catalog: dict,
    *,
    editing: dict | None,
    set_id: str,
    icons: list[dict],
) -> str:
    selected = {
        int(item["bucket_hash"]): item["item_instance_id"]
        for item in (editing["items"] if editing else [])
    }
    buckets = "".join(
        render_bucket(bucket, selected_instance=selected.get(bucket["bucket_hash"]))
        for bucket in catalog["buckets"]
    )
    disabled = "" if catalog["complete_catalog"] else " disabled"
    is_fork = bool(editing and set_id)
    name = (
        f'{editing["name"]} · Set variant'
        if is_fork
        else editing["name"] if editing else ""
    )
    description = editing["description"] if editing else ""
    tags = ", ".join(editing["tags"]) if editing else ""
    icon_choices = "".join(
        render_icon_choice(
            icon,
            selected_hash=(editing or {}).get("cover_icon_hash"),
        )
        for icon in icons
    )
    hidden_context = (
        f'<input type="hidden" name="loadout_id" value="{escape(editing["loadout_id"])}">'
        if editing else ""
    ) + (
        f'<input type="hidden" name="set_id" value="{escape(set_id)}">'
        if set_id else ""
    )
    heading = "Name the new set-specific loadout" if is_fork else (
        "Edit this loadout" if editing else f"Name this {escape(catalog['class_name'])} loadout"
    )
    button = "Create variant and update this set" if is_fork else (
        "Save new revision" if editing else "Validate and save complete loadout"
    )
    return f"""
<form class="builder-form" method="post" action="/loadouts/builder/save">
  {csrf_input(request, '/loadouts/builder/save')}
  <input type="hidden" name="class_type" value="{catalog['class_type']}">
  {hidden_context}
  <section class="builder-metadata">
    <div><p class="eyebrow">Complete revision only</p>
      <h2>{heading}</h2>
      <p>Selections stay in this browser until all ten exact gameplay slots
      validate. Sockets remain read-only and are captured from live items.</p></div>
    <div class="manager-form">
      <label>Name<input name="name" maxlength="80" required value="{escape(name)}"></label>
      <label>Tags<input name="tags" maxlength="400" value="{escape(tags)}" placeholder="raid, boss, damage"></label>
      <label class="wide">Description<textarea name="description" maxlength="2000" rows="2">{escape(description)}</textarea></label>
      <fieldset class="wide"><legend>Cover icon</legend><div class="icon-picker">{icon_choices}</div></fieldset>
      <button type="submit"{disabled}>{button}</button>
    </div>
  </section>
  <div class="builder-toolbar">
    <label>Filter items<input type="search" data-builder-search placeholder="Weapon, armor, location…"></label>
    <span data-builder-count>0 / 10 slots selected</span>
  </div>
  <section class="builder-buckets">{buckets}</section>
</form>"""


def render_bucket(bucket: dict, *, selected_instance: str | None = None) -> str:
    choices = "".join(
        render_choice(
            bucket["bucket_hash"],
            choice,
            selected=choice["instance_id"] == selected_instance,
        )
        for choice in bucket["choices"][:6]
    )
    if not choices:
        choices = '<p class="builder-empty">No compatible owned item.</p>'
    return f"""
<fieldset class="builder-bucket" data-bucket>
  <legend><span>{escape(bucket['name'])}</span>
    <small>{len(bucket['choices'])} compatible</small></legend>
  <div class="builder-bucket-tools">
    <label>Search this slot<input type="search" data-bucket-query placeholder="Name, type, location, instance"></label>
    <button type="button" data-load-more>Load more</button>
    <span data-bucket-result>{min(6, len(bucket['choices']))} of {len(bucket['choices'])}</span>
  </div>
  <div class="builder-choice-grid" data-bucket-grid
    data-class="" data-bucket-hash="{bucket['bucket_hash']}"
    data-offset="{min(6, len(bucket['choices']))}"
    data-total="{len(bucket['choices'])}">{choices}</div>
</fieldset>"""


def render_choice(
    bucket_hash: int, choice: dict, *, selected: bool = False
) -> str:
    image = (
        f'<img src="{escape(url)}" alt="">'
        if (url := icon_url(choice.get("icon_path")))
        else '<span class="builder-item-fallback">◇</span>'
    )
    badges = [choice["location"]]
    if choice["locked"]:
        badges.append("Locked")
    if choice["protected"]:
        badges.append("Saved")
    if choice["exotic"]:
        badges.append("Exotic")
    if choice["non_transferable"]:
        badges.append("Non-transferable")
    if not choice["selectable"]:
        badges.append(choice["unsupported_reason"])
    badge_html = "".join(f"<span>{escape(value)}</span>" for value in badges)
    plug_html = "".join(
        (
            f'<span class="builder-plug" tabindex="0" '
            f'data-tooltip="{escape(plug.get("description") or plug["name"])}">'
            + (
                f'<img src="{escape(url)}" alt="{escape(plug["name"])}">'
                if (url := icon_url(plug.get("icon_path")))
                else "·"
            )
            + "</span>"
        )
        for plug in choice["plugs"]
        if not plug.get("filtered_from_preview")
    )
    stat_total = sum(stat["value"] for stat in choice["stats"])
    stat_detail = ", ".join(
        f'{stat["name"]} {stat["value"]}' for stat in choice["stats"]
    ) or "No displayed stats"
    search = " ".join(
        [choice["name"], choice["type"], choice["location"], *badges]
    ).lower()
    return f"""
<div class="builder-choice" data-search="{escape(search)}">
  <input type="radio" id="pick-{bucket_hash}-{escape(choice['instance_id'])}"
    name="bucket_{bucket_hash}" value="{escape(choice['instance_id'])}" required{' checked' if selected else ''}{' disabled' if not choice['selectable'] else ''}>
  <label for="pick-{bucket_hash}-{escape(choice['instance_id'])}">
    {image}<span class="builder-item-copy"><strong>{escape(choice['name'])}</strong>
      <small title="{escape(stat_detail)}">{escape(choice['type'])} · Power {choice['power'] or '—'} · Stats {stat_total}</small>
      <span class="builder-badges">{badge_html}</span>
      <span class="builder-plugs">{plug_html}</span>
      <code>…{escape(choice['instance_id'][-8:])}</code>
    </span>
  </label>
</div>"""


def render_icon_choice(icon: dict, *, selected_hash: int | None) -> str:
    url = icon_url(icon.get("icon_path"))
    art = (
        f'<img src="{escape(url)}" alt="">'
        if url
        else '<span class="icon-fallback">◇</span>'
    )
    return (
        f'<label class="icon-choice"><input type="radio" '
        f'name="cover_icon_hash" value="{icon["hash"]}" required'
        f'{" checked" if int(icon["hash"]) == int(selected_hash or 0) else ""}>'
        f'<span>{art}<span class="sr-only">{escape(icon["name"])}</span></span></label>'
    )


def notice(value: str, tone: str) -> str:
    return (
        f'<div class="notice {tone}">{escape(value)}</div>' if value else ""
    )


def render_template(name: str, **values: str) -> str:
    from destiny_web_app.ui import render_header

    values.setdefault("header", render_header(name, values))
    return Template((TEMPLATE_ROOT / name).read_text(encoding="utf-8")).safe_substitute(values)
