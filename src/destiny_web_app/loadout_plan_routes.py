"""Server-rendered local activity-plan management routes."""

from __future__ import annotations

import asyncio
import json
import logging
from html import escape
from pathlib import Path
from string import Template
from typing import Any, Callable
from urllib.parse import quote, urlencode

from aiohttp import web

from destiny_web_app.app_keys import (
    ACTIVITY_PLAN_SERVICE_KEY,
    AUTH_SESSION_KEY,
)
from destiny_web_app.auth import csrf_input, require_csrf
from destiny_web_app.loadout_plans import ActivityPlanError
from destiny_web_app.manifest import ManifestError


TEMPLATE_ROOT = Path(__file__).with_name("templates")
LOGGER = logging.getLogger(__name__)


async def activity_plan_page(request: web.Request) -> web.Response:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPSeeOther("/")
    workspace = await asyncio.to_thread(
        request.app[ACTIVITY_PLAN_SERVICE_KEY].workspace,
        authenticated.bungie_membership_id,
        request.match_info["plan_id"],
    )
    if workspace is None:
        raise web.HTTPNotFound()
    plan = workspace["plan"]
    html = render_template(
        "loadout_plan.html",
        guardian_name=escape(authenticated.display_name),
        plan_name=escape(plan["name"]),
        activity_name=escape(plan["activity_name"]),
        notices=(
            message(request.query.get("notice", ""), "success")
            + message(request.query.get("error", ""), "error")
        ),
        plan_header=render_plan_header(plan),
        metadata_form=render_metadata_form(request, plan),
        encounter_form=render_encounter_form(request, plan),
        encounters=render_encounters(request, workspace),
        slot_map=render_slot_map(workspace),
        revision_history=render_plan_history(workspace["plan_revisions"]),
        plan_controls=render_plan_controls(request, plan),
    )
    response = web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )
    response.enable_compression()
    return response


async def create_activity_plan(request: web.Request) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before creating a plan.")
    try:
        form = await request.post()
        require_csrf(request, form)
        plan = await asyncio.to_thread(
            request.app[ACTIVITY_PLAN_SERVICE_KEY].create,
            authenticated.bungie_membership_id,
            name=str(form.get("name") or ""),
            description=str(form.get("description") or ""),
            game=str(form.get("game") or "Destiny 2"),
            activity_name=str(form.get("activity_name") or ""),
            activity_version=str(form.get("activity_version") or ""),
        )
    except web.HTTPForbidden as error:
        return redirect_overview(error.text or "The plan form was rejected.")
    except (ActivityPlanError, LookupError, ValueError) as error:
        return redirect_overview(str(error))
    except Exception:
        LOGGER.exception("Unexpected activity-plan creation failure")
        return redirect_overview("The activity plan could not be created.")
    query = urlencode({"notice": "Activity plan created."})
    raise web.HTTPSeeOther(
        f"/loadout-plans/{quote(plan['plan_id'], safe='')}?{query}"
    )


async def update_activity_plan(request: web.Request) -> web.StreamResponse:
    return await plan_action(
        request,
        "/loadout-plans/update",
        lambda service, owner, plan_id, form: service.update_metadata(
            owner,
            plan_id,
            name=str(form.get("name") or ""),
            description=str(form.get("description") or ""),
            game=str(form.get("game") or "Destiny 2"),
            activity_name=str(form.get("activity_name") or ""),
            activity_version=str(form.get("activity_version") or ""),
            change_note=str(form.get("change_note") or ""),
        ),
        "Plan metadata saved as a new revision.",
    )


async def add_plan_encounter(request: web.Request) -> web.StreamResponse:
    return await plan_action(
        request,
        "/loadout-plans/encounters/add",
        lambda service, owner, plan_id, form: service.add_encounter(
            owner,
            plan_id,
            encounter_order=positive_display_index(
                form.get("encounter_order"), "Encounter order"
            ),
            name=str(form.get("name") or ""),
            notes=str(form.get("notes") or ""),
        ),
        "Encounter added in a new plan revision.",
    )


async def remove_plan_encounter(request: web.Request) -> web.StreamResponse:
    return await plan_action(
        request,
        "/loadout-plans/encounters/remove",
        lambda service, owner, plan_id, form: service.remove_encounter(
            owner,
            plan_id,
            str(form.get("encounter_id") or ""),
        ),
        "Encounter and its assignments removed in a new revision.",
    )


async def update_plan_encounter(request: web.Request) -> web.StreamResponse:
    return await plan_action(
        request,
        "/loadout-plans/encounters/update",
        lambda service, owner, plan_id, form: service.update_encounter(
            owner,
            plan_id,
            str(form.get("encounter_id") or ""),
            encounter_order=positive_display_index(
                form.get("encounter_order"), "Encounter order"
            ),
            name=str(form.get("name") or ""),
            notes=str(form.get("notes") or ""),
        ),
        "Encounter updated without changing its stable identity.",
    )


async def add_plan_assignment(request: web.Request) -> web.StreamResponse:
    return await plan_action(
        request,
        "/loadout-plans/assignments/add",
        lambda service, owner, plan_id, form: service.add_assignment(
            owner,
            plan_id,
            encounter_id=str(form.get("encounter_id") or ""),
            assignment_order=positive_display_index(
                form.get("assignment_order"), "Assignment order"
            ),
            loadout_revision_id=str(
                form.get("loadout_revision_id") or ""
            ),
            target_character_id=str(
                form.get("target_character_id") or ""
            ),
            target_slot_index=positive_display_index(
                form.get("target_slot"), "Target slot"
            ),
            notes=str(form.get("notes") or ""),
        ),
        "Exact loadout revision assigned to the selected slot.",
    )


async def remove_plan_assignment(request: web.Request) -> web.StreamResponse:
    return await plan_action(
        request,
        "/loadout-plans/assignments/remove",
        lambda service, owner, plan_id, form: service.remove_assignment(
            owner,
            plan_id,
            str(form.get("assignment_id") or ""),
        ),
        "Assignment removed in a new plan revision.",
    )


async def archive_activity_plan(request: web.Request) -> web.StreamResponse:
    return await plan_action(
        request,
        "/loadout-plans/archive",
        lambda service, owner, plan_id, form: service.set_archived(
            owner,
            plan_id,
            archived=str(form.get("archived") or "") == "1",
        ),
        "Activity plan archive state updated.",
        overview_after=True,
    )


async def delete_activity_plan(request: web.Request) -> web.StreamResponse:
    def delete(service: Any, owner: str, plan_id: str, form: Any) -> None:
        if str(form.get("confirmation") or "") != "DELETE":
            raise ActivityPlanError("Type DELETE to permanently delete the plan.")
        service.delete(owner, plan_id)

    return await plan_action(
        request,
        "/loadout-plans/delete",
        delete,
        "Activity plan permanently deleted. Destiny was not changed.",
        overview_after=True,
    )


async def export_activity_plan(request: web.Request) -> web.Response:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before exporting a plan.")
    bundle = await asyncio.to_thread(
        request.app[ACTIVITY_PLAN_SERVICE_KEY].export_bundle,
        authenticated.bungie_membership_id,
        request.match_info["plan_id"],
    )
    filename = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in bundle["name"]
    ).strip("-") or "activity-plan"
    return web.Response(
        text=json.dumps(bundle, indent=2, sort_keys=True),
        content_type="application/json",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{filename}.destiny-plan.json"',
        },
    )


async def plan_action(
    request: web.Request,
    csrf_path: str,
    operation: Callable[[Any, str, str, Any], Any],
    success: str,
    *,
    overview_after: bool = False,
) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before changing a plan.")
    plan_id = ""
    try:
        form = await request.post()
        require_csrf(request, form)
        plan_id = str(form.get("plan_id") or "")
        await asyncio.to_thread(
            operation,
            request.app[ACTIVITY_PLAN_SERVICE_KEY],
            authenticated.bungie_membership_id,
            plan_id,
            form,
        )
    except web.HTTPForbidden as error:
        failure = error.text or "The plan form was rejected."
    except (ActivityPlanError, LookupError, ManifestError, ValueError) as error:
        failure = str(error)
    except Exception:
        LOGGER.exception("Unexpected activity-plan mutation failure")
        failure = "The activity plan change could not be saved."
    else:
        destination = "/loadouts" if overview_after else plan_url(plan_id)
        raise web.HTTPSeeOther(
            f"{destination}?{urlencode({'notice': success})}"
        )
    destination = plan_url(plan_id) if plan_id else "/loadouts"
    raise web.HTTPSeeOther(
        f"{destination}?{urlencode({'error': failure})}"
    )


def render_plan_header(plan: dict[str, Any]) -> str:
    archive = '<span class="danger-chip">Archived</span>' if plan["archived_at"] else ""
    return f"""
<section class="saved-hero plan-hero">
  <div><p class="eyebrow">{escape(plan['game'])} · Immutable activity plan</p>
    <h1>{escape(plan['name'])}</h1>
    <p class="saved-description">{escape(plan['description'])}</p>{archive}
  </div>
  <dl class="provenance">
    <div><dt>Activity</dt><dd>{escape(plan['activity_name'])}</dd></div>
    <div><dt>Version</dt><dd>{escape(plan['activity_version'] or 'Unspecified')}</dd></div>
    <div><dt>Plan revision</dt><dd>{plan['revision_number']} · immutable</dd></div>
    <div><dt>Encounters</dt><dd>{plan['encounter_count']}</dd></div>
    <div><dt>Assignments</dt><dd>{plan['assignment_count']}</dd></div>
    <div><dt>Characters</dt><dd>{plan['target_character_count']}</dd></div>
  </dl>
</section>"""


def render_metadata_form(request: web.Request, plan: dict[str, Any]) -> str:
    disabled = " disabled" if plan["archived_at"] else ""
    return f"""
<details class="manager-panel"><summary>Edit plan metadata</summary>
  <form class="manager-form" method="post" action="/loadout-plans/update">
    {csrf_input(request, '/loadout-plans/update')}
    <input type="hidden" name="plan_id" value="{escape(plan['plan_id'])}">
    {text_field('Name', 'name', plan['name'], 100, disabled)}
    {text_field('Game', 'game', plan['game'], 80, disabled)}
    {text_field('Activity', 'activity_name', plan['activity_name'], 120, disabled)}
    {text_field('Activity version', 'activity_version', plan['activity_version'], 80, disabled, required=False)}
    {textarea_field('Description', 'description', plan['description'], 3000, disabled)}
    {text_field('Change note', 'change_note', '', 500, disabled, required=False)}
    <button type="submit"{disabled}>Save as plan revision {plan['revision_number'] + 1}</button>
  </form>
</details>"""


def render_encounter_form(request: web.Request, plan: dict[str, Any]) -> str:
    if plan["archived_at"]:
        return ""
    next_order = max(
        (int(row["encounter_order"]) for row in plan["encounters"]),
        default=-1,
    ) + 2
    return f"""
<section class="manager-panel open-panel">
  <div><p class="eyebrow">Plan hierarchy</p><h2>Add encounter group</h2></div>
  <form class="manager-form compact-form" method="post" action="/loadout-plans/encounters/add">
    {csrf_input(request, '/loadout-plans/encounters/add')}
    <input type="hidden" name="plan_id" value="{escape(plan['plan_id'])}">
    <label>Order<input type="number" name="encounter_order" min="1" required value="{next_order}"></label>
    <label>Name<input name="name" maxlength="100" required placeholder="Atraks-1"></label>
    <label>Notes<input name="notes" maxlength="2000" placeholder="Roles or strategy"></label>
    <button type="submit">Add encounter</button>
  </form>
</section>"""


def render_encounters(request: web.Request, workspace: dict[str, Any]) -> str:
    plan = workspace["plan"]
    if not plan["encounters"]:
        return '<section class="saved-empty">Add the first encounter or a General group, then assign pinned loadout revisions to exact character slots.</section>'
    return "".join(
        render_encounter(request, plan, encounter, workspace)
        for encounter in plan["encounters"]
    )


def render_encounter(
    request: web.Request,
    plan: dict[str, Any],
    encounter: dict[str, Any],
    workspace: dict[str, Any],
) -> str:
    assignments = "".join(
        render_assignment(request, plan, assignment)
        for assignment in encounter["assignments"]
    ) or '<p class="section-copy">No slot assignments yet.</p>'
    remove = "" if plan["archived_at"] else f"""
<form method="post" action="/loadout-plans/encounters/remove">
  {csrf_input(request, '/loadout-plans/encounters/remove')}
  <input type="hidden" name="plan_id" value="{escape(plan['plan_id'])}">
  <input type="hidden" name="encounter_id" value="{escape(encounter['encounter_id'])}">
  <button class="text-button danger" type="submit">Remove encounter</button>
</form>"""
    edit = "" if plan["archived_at"] else f"""
<details class="inline-editor"><summary>Edit encounter</summary>
  <form class="manager-form compact-form" method="post" action="/loadout-plans/encounters/update">
    {csrf_input(request, '/loadout-plans/encounters/update')}
    <input type="hidden" name="plan_id" value="{escape(plan['plan_id'])}">
    <input type="hidden" name="encounter_id" value="{escape(encounter['encounter_id'])}">
    <label>Order<input type="number" name="encounter_order" min="1" required value="{encounter['encounter_order'] + 1}"></label>
    <label>Name<input name="name" maxlength="100" required value="{escape(encounter['name'])}"></label>
    <label>Notes<input name="notes" maxlength="2000" value="{escape(encounter['notes'])}"></label>
    <button type="submit">Save as new plan revision</button>
  </form>
</details>"""
    add_form = (
        ""
        if plan["archived_at"]
        else render_assignment_form(request, plan, encounter, workspace)
    )
    return f"""
<section class="encounter-card">
  <header><div><p class="eyebrow">Encounter {encounter['encounter_order'] + 1}</p>
    <h2>{escape(encounter['name'])}</h2><p>{escape(encounter['notes'])}</p></div>{remove}</header>
  {edit}<div class="assignment-list">{assignments}</div>{add_form}
</section>"""


def render_assignment(
    request: web.Request,
    plan: dict[str, Any],
    assignment: dict[str, Any],
) -> str:
    archived = '<span class="danger-chip">Loadout archived</span>' if assignment["loadout_archived"] else ""
    remove = "" if plan["archived_at"] else f"""
<form method="post" action="/loadout-plans/assignments/remove">
  {csrf_input(request, '/loadout-plans/assignments/remove')}
  <input type="hidden" name="plan_id" value="{escape(plan['plan_id'])}">
  <input type="hidden" name="assignment_id" value="{escape(assignment['assignment_id'])}">
  <button class="text-button danger" type="submit">Remove</button>
</form>"""
    return f"""
<article class="assignment-row">
  <strong>{assignment['assignment_order'] + 1}</strong>
  <div><h3>{escape(assignment['loadout_name'])} · revision {assignment['loadout_revision_number']}</h3>
    <p>{escape(assignment['target_character_name'])} · exact slot {assignment['slot_display_index']} · {escape(assignment['notes'])}</p>{archived}</div>
  {remove}
</article>"""


def render_assignment_form(
    request: web.Request,
    plan: dict[str, Any],
    encounter: dict[str, Any],
    workspace: dict[str, Any],
) -> str:
    revision_options = "".join(
        f'<option value="{escape(row["revision_id"])}">'
        f'{escape(row["loadout_name"])} · rev {row["revision_number"]} · '
        f'{escape(row["class_name"])}{" · archived" if row["archived"] else ""}</option>'
        for row in workspace["revision_options"]
    )
    character_options = "".join(
        f'<option value="{escape(row["character_id"])}">'
        f'{escape(row["class_name"])} · {row["slot_count"]} available slots</option>'
        for row in workspace["characters"]
    )
    disabled = "" if revision_options and character_options else " disabled"
    next_order = max(
        (int(row["assignment_order"]) for row in encounter["assignments"]),
        default=-1,
    ) + 2
    return f"""
<details class="assignment-form"><summary>Assign a pinned revision</summary>
  <form class="manager-form compact-form" method="post" action="/loadout-plans/assignments/add">
    {csrf_input(request, '/loadout-plans/assignments/add')}
    <input type="hidden" name="plan_id" value="{escape(plan['plan_id'])}">
    <input type="hidden" name="encounter_id" value="{escape(encounter['encounter_id'])}">
    <label>Order<input type="number" name="assignment_order" min="1" required value="{next_order}"{disabled}></label>
    <label>Saved revision<select name="loadout_revision_id" required{disabled}>{revision_options}</select></label>
    <label>Character<select name="target_character_id" required{disabled}>{character_options}</select></label>
    <label>Exact in-game slot<input type="number" name="target_slot" min="1" max="20" required{disabled}></label>
    <label>Notes<input name="notes" maxlength="1000" placeholder="Role or purpose"{disabled}></label>
    <button type="submit"{disabled}>Add exact slot assignment</button>
  </form>
</details>"""


def render_slot_map(workspace: dict[str, Any]) -> str:
    plan = workspace["plan"]
    assignments = {
        (row["target_character_id"], int(row["target_slot_index"])): row
        for row in plan["assignments"]
    }
    characters = []
    for character in workspace["characters"]:
        slots = []
        for index in range(int(character["slot_count"])):
            assignment = assignments.get((character["character_id"], index))
            if assignment:
                slots.append(
                    f'<div class="mapped"><strong>{index + 1}</strong><span>{escape(assignment["loadout_name"])}</span></div>'
                )
            else:
                slots.append(
                    f'<div class="clear"><strong>{index + 1}</strong><span>Unassigned · explicit clear in sync preview</span></div>'
                )
        characters.append(
            f'<section><h3>{escape(character["class_name"])}</h3><div class="plan-slot-grid">{"".join(slots)}</div></section>'
        )
    return f"""
<section class="slot-map"><div class="section-heading"><div><p class="eyebrow">Complete desired state</p>
  <h2>Exact character slot map</h2></div><span class="read-only-badge">Preview only</span></div>
  <p>Every unassigned available slot is explicit. Goal 7 will treat it as a clear action; this goal makes no Bungie call.</p>
  {''.join(characters) if characters else '<p>No character loadout data is available.</p>'}
</section>"""


def render_plan_history(revisions: list[dict[str, Any]]) -> str:
    rows = "".join(
        f"<tr><td>{row['revision_number']}</td><td>{escape(row['change_note'])}</td>"
        f"<td>{row['encounter_count']}</td><td>{row['assignment_count']}</td>"
        f"<td>{escape(row['created_at'])}</td></tr>"
        for row in revisions
    )
    return f"""
<section class="manager-panel open-panel"><div><p class="eyebrow">Audit history</p><h2>Plan revisions</h2></div>
  <div class="table-scroll"><table><thead><tr><th>Revision</th><th>Change</th><th>Encounters</th><th>Assignments</th><th>Created</th></tr></thead><tbody>{rows}</tbody></table></div>
</section>"""


def render_plan_controls(request: web.Request, plan: dict[str, Any]) -> str:
    archived = bool(plan["archived_at"])
    preview_disabled = " disabled" if archived or not plan["assignments"] else ""
    return f"""
<section class="confirmation-card plan-preview-card"><div><p class="eyebrow">Goals 5–7</p><h2>Preview the complete activity synchronization</h2>
  <p>Refreshes live state and shows every ordered replacement plus an explicit clear for every unassigned available slot. Preview itself makes no Bungie write.</p></div>
  <form method="post" action="/loadout-plans/preview">
    {csrf_input(request, '/loadout-plans/preview')}
    <input type="hidden" name="plan_id" value="{escape(plan['plan_id'])}">
    <button type="submit"{preview_disabled}>Refresh and preview complete plan</button>
  </form>
</section>
<section class="danger-zone"><div><p class="eyebrow">Local plan controls</p><h2>Archive or delete</h2>
  <p>These actions affect only the application database and never Destiny.</p></div>
  <form method="post" action="/loadout-plans/archive">
    {csrf_input(request, '/loadout-plans/archive')}
    <input type="hidden" name="plan_id" value="{escape(plan['plan_id'])}">
    <input type="hidden" name="archived" value="{'0' if archived else '1'}">
    <button type="submit">{'Restore from archive' if archived else 'Archive plan'}</button>
  </form>
  <a class="export-link" href="/loadout-plans/{escape(plan['plan_id'])}/export">Export plan + pinned revisions</a>
  <form method="post" action="/loadout-plans/delete">
    {csrf_input(request, '/loadout-plans/delete')}
    <input type="hidden" name="plan_id" value="{escape(plan['plan_id'])}">
    <label>Type DELETE<input name="confirmation" required autocomplete="off"></label>
    <button class="danger-button" type="submit">Delete local plan permanently</button>
  </form>
</section>"""


def positive_display_index(value: Any, label: str) -> int:
    try:
        parsed = int(str(value))
    except ValueError as error:
        raise ActivityPlanError(f"{label} must be a number.") from error
    if parsed <= 0:
        raise ActivityPlanError(f"{label} must be at least 1.")
    return parsed - 1


def text_field(
    label: str,
    name: str,
    value: str,
    maxlength: int,
    disabled: str,
    *,
    required: bool = True,
) -> str:
    requirement = " required" if required else ""
    return f'<label>{escape(label)}<input name="{escape(name)}" value="{escape(value)}" maxlength="{maxlength}"{requirement}{disabled}></label>'


def textarea_field(
    label: str, name: str, value: str, maxlength: int, disabled: str
) -> str:
    return f'<label class="wide">{escape(label)}<textarea name="{escape(name)}" maxlength="{maxlength}" rows="3"{disabled}>{escape(value)}</textarea></label>'


def message(value: str, tone: str) -> str:
    return f'<div class="notice {tone}">{escape(value)}</div>' if value else ""


def redirect_overview(error: str) -> web.HTTPSeeOther:
    raise web.HTTPSeeOther(f"/loadouts?{urlencode({'error': error})}")


def plan_url(plan_id: str) -> str:
    return f"/loadout-plans/{quote(plan_id, safe='')}"


def render_template(template_name: str, **values: str) -> str:
    return Template(
        (TEMPLATE_ROOT / template_name).read_text(encoding="utf-8")
    ).substitute(values)
