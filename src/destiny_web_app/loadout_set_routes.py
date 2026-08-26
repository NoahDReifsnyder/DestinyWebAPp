"""Set-oriented loadout dashboard creation and board editing routes."""

from __future__ import annotations

import asyncio
import logging
import json
from html import escape
from pathlib import Path
from string import Template
from urllib.parse import quote, urlencode

from aiohttp import web

from destiny_web_app.app_keys import (
    AUTH_SESSION_KEY,
    LOADOUT_MANAGER_SERVICE_KEY,
    LOADOUT_FUNCTIONS_KEY,
)
from destiny_web_app.auth import csrf_input, require_csrf
from destiny_web_app.inventory_routes import icon_url
from destiny_web_app.loadout_freshness import ensure_fresh_loadout_snapshot
from destiny_web_app.loadout_manager import CLASS_NAMES, LoadoutInspectionError
from destiny_web_app.loadout_sets import LoadoutSetError
from destiny_web_app.loadouts.inspection import inspect_in_game_loadouts
from destiny_web_app.loadouts.library import list_loadouts, list_set_membership_names
from destiny_web_app.loadouts.sets import (
    create_loadout_set,
    create_loadout_set_from_character,
    delete_loadout_set,
    get_loadout_set,
    rename_loadout_set,
    save_loadout_set_board,
)


LOGGER = logging.getLogger(__name__)
TEMPLATE_ROOT = Path(__file__).with_name("templates")


async def create_loadout_page(request: web.Request) -> web.Response:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPSeeOther("/")
    functions = request.app[LOADOUT_FUNCTIONS_KEY]
    error = ""
    try:
        await ensure_fresh_loadout_snapshot(
            request, authenticated, force=True
        )
        inspection, saved_loadouts = await asyncio.gather(
            asyncio.to_thread(
                inspect_in_game_loadouts,
                functions,
                authenticated.bungie_membership_id,
            ),
            asyncio.to_thread(
                list_loadouts,
                functions,
                authenticated.bungie_membership_id,
                include_archived=False,
            ),
        )
        set_memberships = await asyncio.to_thread(
            list_set_membership_names,
            functions,
            authenticated.bungie_membership_id,
        )
    except Exception as exc:
        LOGGER.exception("Could not prepare in-game loadout import")
        inspection, saved_loadouts = None, []
        set_memberships = {}
        error = str(exc)
    characters = inspection["characters"] if inspection else []
    html = render_template(
        "loadout_create.html",
        guardian_name=escape(authenticated.display_name),
        notices=(
            notice(request.query.get("notice", ""), "success")
            + notice(request.query.get("error", "") or error, "error")
        ),
        character_options="".join(
            f'<option value="{escape(row["character_id"])}">'
            f'{escape(row["class_name"])}</option>'
            for row in characters
        ),
        saved_loadouts="".join(
            render_edit_loadout(
                row,
                set_memberships.get(str(row["loadout_id"]), []),
            )
            for row in saved_loadouts
        ),
        rename_csrf=csrf_input(request, "/loadouts/create/rename"),
        delete_csrf=csrf_input(request, "/loadouts/create/delete"),
        duplicate_loadouts=render_duplicate_loadouts(
            saved_loadouts,
            csrf_input(request, "/loadouts/saved/delete-duplicates"),
            csrf_input(request, "/loadouts/saved/delete"),
        ),
    )
    return web.Response(
        text=html, content_type="text/html", headers={"Cache-Control": "no-store"}
    )


async def create_set(request: web.Request) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before creating a set.")
    try:
        form = await request.post()
        require_csrf(request, form)
        board = await asyncio.to_thread(
            create_loadout_set,
            request.app[LOADOUT_FUNCTIONS_KEY],
            authenticated.bungie_membership_id,
            name=str(form.get("name") or ""),
            character_class=int(str(form.get("character_class") or "-1")),
        )
    except Exception as error:
        raise web.HTTPSeeOther(
            "/loadouts?" + urlencode({"error": str(error)})
        )
    raise web.HTTPSeeOther(f'/loadout-sets/{board["set_id"]}')


async def create_set_from_character(
    request: web.Request,
) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before importing a set.")
    try:
        form = await request.post()
        require_csrf(request, form)
        await ensure_fresh_loadout_snapshot(
            request, authenticated, force=True
        )
        board = await asyncio.to_thread(
            create_loadout_set_from_character,
            request.app[LOADOUT_FUNCTIONS_KEY],
            authenticated.bungie_membership_id,
            name=str(form.get("name") or ""),
            character_id=str(form.get("character_id") or ""),
        )
    except Exception as error:
        LOGGER.exception("Could not import a character's loadout set")
        raise web.HTTPSeeOther(
            "/loadouts?" + urlencode({"error": str(error)})
        )
    raise web.HTTPSeeOther(
        f'/loadout-sets/{board["set_id"]}?'
        + urlencode(
            {
                "notice": (
                    f'Imported {board["filled_count"]} loadouts in their '
                    "original positions."
                )
            }
        )
    )


async def loadout_set_page(request: web.Request) -> web.Response:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPSeeOther("/")
    functions = request.app[LOADOUT_FUNCTIONS_KEY]
    try:
        await ensure_fresh_loadout_snapshot(request, authenticated)
        board, loadouts = await asyncio.gather(
            asyncio.to_thread(
                get_loadout_set,
                functions,
                authenticated.bungie_membership_id,
                set_id=request.match_info["set_id"],
            ),
            asyncio.to_thread(
                list_loadouts,
                functions,
                authenticated.bungie_membership_id,
                include_archived=True,
            ),
        )
    except Exception as error:
        LOGGER.exception("Could not prepare the loadout set editor")
        raise web.HTTPSeeOther(
            "/loadouts?" + urlencode({"error": str(error)})
        )
    if board is None:
        raise web.HTTPNotFound()
    compatible = [
        row
        for row in loadouts
        if int(row["character_class_type"])
        == int(board["character_class_type"])
    ]
    html = render_template(
        "loadout_set.html",
        guardian_name=escape(authenticated.display_name),
        set_name=escape(board["name"]),
        class_name=escape(board["class_name"]),
        notices=(
            notice(request.query.get("notice", ""), "success")
            + notice(request.query.get("error", ""), "error")
        ),
        board=render_editor_board(board),
        tray="".join(render_tray_item(row) for row in compatible)
        or '<p class="set-empty-copy">Create a loadout for this class first.</p>',
        set_id=escape(board["set_id"]),
        version=str(board["version"]),
        save_csrf=csrf_input(request, "/loadout-sets/save"),
        delete_csrf=csrf_input(request, "/loadout-sets/delete"),
        rename_csrf=csrf_input(request, "/loadout-sets/rename"),
        preview_csrf=csrf_input(request, "/loadout-sets/preview"),
        preview_disabled="",
    )
    return web.Response(
        text=html, content_type="text/html", headers={"Cache-Control": "no-store"}
    )


async def rename_edit_loadout(request: web.Request) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before renaming a loadout.")
    try:
        form = await request.post()
        require_csrf(request, form)
        loadout_id = str(form.get("loadout_id") or "")
        name = str(form.get("name") or "")
        service = request.app[LOADOUT_MANAGER_SERVICE_KEY]
        current = await asyncio.to_thread(
            service.saved_loadout,
            authenticated.bungie_membership_id,
            loadout_id,
            include_archived=True,
        )
        if current is None:
            raise LookupError("The saved loadout is unavailable.")
        await asyncio.to_thread(
            service.update_metadata,
            authenticated.bungie_membership_id,
            loadout_id,
            name=name,
            description=current["description"],
            tags=current["tags"],
            cover_icon_hash=current.get("cover_icon_hash"),
        )
    except Exception as error:
        LOGGER.exception("Could not rename loadout from edit page")
        raise web.HTTPSeeOther(
            "/loadouts/create?" + urlencode({"error": str(error)})
        )
    raise web.HTTPSeeOther(
        "/loadouts/create?" + urlencode({"notice": "Loadout name saved."})
    )


async def delete_edit_loadout(request: web.Request) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before deleting a loadout.")
    try:
        form = await request.post()
        require_csrf(request, form)
        loadout_id = str(form.get("loadout_id") or "")
        detach = str(form.get("remove_from_sets") or "") == "1"
        service = request.app[LOADOUT_MANAGER_SERVICE_KEY]
        memberships = await asyncio.to_thread(
            service.loadout_set_memberships,
            authenticated.bungie_membership_id,
            loadout_id,
        )
        if memberships and not detach:
            names = sorted({str(row["name"]) for row in memberships})
            raise LoadoutSetError(
                "This loadout is used by "
                + ", ".join(names)
                + ". Confirm removal from those sets before deleting it."
            )
        await asyncio.to_thread(
            service.delete_loadout,
            authenticated.bungie_membership_id,
            loadout_id,
            detach_from_sets=detach,
        )
    except Exception as error:
        LOGGER.exception("Could not delete loadout from edit page")
        raise web.HTTPSeeOther("/loadouts/create?" + urlencode({"error": str(error)}))
    notice_text = "Loadout deleted. Destiny was not changed."
    if memberships:
        notice_text += f" Removed from {len(memberships)} set position(s)."
    raise web.HTTPSeeOther(
        "/loadouts/create?" + urlencode({"notice": notice_text})
    )


async def save_set(request: web.Request) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before saving a set.")
    set_id = ""
    try:
        form = await request.post()
        require_csrf(request, form)
        set_id = str(form.get("set_id") or "")
        slots = {
            position: str(form.get(f"slot_{position}") or "")
            for position in range(20)
            if str(form.get(f"slot_{position}") or "")
        }
        await asyncio.to_thread(
            save_loadout_set_board,
            request.app[LOADOUT_FUNCTIONS_KEY],
            authenticated.bungie_membership_id,
            set_id=set_id,
            slots=slots,
            expected_version=int(str(form.get("version") or "0")),
        )
    except (LoadoutSetError, LookupError, ValueError) as error:
        raise web.HTTPSeeOther(
            f"/loadout-sets/{quote(set_id, safe='')}?"
            + urlencode({"error": str(error)})
        )
    raise web.HTTPSeeOther(
        f"/loadout-sets/{quote(set_id, safe='')}?"
        + urlencode({"notice": "Set board saved."})
    )


async def rename_set(request: web.Request) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before renaming a set.")
    set_id = ""
    try:
        form = await request.post()
        require_csrf(request, form)
        set_id = str(form.get("set_id") or "")
        await asyncio.to_thread(
            rename_loadout_set,
            request.app[LOADOUT_FUNCTIONS_KEY],
            authenticated.bungie_membership_id,
            set_id=set_id,
            name=str(form.get("name") or ""),
        )
    except (LoadoutSetError, LookupError, ValueError) as error:
        raise web.HTTPSeeOther(
            f"/loadout-sets/{quote(set_id, safe='')}?"
            + urlencode({"error": str(error)})
        )
    raise web.HTTPSeeOther(
        f"/loadout-sets/{quote(set_id, safe='')}?"
        + urlencode({"notice": "Set renamed."})
    )


async def delete_set(request: web.Request) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before deleting a set.")
    form = await request.post()
    require_csrf(request, form)
    set_id = str(form.get("set_id") or "")
    await asyncio.to_thread(
        delete_loadout_set,
        request.app[LOADOUT_FUNCTIONS_KEY],
        authenticated.bungie_membership_id,
        set_id=set_id,
    )
    raise web.HTTPSeeOther(
        "/loadouts?" + urlencode({"notice": "Loadout set permanently deleted."})
    )


def render_import_character(character: dict) -> str:
    slots = "".join(render_import_slot(character, slot) for slot in character["slots"])
    return (
        f'<section class="import-slots" data-import-character="{escape(character["character_id"])}">'
        f'<h2>{escape(character["class_name"])} loadouts</h2><div class="import-slot-grid">{slots}</div></section>'
    )


def render_edit_loadout(loadout: dict, set_names: list[str] | None = None) -> str:
    display_name = loadout_display_name(loadout)
    api_name = str(loadout.get("destiny_api_name") or display_name)
    preview = json.dumps(
        detailed_loadout_preview(loadout), separators=(",", ":")
    )
    sets = json.dumps(set_names or [], separators=(",", ":"))
    return f'''
<button type="button" class="edit-loadout" data-edit-loadout
 data-character-id="{escape(str(loadout.get("source_character_id", "")))}"
 data-loadout-name="{escape(display_name)}"
 data-destiny-api-name="{escape(api_name)}"
 data-preview-json="{escape(preview, quote=True)}"
 data-set-names="{escape(sets, quote=True)}"
 data-loadout-id="{escape(loadout["loadout_id"])}">
 {image_or_fallback(loadout.get("cover_icon_path"), loadout["class_name"][:1])}
 <span>{escape(display_name)}</span>
</button>'''


def loadout_display_name(loadout: dict) -> str:
    return str(loadout.get("name") or loadout.get("destiny_api_name") or "Unnamed loadout")


def detailed_loadout_preview(loadout: dict) -> dict[str, list]:
    armor_buckets = {"helmet", "gauntlets", "chest armor", "leg armor", "class armor"}
    weapons = []
    armor = []
    subclass = {"super": [], "aspects": [], "abilities": [], "fragments": []}
    for item in loadout.get("items", []):
        bucket = str(item.get("bucket_name", "")).lower()
        if "weapon" in bucket:
            target = weapons
        elif bucket in armor_buckets:
            target = armor
        elif bucket == "subclass":
            for plug in item.get("plugs", []):
                if plug.get("plug_hash") is None or not plug.get("icon_path"):
                    continue
                category = str(plug.get("category", "")).lower()
                name = str(plug.get("name", "Unknown selection"))
                if "super" in category:
                    group = "super"
                elif "aspect" in category:
                    group = "aspects"
                elif "fragment" in category:
                    group = "fragments"
                else:
                    group = "abilities"
                subclass[group].append(preview_image(plug))
            continue
        else:
            continue
        plugs = [
            plug
            for plug in item.get("plugs", [])
            if plug.get("plug_hash") is not None
            and not is_cosmetic_plug(plug)
        ]
        plugs.sort(key=preview_mod_order)
        target.append(
            {
                "name": str(item.get("name", "Unknown item")),
                "icon_path": str(item.get("icon_path") or ""),
                "mods": [preview_image(plug) for plug in plugs if plug.get("icon_path")],
            }
        )
    return {"weapons": weapons, "armor": armor, "subclass": subclass}


def preview_image(value: dict) -> dict[str, str]:
    return {
        "name": str(value.get("name", "Unknown item")),
        "icon_path": str(value.get("icon_path") or ""),
    }


def preview_mod_order(plug: dict) -> tuple[int, int]:
    text = " ".join(
        str(plug.get(key, "")).lower()
        for key in ("name", "category", "socket_type")
    )
    if "stat" in text:
        priority = 0
    elif "tuning" in text or "masterwork" in text:
        priority = 1
    else:
        priority = 2
    return priority, int(plug.get("socket_index", 0))


def is_cosmetic_plug(plug: dict) -> bool:
    text = " ".join(
        str(plug.get(key, "")).lower()
        for key in ("name", "category", "socket_type")
    )
    return "ornament" in text or "shader" in text


def loadout_fingerprint(loadout: dict) -> tuple:
    return tuple(
        (
            int(item.get("bucket_hash", 0)),
            str(item.get("item_instance_id", "")),
            tuple(
                (int(plug.get("socket_index", 0)), plug.get("plug_hash"))
                for plug in item.get("plugs", [])
            ),
        )
        for item in sorted(
            loadout.get("items", []),
            key=lambda item: int(item.get("bucket_hash", 0)),
        )
    )


def render_duplicate_loadouts(
    loadouts: list[dict],
    bulk_delete_csrf: str,
    delete_csrf: str,
) -> str:
    groups: dict[tuple, list[dict]] = {}
    for loadout in loadouts:
        groups.setdefault(loadout_fingerprint(loadout), []).append(loadout)
    duplicates = [group for group in groups.values() if len(group) > 1]
    if not duplicates:
        return '<p class="duplicate-empty">No exact duplicate loadouts found.</p>'
    duplicate_ids = [loadout["loadout_id"] for group in duplicates for loadout in group[1:]]
    bulk_form = (
        '<form method="post" action="/loadouts/saved/delete-duplicates" '
        'onsubmit="return confirm(\'Permanently remove all found duplicate loadouts?\')">'
        f'{bulk_delete_csrf}'
        + "".join(
            f'<input type="hidden" name="loadout_id" value="{escape(loadout_id)}">'
            for loadout_id in duplicate_ids
        )
        + '<input type="hidden" name="confirmation" value="DELETE">'
        '<button class="danger" type="submit">Remove all duplicates</button></form>'
    )
    return bulk_form + "".join(
        '<section class="duplicate-group">'
        f'<h3>{escape(group[0]["class_name"])} · {len(group)} exact matches</h3>'
        '<div class="duplicate-list">'
        + "".join(
            f'<div class="duplicate-item"><span>{escape(loadout_display_name(loadout))}</span>'
            f'<form method="post" action="/loadouts/saved/delete" '
            f'onclick="return confirm(\'Permanently delete {escape(loadout["name"], quote=True)}?\')">'
            f'{delete_csrf}<input type="hidden" name="loadout_id" value="{escape(loadout["loadout_id"])}">'
            '<input type="hidden" name="confirmation" value="DELETE">'
            '<button class="danger" type="submit">Remove</button></form></div>'
            for loadout in group[1:]
        )
        + "</div></section>"
        for group in duplicates
    )


def render_import_slot(character: dict, slot: dict) -> str:
    disabled = not slot["items"]
    state = "Empty" if not slot["items"] else (
        f'{slot["unresolved_count"]} missing · kept blank'
        if slot["unresolved_count"] else f'{len(slot["items"])} items'
    )
    icon = image_or_fallback(slot.get("icon_path"), character["class_name"][:1])
    return f"""
<button type="button" class="import-slot" data-import-slot
 data-character-id="{escape(character['character_id'])}"
 data-slot-index="{slot['slot_index']}"
 data-slot-name="{escape(slot['name'])}"
 data-icon-hash="{slot['identifiers'].get('icon') or ''}"
 {'disabled' if disabled else ''}>
 {icon}<strong>Slot {slot['display_index']}</strong><span>{escape(state)}</span>
</button>"""


def render_icon_choices(icons: list[dict]) -> str:
    return "".join(
        f'<label class="icon-choice"><input type="radio" name="cover_icon_hash" '
        f'value="{row["hash"]}" required><span>{image_or_fallback(row.get("icon_path"), "◇")}'
        f'<span class="sr-only">{escape(row["name"])}</span></span></label>'
        for row in icons
    )


def render_editor_board(board: dict) -> str:
    slots = {int(row["position"]): row for row in board["slots"]}
    return "".join(render_editor_cell(position, slots.get(position), board) for position in range(20))


def render_editor_cell(position: int, row: dict | None, board: dict) -> str:
    loadout = row.get("loadout") if row else None
    loadout_id = loadout["loadout_id"] if loadout else ""
    content = render_loadout_icon(loadout, position, board["set_id"]) if loadout else '<span class="empty-plus">+</span>'
    clear = (
        '<button type="button" class="cell-select" data-cell-select '
        'aria-label="Select this position to move or swap">↔</button>'
        '<button type="button" class="cell-clear" data-cell-clear '
        'aria-label="Empty this position">×</button>'
        if loadout else ""
    )
    return f"""
<div class="set-cell {'occupied' if loadout else 'empty'}" data-set-cell data-position="{position}" tabindex="0" aria-label="Set position {position + 1}" draggable="{'true' if loadout else 'false'}">
  <input type="hidden" name="slot_{position}" value="{escape(loadout_id)}" data-slot-value>
  <span class="slot-number">{position + 1}</span>{content}{clear}
</div>"""


def render_tray_item(loadout: dict) -> str:
    preview = json.dumps(loadout_preview_items(loadout), separators=(",", ":"))
    return f"""
<button type="button" class="tray-loadout" draggable="true" data-tray-loadout
 data-loadout-id="{escape(loadout['loadout_id'])}" data-loadout-name="{escape(loadout['name'])}"
 data-icon-path="{escape(loadout.get('cover_icon_path') or '')}"
 data-preview-json="{escape(preview, quote=True)}"
 aria-label="Select {escape(loadout['name'])}">
 {image_or_fallback(loadout.get('cover_icon_path'), loadout['class_name'][:1])}
 <span>{escape(loadout['name'])}</span>
</button>"""


def loadout_preview_items(loadout: dict) -> list[dict[str, str]]:
    armor_slots = {"helmet", "gauntlets", "chest armor", "leg armor", "class armor"}
    items = [
        item
        for item in loadout.get("items", [])
        if "weapon" in str(item.get("bucket_name", "")).lower()
        or str(item.get("bucket_name", "")).lower() in armor_slots
    ]
    weapon_items = [
        {
            "category": "Weapons",
            "name": str(item.get("name", "Unknown item")),
            "icon_path": str(item.get("icon_path") or ""),
        }
        for item in items
        if "weapon" in str(item.get("bucket_name", "")).lower()
    ]
    armor_items = [
        {
            "category": "Armor",
            "name": str(item.get("name", "Unknown item")),
            "icon_path": str(item.get("icon_path") or ""),
        }
        for item in items
        if str(item.get("bucket_name", "")).lower() in armor_slots
    ]
    subclass = next(
        (
            item
            for item in loadout.get("items", [])
            if str(item.get("bucket_name", "")).lower() == "subclass"
        ),
        None,
    )
    plugs = (subclass or {}).get("plugs") or []
    super_plug = next(
        (
            plug
            for plug in plugs
            if "super" in str(plug.get("category", "")).lower()
        ),
        {},
    )
    super_items = []
    subclass_items = []
    if super_plug.get("icon_path"):
        super_items.append(
            {
                "category": "Super",
                "name": str(super_plug.get("name", "Super")),
                "icon_path": str(super_plug["icon_path"]),
            }
        )
    for plug in plugs:
        if plug.get("plug_hash") is None:
            continue
        category = str(plug.get("category", "")).lower()
        name = str(plug.get("name", "Unknown ability"))
        if "super" in category:
            continue
        if "fragment" in category:
            group = "Fragments"
        elif "aspect" in category:
            group = "Aspects"
        else:
            group = "Abilities"
        subclass_items.append(
            {"category": group, "name": name, "icon_path": str(plug.get("icon_path") or "")}
        )
    return weapon_items + super_items + armor_items + subclass_items


def render_loadout_icon(loadout: dict, position: int, set_id: str) -> str:
    href = (
        f'/loadouts/saved/{quote(loadout["loadout_id"], safe="")}'
        f'?set_id={quote(set_id, safe="")}&position={position}'
    )
    return (
        f'<a class="board-loadout-link" href="{escape(href, quote=True)}" title="{escape(loadout["name"])} · Slot {position + 1}" '
        f'aria-label="Open {escape(loadout["name"])} in slot {position + 1}">'
        f'{image_or_fallback(loadout.get("cover_icon_path"), loadout["class_name"][:1])}</a>'
    )


def image_or_fallback(path: str | None, fallback: str) -> str:
    url = icon_url(path) if path else ""
    return f'<img src="{escape(url)}" alt="">' if url else f'<span class="icon-fallback">{escape(fallback)}</span>'


def notice(value: str, tone: str) -> str:
    return f'<div class="notice {tone}">{escape(value)}</div>' if value else ""


def render_template(name: str, **values: str) -> str:
    from destiny_web_app.ui import render_header

    values.setdefault("header", render_header(name, values))
    return Template((TEMPLATE_ROOT / name).read_text(encoding="utf-8")).substitute(values)
