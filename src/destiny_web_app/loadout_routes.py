"""Pages for inspecting and saving exact Destiny loadouts."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from string import Template
from typing import Any
from urllib.parse import quote, urlencode

from aiohttp import web

from destiny_web_app.app_keys import (
    ACTIVITY_PLAN_SERVICE_KEY,
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
    LoadoutInspectionError,
    snapshot_is_fresh,
)
from destiny_web_app.manifest import ManifestError
from destiny_web_app.loadouts.library import list_loadouts
from destiny_web_app.loadouts.sets import list_loadout_sets


TEMPLATE_ROOT = Path(__file__).with_name("templates")
LOGGER = logging.getLogger(__name__)


async def loadout_manager_page(request: web.Request) -> web.Response:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPSeeOther("/")
    functions = request.app[LOADOUT_FUNCTIONS_KEY]
    try:
        await ensure_fresh_loadout_snapshot(request, authenticated)
        saved_loadouts, loadout_sets, source = await asyncio.gather(
            asyncio.to_thread(
                list_loadouts,
                functions,
                authenticated.bungie_membership_id,
                include_archived=False,
            ),
            asyncio.to_thread(
                list_loadout_sets,
                functions,
                authenticated.bungie_membership_id,
            ),
            asyncio.to_thread(
                functions.store.load_active_loadout_source,
                authenticated.bungie_membership_id,
            ),
        )
        error_html = ""
    except (
        BungieError,
        InventoryDataError,
        LoadoutInspectionError,
        ManifestError,
        ValueError,
    ) as error:
        saved_loadouts, loadout_sets, source = [], [], None
        error_html = notice(str(error), "error")
    warnings = (
        message_html(request.query.get("notice", ""), "success")
        + message_html(request.query.get("error", ""), "error")
        + error_html
    )
    html = render_template(
        "loadouts.html",
        guardian_name=escape(authenticated.display_name),
        warnings=warnings,
        loadout_count=str(len(saved_loadouts)),
        set_count=str(len(loadout_sets)),
        set_sections=(
            "".join(
                render_dashboard_set(
                    board,
                    preview_csrf=csrf_input(
                        request, "/loadout-sets/preview"
                    ),
                )
                for board in loadout_sets
            )
            or '<div class="dashboard-empty">No sets yet.</div>'
        ),
        all_loadouts=render_all_dashboard_loadouts(saved_loadouts),
        create_set_csrf=csrf_input(request, "/loadout-sets/create"),
        import_set_csrf=csrf_input(
            request, "/loadout-sets/create-from-character"
        ),
        character_options=render_set_character_options(source),
        import_set_disabled=(
            "" if source and source.get("characters") else " disabled"
        ),
    )
    response = web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )
    response.enable_compression()
    return response


def render_set_character_options(source: dict[str, Any] | None) -> str:
    return "".join(
        f'<option value="{escape(str(character["character_id"]))}">'
        f'{escape(CLASS_NAMES.get(int(character.get("class_type", 3)), "Guardian"))}'
        "</option>"
        for character in (source or {}).get("characters", [])
        if int(character.get("class_type", 3)) in (0, 1, 2)
    )


async def loadout_slot_page(request: web.Request) -> web.Response:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPSeeOther("/")
    character_id = request.match_info["character_id"]
    if not character_id.isdecimal() or int(character_id) <= 0:
        raise web.HTTPNotFound()
    try:
        slot_index = int(request.match_info["slot_index"])
    except ValueError as error:
        raise web.HTTPNotFound() from error
    if slot_index < 0:
        raise web.HTTPNotFound()

    try:
        await ensure_fresh_loadout_snapshot(request, authenticated)
        inspection = await asyncio.to_thread(
            request.app[LOADOUT_MANAGER_SERVICE_KEY].inspect,
            authenticated.bungie_membership_id,
        )
    except (
        BungieError,
        InventoryDataError,
        LoadoutInspectionError,
        ManifestError,
        ValueError,
    ) as error:
        return loadout_error_response(authenticated.display_name, str(error))
    if inspection is None:
        raise web.HTTPSeeOther("/loadouts")

    character = next(
        (
            value
            for value in inspection["characters"]
            if value["character_id"] == character_id
        ),
        None,
    )
    if character is None or slot_index >= len(character["slots"]):
        raise web.HTTPNotFound()
    slot = character["slots"][slot_index]
    html = render_template(
        "loadout_slot.html",
        guardian_name=escape(authenticated.display_name),
        class_name=escape(character["class_name"]),
        slot_number=str(slot["display_index"]),
        loadout_name=escape(slot["name"]),
        notices=(
            message_html(request.query.get("notice", ""), "success")
            + message_html(request.query.get("error", ""), "error")
        ),
        slot_art=slot_art(slot),
        slot_summary=render_slot_summary(slot),
        item_list=render_slot_items(slot["items"]),
        import_form=render_import_form(
            request,
            character_id,
            slot,
            enabled=bool(slot["items"]),
        ),
        snapshot_label=escape(snapshot_age(inspection["snapshot"])),
        freshness=(
            "fresh" if snapshot_is_fresh(inspection["snapshot"]) else "stale"
        ),
    )
    response = web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )
    response.enable_compression()
    return response


async def capture_current_loadout(request: web.Request) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before saving a loadout.")
    try:
        form = await request.post()
        require_csrf(request, form)
        await ensure_fresh_loadout_snapshot(
            request, authenticated, force=True
        )
        saved = await asyncio.to_thread(
            request.app[LOADOUT_MANAGER_SERVICE_KEY].capture_current_equipment,
            authenticated.bungie_membership_id,
            character_id=str(form.get("character_id") or ""),
            name=str(form.get("name") or ""),
            description=str(form.get("description") or ""),
            tags=parse_tags(form.get("tags")),
        )
    except web.HTTPForbidden as error:
        query = urlencode({"error": error.text or "The save form was rejected."})
        raise web.HTTPSeeOther(f"/loadouts?{query}")
    except (
        BungieError,
        InventoryDataError,
        LoadoutInspectionError,
        ManifestError,
        LookupError,
        ValueError,
    ) as error:
        query = urlencode({"error": str(error)})
        raise web.HTTPSeeOther(f"/loadouts?{query}")
    except Exception:
        LOGGER.exception("Unexpected current-loadout capture failure")
        query = urlencode({"error": "The current loadout could not be saved."})
        raise web.HTTPSeeOther(f"/loadouts?{query}")
    query = urlencode({"notice": f"Saved {saved['name']} as revision 1."})
    raise web.HTTPSeeOther(f"/loadouts/saved/{saved['loadout_id']}?{query}")


async def import_in_game_loadout(request: web.Request) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before importing a loadout.")
    return_path = "/loadouts"
    try:
        form = await request.post()
        require_csrf(request, form)
        character_id = str(form.get("character_id") or "")
        slot_index = int(str(form.get("slot_index") or "-1"))
        if character_id.isdecimal() and slot_index >= 0:
            return_path = (
                f"/loadouts/{quote(character_id, safe='')}/{slot_index}"
            )
        await ensure_fresh_loadout_snapshot(
            request, authenticated, force=True
        )
        saved = await asyncio.to_thread(
            request.app[LOADOUT_MANAGER_SERVICE_KEY].import_in_game_slot,
            authenticated.bungie_membership_id,
            character_id=character_id,
            slot_index=slot_index,
            name=str(form.get("name") or ""),
            description=str(form.get("description") or ""),
            tags=parse_tags(form.get("tags")),
            cover_icon_hash=(
                int(str(form.get("cover_icon_hash")))
                if form.get("cover_icon_hash")
                else None
            ),
        )
    except web.HTTPForbidden as error:
        query = urlencode({"error": error.text or "The import form was rejected."})
        raise web.HTTPSeeOther(f"{return_path}?{query}")
    except (
        BungieError,
        InventoryDataError,
        LoadoutInspectionError,
        ManifestError,
        LookupError,
        ValueError,
    ) as error:
        query = urlencode({"error": str(error)})
        raise web.HTTPSeeOther(f"{return_path}?{query}")
    except Exception:
        LOGGER.exception("Unexpected in-game loadout import failure")
        query = urlencode({"error": "The in-game loadout could not be imported."})
        raise web.HTTPSeeOther(f"{return_path}?{query}")
    query = urlencode({"notice": f"Imported {saved['name']} as revision 1."})
    raise web.HTTPSeeOther(f"/loadouts/saved/{saved['loadout_id']}?{query}")


async def saved_loadout_page(request: web.Request) -> web.Response:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPSeeOther("/")
    service = request.app[LOADOUT_MANAGER_SERVICE_KEY]
    loadout_id = request.match_info["loadout_id"]
    try:
        await ensure_fresh_loadout_snapshot(request, authenticated)
        loadout, revisions, inspection, icons, usages = await asyncio.gather(
            asyncio.to_thread(
                service.saved_loadout,
                authenticated.bungie_membership_id,
                loadout_id,
                include_archived=True,
            ),
            asyncio.to_thread(
                service.loadout_revisions,
                authenticated.bungie_membership_id,
                loadout_id,
            ),
            asyncio.to_thread(
                service.inspect,
                authenticated.bungie_membership_id,
            ),
            asyncio.to_thread(service.loadout_icons),
            asyncio.to_thread(
                request.app[LOADOUT_FUNCTIONS_KEY].sets.usages,
                authenticated.bungie_membership_id,
                loadout_id,
            ),
        )
    except (
        BungieError,
        InventoryDataError,
        LoadoutInspectionError,
        ManifestError,
        ValueError,
    ) as error:
        return loadout_error_response(authenticated.display_name, str(error))
    if loadout is None:
        raise web.HTTPNotFound()
    comparison = ""
    left = request.query.get("left", "")
    right = request.query.get("right", "")
    if left and right:
        try:
            comparison_data = await asyncio.to_thread(
                service.compare_revisions,
                authenticated.bungie_membership_id,
                loadout_id,
                left,
                right,
            )
        except LoadoutInspectionError as error:
            comparison = notice(str(error), "error")
        else:
            comparison = render_revision_comparison(comparison_data)
    characters = [
        row
        for row in (inspection["characters"] if inspection else [])
        if row["class_name"] == loadout["class_name"]
    ]
    html = render_template(
        "saved_loadout.html",
        guardian_name=escape(authenticated.display_name),
        loadout_name=escape(loadout["name"]),
        class_name=escape(loadout["class_name"]),
        notices=(
            message_html(request.query.get("notice", ""), "success")
            + message_html(request.query.get("error", ""), "error")
        ),
        status=render_saved_status(loadout),
        provenance=render_saved_provenance(loadout),
        description=(
            f'<p class="saved-description">{escape(loadout["description"])}</p>'
            if loadout["description"]
            else ""
        ),
        tags=render_tags(loadout["tags"]),
        cover_icon=dashboard_icon(loadout),
        set_usages=render_set_usages(usages),
        edit_link=render_exact_edit_link(request, loadout),
        loadout_controls=render_loadout_controls(
            request, loadout, revisions, characters, icons
        ),
        comparison=comparison,
        revision_history=render_loadout_revision_history(
            request, loadout, revisions
        ),
        item_list=render_saved_items(loadout["items"]),
        local_controls=render_local_loadout_controls(request, loadout),
    )
    response = web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )
    response.enable_compression()
    return response


async def update_saved_loadout(request: web.Request) -> web.StreamResponse:
    return await saved_loadout_action(
        request,
        "/loadouts/saved/update",
        lambda service, owner, loadout_id, form: service.update_metadata(
            owner,
            loadout_id,
            name=str(form.get("name") or ""),
            description=str(form.get("description") or ""),
            tags=parse_tags(form.get("tags")),
            cover_icon_hash=(
                int(str(form.get("cover_icon_hash")))
                if form.get("cover_icon_hash")
                else None
            ),
        ),
        "Loadout metadata updated.",
    )


async def revise_saved_loadout(request: web.Request) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before revising a loadout.")
    loadout_id = ""
    try:
        form = await request.post()
        require_csrf(request, form)
        loadout_id = str(form.get("loadout_id") or "")
        await ensure_fresh_loadout_snapshot(
            request, authenticated, force=True
        )
        saved = await asyncio.to_thread(
            request.app[LOADOUT_MANAGER_SERVICE_KEY].revise_with_current_equipment,
            authenticated.bungie_membership_id,
            loadout_id,
            character_id=str(form.get("character_id") or ""),
            revision_note=str(form.get("revision_note") or ""),
        )
    except web.HTTPForbidden as error:
        failure = error.text or "The revision form was rejected."
    except (
        BungieError,
        InventoryDataError,
        LoadoutInspectionError,
        LookupError,
        ManifestError,
        ValueError,
    ) as error:
        failure = str(error)
    except Exception:
        LOGGER.exception("Unexpected loadout revision failure")
        failure = "The new loadout revision could not be captured."
    else:
        return redirect_saved(
            saved["loadout_id"],
            notice_text=f"Current equipment saved as revision {saved['revision_number']}.",
        )
    return redirect_saved(loadout_id, error_text=failure)


async def clone_saved_loadout(request: web.Request) -> web.StreamResponse:
    return await saved_loadout_action(
        request,
        "/loadouts/saved/clone",
        lambda service, owner, loadout_id, form: service.clone_loadout(
            owner,
            loadout_id,
            name=str(form.get("name") or ""),
        ),
        "Loadout cloned with an independent immutable history.",
        use_result_id=True,
    )


async def restore_saved_loadout_revision(
    request: web.Request,
) -> web.StreamResponse:
    return await saved_loadout_action(
        request,
        "/loadouts/saved/restore-revision",
        lambda service, owner, loadout_id, form: service.restore_revision(
            owner,
            loadout_id,
            str(form.get("revision_id") or ""),
            revision_note=str(form.get("revision_note") or ""),
        ),
        "Selected revision restored as a new current revision.",
    )


async def archive_saved_loadout(request: web.Request) -> web.StreamResponse:
    return await saved_loadout_action(
        request,
        "/loadouts/saved/archive",
        lambda service, owner, loadout_id, form: service.set_archived(
            owner,
            loadout_id,
            archived=str(form.get("archived") or "") == "1",
        ),
        "Loadout archive state updated.",
    )


async def delete_saved_loadout(request: web.Request) -> web.StreamResponse:
    def delete(service: Any, owner: str, loadout_id: str, form: Any) -> None:
        if str(form.get("confirmation") or "") != "DELETE":
            raise LoadoutInspectionError(
                "Type DELETE to permanently delete the local loadout."
            )
        service.delete_loadout(owner, loadout_id)

    return await saved_loadout_action(
        request,
        "/loadouts/saved/delete",
        delete,
        "Local loadout permanently deleted. Destiny was not changed.",
        overview_after=True,
    )


async def favorite_saved_loadout(request: web.Request) -> web.StreamResponse:
    return await saved_loadout_action(
        request,
        "/loadouts/saved/favorite",
        lambda service, owner, loadout_id, form: service.set_favorite(
            owner,
            loadout_id,
            favorite=str(form.get("favorite") or "") == "1",
        ),
        "Favorite state updated.",
    )


async def export_saved_loadout(request: web.Request) -> web.Response:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before exporting a loadout.")
    bundle = await asyncio.to_thread(
        request.app[LOADOUT_MANAGER_SERVICE_KEY].export_bundle,
        authenticated.bungie_membership_id,
        request.match_info["loadout_id"],
    )
    filename = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in bundle["name"]
    ).strip("-") or "loadout"
    return web.Response(
        text=json.dumps(bundle, indent=2, sort_keys=True),
        content_type="application/json",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{filename}.destiny-loadout.json"',
        },
    )


async def import_saved_loadout_bundle(request: web.Request) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before importing a loadout.")
    try:
        form = await request.post()
        require_csrf(request, form)
        raw = str(form.get("bundle") or "")
        if len(raw.encode("utf-8")) > 1024 * 1024:
            raise LoadoutInspectionError("The import bundle exceeds 1 MiB.")
        bundle = json.loads(raw)
        if not isinstance(bundle, dict):
            raise LoadoutInspectionError("The import must be a JSON object.")
        await ensure_fresh_loadout_snapshot(
            request, authenticated, force=True
        )
        if bundle.get("format") == "destiny-web-app/activity-plan":
            imported = await asyncio.to_thread(
                request.app[ACTIVITY_PLAN_SERVICE_KEY].import_bundle,
                authenticated.bungie_membership_id,
                bundle,
            )
            destination = f"/loadout-plans/{imported['plan_id']}"
            imported_label = "Version 1 activity plan and exact loadouts imported after ownership validation."
        else:
            imported = await asyncio.to_thread(
                request.app[LOADOUT_MANAGER_SERVICE_KEY].import_bundle,
                authenticated.bungie_membership_id,
                bundle,
            )
            destination = f"/loadouts/saved/{imported['loadout_id']}"
            imported_label = "Version 1 loadout imported after exact ownership validation."
    except web.HTTPForbidden as error:
        failure = error.text or "The import form was rejected."
    except (
        json.JSONDecodeError,
        BungieError,
        InventoryDataError,
        LoadoutInspectionError,
        ManifestError,
        ValueError,
    ) as error:
        failure = str(error)
    except Exception:
        LOGGER.exception("Unexpected loadout bundle import failure")
        failure = "The versioned loadout bundle could not be imported."
    else:
        raise web.HTTPSeeOther(
            destination + "?" + urlencode({"notice": imported_label})
        )
    raise web.HTTPSeeOther(f"/loadouts?{urlencode({'error': failure})}")


async def saved_loadout_action(
    request: web.Request,
    csrf_path: str,
    operation: Any,
    success: str,
    *,
    use_result_id: bool = False,
    overview_after: bool = False,
) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before changing a loadout.")
    loadout_id = ""
    try:
        form = await request.post()
        require_csrf(request, form)
        loadout_id = str(form.get("loadout_id") or "")
        result = await asyncio.to_thread(
            operation,
            request.app[LOADOUT_MANAGER_SERVICE_KEY],
            authenticated.bungie_membership_id,
            loadout_id,
            form,
        )
    except web.HTTPForbidden as error:
        failure = error.text or "The loadout form was rejected."
    except (LoadoutInspectionError, LookupError, ManifestError, ValueError) as error:
        failure = str(error)
    except Exception:
        LOGGER.exception("Unexpected saved-loadout mutation failure")
        failure = "The saved loadout change could not be completed."
    else:
        if use_result_id and isinstance(result, dict):
            loadout_id = result["loadout_id"]
        if overview_after:
            raise web.HTTPSeeOther(
                f"/loadouts?{urlencode({'notice': success})}"
            )
        return redirect_saved(loadout_id, notice_text=success)
    return redirect_saved(loadout_id, error_text=failure)


def parse_tags(value: Any) -> list[str]:
    return [part for part in str(value or "").split(",")]


def render_loadout_controls(
    request: web.Request,
    loadout: dict[str, Any],
    revisions: list[dict[str, Any]],
    characters: list[dict[str, Any]],
    icons: list[dict[str, Any]],
) -> str:
    loadout_id = escape(loadout["loadout_id"])
    archived = bool(loadout["archived_at"])
    disabled = " disabled" if archived else ""
    character_options = "".join(
        f'<option value="{escape(row["character_id"])}" '
        f'data-slot-count="{int(row["slot_count"])}">'
        f'{escape(row["class_name"])} · {int(row["slot_count"])} slots</option>'
        for row in characters
    )
    revision_options = "".join(
        f'<option value="{escape(row["revision_id"])}">'
        f'Revision {row["revision_number"]} · {escape(row["revision_action"])}</option>'
        for row in revisions
    )
    icon_choices = "".join(
        render_metadata_icon_choice(
            row, selected_hash=loadout.get("cover_icon_hash")
        )
        for row in icons
    )
    return f"""
<section class="manager-grid">
  <details class="manager-panel sync-panel" open><summary>Preview synchronization to one slot</summary>
    <form class="manager-form" method="post" action="/loadouts/preview">
      {csrf_input(request, '/loadouts/preview')}
      <input type="hidden" name="loadout_id" value="{loadout_id}">
      <input type="hidden" name="revision_id" value="{escape(loadout['revision_id'])}">
      <label>Target character<select name="target_character_id" required{disabled}>{character_options}</select></label>
      <label>Exact in-game slot<input type="number" name="target_slot" min="1" max="20" required placeholder="1"{disabled}></label>
      <p class="wide">Creates a fresh read-only plan showing the slot's current contents, exact transfers, preparation, overwrite, verification, and equipment restoration. No Bungie write occurs in preview.</p>
      <button type="submit"{disabled or (' disabled' if not character_options else '')}>Refresh and preview exact slot</button>
    </form>
  </details>
  <details class="manager-panel"><summary>Edit display metadata</summary>
    <form class="manager-form" method="post" action="/loadouts/saved/update">
      {csrf_input(request, '/loadouts/saved/update')}
      <input type="hidden" name="loadout_id" value="{loadout_id}">
      <label>Name<input name="name" maxlength="80" required value="{escape(loadout['name'])}"{disabled}></label>
      <label>Tags<input name="tags" maxlength="400" value="{escape(', '.join(loadout['tags']))}"{disabled}></label>
      <label class="wide">Description<textarea name="description" maxlength="2000" rows="3"{disabled}>{escape(loadout['description'])}</textarea></label>
      <fieldset class="wide"><legend>Cover icon</legend><div class="icon-picker">{icon_choices}</div></fieldset>
      <button type="submit"{disabled}>Save metadata</button>
    </form>
  </details>
  <details class="manager-panel"><summary>Clone this loadout</summary>
    <form class="manager-form" method="post" action="/loadouts/saved/clone">
      {csrf_input(request, '/loadouts/saved/clone')}
      <input type="hidden" name="loadout_id" value="{loadout_id}">
      <label>Clone name<input name="name" maxlength="80" required value="Copy of {escape(loadout['name'])}"></label>
      <button type="submit">Create independent clone</button>
    </form>
  </details>
  <details class="manager-panel"><summary>Compare revisions</summary>
    <form class="manager-form" method="get" action="/loadouts/saved/{loadout_id}">
      <label>Earlier<select name="left" required>{revision_options}</select></label>
      <label>Later<select name="right" required>{revision_options}</select></label>
      <button type="submit">Compare exact state</button>
    </form>
  </details>
</section>"""


def render_metadata_icon_choice(
    icon: dict[str, Any], *, selected_hash: int | None
) -> str:
    path = icon.get("icon_path")
    url = icon_url(path) if path else ""
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


def render_set_usages(usages: list[dict[str, Any]]) -> str:
    if not usages:
        return '<p class="usage-empty">This loadout is not currently used by a set.</p>'
    return "".join(
        f'<a class="usage-chip" href="/loadout-sets/{quote(row["set_id"], safe="")}">'
        f'{escape(row["name"])} · slots '
        f'{", ".join(str(position + 1) for position in row["positions"])}</a>'
        for row in usages
    )


def render_exact_edit_link(
    request: web.Request, loadout: dict[str, Any]
) -> str:
    set_id = str(request.query.get("set_id") or "")
    query = {"edit": loadout["loadout_id"]}
    label = "Edit exact items"
    if set_id:
        query["set_id"] = set_id
        label = "Create a set-specific item variant"
    return (
        f'<a class="primary-action" href="/loadouts/builder?{urlencode(query)}">'
        f'{escape(label)}</a>'
    )


def render_loadout_revision_history(
    request: web.Request,
    loadout: dict[str, Any],
    revisions: list[dict[str, Any]],
) -> str:
    rows = []
    for revision in revisions:
        current = revision["revision_id"] == loadout["revision_id"]
        restore = ""
        if not current and not loadout["archived_at"]:
            restore = f"""
<form method="post" action="/loadouts/saved/restore-revision">
  {csrf_input(request, '/loadouts/saved/restore-revision')}
  <input type="hidden" name="loadout_id" value="{escape(loadout['loadout_id'])}">
  <input type="hidden" name="revision_id" value="{escape(revision['revision_id'])}">
  <input type="hidden" name="revision_note" value="Restored revision {revision['revision_number']}">
  <button class="text-button" type="submit">Restore as new revision</button>
</form>"""
        rows.append(
            f"<tr><td>{revision['revision_number']}{' · current' if current else ''}</td>"
            f"<td>{escape(revision['revision_action'])}</td>"
            f"<td>{escape(revision['revision_note'])}</td>"
            f"<td>{escape(revision['captured_at'])}</td><td>{restore}</td></tr>"
        )
    return f"""
<section class="manager-panel open-panel"><div><p class="eyebrow">Immutable history</p><h2>Loadout revisions</h2></div>
  <div class="table-scroll"><table><thead><tr><th>Revision</th><th>Action</th><th>Note</th><th>Created</th><th></th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
</section>"""


def render_revision_comparison(comparison: dict[str, Any]) -> str:
    rows = []
    for row in comparison["rows"]:
        left = row["left"]
        right = row["right"]
        rows.append(
            f"<tr class=\"{escape(row['status'].replace(' ', '-'))}\">"
            f"<th>{escape(row['bucket_name'])}</th>"
            f"<td>{escape(left['name'] if left else 'Missing')}</td>"
            f"<td>{escape(right['name'] if right else 'Missing')}</td>"
            f"<td>{escape(row['status'])}</td></tr>"
        )
    return f"""
<section class="manager-panel open-panel comparison"><div><p class="eyebrow">Exact comparison</p>
  <h2>Revision {comparison['left']['revision_number']} → {comparison['right']['revision_number']}</h2></div>
  <div class="table-scroll"><table><thead><tr><th>Slot</th><th>Earlier</th><th>Later</th><th>Result</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
</section>"""


def render_local_loadout_controls(
    request: web.Request,
    loadout: dict[str, Any],
) -> str:
    return f"""
<section class="danger-zone"><div><p class="eyebrow">Local controls</p><h2>Export or delete</h2>
  <p>Deletion never changes Destiny and is blocked while any set uses this loadout.</p></div>
  <form method="post" action="/loadouts/saved/favorite">
    {csrf_input(request, '/loadouts/saved/favorite')}
    <input type="hidden" name="loadout_id" value="{escape(loadout['loadout_id'])}">
    <input type="hidden" name="favorite" value="{'0' if loadout['favorite'] else '1'}">
    <button type="submit">{'Remove favorite' if loadout['favorite'] else 'Add favorite'}</button>
  </form>
  <a class="export-link" href="/loadouts/saved/{escape(loadout['loadout_id'])}/export">Export version 1 JSON</a>
  <form method="post" action="/loadouts/saved/delete">
    {csrf_input(request, '/loadouts/saved/delete')}
    <input type="hidden" name="loadout_id" value="{escape(loadout['loadout_id'])}">
    <label>Type DELETE<input name="confirmation" required autocomplete="off"></label>
    <button class="danger-button" type="submit">Delete local loadout permanently</button>
  </form>
</section>"""


def render_capture_form(
    request: web.Request,
    inspection: dict[str, Any] | None,
) -> str:
    characters = inspection["characters"] if inspection else []
    options = "".join(
        f'<option value="{escape(character["character_id"])}">'
        f'{escape(character["class_name"])}</option>'
        for character in characters
    )
    disabled = "" if characters else " disabled"
    guidance = (
        "Capture refreshes inventory first, then stores all equipped gameplay "
        "items and ordered socket state as an immutable first revision."
        if characters
        else "Synchronize inventory once before capturing current equipment."
    )
    return f"""
<section class="capture-card">
  <div>
    <p class="eyebrow">Application loadout</p>
    <h2>Save current equipment</h2>
    <p>{escape(guidance)}</p>
  </div>
  <form method="post" action="/loadouts/capture-current">
    {csrf_input(request, "/loadouts/capture-current")}
    <label>Character<select name="character_id" required{disabled}>{options}</select></label>
    <label>Name<input name="name" maxlength="80" required placeholder="Deep Stone Crypt · Atraks"></label>
    <label>Tags<input name="tags" maxlength="400" placeholder="raid, dsc, boss"></label>
    <label class="wide">Description<textarea name="description" maxlength="2000" rows="2"></textarea></label>
    <button type="submit"{disabled}>Capture exact equipment</button>
  </form>
  <a class="builder-launch" href="/loadouts/builder">Open exact-item builder →</a>
</section>"""


def render_import_form(
    request: web.Request,
    character_id: str,
    slot: dict[str, Any],
    *,
    enabled: bool,
) -> str:
    disabled = "" if enabled else " disabled"
    guidance = (
        "Import refreshes inventory first, then stores this exact in-game "
        "slot in the application library."
        if enabled
        else "An empty in-game slot cannot be imported."
    )
    return f"""
<section class="capture-card compact">
  <div>
    <p class="eyebrow">Immutable import</p>
    <h2>Save this in-game slot</h2>
    <p>{escape(guidance)}</p>
  </div>
  <form method="post" action="/loadouts/import-slot">
    {csrf_input(request, "/loadouts/import-slot")}
    <input type="hidden" name="character_id" value="{escape(character_id)}">
    <input type="hidden" name="slot_index" value="{slot['slot_index']}">
    <label>Name<input name="name" maxlength="80" required value="{escape(slot['name'])}"{disabled}></label>
    <label>Tags<input name="tags" maxlength="400" placeholder="raid, encounter"{disabled}></label>
    <label class="wide">Description<textarea name="description" maxlength="2000" rows="2"{disabled}></textarea></label>
    <button type="submit"{disabled}>Import exact slot</button>
  </form>
</section>"""


def render_saved_library(loadouts: list[dict[str, Any]]) -> str:
    if not loadouts:
        cards = """
<div class="saved-empty">
  No application loadouts yet. Capture current equipment or import a resolved
  in-game slot to create the first immutable revision.
</div>"""
    else:
        cards = "".join(render_saved_card(loadout) for loadout in loadouts)
    return f"""
<section class="saved-library">
  <div class="section-heading">
    <div><p class="eyebrow">Persistent library</p><h2>Saved loadouts</h2></div>
    <span class="read-only-badge">No Bungie writes</span>
  </div>
  <div class="saved-grid">{cards}</div>
</section>"""


def render_dashboard_set(
    board: dict[str, Any], *, preview_csrf: str = ""
) -> str:
    slots = {int(row["position"]): row["loadout"] for row in board["slots"]}
    cells = "".join(
        render_dashboard_cell(
            slots.get(position),
            position=position,
            set_id=board["set_id"],
        )
        for position in range(20)
    )
    return f"""
<details class="dashboard-section set-dashboard-section">
  <summary><span><strong>{escape(board['name'])}</strong>
    <small>{escape(board['class_name'])} · {board['filled_count']} of 20 filled</small></span>
    <span class="section-chevron" aria-hidden="true">⌄</span></summary>
  <div class="dashboard-section-body">
    <div class="dashboard-board">{cells}</div>
    <div class="dashboard-set-actions">
      <form method="post" action="/loadout-sets/preview">
        {preview_csrf}
        <input type="hidden" name="set_id" value="{escape(board['set_id'])}">
        <button class="primary-action" type="submit"{' disabled' if not board['filled_count'] else ''}>Preview applying set</button>
      </form>
      <a class="secondary-action" href="/loadout-sets/{quote(board['set_id'], safe='')}">Edit set</a>
    </div>
  </div>
</details>"""


def render_dashboard_cell(
    loadout: dict[str, Any] | None,
    *,
    position: int,
    set_id: str,
) -> str:
    if loadout is None:
        return (
            f'<span class="dashboard-cell empty" title="Empty · Destiny slot {position + 1}">'
            f'<span class="slot-number">{position + 1}</span></span>'
        )
    href = (
        f'/loadouts/saved/{quote(loadout["loadout_id"], safe="")}'
        f'?set_id={quote(set_id, safe="")}&position={position}'
    )
    art = dashboard_icon(loadout)
    return f"""
<a class="dashboard-cell occupied" href="{escape(href, quote=True)}"
 title="{escape(loadout['name'])} · Destiny slot {position + 1}"
 aria-label="Open {escape(loadout['name'])}, Destiny slot {position + 1}">
 <span class="slot-number">{position + 1}</span>{art}
</a>"""


def render_all_dashboard_loadouts(loadouts: list[dict[str, Any]]) -> str:
    groups = []
    for class_type, class_name in ((0, "Titan"), (1, "Hunter"), (2, "Warlock")):
        rows = [
            row for row in loadouts
            if int(row["character_class_type"]) == class_type
        ]
        icons = "".join(
            f'<a class="library-icon" href="/loadouts/saved/{quote(row["loadout_id"], safe="")}" '
            f'title="{escape(row["name"])}" aria-label="Open {escape(row["name"])}">'
            f'{dashboard_icon(row)}</a>'
            for row in rows
        ) or '<span class="class-empty">No saved loadouts.</span>'
        groups.append(
            f'<section class="class-loadouts"><h3>{class_name}</h3><div class="library-icon-grid">{icons}</div></section>'
        )
    return "".join(groups)


def dashboard_icon(loadout: dict[str, Any]) -> str:
    path = loadout.get("cover_icon_path")
    url = icon_url(path) if path else ""
    if url:
        return f'<img src="{escape(url)}" alt="">'
    return f'<span class="icon-fallback">{escape(loadout["class_name"][:1])}</span>'


def render_saved_card(loadout: dict[str, Any]) -> str:
    status = loadout["live_status"]
    if loadout["revision_action"] == "builder":
        source = "Exact inventory builder"
    elif loadout["revision_action"] == "import" and loadout[
        "capture_source"
    ] != "in_game_slot":
        source = "Versioned JSON import"
    elif loadout["capture_source"] == "in_game_slot":
        source = f'In-game slot {int(loadout["source_slot_index"]) + 1}'
    else:
        source = "Current equipment"
    reference_label = (
        f" · {loadout['reference_item_count']} reference-only"
        if loadout["reference_item_count"]
        else ""
    )
    archived = bool(loadout.get("archived_at"))
    archive_label = " · archived" if archived else ""
    favorite = "★ " if loadout.get("favorite") else ""
    return f"""
<a class="saved-card {escape(status)}" href="/loadouts/saved/{quote(loadout['loadout_id'], safe='')}">
  <span class="saved-state">{escape(status)}{archive_label}</span>
  <p class="eyebrow">{escape(loadout['class_name'])} · Revision {loadout['revision_number']}</p>
  <h3>{favorite}{escape(loadout['name'])}</h3>
  <p>{escape(source)} · {loadout['item_count']} gameplay items · {loadout['plug_count']} sockets{reference_label}</p>
</a>"""


def filter_saved_loadouts(
    loadouts: list[dict[str, Any]],
    *,
    query: str,
    class_filter: str,
    tag_filter: str,
    view: str,
) -> tuple[list[dict[str, Any]], str]:
    query_text = " ".join(query.split()).lower()
    valid_views = {"active", "archived", "favorites", "all"}
    view = view if view in valid_views else "active"
    class_filter = class_filter if class_filter in {"0", "1", "2"} else ""
    tag_filter = " ".join(tag_filter.split()).lower()
    tags = sorted({tag for loadout in loadouts for tag in loadout["tags"]})
    filtered = []
    for loadout in loadouts:
        if view == "active" and loadout["archived_at"]:
            continue
        if view == "archived" and not loadout["archived_at"]:
            continue
        if view == "favorites" and not loadout.get("favorite"):
            continue
        if class_filter and str(loadout["character_class_type"]) != class_filter:
            continue
        if tag_filter and tag_filter not in loadout["tags"]:
            continue
        haystack = " ".join(
            [loadout["name"], loadout["description"], *loadout["tags"]]
        ).lower()
        if query_text and query_text not in haystack:
            continue
        filtered.append(loadout)
    view_options = "".join(
        f'<option value="{value}"{" selected" if view == value else ""}>{label}</option>'
        for value, label in (
            ("active", "Active"),
            ("favorites", "Favorites"),
            ("archived", "Archived"),
            ("all", "All"),
        )
    )
    class_options = "".join(
        f'<option value="{value}"{" selected" if class_filter == value else ""}>{label}</option>'
        for value, label in (("", "All classes"), ("0", "Titan"), ("1", "Hunter"), ("2", "Warlock"))
    )
    tag_options = '<option value="">All tags</option>' + "".join(
        f'<option value="{escape(tag)}"{" selected" if tag_filter == tag else ""}>{escape(tag)}</option>'
        for tag in tags
    )
    controls = f"""
<form class="library-filters" method="get" action="/loadouts">
  <label>Search<input type="search" name="q" value="{escape(query)}" placeholder="Name, description, tag"></label>
  <label>View<select name="view">{view_options}</select></label>
  <label>Class<select name="class">{class_options}</select></label>
  <label>Tag<select name="tag">{tag_options}</select></label>
  <button type="submit">Apply filters</button>
  <span>{len(filtered)} result(s)</span>
</form>"""
    return filtered, controls


def render_import_card(request: web.Request) -> str:
    return f"""
<details class="manager-panel import-panel"><summary>Import version 1 loadout or activity-plan JSON</summary>
  <form class="manager-form" method="post" action="/loadouts/import-bundle">
    {csrf_input(request, '/loadouts/import-bundle')}
    <label class="wide">Bundle JSON<textarea name="bundle" rows="7" maxlength="1048576" required placeholder='{{"format":"destiny-web-app/loadout","version":1,…}}'></textarea></label>
    <p class="wide">Import refreshes inventory and accepts either supported bundle only if every exact instance, class, target character, slot, and unfiltered socket still belongs to this signed-in account.</p>
    <button type="submit">Validate ownership and import</button>
  </form>
</details>"""


def render_plan_creator(request: web.Request) -> str:
    return f"""
<section class="capture-card plan-creator">
  <div><p class="eyebrow">Goal 3 · Activity plans</p>
    <h2>Create a loadout set</h2>
    <p>Build an ordered raid or activity plan with encounter groups and exact
    character-slot assignments. This is local only.</p></div>
  <form method="post" action="/loadout-plans/create">
    {csrf_input(request, '/loadout-plans/create')}
    <label>Plan name<input name="name" maxlength="100" required placeholder="Deep Stone Crypt prep"></label>
    <label>Activity<input name="activity_name" maxlength="120" required placeholder="Deep Stone Crypt"></label>
    <label>Version<input name="activity_version" maxlength="80" placeholder="Current season"></label>
    <input type="hidden" name="game" value="Destiny 2">
    <label class="wide">Description<textarea name="description" maxlength="3000" rows="2"></textarea></label>
    <button type="submit">Create activity plan</button>
  </form>
</section>"""


def render_activity_plans(plans: list[dict[str, Any]]) -> str:
    cards = "".join(render_activity_plan_card(plan) for plan in plans)
    if not cards:
        cards = '<div class="saved-empty">No activity plans yet.</div>'
    return f"""
<section class="saved-library plan-library">
  <div class="section-heading"><div><p class="eyebrow">Ordered loadout sets</p>
    <h2>Activity plans</h2></div><span class="read-only-badge">Local only</span></div>
  <div class="saved-grid">{cards}</div>
</section>"""


def render_activity_plan_card(plan: dict[str, Any]) -> str:
    archived = " · archived" if plan["archived_at"] else ""
    return f"""
<a class="saved-card plan-card" href="/loadout-plans/{quote(plan['plan_id'], safe='')}">
  <span class="saved-state">Revision {plan['revision_number']}{archived}</span>
  <p class="eyebrow">{escape(plan['activity_name'])}</p>
  <h3>{escape(plan['name'])}</h3>
  <p>{plan['encounter_count']} encounter groups · {plan['assignment_count']} exact slot assignments</p>
</a>"""


def render_saved_status(loadout: dict[str, Any]) -> str:
    issues = loadout["validation_issues"]
    issue_rows = (
        "<ul>" + "".join(f"<li>{escape(issue)}</li>" for issue in issues) + "</ul>"
        if issues
        else "<p>Every exact item is still in its captured location.</p>"
    )
    if loadout.get("partial"):
        issue_rows += (
            "<p>This is a partial Destiny loadout. Missing items and blank "
            "sockets intentionally keep their current setting when applied.</p>"
        )
    return f"""
<section class="saved-status {escape(loadout['live_status'])}">
  <strong>{escape(loadout['live_status'])}</strong>
  <div><h2>Live exact-instance validation</h2>{issue_rows}</div>
</section>"""


def render_saved_provenance(loadout: dict[str, Any]) -> str:
    if loadout["revision_action"] == "builder":
        source = "Exact inventory builder"
    elif loadout["revision_action"] == "import" and loadout[
        "capture_source"
    ] != "in_game_slot":
        source = "Versioned JSON import"
    elif loadout["capture_source"] == "in_game_slot":
        source = f'In-game slot {int(loadout["source_slot_index"]) + 1}'
    else:
        source = "Current equipment"
    return f"""
<dl class="provenance">
  <div><dt>Source</dt><dd>{escape(source)}</dd></div>
  <div><dt>Source character</dt><dd>{escape(loadout['source_character_id'])}</dd></div>
  <div><dt>Revision</dt><dd>{loadout['revision_number']} · immutable</dd></div>
  <div><dt>Captured</dt><dd>{escape(loadout['captured_at'])}</dd></div>
  <div><dt>Snapshot</dt><dd>{escape(loadout['snapshot_id'])}</dd></div>
  <div><dt>Manifest</dt><dd>{escape(loadout['manifest_version'])}</dd></div>
</dl>"""


def render_tags(tags: list[str]) -> str:
    return "".join(f'<span class="tag">{escape(tag)}</span>' for tag in tags)


def render_saved_items(items: list[dict[str, Any]]) -> str:
    return "".join(render_saved_item(item) for item in items)


def render_saved_item(item: dict[str, Any]) -> str:
    item_icon = icon_url(item.get("icon_path"))
    icon_html = (
        f'<img src="{escape(item_icon)}" alt="" loading="lazy">'
        if item_icon
        else '<span aria-hidden="true">?</span>'
    )
    plug_rows = "".join(
        f"""
<div class="plug-row {escape(plug['sync_capability'])}">
  <div><strong>Socket {plug['socket_index'] + 1} · {escape(plug['name'])}</strong>
  <small>Hash {plug['plug_hash'] if plug['plug_hash'] is not None else 'missing'} · {escape(plug['sync_capability'])}</small></div>
</div>"""
        for plug in item["plugs"]
    )
    live = item["live_status"]
    return f"""
<article class="loadout-item {escape(live)}">
  <header>
    <span class="item-icon">{icon_html}</span>
    <div><p class="eyebrow">{escape(item['bucket_name'])}</p>
      <h2>{escape(item['name'])}</h2>
      <p>{escape(item['type'])} · Instance {escape(item['item_instance_id'])}</p>
    </div>
    <span class="resolution {escape(live)}">{escape(live)}</span>
  </header>
  <details><summary>Inspect {len(item['plugs'])} ordered sockets
    <span>Restoration capability is recorded per socket</span></summary>
    <div class="plug-grid">{plug_rows}</div>
  </details>
</article>"""


def summary_cards(inspection: dict[str, Any] | None) -> str:
    if inspection is None:
        values = ("0", "0", "—", "0")
    else:
        data = inspection["summary"]
        values = (
            str(data["character_count"]),
            str(data["observed_slots_per_character"]),
            (
                str(data["manifest_capacity"])
                if data["manifest_capacity"] is not None
                else "—"
            ),
            str(data["unique_unresolved_count"]),
        )
    labels = (
        "Characters",
        "Observed slots each",
        "Manifest maximum",
        "Unresolved exact items",
    )
    return "".join(
        f"<div><strong>{escape(value)}</strong><span>{label}</span></div>"
        for value, label in zip(values, labels, strict=True)
    )


def render_capability_matrix(rows: tuple[dict[str, str], ...]) -> str:
    body = []
    for row in rows:
        body.append(
            f"""
<tr>
  <th scope="row">{escape(row["field"])}</th>
  <td>{escape(row["read"])}</td>
  <td><span class="capability {escape(row["status"])}">
    {escape(row["write"])}</span></td>
  <td>{escape(row["detail"])}</td>
</tr>"""
        )
    return f"""
<section class="capability-card">
  <div class="section-heading">
    <div>
      <p class="eyebrow">Goal 1 capability audit</p>
      <h2>What the API can represent</h2>
    </div>
    <span class="read-only-badge">Read only</span>
  </div>
  <p>This matrix is based on the checked-in Bungie OpenAPI contract and the
  active CharacterLoadouts response. “Contract available” does not mean a live
  write has been accepted yet.</p>
  <div class="table-scroll">
    <table>
      <thead><tr><th>Field</th><th>Read</th><th>Write path</th><th>Evidence</th></tr></thead>
      <tbody>{''.join(body)}</tbody>
    </table>
  </div>
</section>"""


def render_characters(characters: list[dict[str, Any]]) -> str:
    sections = []
    for character in characters:
        slots = "".join(
            render_slot_card(character["character_id"], slot)
            for slot in character["slots"]
        )
        emblem = icon_url(character.get("emblem_path"))
        style = (
            f' style="--emblem:url(&quot;{escape(emblem)}&quot;)"'
            if emblem
            else ""
        )
        sections.append(
            f"""
<section class="character-loadouts"{style}>
  <header>
    <div>
      <p class="eyebrow">Character loadouts</p>
      <h2>{escape(character["class_name"])}</h2>
    </div>
    <dl>
      <div><dt>Entries</dt><dd>{character["slot_count"]}</dd></div>
      <div><dt>Populated</dt><dd>{character["populated_count"]}</dd></div>
      <div><dt>Unresolved</dt><dd>{character["unresolved_count"]}</dd></div>
    </dl>
  </header>
  <div class="slot-grid">{slots}</div>
</section>"""
        )
    return "".join(sections)


def render_slot_card(character_id: str, slot: dict[str, Any]) -> str:
    icon = icon_url(slot.get("icon_path"))
    color = icon_url(slot.get("color_path"))
    art_style = (
        f' style="--slot-color:url(&quot;{escape(color)}&quot;)"'
        if color
        else ""
    )
    icon_html = (
        f'<img src="{escape(icon)}" alt="" loading="lazy">'
        if icon
        else '<span aria-hidden="true">◇</span>'
    )
    preview_icons = []
    for item in slot["items"][:5]:
        item_icon = icon_url(item.get("icon_path"))
        preview_icons.append(
            (
                f'<img src="{escape(item_icon)}" alt="" loading="lazy">'
                if item_icon
                else '<span aria-hidden="true">·</span>'
            )
        )
    state = "complete" if slot["unresolved_count"] == 0 else "warning"
    href = (
        f"/loadouts/{quote(character_id, safe='')}/"
        f"{slot['slot_index']}"
    )
    return f"""
<a class="slot-card {state}" href="{href}"{art_style}>
  <span class="slot-number">{slot["display_index"]}</span>
  <span class="slot-icon">{icon_html}</span>
  <span class="slot-copy">
    <strong>{escape(slot["name"])}</strong>
    <small>API index {slot["slot_index"]} · {len(slot["items"])} items</small>
  </span>
  <span class="item-preview">{''.join(preview_icons)}</span>
  <span class="slot-state">
    {"Resolved" if state == "complete" else f'{slot["unresolved_count"]} unresolved'}
  </span>
</a>"""


def render_slot_summary(slot: dict[str, Any]) -> str:
    identifiers = slot["identifiers"]
    rows = []
    for label, key in (
        ("Name hash", "name"),
        ("Icon hash", "icon"),
        ("Color hash", "color"),
    ):
        value = identifiers.get(key)
        rows.append(
            f"<div><dt>{label}</dt><dd>{value if value is not None else 'Missing'}</dd></div>"
        )
    rows.extend(
        (
            f"<div><dt>Exact items</dt><dd>{len(slot['items'])}</dd></div>",
            f"<div><dt>Unresolved</dt><dd>{slot['unresolved_count']}</dd></div>",
        )
    )
    return "".join(rows)


def render_slot_items(items: list[dict[str, Any]]) -> str:
    if not items:
        return """
<section class="empty-state compact">
  <h2>This slot is empty.</h2>
  <p>No exact item instances were returned for this loadout.</p>
</section>"""
    return "".join(render_loadout_item(item) for item in items)


def render_loadout_item(item: dict[str, Any]) -> str:
    item_icon = icon_url(item.get("icon_path"))
    icon_html = (
        f'<img src="{escape(item_icon)}" alt="" loading="lazy">'
        if item_icon
        else '<span aria-hidden="true">?</span>'
    )
    resolution = "Resolved" if item["resolved"] else "Missing exact instance"
    state = "resolved" if item["resolved"] else "unresolved"
    instance = item.get("instance_id") or "Invalid instance ID"
    plug_rows = "".join(render_plug(plug) for plug in item["plugs"])
    return f"""
<article class="loadout-item {state}">
  <header>
    <span class="item-icon">{icon_html}</span>
    <div>
      <p class="eyebrow">{escape(item["type"])}</p>
      <h2>{escape(item["name"])}</h2>
      <p>{escape(item["location"])} · Instance {escape(instance)}</p>
    </div>
    <span class="resolution {state}">{resolution}</span>
  </header>
  <details>
    <summary>
      Inspect {item["plug_count"]} ordered plug hashes
      <span>{item["resolved_plug_count"]} resolved definitions</span>
    </summary>
    <div class="plug-grid">{plug_rows}</div>
  </details>
</article>"""


def render_plug(plug: dict[str, Any]) -> str:
    plug_icon = icon_url(plug.get("icon_path"))
    icon_html = (
        f'<img src="{escape(plug_icon)}" alt="" loading="lazy">'
        if plug_icon
        else '<span aria-hidden="true">·</span>'
    )
    classes = ["plug-row"]
    if not plug["resolved"]:
        classes.append("unresolved")
    if plug["filtered_from_preview"]:
        classes.append("filtered")
    filtered = (
        '<span class="filtered-label">Bungie preview filter</span>'
        if plug["filtered_from_preview"]
        else ""
    )
    description = (
        f'<p>{escape(plug["description"])}</p>'
        if plug["description"]
        else ""
    )
    plug_hash = (
        str(plug["plug_hash"])
        if plug["plug_hash"] is not None
        else "missing"
    )
    return f"""
<div class="{' '.join(classes)}">
  <span class="plug-icon">{icon_html}</span>
  <div>
    <strong>Socket {plug["socket_index"] + 1} · {escape(plug["name"])}</strong>
    <small>{escape(plug["category"])} · Hash {plug_hash}</small>
    {description}{filtered}
  </div>
</div>"""


def slot_art(slot: dict[str, Any]) -> str:
    icon = icon_url(slot.get("icon_path"))
    color = icon_url(slot.get("color_path"))
    icon_html = (
        f'<img src="{escape(icon)}" alt="">'
        if icon
        else '<span aria-hidden="true">◇</span>'
    )
    style = (
        f' style="--slot-color:url(&quot;{escape(color)}&quot;)"'
        if color
        else ""
    )
    return f'<span class="detail-slot-art"{style}>{icon_html}</span>'


def empty_state() -> str:
    return """
<section class="empty-state">
  <p class="eyebrow">No active loadout snapshot</p>
  <h2>Synchronize your inventory first.</h2>
  <p>The same profile response collects inventory and CharacterLoadouts as one
  coherent, read-only snapshot.</p>
  <a class="button" href="/data/status">Open data synchronization</a>
</section>"""


def notice(message: str, tone: str) -> str:
    return f'<div class="notice {tone}">{escape(message)}</div>'


def message_html(message: str, tone: str) -> str:
    return notice(message, tone) if message else ""


def redirect_saved(
    loadout_id: str,
    *,
    notice_text: str = "",
    error_text: str = "",
) -> web.HTTPSeeOther:
    if not loadout_id:
        raise web.HTTPSeeOther(
            f"/loadouts?{urlencode({'error': error_text or 'The loadout is unavailable.'})}"
        )
    query = urlencode(
        {"notice": notice_text} if notice_text else {"error": error_text}
    )
    raise web.HTTPSeeOther(
        f"/loadouts/saved/{quote(loadout_id, safe='')}?{query}"
    )


def snapshot_age(snapshot: dict[str, Any]) -> str:
    value = snapshot.get("fetched_at")
    if not isinstance(value, str):
        return "Unknown snapshot age"
    try:
        fetched = datetime.fromisoformat(value)
    except ValueError:
        return "Unknown snapshot age"
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=UTC)
    seconds = max(0, int((datetime.now(UTC) - fetched).total_seconds()))
    if seconds < 60:
        return "Updated just now"
    if seconds < 3600:
        return f"Updated {seconds // 60}m ago"
    if seconds < 86400:
        return f"Updated {seconds // 3600}h ago"
    return f"Updated {seconds // 86400}d ago"


def loadout_error_response(display_name: str, message: str) -> web.Response:
    html = render_template(
        "loadouts.html",
        guardian_name=escape(display_name),
        snapshot_label="Unavailable",
        snapshot_tone="stale",
        warnings=notice(message, "error"),
        summary=summary_cards(None),
        capability_matrix="",
        capture_form="",
        saved_library="",
        plan_creator="",
        activity_plans="",
        body=empty_state(),
    )
    return web.Response(
        text=html,
        content_type="text/html",
        status=409,
        headers={"Cache-Control": "no-store"},
    )


def render_template(template_name: str, **values: str) -> str:
    template = Template(
        (TEMPLATE_ROOT / template_name).read_text(encoding="utf-8")
    )
    return template.substitute(values)
