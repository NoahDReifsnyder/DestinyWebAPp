"""Routes for previewing and explicitly confirming live loadout actions."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from string import Template
from urllib.parse import quote, urlencode

from aiohttp import web

from destiny_web_app.app_keys import (
    AUTH_SESSION_KEY,
    LOADOUT_SYNC_SERVICE_KEY,
)
from destiny_web_app.auth import csrf_input, require_csrf
from destiny_web_app.bungie import BungieError
from destiny_web_app.inventory import InventoryDataError
from destiny_web_app.inventory_routes import icon_url
from destiny_web_app.loadout_freshness import ensure_fresh_loadout_snapshot
from destiny_web_app.loadout_sync import (
    LoadoutOperationError,
    LoadoutPreviewError,
)
from destiny_web_app.manifest import ManifestError


LOGGER = logging.getLogger(__name__)
TEMPLATE_ROOT = Path(__file__).with_name("templates")


async def create_single_loadout_preview(request: web.Request) -> web.StreamResponse:
    return await create_preview(request, preview_type="single_slot")


async def create_activity_plan_preview(request: web.Request) -> web.StreamResponse:
    return await create_preview(request, preview_type="activity_plan")


async def create_loadout_set_preview(request: web.Request) -> web.StreamResponse:
    return await create_preview(request, preview_type="loadout_set")


async def create_preview(request: web.Request, *, preview_type: str) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before creating a preview.")
    return_path = "/loadouts"
    try:
        form = await request.post()
        require_csrf(request, form)
        await ensure_fresh_loadout_snapshot(
            request, authenticated, force=True
        )
        service = request.app[LOADOUT_SYNC_SERVICE_KEY]
        if preview_type == "single_slot":
            loadout_id = str(form.get("loadout_id") or "")
            return_path = f"/loadouts/saved/{quote(loadout_id, safe='')}"
            preview = await asyncio.to_thread(
                service.create_single_preview,
                authenticated.bungie_membership_id,
                loadout_id=loadout_id,
                revision_id=str(form.get("revision_id") or ""),
                target_character_id=str(form.get("target_character_id") or ""),
                target_slot_index=display_slot(form.get("target_slot")),
            )
        elif preview_type == "activity_plan":
            plan_id = str(form.get("plan_id") or "")
            return_path = f"/loadout-plans/{quote(plan_id, safe='')}"
            preview = await asyncio.to_thread(
                service.create_plan_preview,
                authenticated.bungie_membership_id,
                plan_id=plan_id,
            )
        else:
            set_id = str(form.get("set_id") or "")
            return_path = f"/loadout-sets/{quote(set_id, safe='')}"
            preview = await asyncio.to_thread(
                service.create_set_preview,
                authenticated.bungie_membership_id,
                set_id=set_id,
            )
    except web.HTTPForbidden as error:
        failure = error.text or "The preview form was rejected."
    except (
        BungieError,
        InventoryDataError,
        LoadoutOperationError,
        LoadoutPreviewError,
        ManifestError,
        LookupError,
        ValueError,
    ) as error:
        failure = str(error)
    except Exception:
        LOGGER.exception("Unexpected loadout preview failure")
        failure = "The live-state preview could not be created."
    else:
        raise web.HTTPSeeOther(f"/loadout-previews/{preview['preview_id']}")
    raise web.HTTPSeeOther(f"{return_path}?{urlencode({'error': failure})}")


async def loadout_preview_page(request: web.Request) -> web.Response:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPSeeOther("/")
    preview = await asyncio.to_thread(
        request.app[LOADOUT_SYNC_SERVICE_KEY].preview,
        authenticated.bungie_membership_id,
        request.match_info["preview_id"],
    )
    if preview is None:
        raise web.HTTPNotFound()
    html = render_template(
        "loadout_preview.html",
        guardian_name=escape(authenticated.display_name),
        title=escape(preview["action_plan"]["title"]),
        notices=(
            message(request.query.get("notice", ""), "success")
            + message(request.query.get("error", ""), "error")
        ),
        status=render_preview_status(preview),
        summary=render_preview_summary(preview),
        blockers=render_validation(preview),
        jobs=render_slot_jobs(preview["action_plan"]["slot_jobs"]),
        confirmation=render_confirmation(request, preview),
    )
    response = web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )
    response.enable_compression()
    return response


async def confirm_loadout_preview(request: web.Request) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before confirming an operation.")
    preview_id = request.match_info["preview_id"]
    try:
        form = await request.post()
        require_csrf(request, form)
        if str(form.get("confirmation") or "") != "confirmed":
            raise LoadoutOperationError(
                "Check the explicit confirmation before starting live writes."
            )
        backup_choice = str(form.get("backup_choice") or "")
        await ensure_fresh_loadout_snapshot(
            request, authenticated, force=False
        )
        service = request.app[LOADOUT_SYNC_SERVICE_KEY]
        try:
            preview = await asyncio.to_thread(
                service.validate_preview_state,
                authenticated.bungie_membership_id,
                preview_id,
            )
        except LoadoutPreviewError:
            preview = await asyncio.to_thread(
                service.rebuild_preview,
                authenticated.bungie_membership_id,
                preview_id,
            )
            preview_id = preview["preview_id"]
            if not preview["confirmable"]:
                blockers = preview["validation"].get("blockers", [])
                detail = (
                    str(blockers[0])
                    if blockers
                    else "The refreshed preview is not safe to confirm."
                )
                raise LoadoutPreviewError(
                    "Live state was refreshed, but the updated safety "
                    f"preview is blocked: {detail}"
                )
        if preview["action_plan"].get("automatic_backup") is False:
            backup_choice = "skip"
        backup = None
        if backup_choice == "import":
            backup = await asyncio.to_thread(
                service.import_current_slot_backup,
                authenticated.bungie_membership_id,
                character_id=preview["target_character_id"],
                label=f"Pre-sync backup {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
            )
        await service.cancel_active_for_character(
            authenticated.bungie_membership_id,
            preview["target_character_id"],
        )
        operation = await asyncio.to_thread(
            service.create_operation,
            authenticated.bungie_membership_id,
            preview_id,
            backup_choice=backup_choice,
            backup=backup,
        )
        service.start(
            authenticated.bungie_membership_id,
            operation["operation_id"],
            authenticated.token.access_token,
        )
    except web.HTTPForbidden as error:
        failure = error.text or "The confirmation form was rejected."
    except (
        BungieError,
        InventoryDataError,
        LoadoutOperationError,
        LoadoutPreviewError,
        ManifestError,
        LookupError,
        ValueError,
    ) as error:
        failure = str(error)
    except Exception:
        LOGGER.exception("Unexpected loadout confirmation failure")
        failure = "The confirmed operation could not be started."
    else:
        raise web.HTTPSeeOther(
            f"/loadout-operations/{operation['operation_id']}"
        )
    raise web.HTTPSeeOther(
        f"/loadout-previews/{preview_id}?{urlencode({'error': failure})}"
    )


async def loadout_operation_page(request: web.Request) -> web.Response:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPSeeOther("/")
    operation = await asyncio.to_thread(
        request.app[LOADOUT_SYNC_SERVICE_KEY].operation,
        authenticated.bungie_membership_id,
        request.match_info["operation_id"],
    )
    if operation is None:
        raise web.HTTPNotFound()
    html = render_template(
        "loadout_operation.html",
        guardian_name=escape(authenticated.display_name),
        operation_id=escape(operation["operation_id"]),
        title=escape(operation["action_plan"]["title"]),
        notices=(
            message(request.query.get("notice", ""), "success")
            + message(request.query.get("error", ""), "error")
        ),
        operation=render_operation(operation),
        resume=render_resume(request, operation),
    )
    response = web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )
    response.enable_compression()
    return response


async def loadout_operation_status(request: web.Request) -> web.Response:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before viewing operation progress.")
    operation = await asyncio.to_thread(
        request.app[LOADOUT_SYNC_SERVICE_KEY].operation,
        authenticated.bungie_membership_id,
        request.match_info["operation_id"],
    )
    if operation is None:
        raise web.HTTPNotFound()
    current = next(
        (
            action for action in operation["actions"]
            if action["status"] in {"pending", "running", "failed"}
        ),
        None,
    )
    return web.json_response(
        {
            "operation_id": operation["operation_id"],
            "status": operation["status"],
            "completed": operation["completed_actions"],
            "total": operation["total_actions"],
            "progress_percent": operation["progress_percent"],
            "elapsed_seconds": operation["elapsed_seconds"],
            "estimated_remaining_seconds": operation[
                "estimated_remaining_seconds"
            ],
            "eta_confidence": operation["eta_confidence"],
            "phase": current["phase"] if current else "Complete",
            "action_number": (
                int(current["action_index"]) + 1 if current else None
            ),
            "attempts": current["attempts"] if current else 0,
            "last_error": (
                operation.get("last_error")
                or (current or {}).get("last_message")
            ),
        },
        headers={"Cache-Control": "no-store"},
    )


async def resume_loadout_operation(request: web.Request) -> web.StreamResponse:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(text="Sign in before resuming an operation.")
    operation_id = request.match_info["operation_id"]
    try:
        require_csrf(request, await request.post())
        request.app[LOADOUT_SYNC_SERVICE_KEY].start(
            authenticated.bungie_membership_id,
            operation_id,
            authenticated.token.access_token,
        )
    except web.HTTPForbidden as error:
        failure = error.text or "The resume form was rejected."
    except (LoadoutOperationError, BungieError, ValueError) as error:
        failure = str(error)
    except Exception:
        LOGGER.exception("Unexpected loadout resume failure")
        failure = "The operation could not be resumed."
    else:
        raise web.HTTPSeeOther(
            f"/loadout-operations/{operation_id}?{urlencode({'notice': 'Resume started from the durable checkpoint.'})}"
        )
    raise web.HTTPSeeOther(
        f"/loadout-operations/{operation_id}?{urlencode({'error': failure})}"
    )


def render_preview_status(preview: dict) -> str:
    tone = "success" if preview["status"] == "ready" else "error"
    copy = {
        "ready": "Ready for explicit confirmation",
        "blocked": "Blocked — no live action can start",
        "invalidated": "Invalidated by a state change",
        "expired": "Expired — create a fresh preview",
        "confirmed": "Already confirmed",
    }.get(preview["status"], preview["status"])
    return f'<div class="preview-state {tone}"><strong>{escape(copy)}</strong><span>Expires {escape(preview["expires_at"])}</span></div>'


def render_preview_summary(preview: dict) -> str:
    plan = preview["action_plan"]
    speed = plan.get("speed_summary", {})
    unchanged = sum(bool(job.get("already_correct")) for job in plan["slot_jobs"])
    replacements = sum(
        job["kind"] == "replace" and not job.get("already_correct")
        for job in plan["slot_jobs"]
    )
    clears = sum(
        job["kind"] == "clear" and not job.get("already_correct")
        for job in plan["slot_jobs"]
    )
    timing_copy = (
        f"Estimated runtime: about {escape(format_duration(speed['estimated_seconds']))} "
        f"across {int(speed['durable_checkpoints'])} durable checkpoints. "
        f"Transfer work: {int(speed['transfer_items'])} items in approximately "
        f"{int(speed['transfer_waves'])} full waves; loadout preparation: "
        f"{int(speed['unique_preparations'])} unique states; slot clears: "
        f"{int(speed['clear_slots'])}. This estimate adapts during execution."
        if speed
        else "Create a new preview to see the wave-based runtime estimate."
    )
    return f"""
<section class="summary preview-summary">
  <div><strong>{replacements}</strong><span>Slots replaced</span></div>
  <div><strong>{clears}</strong><span>Explicit clears</span></div>
  <div><strong>{unchanged}</strong><span>Already correct</span></div>
  <div><strong>{plan['write_request_count']} + {plan['read_request_count']}</strong><span>Writes + verification reads</span></div>
</section>
<div class="notice warning">{escape(plan['eligibility'])} {timing_copy}</div>"""


def render_validation(preview: dict) -> str:
    blockers = preview["validation"]["blockers"]
    warnings = preview["validation"]["warnings"]
    rows = "".join(f"<li>{escape(value)}</li>" for value in blockers)
    warning_rows = "".join(f"<li>{escape(value)}</li>" for value in warnings)
    return f"""
<section class="preview-validation {'blocked' if blockers else 'ready'}">
  <div><p class="eyebrow">Safety decision</p><h2>{'Resolve these blockers' if blockers else 'All preconditions passed'}</h2></div>
  {'<ul>' + rows + '</ul>' if rows else '<p>No missing items, unsafe transfers, capacity conflicts, Exotic conflicts, stale data, or unverified socket changes were found.</p>'}
  <ul class="preview-warnings">{warning_rows}</ul>
</section>"""


def render_slot_jobs(jobs: list[dict]) -> str:
    return "".join(render_slot_job(job) for job in jobs)


def render_slot_job(job: dict) -> str:
    encounter = job.get("encounter")
    prefix = f"{escape(encounter['name'])} · " if encounter else ""
    if job["kind"] == "clear":
        current = "populated" if job["current_slot"]["populated"] else "already empty"
        return f"""
<article class="preview-job clear-job">
  <header><span class="slot-pill">Slot {job['display_index']}</span><div><small>{prefix}Explicit unassigned action</small><h3>Clear this slot</h3></div><strong>{escape(current)}</strong></header>
  <p>No assignment exists for this available slot. Confirmation explicitly authorizes clearing it.</p>
</article>"""
    if job.get("already_correct"):
        return f"""
<article class="preview-job replace-job">
  <header><span class="slot-pill">Slot {job['display_index']}</span><div><small>{prefix}Pinned revision {job['loadout']['revision_number']}</small><h3>{escape(job['loadout']['name'])}</h3></div><strong>Already correct</strong></header>
  <p>The saved items, supported sockets, and slot identifiers already match. No preparation or snapshot is planned for this slot.</p>
</article>"""
    items = "".join(render_preview_item(item) for item in job["classifications"])
    transfers = "".join(
        f"<li>{escape(row['direction'].replace('_', ' '))}: …{escape(row['item_instance_id'][-8:])}</li>"
        for row in job["transfers"]
    ) or "<li>No transfer required.</li>"
    socket_steps = "".join(
        f"<li>Apply and verify free {escape(row.get('socket_kind', 'gameplay plug'))}: "
        f"{escape(row['item_name'])} socket {int(row['socket_index']) + 1}.</li>"
        for row in job.get("socket_changes", [])
    )
    return f"""
<article class="preview-job replace-job">
  <header><span class="slot-pill">Slot {job['display_index']}</span><div><small>{prefix}Pinned revision {job['loadout']['revision_number']}</small><h3>{escape(job['loadout']['name'])}</h3></div><strong>{job['current_slot']['item_count']} items overwritten</strong></header>
  <div class="preview-item-grid">{items}</div>
  <details><summary>Transfer and preparation sequence</summary><ol>{transfers}<li>Replace and verify currently equipped Exotic slots.</li>{socket_steps}<li>Equip and verify all ten exact items.</li><li>Snapshot this exact slot and verify it from CharacterLoadouts.</li></ol></details>
</article>"""


def render_preview_item(item: dict) -> str:
    image = f'<img src="{escape(url)}" alt="">' if (url := icon_url(item.get("icon_path"))) else "◇"
    return (
        f'<div class="preview-item">{image}<span><strong>'
        f'{escape(item["name"])}</strong><small>'
        f'{escape(item["bucket_name"])} · '
        f'{escape(item["location"].replace("_", " "))}</small>'
        f'<code>…{escape(item["instance_id"][-8:])}</code></span></div>'
    )


def render_confirmation(request: web.Request, preview: dict) -> str:
    if not preview["confirmable"]:
        return '<div class="notice error">This preview cannot be confirmed. Return to the source, refresh live data, and create a new preview.</div>'
    action = f"/loadout-previews/{preview['preview_id']}/confirm"
    if preview["action_plan"].get("automatic_backup") is False:
        backup_control = """
    <input type="hidden" name="backup_choice" value="skip">
    <div class="notice warning">This set will be applied without creating a local backup. Empty board positions will clear their matching Destiny slots. Carried weapon and armor items not used by the set will be moved to the vault, and the highest occupied set position will remain equipped. A full vault may require moving vault items into spare inventory on another character.</div>"""
        heading = "Confirm the exact 20-slot board."
    else:
        backup_control = """
    <fieldset><legend>Import current in-game slots as local backup?</legend>
      <label><input type="radio" name="backup_choice" value="import" required> Yes, import every populated slot first</label>
      <label><input type="radio" name="backup_choice" value="skip" required> No, continue without a local slot backup</label>
    </fieldset>"""
        heading = "Choose backup behavior, then confirm."
    return f"""
<section class="confirmation-card">
  <div><p class="eyebrow">Live Destiny mutation</p><h2>{heading}</h2><p>This is the first control on this page that can write to Destiny. The operation is tied to this five-minute preview and exact target slots.</p></div>
  <form method="post" action="{action}">
    {csrf_input(request, action)}
    {backup_control}
    <label class="confirm-check"><input type="checkbox" name="confirmation" value="confirmed" required> I reviewed every replacement and clear action and authorize these exact live changes.</label>
    <button type="submit">Start confirmed synchronization</button>
  </form>
</section>"""


def render_operation(operation: dict) -> str:
    current = next((row for row in operation["actions"] if row["status"] in {"pending", "running", "failed"}), None)
    phase = current["phase"] if current else "All durable actions complete"
    actions = render_actions(operation["actions"])
    recovery = ""
    if operation.get("recovery"):
        recovery = f"""
<section class="recovery-card"><p class="eyebrow">Recovery plan</p><h2>{escape(operation['recovery'].get('failed_phase', 'Operation stopped'))}</h2><p>{escape(operation['recovery'].get('reason', ''))}</p><p>{escape(operation['recovery'].get('next_step', ''))}</p></section>"""
    report = render_slot_report(operation)
    elapsed = format_duration(operation["elapsed_seconds"])
    remaining = operation["estimated_remaining_seconds"]
    eta = (
        f"About {format_duration(remaining)} remaining"
        if remaining is not None and operation["status"] not in {"completed"}
        else "Complete"
        if operation["status"] == "completed"
        else "ETA unavailable while stopped"
    )
    return f"""
<section class="operation-progress" data-operation-id="{escape(operation['operation_id'])}" data-operation-status="{escape(operation['status'])}">
  <div class="operation-heading"><div><p class="eyebrow">{escape(operation['status'])}</p><h2 data-progress-phase>{escape(phase)}</h2></div><strong data-progress-count>{operation['completed_actions']} / {operation['total_actions']}</strong></div>
  <div class="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{operation['progress_percent']}"><span data-progress-bar style="width:{operation['progress_percent']}%"></span></div>
  <p data-progress-copy>{operation['progress_percent']}% complete · every completed checkpoint is durable.</p>
  <div class="progress-timing"><span data-progress-elapsed>Elapsed: {escape(elapsed)}</span><span data-progress-eta>{escape(eta)}</span></div>
</section>
{recovery}{report}
<section class="operation-actions"><div><p class="eyebrow">Durable audit</p><h2>Action checkpoints</h2></div>{actions}</section>"""


def format_duration(seconds: int | float) -> str:
    total = max(0, round(float(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def render_actions(actions: list[dict]) -> str:
    """Collapse long consecutive transfer runs without hiding their evidence."""

    groups: list[list[dict]] = []
    for row in actions:
        if (
            groups
            and row["action_type"] == groups[-1][0]["action_type"]
            and row["phase"] == groups[-1][0]["phase"]
            and row.get("target_slot_index") is None
        ):
            groups[-1].append(row)
        else:
            groups.append([row])
    rendered = []
    for group in groups:
        if len(group) < 4:
            rendered.extend(render_action(row) for row in group)
            continue
        completed = sum(
            row["status"] in {"completed", "skipped"} for row in group
        )
        failed = sum(row["status"] == "failed" for row in group)
        status = "failed" if failed else "completed" if completed == len(group) else "pending"
        start = int(group[0]["action_index"]) + 1
        end = int(group[-1]["action_index"]) + 1
        rendered.append(
            f"""
<details class="operation-action-group status-{status}">
  <summary><span>{start}–{end}</span><strong>{escape(group[0]['phase'])}</strong><em>{completed} / {len(group)} complete</em></summary>
  <div><p>{len(group)} item-level API actions are grouped here. Expand for their durable evidence.</p>{''.join(render_action(row) for row in group)}</div>
</details>"""
        )
    return "".join(rendered)


def render_action(action: dict) -> str:
    attempts = "".join(
        f"<li>Attempt {row['attempt_number']}: {escape(row['status'])}{' · ' + escape(row['message']) if row['message'] else ''}</li>"
        for row in action["attempt_history"]
    )
    slot = f" · slot {action['target_slot_index'] + 1}" if action.get("target_slot_index") is not None else ""
    return f"""
<details class="operation-action status-{escape(action['status'])}" {'open' if action['status'] == 'failed' else ''}>
  <summary><span>{action['action_index'] + 1}</span><strong>{escape(action['phase'])}{slot}</strong><em>{escape(action['status'])}</em></summary>
  <div><p>Type: {escape(action['action_type'].replace('_', ' '))} · Attempts: {action['attempts']} · Throttle: {action['throttle_seconds']}s</p>{'<ol>' + attempts + '</ol>' if attempts else '<p>No request attempted yet.</p>'}</div>
</details>"""


def render_slot_report(operation: dict) -> str:
    if operation["status"] not in {"completed", "failed", "paused"}:
        return ""
    groups: dict[str, list[str]] = {}
    for job in operation["action_plan"]["slot_jobs"]:
        slot_actions = [
            row for row in operation["actions"]
            if row.get("target_slot_index") == job["slot_index"]
        ]
        if any(row["status"] == "failed" for row in slot_actions):
            failed = next(row for row in slot_actions if row["status"] == "failed")
            status = (
                "divergent"
                if failed["action_type"].startswith("verify_")
                else "failed"
            )
        elif slot_actions and all(
            row["status"] in {"completed", "skipped"} for row in slot_actions
        ):
            status = "succeeded"
        else:
            status = "skipped"
        encounter = (job.get("encounter") or {}).get(
            "name", "Unassigned slot clears"
        )
        groups.setdefault(encounter, []).append(
            f'<li class="report-{status}"><span>Slot {job["slot_index"] + 1}</span>'
            f'<strong>{escape(job["label"])}</strong><em>{status}</em></li>'
        )
    grouped = "".join(
        f'<section><h3>{escape(name)}</h3><ul>{"".join(rows)}</ul></section>'
        for name, rows in groups.items()
    )
    restored = (
        operation.get("result", {}).get("original_equipment")
        or (
            "verified"
            if any(
                row["action_type"] == "verify_restored"
                and row["status"] == "completed"
                for row in operation["actions"]
            )
            else "not yet verified"
        )
    )
    return f"""
<section class="slot-report"><div><p class="eyebrow">Activity → encounter → character → slot</p>
  <h2>{escape(operation['action_plan'].get('activity_name') or operation['action_plan']['title'])}</h2>
  <p>{escape(operation['action_plan']['target_character_name'])} · Original equipment {escape(restored)}</p></div>
  {grouped}
</section>"""


def render_resume(request: web.Request, operation: dict) -> str:
    if not operation["can_resume"]:
        return ""
    action = f"/loadout-operations/{operation['operation_id']}/resume"
    return f"""
<form class="resume-form" method="post" action="{action}">
  {csrf_input(request, action)}
  <p>Resume performs a fresh live comparison and continues from the first incomplete checkpoint. Completed slots are not replayed.</p>
  <button type="submit">Resume from durable checkpoint</button>
</form>"""


def display_slot(value: object) -> int:
    try:
        slot = int(str(value)) - 1
    except ValueError as error:
        raise LoadoutPreviewError("Choose a target slot.") from error
    if slot < 0:
        raise LoadoutPreviewError("Choose a positive target slot.")
    return slot


def message(value: str, tone: str) -> str:
    return f'<div class="notice {tone}">{escape(value)}</div>' if value else ""


def render_template(name: str, **values: str) -> str:
    return Template((TEMPLATE_ROOT / name).read_text(encoding="utf-8")).safe_substitute(values)
