"""Read-only previews and durable, idempotent Destiny loadout operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Iterable

from destiny_web_app.bungie import (
    BungieActionError,
    BungieAuthenticationRejected,
    BungieClient,
    BungieError,
)
from destiny_web_app.database import as_iso, utc_now
from destiny_web_app.database import GENERAL_VAULT_BUCKET_HASH
from destiny_web_app.inventory import InventoryService, POSTMASTER_BUCKET_HASH
from destiny_web_app.loadout_manager import (
    CLASS_NAMES,
    GAMEPLAY_BUCKET_NAMES,
    INVALID_HASH_SENTINEL,
    REQUIRED_GAMEPLAY_BUCKET_ORDER,
    REQUIRED_GAMEPLAY_BUCKETS,
    LoadoutInspectionError,
    LoadoutManagerService,
    component_items,
    component_plug_hashes,
    intended_bucket_hash,
    snapshot_is_fresh,
    valid_hash,
    valid_instance_id,
)
from destiny_web_app.loadout_plans import ActivityPlanService
from destiny_web_app.manifest import ManifestService

if TYPE_CHECKING:
    from destiny_web_app.loadouts.storage import LoadoutStore


PREVIEW_LIFETIME = timedelta(minutes=5)
MAX_ACTION_ATTEMPTS = 3
RETRY_DELAYS = (1.0, 2.0)
POST_WRITE_REFRESH_DELAYS = (1.0, 2.0, 4.0, 8.0, 12.0, 18.0)
PREPARATION_VERIFY_DELAYS = (1.0, 2.0, 4.0)
TRANSFER_INTERVAL = 0.1
EQUIP_INTERVAL = 0.1
SOCKET_INTERVAL = 0.5
LOADOUT_INTERVAL = 1.0
# Bungie's bucket definition reports ten total slots: one equipped item plus
# nine carried items.
CHARACTER_BUCKET_CAPACITY = 9
ARMOR_MOD_SOCKET_CATEGORY_HASH = 590099826
SUBCLASS_BUCKET_HASH = 3284755031
SOCIAL_ACTIVITY_MODE_TYPE = 40
# The current official manifest resolves this place as "Orbit". Keep the
# manifest-name fallback below so a future orbit place hash can still work.
KNOWN_ORBIT_PLACE_HASHES = {2961497387}
FALLBACK_VAULT_CAPACITY = 1300
LOGGER = logging.getLogger(__name__)


class LoadoutPreviewError(ValueError):
    """A complete and safe live-state preview could not be created."""


class LoadoutOperationError(RuntimeError):
    """A confirmed live operation could not safely continue."""


class LoadoutSyncService:
    def __init__(
        self,
        database: LoadoutStore,
        bungie: BungieClient,
        inventory: InventoryService,
        manifest: ManifestService,
        loadouts: LoadoutManagerService,
        plans: ActivityPlanService,
    ) -> None:
        self.database = database
        self.bungie = bungie
        self.inventory = inventory
        self.manifest = manifest
        self.loadouts = loadouts
        self.plans = plans
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._recover_interrupted()

    def create_single_preview(
        self,
        owner: str,
        *,
        loadout_id: str,
        revision_id: str,
        target_character_id: str,
        target_slot_index: int,
    ) -> dict[str, Any]:
        source = self._source(owner)
        loadout = self.loadouts.saved_loadout(
            owner,
            loadout_id,
            revision_id=revision_id,
            include_archived=True,
        )
        if loadout is None:
            raise LoadoutPreviewError("The selected loadout revision is unavailable.")
        blockers = []
        if loadout.get("archived_at"):
            blockers.append("The selected loadout is archived.")
        character, slots = self._target_character(
            source, target_character_id, int(loadout["character_class_type"])
        )
        if target_slot_index < 0 or target_slot_index >= len(slots):
            raise LoadoutPreviewError("The selected in-game slot is unavailable.")
        slot_job, item_blockers = self._replacement_job(
            source,
            character,
            loadout,
            target_slot_index,
            current_slot=slots[target_slot_index],
            encounter=None,
            assignment=None,
        )
        blockers.extend(item_blockers)
        blockers.extend(
            self._aggregate_plan_capacity_blockers(
                source, target_character_id, [slot_job]
            )
        )
        blockers.extend(self._activity_blockers(source, target_character_id))
        original = self._original_equipment(source, target_character_id)
        if original["issues"]:
            blockers.extend(original["issues"])
        plan = self._finalize_action_plan(
            source,
            character,
            [slot_job],
            original,
            title=f"{loadout['name']} → slot {target_slot_index + 1}",
            activity_name=None,
        )
        return self._persist_preview(
            owner,
            preview_type="single_slot",
            source_entity_id=loadout_id,
            source_revision_id=revision_id,
            target_character_id=target_character_id,
            target_slot_index=target_slot_index,
            source=source,
            action_plan=plan,
            blockers=blockers,
        )

    def create_plan_preview(
        self,
        owner: str,
        *,
        plan_id: str,
    ) -> dict[str, Any]:
        source = self._source(owner)
        plan = self.database.load_loadout_plan(
            owner, plan_id, include_archived=True
        )
        if plan is None:
            raise LoadoutPreviewError("The activity plan is unavailable.")
        blockers: list[str] = []
        if plan.get("archived_at"):
            blockers.append("The activity plan is archived.")
        assignments = [
            assignment
            for encounter in plan["encounters"]
            for assignment in encounter["assignments"]
        ]
        if not assignments:
            blockers.append("Add at least one slot assignment before previewing.")
            target_character_id = ""
        else:
            character_ids = {
                str(assignment["target_character_id"])
                for assignment in assignments
            }
            if len(character_ids) != 1:
                blockers.append(
                    "An activity synchronization must target one character at a time."
                )
            target_character_id = sorted(character_ids)[0]
        if not target_character_id:
            raise LoadoutPreviewError("The plan has no target character.")
        first_class = int(assignments[0]["character_class_type"])
        character, slots = self._target_character(
            source, target_character_id, first_class
        )
        seen_slots: set[int] = set()
        slot_jobs = []
        for encounter in plan["encounters"]:
            for assignment in encounter["assignments"]:
                slot_index = int(assignment["target_slot_index"])
                if assignment["target_character_id"] != target_character_id:
                    continue
                if slot_index in seen_slots:
                    blockers.append(
                        f"Slot {slot_index + 1} is assigned more than once."
                    )
                    continue
                seen_slots.add(slot_index)
                if slot_index < 0 or slot_index >= len(slots):
                    blockers.append(
                        f"Slot {slot_index + 1} is unavailable on this character."
                    )
                    continue
                loadout = self.loadouts.saved_loadout(
                    owner,
                    assignment["loadout_id"],
                    revision_id=assignment["loadout_revision_id"],
                    include_archived=True,
                )
                if loadout is None:
                    blockers.append(
                        f"The revision assigned to slot {slot_index + 1} is unavailable."
                    )
                    continue
                if loadout.get("archived_at"):
                    blockers.append(
                        f"{loadout['name']} is archived but assigned to slot "
                        f"{slot_index + 1}."
                    )
                if int(loadout["character_class_type"]) != first_class:
                    blockers.append(
                        f"{loadout['name']} has the wrong character class."
                    )
                    continue
                job, job_blockers = self._replacement_job(
                    source,
                    character,
                    loadout,
                    slot_index,
                    current_slot=slots[slot_index],
                    encounter=encounter,
                    assignment=assignment,
                )
                slot_jobs.append(job)
                blockers.extend(job_blockers)
        for slot_index in range(len(slots)):
            if slot_index in seen_slots:
                continue
            slot_jobs.append(
                {
                    "kind": "clear",
                    "slot_index": slot_index,
                    "display_index": slot_index + 1,
                    "encounter": None,
                    "assignment": None,
                    "current_slot": summarize_slot(slots[slot_index]),
                    "label": f"Clear unassigned slot {slot_index + 1}",
                    "write_request_count": 0 if slot_empty(slots[slot_index]) else 1,
                }
            )
        blockers.extend(
            self._aggregate_plan_capacity_blockers(
                source, target_character_id, slot_jobs
            )
        )
        blockers.extend(self._activity_blockers(source, target_character_id))
        original = self._original_equipment(source, target_character_id)
        blockers.extend(original["issues"])
        finalized = self._finalize_action_plan(
            source,
            character,
            slot_jobs,
            original,
            title=plan["name"],
            activity_name=plan["activity_name"],
        )
        return self._persist_preview(
            owner,
            preview_type="activity_plan",
            source_entity_id=plan_id,
            source_revision_id=plan["plan_revision_id"],
            target_character_id=target_character_id,
            target_slot_index=None,
            source=source,
            action_plan=finalized,
            blockers=dedupe(blockers),
        )

    def create_set_preview(
        self,
        owner: str,
        *,
        set_id: str,
    ) -> dict[str, Any]:
        """Preview a class-bound 20-position board using current loadout revisions."""

        source = self._source(owner)
        board = self.database.load_loadout_set(owner, set_id)
        if board is None:
            raise LoadoutPreviewError("The loadout set is unavailable.")
        if not board["slots"]:
            raise LoadoutPreviewError("An empty loadout set cannot be applied.")
        class_type = int(board["character_class_type"])
        characters = [
            row
            for row in source["characters"]
            if int(row.get("class_type", -1)) == class_type
        ]
        if len(characters) != 1:
            raise LoadoutPreviewError(
                "The live profile must contain exactly one character of this set's class."
            )
        character = characters[0]
        target_character_id = str(character["character_id"])
        character, live_slots = self._target_character(
            source, target_character_id, class_type
        )
        if len(live_slots) != 20:
            raise LoadoutPreviewError(
                f"This set requires 20 live slots, but {len(live_slots)} are available."
            )
        assigned = {int(row["position"]): row for row in board["slots"]}
        blockers: list[str] = []
        slot_jobs: list[dict[str, Any]] = []
        for position in range(20):
            slot_row = assigned.get(position)
            if slot_row is None:
                slot_jobs.append(
                    {
                        "kind": "clear",
                        "slot_index": position,
                        "display_index": position + 1,
                        "encounter": None,
                        "assignment": None,
                        "current_slot": summarize_slot(live_slots[position]),
                        "label": f"Clear empty board position {position + 1}",
                        "write_request_count": (
                            0 if slot_empty(live_slots[position]) else 1
                        ),
                    }
                )
                continue
            loadout = self.loadouts.saved_loadout(
                owner,
                str(slot_row["loadout_id"]),
                include_archived=True,
            )
            if loadout is None:
                blockers.append(
                    f"The loadout in position {position + 1} is unavailable."
                )
                continue
            job, job_blockers = self._replacement_job(
                source,
                character,
                loadout,
                position,
                current_slot=live_slots[position],
                encounter=None,
                assignment=None,
            )
            slot_jobs.append(job)
            blockers.extend(job_blockers)
        blockers.extend(
            self._aggregate_plan_capacity_blockers(
                source,
                target_character_id,
                slot_jobs,
                retain_only_desired=True,
            )
        )
        blockers.extend(self._activity_blockers(source, target_character_id))
        original = self._original_equipment(source, target_character_id)
        action_plan = self._finalize_action_plan(
            source,
            character,
            slot_jobs,
            original,
            title=str(board["name"]),
            activity_name=None,
        )
        action_plan["set_version"] = int(board["version"])
        action_plan["clears_empty_positions"] = True
        action_plan["automatic_backup"] = False
        return self._persist_preview(
            owner,
            # Reuse the durable multi-slot operation category so historical
            # operation evidence and its schema remain compatible.
            preview_type="activity_plan",
            source_entity_id=set_id,
            source_revision_id=str(board["version"]),
            target_character_id=target_character_id,
            target_slot_index=None,
            source=source,
            action_plan=action_plan,
            blockers=dedupe(blockers),
        )

    def preview(self, owner: str, preview_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM loadout_previews
                WHERE bungie_membership_id = ? AND preview_id = ?
                """,
                (owner, preview_id),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["action_plan"] = json.loads(result.pop("action_plan_json"))
        result["validation"] = json.loads(result.pop("validation_json"))
        if result["status"] in {"ready", "blocked"} and expired(result):
            self._set_preview_status(owner, preview_id, "expired")
            result["status"] = "expired"
        result["confirmable"] = result["status"] == "ready"
        return result

    def validate_preview_state(self, owner: str, preview_id: str) -> dict[str, Any]:
        preview = self.preview(owner, preview_id)
        if preview is None:
            raise LoadoutPreviewError("The preview is unavailable.")
        if preview["status"] != "ready":
            raise LoadoutPreviewError(
                "Only a ready, unexpired preview can be confirmed."
            )
        source = self._source(owner)
        if state_fingerprint(source) != preview["state_fingerprint"]:
            self._set_preview_status(owner, preview_id, "invalidated")
            raise LoadoutPreviewError(
                "Live inventory, equipment, or in-game loadouts changed. "
                "Create and review a new preview."
            )
        manifest_version = self.manifest.status().get("version")
        if manifest_version != preview["manifest_version"]:
            self._set_preview_status(owner, preview_id, "invalidated")
            raise LoadoutPreviewError(
                "The Destiny manifest changed. Create a new preview."
            )
        with self.database.connection() as connection:
            if preview["preview_type"] == "single_slot":
                current = connection.execute(
                    """
                    SELECT current_revision_id FROM loadouts
                    WHERE bungie_membership_id = ? AND loadout_id = ?
                      AND archived_at IS NULL
                    """,
                    (owner, preview["source_entity_id"]),
                ).fetchone()
            else:
                board = connection.execute(
                    """
                    SELECT CAST(version AS TEXT) AS current_revision_id
                    FROM loadout_sets
                    WHERE bungie_membership_id = ? AND set_id = ?
                    """,
                    (owner, preview["source_entity_id"]),
                ).fetchone()
                if board is not None:
                    current = board
                else:
                    current = connection.execute(
                        """
                        SELECT current_revision_id FROM loadout_plans
                        WHERE bungie_membership_id = ? AND plan_id = ?
                          AND archived_at IS NULL
                        """,
                        (owner, preview["source_entity_id"]),
                    ).fetchone()
        if current is None or current["current_revision_id"] != preview[
            "source_revision_id"
        ]:
            self._set_preview_status(owner, preview_id, "invalidated")
            raise LoadoutPreviewError(
                "The saved source changed after preview. Create a new preview."
            )
        return preview

    def rebuild_preview(
        self, owner: str, preview_id: str
    ) -> dict[str, Any]:
        """Recreate stale preview evidence from the current saved source."""

        previous = self.preview(owner, preview_id)
        if previous is None:
            raise LoadoutPreviewError("The preview is unavailable.")
        if previous["status"] == "confirmed":
            raise LoadoutPreviewError(
                "This preview has already started an operation."
            )
        if previous["status"] in {"ready", "blocked"}:
            self._set_preview_status(owner, preview_id, "invalidated")
        if previous["preview_type"] == "single_slot":
            loadout = self.loadouts.saved_loadout(
                owner,
                previous["source_entity_id"],
                include_archived=True,
            )
            if loadout is None:
                raise LoadoutPreviewError(
                    "The loadout used by this preview is unavailable."
                )
            target_slot_index = previous.get("target_slot_index")
            if target_slot_index is None:
                raise LoadoutPreviewError(
                    "The preview no longer identifies a target slot."
                )
            return self.create_single_preview(
                owner,
                loadout_id=previous["source_entity_id"],
                revision_id=loadout["revision_id"],
                target_character_id=previous["target_character_id"],
                target_slot_index=int(target_slot_index),
            )
        if previous["action_plan"].get("set_version") is not None:
            return self.create_set_preview(
                owner,
                set_id=previous["source_entity_id"],
            )
        return self.create_plan_preview(
            owner,
            plan_id=previous["source_entity_id"],
        )

    def create_operation(
        self,
        owner: str,
        preview_id: str,
        *,
        backup_choice: str,
        backup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if backup_choice not in {"import", "skip"}:
            raise LoadoutOperationError("Choose whether to import a backup.")
        preview = self.validate_preview_state(owner, preview_id)
        plan = preview["action_plan"]
        operation_id = secrets.token_hex(16)
        now = as_iso(utc_now())
        actions = plan["actions"]
        try:
            with self.database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                # Explicitly confirming a newly generated preview supersedes
                # any interrupted plan for the same character. Preserve its
                # evidence as failed, but do not force the user to resume an
                # obsolete action list before the current plan can start.
                connection.execute(
                    """
                    UPDATE loadout_sync_operations
                    SET status = 'failed',
                        last_error = (
                            'Superseded by a newly confirmed live preview.'
                        ),
                        updated_at = ?, completed_at = ?
                    WHERE bungie_membership_id = ?
                      AND target_character_id = ?
                      AND status = 'paused'
                    """,
                    (
                        now,
                        now,
                        owner,
                        preview["target_character_id"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO loadout_sync_operations (
                        operation_id, preview_id, bungie_membership_id,
                        operation_type, target_character_id, status,
                        backup_choice, current_action_index, total_actions,
                        completed_actions, original_equipment_json,
                        backup_json, recovery_json, result_json,
                        created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, 'pending', ?, 0, ?, 0, ?, ?, '{}',
                        '{}', ?, ?
                    )
                    """,
                    (
                        operation_id,
                        preview_id,
                        owner,
                        preview["preview_type"],
                        preview["target_character_id"],
                        backup_choice,
                        len(actions),
                        compact_json(plan["original_equipment"]),
                        compact_json(backup or {}),
                        now,
                        now,
                    ),
                )
                for index, action in enumerate(actions):
                    connection.execute(
                        """
                        INSERT INTO loadout_sync_actions (
                            operation_id, action_index, action_type, phase,
                            encounter_id, assignment_id, target_slot_index,
                            item_instance_id, request_json, expected_json,
                            status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                        """,
                        (
                            operation_id,
                            index,
                            action["action_type"],
                            action["phase"],
                            action.get("encounter_id"),
                            action.get("assignment_id"),
                            action.get("target_slot_index"),
                            action.get("item_instance_id"),
                            compact_json(action.get("request", {})),
                            compact_json(action.get("expected", {})),
                        ),
                    )
                connection.execute(
                    """
                    UPDATE loadout_previews
                    SET status = 'confirmed', confirmed_at = ?
                    WHERE preview_id = ? AND bungie_membership_id = ?
                      AND status = 'ready'
                    """,
                    (now, preview_id, owner),
                )
        except sqlite3.IntegrityError as error:
            raise LoadoutOperationError(
                "Another loadout operation is already active for this character."
            ) from error
        operation = self.operation(owner, operation_id)
        if operation is None:
            raise RuntimeError("The confirmed operation could not be loaded.")
        return operation

    def import_current_slot_backup(
        self,
        owner: str,
        *,
        character_id: str,
        label: str,
    ) -> dict[str, Any]:
        """Import every populated current slot only after the user's choice."""
        source = self._source(owner)
        component = (
            source["profile"]
            .get("characterLoadouts", {})
            .get("data", {})
            .get(character_id)
        )
        slots = component.get("loadouts") if isinstance(component, dict) else None
        if not isinstance(slots, list):
            raise LoadoutOperationError(
                "Current in-game loadouts are unavailable for backup."
            )
        created = []
        for index, slot in enumerate(slots):
            if not isinstance(slot, dict) or slot_empty(slot):
                continue
            saved = self.loadouts.import_in_game_slot(
                owner,
                character_id=character_id,
                slot_index=index,
                name=f"{label} · Slot {index + 1}",
                description=(
                    "Automatic local backup created immediately before a "
                    "confirmed live synchronization."
                ),
                tags=["automatic-backup", "pre-sync"],
            )
            created.append(
                {
                    "slot_index": index,
                    "loadout_id": saved["loadout_id"],
                    "revision_id": saved["revision_id"],
                    "name": saved["name"],
                }
            )
        return {
            "created_at": as_iso(utc_now()),
            "character_id": character_id,
            "loadouts": created,
        }

    def operation(self, owner: str, operation_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT operation.*, preview.action_plan_json,
                       preview.source_entity_id, preview.source_revision_id
                FROM loadout_sync_operations AS operation
                JOIN loadout_previews AS preview
                  ON preview.preview_id = operation.preview_id
                WHERE operation.bungie_membership_id = ?
                  AND operation.operation_id = ?
                """,
                (owner, operation_id),
            ).fetchone()
            actions = connection.execute(
                """
                SELECT * FROM loadout_sync_actions
                WHERE operation_id = ? ORDER BY action_index
                """,
                (operation_id,),
            ).fetchall() if row else []
            attempts = connection.execute(
                """
                SELECT * FROM loadout_sync_action_attempts
                WHERE operation_id = ?
                ORDER BY action_index, attempt_number
                """,
                (operation_id,),
            ).fetchall() if row else []
        if row is None:
            return None
        result = dict(row)
        for key in (
            "original_equipment_json",
            "backup_json",
            "recovery_json",
            "result_json",
            "action_plan_json",
        ):
            result[key.removesuffix("_json")] = json.loads(result.pop(key))
        result["actions"] = []
        attempts_by_action: dict[int, list[dict[str, Any]]] = {}
        for attempt in attempts:
            value = dict(attempt)
            attempts_by_action.setdefault(int(value["action_index"]), []).append(value)
        for raw in actions:
            action = dict(raw)
            action["request"] = json.loads(action.pop("request_json"))
            action["expected"] = json.loads(action.pop("expected_json"))
            action["attempt_history"] = attempts_by_action.get(
                int(action["action_index"]), []
            )
            result["actions"].append(action)
        result["progress_percent"] = (
            round(100 * result["completed_actions"] / result["total_actions"])
            if result["total_actions"]
            else 100
        )
        result["can_resume"] = result["status"] in {"paused", "failed"}
        return result

    def recent_operations(self, owner: str, *, limit: int = 10) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT operation_id FROM loadout_sync_operations
                WHERE bungie_membership_id = ?
                ORDER BY updated_at DESC LIMIT ?
                """,
                (owner, limit),
            ).fetchall()
        return [
            operation
            for row in rows
            if (operation := self.operation(owner, row["operation_id"]))
            is not None
        ]

    def start(self, owner: str, operation_id: str, access_token: str) -> None:
        operation = self.operation(owner, operation_id)
        if operation is None:
            raise LoadoutOperationError("The operation is unavailable.")
        if operation["status"] == "completed":
            return
        key = (owner, operation["target_character_id"])
        lock = self._locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            raise LoadoutOperationError(
                "A loadout operation is already running for this character."
            )
        if operation["status"] in {"paused", "failed"}:
            self._validate_operation_source(owner, operation)
            self._prepare_resume(owner, operation_id)
        task = asyncio.create_task(
            self._run(owner, operation_id, access_token, lock)
        )
        self._tasks.add(task)
        task.add_done_callback(
            lambda completed: self._operation_task_finished(
                owner, operation_id, completed
            )
        )

    def _operation_task_finished(
        self,
        owner: str,
        operation_id: str,
        task: asyncio.Task[None],
    ) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            self._pause_interrupted_operation(
                owner,
                operation_id,
                "The operation worker stopped before its current action "
                "completed.",
            )
            return
        error = task.exception()
        if error is None:
            return
        LOGGER.error(
            "Loadout operation worker stopped unexpectedly",
            exc_info=(type(error), error, error.__traceback__),
        )
        self._pause_interrupted_operation(
            owner,
            operation_id,
            "The operation worker stopped unexpectedly. Its durable "
            "checkpoint can be resumed safely.",
        )

    def _pause_interrupted_operation(
        self, owner: str, operation_id: str, reason: str
    ) -> None:
        now = as_iso(utc_now())
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE loadout_sync_action_attempts
                SET status = 'failed', message = ?, completed_at = ?
                WHERE operation_id = ? AND status = 'running'
                """,
                (reason, now, operation_id),
            )
            connection.execute(
                """
                UPDATE loadout_sync_actions
                SET status = 'pending'
                WHERE operation_id = ? AND status = 'running'
                """,
                (operation_id,),
            )
            connection.execute(
                """
                UPDATE loadout_sync_operations
                SET status = 'paused', updated_at = ?, last_error = ?
                WHERE bungie_membership_id = ? AND operation_id = ?
                  AND status = 'running'
                """,
                (now, reason, owner, operation_id),
            )

    def _validate_operation_source(
        self,
        owner: str,
        operation: dict[str, Any],
    ) -> None:
        with self.database.connection() as connection:
            if operation["operation_type"] == "single_slot":
                row = connection.execute(
                    """
                    SELECT current_revision_id FROM loadouts
                    WHERE bungie_membership_id = ? AND loadout_id = ?
                      AND archived_at IS NULL
                    """,
                    (owner, operation["source_entity_id"]),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT CAST(version AS TEXT) AS current_revision_id
                    FROM loadout_sets
                    WHERE bungie_membership_id = ? AND set_id = ?
                    """,
                    (owner, operation["source_entity_id"]),
                ).fetchone()
                if row is None:
                    row = connection.execute(
                        """
                        SELECT current_revision_id FROM loadout_plans
                        WHERE bungie_membership_id = ? AND plan_id = ?
                          AND archived_at IS NULL
                        """,
                        (owner, operation["source_entity_id"]),
                    ).fetchone()
        if row is None or row["current_revision_id"] != operation[
            "source_revision_id"
        ]:
            raise LoadoutOperationError(
                "The pinned source was archived, deleted, or revised. This "
                "operation cannot resume; create a new preview instead."
            )

    async def _run(
        self,
        owner: str,
        operation_id: str,
        access_token: str,
        lock: asyncio.Lock,
    ) -> None:
        async with lock:
            operation = self.operation(owner, operation_id)
            if operation is None:
                return
            if operation["completed_actions"]:
                try:
                    await self._audit_completed_slots(
                        owner, operation, access_token
                    )
                except Exception as error:
                    self._fail_resume_audit(owner, operation_id, error)
                    return
            self._set_operation_running(owner, operation_id)
            while True:
                operation = self.operation(owner, operation_id)
                if operation is None:
                    return
                action = next(
                    (
                        row
                        for row in operation["actions"]
                        if row["status"] in {"pending", "running"}
                    ),
                    None,
                )
                if action is None:
                    self._complete_operation(owner, operation_id)
                    return
                try:
                    await self._perform_with_retry(
                        owner,
                        operation,
                        action,
                        access_token,
                    )
                except Exception as error:
                    self._fail_action(owner, operation_id, action, error)
                    return
                else:
                    self._complete_action(owner, operation_id, action)

    async def _audit_completed_slots(
        self,
        owner: str,
        operation: dict[str, Any],
        access_token: str,
    ) -> None:
        """Verify durable slot checkpoints before a resumed mutation."""
        source = await self._fresh_source(owner, access_token)
        character_id = operation["target_character_id"]
        for checkpoint in operation["actions"]:
            if checkpoint["status"] != "completed":
                continue
            slot_index = checkpoint.get("target_slot_index")
            if slot_index is None:
                continue
            if checkpoint["action_type"] == "verify_slot":
                slot = character_slot(source, character_id, slot_index)
                if not slot_matches(slot, checkpoint["expected"]["items"]):
                    raise LoadoutOperationError(
                        f"Previously completed slot {slot_index + 1} diverged; "
                        "resume stopped before another write."
                    )
            elif checkpoint["action_type"] == "verify_clear":
                slot = character_slot(source, character_id, slot_index)
                if not slot_empty(slot):
                    raise LoadoutOperationError(
                        f"Previously cleared slot {slot_index + 1} is populated; "
                        "resume stopped before another write."
                    )

    async def _perform_with_retry(
        self,
        owner: str,
        operation: dict[str, Any],
        action: dict[str, Any],
        access_token: str,
    ) -> None:
        last_error: Exception | None = None
        for cycle_attempt in range(MAX_ACTION_ATTEMPTS):
            attempt_number = self._start_action_attempt(
                operation["operation_id"], action["action_index"]
            )
            item_id = str(action.get("item_instance_id") or "")
            LOGGER.info(
                "Operation %s checkpoint %s/%s starting attempt %s: %s%s",
                operation["operation_id"],
                int(action["action_index"]) + 1,
                len(operation["actions"]),
                attempt_number,
                action["phase"],
                f" (item …{item_id[-8:]})" if item_id else "",
            )
            try:
                evidence = await self._perform_action(
                    owner,
                    operation,
                    action,
                    access_token,
                    enforce_initial_state=(
                        attempt_number == 1
                        and int(operation["completed_actions"]) == 0
                    ),
                )
            except BungieAuthenticationRejected:
                raise
            except Exception as error:
                last_error = error
                self._record_action_error(
                    operation["operation_id"],
                    action["action_index"],
                    attempt_number,
                    error,
                )
                LOGGER.warning(
                    "Operation %s checkpoint %s attempt %s failed: %s",
                    operation["operation_id"],
                    int(action["action_index"]) + 1,
                    attempt_number,
                    error,
                )
                if isinstance(error, BungieActionError) and not error.transient:
                    # Bungie has already told us this request cannot succeed in
                    # the current state. Repeating the identical write only
                    # delays useful failure evidence and can never repair it.
                    break
                if cycle_attempt + 1 >= MAX_ACTION_ATTEMPTS:
                    break
                throttle = (
                    error.throttle_seconds
                    if isinstance(error, BungieActionError)
                    else 0
                )
                await asyncio.sleep(max(RETRY_DELAYS[cycle_attempt], throttle))
            else:
                self._record_action_evidence(
                    operation["operation_id"],
                    action["action_index"],
                    attempt_number,
                    evidence,
                )
                LOGGER.info(
                    "Operation %s checkpoint %s completed on attempt %s: %s",
                    operation["operation_id"],
                    int(action["action_index"]) + 1,
                    attempt_number,
                    evidence.get("message") or "verified",
                )
                return
        assert last_error is not None
        raise last_error

    async def _perform_action(
        self,
        owner: str,
        operation: dict[str, Any],
        action: dict[str, Any],
        access_token: str,
        *,
        enforce_initial_state: bool,
    ) -> dict[str, Any]:
        action_type = action["action_type"]
        request = action["request"]
        expected = action["expected"]
        character_id = operation["target_character_id"]
        minted_after = self._verification_minted_after(operation, action)
        source = (
            await self._fresh_source_after(
                owner,
                access_token,
                minted_after,
            )
            if minted_after is not None
            else await self._available_source(owner, access_token)
        )
        membership_type = int(source["snapshot"]["membership_type"])
        if enforce_initial_state and state_fingerprint(source) != operation["action_plan"][
            "initial_state_fingerprint"
        ]:
            raise LoadoutOperationError(
                "Live state changed after confirmation; no write was sent."
            )

        if action_type in {"transfer_to_vault", "transfer_from_vault"}:
            item = item_by_instance(source, action["item_instance_id"])
            if item is None:
                raise LoadoutOperationError("The exact transfer item is missing.")
            terminal_character = request["target_character_id"]
            if action_type == "transfer_to_vault":
                vault_terminal = request.get("mode") in {
                    "capacity_stage",
                    "restore_vault_origin",
                    "retain_set_only",
                }
                if item["source_kind"] == "vault":
                    return {"message": "Transfer already reached the vault."}
                if not vault_terminal and (
                    item.get("character_id") == terminal_character
                    and item["source_kind"] in {"character_inventory", "equipped"}
                ):
                    return {"message": "Transfer already reached a safe later state."}
                if item["source_kind"] != "character_inventory":
                    raise LoadoutOperationError(
                        "The item cannot safely move to the vault from its current location."
                    )
                write_started_at = utc_now()
                result = await self.bungie.transfer_item(
                    access_token,
                    item_instance_id=action["item_instance_id"],
                    item_hash=int(request["item_hash"]),
                    character_id=str(item["character_id"]),
                    membership_type=membership_type,
                    transfer_to_vault=True,
                )
                await asyncio.sleep(max(TRANSFER_INTERVAL, result["throttle_seconds"]))
                verified = await self._fresh_source_after(
                    owner, access_token, write_started_at
                )
                moved = item_by_instance(verified, action["item_instance_id"])
                if moved is None or moved["source_kind"] != "vault":
                    raise LoadoutOperationError("Transfer-to-vault did not verify.")
                return result
            if (
                item.get("character_id") == terminal_character
                and item["source_kind"] in {"character_inventory", "equipped"}
            ):
                return {"message": "Transfer already verified on target character."}
            if item["source_kind"] != "vault":
                raise LoadoutOperationError(
                    "The exact item is no longer in the vault for transfer."
                )
            write_started_at = utc_now()
            result = await self.bungie.transfer_item(
                access_token,
                item_instance_id=action["item_instance_id"],
                item_hash=int(request["item_hash"]),
                character_id=terminal_character,
                membership_type=membership_type,
                transfer_to_vault=False,
            )
            await asyncio.sleep(max(TRANSFER_INTERVAL, result["throttle_seconds"]))
            verified = await self._fresh_source_after(
                owner, access_token, write_started_at
            )
            moved = item_by_instance(verified, action["item_instance_id"])
            if moved is None or moved.get("character_id") != terminal_character:
                raise LoadoutOperationError("Transfer-from-vault did not verify.")
            return result

        if action_type == "insert_socket_plug":
            changes = request.get("changes")
            if isinstance(changes, list):
                return await self._apply_socket_wave(
                    owner,
                    access_token,
                    source,
                    character_id,
                    membership_type,
                    changes,
                )
            item = item_by_instance(source, action["item_instance_id"])
            if item is None:
                raise LoadoutOperationError(
                    "The exact socketed item is missing."
                )
            if (
                item.get("character_id") != character_id
                or item.get("source_kind")
                not in {"character_inventory", "equipped"}
            ):
                raise LoadoutOperationError(
                    "The socketed item is not available on the target "
                    "character."
                )
            socket_index = int(request["socket_index"])
            plug_hash = int(request["plug_hash"])
            current_plugs = component_plug_hashes(item.get("components"))
            current = (
                current_plugs[socket_index]
                if socket_index < len(current_plugs)
                else None
            )
            if current == plug_hash:
                return {"message": "Free gameplay plug was already inserted."}
            if self._free_socket_change(
                source,
                item,
                character_id,
                socket_index=socket_index,
                plug_hash=plug_hash,
                item_name=str(request["item_name"]),
            ) is None:
                raise LoadoutOperationError(
                    "The saved gameplay plug is no longer reported as a free, "
                    "insertable plug."
                )
            result = await self.bungie.insert_socket_plug_free(
                access_token,
                item_instance_id=str(item["item_instance_id"]),
                socket_index=socket_index,
                plug_hash=plug_hash,
                character_id=character_id,
                membership_type=membership_type,
            )
            await asyncio.sleep(
                max(SOCKET_INTERVAL, result["throttle_seconds"])
            )
            return result

        if action_type == "verify_socket_plug":
            changes = expected.get("changes")
            if isinstance(changes, list):
                for change in changes:
                    item = item_by_instance(
                        source, str(change["item_instance_id"])
                    )
                    if item is None:
                        raise LoadoutOperationError(
                            "A socketed item disappeared during verification."
                        )
                    socket_index = int(change["socket_index"])
                    current_plugs = component_plug_hashes(
                        item.get("components")
                    )
                    current = (
                        current_plugs[socket_index]
                        if socket_index < len(current_plugs)
                        else None
                    )
                    if current != int(change["plug_hash"]):
                        raise LoadoutOperationError(
                            f"{change['item_name']} socket "
                            f"{socket_index + 1} did not verify after the "
                            "free plug actions."
                        )
                return {
                    "message": (
                        f"Verified {len(changes)} free gameplay plug "
                        "insertion(s)."
                    )
                }
            item = item_by_instance(source, action["item_instance_id"])
            if item is None:
                raise LoadoutOperationError(
                    "The exact socketed item disappeared during verification."
                )
            socket_index = int(expected["socket_index"])
            current_plugs = component_plug_hashes(item.get("components"))
            current = (
                current_plugs[socket_index]
                if socket_index < len(current_plugs)
                else None
            )
            if current != int(expected["plug_hash"]):
                raise LoadoutOperationError(
                    f"{expected['item_name']} socket {socket_index + 1} did "
                    "not verify after the free plug action."
                )
            return {"message": "Free gameplay plug insertion verified."}

        if (
            action_type == "equip"
            and request.get("mode") == "parallel_prepare"
        ):
            return await self._prepare_loadout_wave(
                owner,
                access_token,
                source,
                character_id,
                membership_type,
                expected["items"],
                request.get("socket_clears", []),
                request.get("socket_changes", []),
            )

        if (
            action_type == "equip"
            and request.get("mode") == "exotic_replacements"
        ):
            replacement_group = request.get("group")
            replacements = self._exotic_replacements(
                source,
                character_id,
                expected["items"],
                group=(
                    str(replacement_group)
                    if replacement_group in {"weapon", "armor"}
                    else None
                ),
            )
            if not replacements:
                return {
                    "message": (
                        "No conflicting equipped Exotic remains; preparatory "
                        "equip skipped."
                    )
                }
            item_ids = [str(item["item_instance_id"]) for item in replacements]
            requests = []
            for item_id in item_ids:
                item = item_by_instance(source, item_id)
                if (
                    item is None
                    or item.get("character_id") != character_id
                    or item.get("source_kind")
                    not in {"character_inventory", "equipped"}
                ):
                    raise LoadoutOperationError(
                        "Every Exotic-slot replacement must verify on the "
                        "target character before equip."
                    )
                # Weapon and armor replacements are independent. Send them at
                # the same time, while keeping each request independently
                # observable because Bungie's bulk response can contain
                # per-item failures.
                requests.append(
                    self.bungie.equip_items(
                        access_token,
                        item_instance_ids=[item_id],
                        character_id=character_id,
                        membership_type=membership_type,
                    )
                )
            results = await asyncio.gather(*requests)
            await asyncio.sleep(
                max(
                    EQUIP_INTERVAL,
                    *(result["throttle_seconds"] for result in results),
                )
            )
            evidence = dict(results[-1])
            evidence["message"] = (
                "Bungie accepted and individually validated Exotic-slot "
                f"replacement equip status for {len(results)} item(s)."
            )
            return evidence

        if (
            action_type == "verify_prepared"
            and request.get("mode") == "exotic_replacements"
        ):
            replacement_group = request.get("group")
            remaining = self._exotic_replacements(
                source,
                character_id,
                expected["items"],
                group=(
                    str(replacement_group)
                    if replacement_group in {"weapon", "armor"}
                    else None
                ),
            )
            if remaining:
                names = ", ".join(
                    str(item.get("name") or item["item_instance_id"])
                    for item in remaining
                )
                raise LoadoutOperationError(
                    "Equipped Exotic replacement did not verify for: "
                    f"{names}."
                )
            group_label = (
                str(replacement_group)
                if replacement_group in {"weapon", "armor"}
                else "weapon and armor"
            )
            return {
                "message": (
                    f"Equipped Exotic {group_label} conflicts were cleared."
                )
            }

        if action_type == "equip":
            if equipment_matches(source, character_id, expected["items"]):
                return {"message": "Exact equipment was already prepared."}
            for item_id in request["item_instance_ids"]:
                item = item_by_instance(source, item_id)
                if item is None or item.get("character_id") != character_id:
                    raise LoadoutOperationError(
                        "Every item must verify on the target character before equip."
                    )
            result = await self.bungie.equip_items(
                access_token,
                item_instance_ids=request["item_instance_ids"],
                character_id=character_id,
                membership_type=membership_type,
            )
            await asyncio.sleep(max(EQUIP_INTERVAL, result["throttle_seconds"]))
            return result

        if action_type == "verify_prepared":
            if not equipment_matches(source, character_id, expected["items"]):
                raise LoadoutOperationError(
                    "Prepared equipment or socket state did not match the pinned revision."
                )
            return {"message": "Prepared equipment and sockets verified."}

        if action_type == "snapshot":
            slot = character_slot(source, character_id, action["target_slot_index"])
            if slot_matches(
                slot,
                expected["items"],
                allow_extra=bool(expected.get("partial")),
            ):
                return {"message": "Target slot already matches; snapshot skipped."}
            result = await self.bungie.snapshot_loadout(
                access_token,
                loadout_index=int(action["target_slot_index"]),
                character_id=character_id,
                membership_type=membership_type,
                color_hash=valid_hash(request.get("color_hash")),
                icon_hash=valid_hash(request.get("icon_hash")),
                name_hash=valid_hash(request.get("name_hash")),
            )
            await asyncio.sleep(max(LOADOUT_INTERVAL, result["throttle_seconds"]))
            return result

        if action_type == "identifiers":
            slot = character_slot(source, character_id, action["target_slot_index"])
            if identifiers_match(slot, expected):
                return {"message": "Loadout identifiers already match."}
            result = await self.bungie.update_loadout_identifiers(
                access_token,
                loadout_index=int(action["target_slot_index"]),
                character_id=character_id,
                membership_type=membership_type,
                name_hash=expected.get("name_hash"),
                icon_hash=expected.get("icon_hash"),
                color_hash=expected.get("color_hash"),
            )
            await asyncio.sleep(max(LOADOUT_INTERVAL, result["throttle_seconds"]))
            return result

        if action_type == "verify_slot":
            slot = character_slot(source, character_id, action["target_slot_index"])
            differences = slot_mismatch_reasons(
                slot,
                expected["items"],
                allow_extra=bool(expected.get("partial")),
            )
            if differences:
                raise LoadoutOperationError(
                    f"Slot {int(action['target_slot_index']) + 1} did not "
                    "match the pinned revision: " + "; ".join(differences)
                )
            if expected.get("identifiers") and not identifiers_match(
                slot, expected["identifiers"]
            ):
                raise LoadoutOperationError("The slot identifiers did not verify.")
            return {"message": "Exact in-game slot verified."}

        if action_type == "clear_slot":
            slot = character_slot(source, character_id, action["target_slot_index"])
            if slot_empty(slot):
                return {"message": "Unassigned slot was already empty."}
            result = await self.bungie.clear_loadout(
                access_token,
                loadout_index=int(action["target_slot_index"]),
                character_id=character_id,
                membership_type=membership_type,
            )
            await asyncio.sleep(max(LOADOUT_INTERVAL, result["throttle_seconds"]))
            return result

        if action_type == "verify_clear":
            slot = character_slot(source, character_id, action["target_slot_index"])
            if not slot_empty(slot):
                raise LoadoutOperationError("The unassigned slot is still populated.")
            return {"message": "Empty slot verified."}

        if action_type == "restore_equipment":
            if equipment_matches(source, character_id, expected["items"], compare_plugs=False):
                return {"message": "Original equipment was already restored."}
            result = await self.bungie.equip_items(
                access_token,
                item_instance_ids=request["item_instance_ids"],
                character_id=character_id,
                membership_type=membership_type,
            )
            await asyncio.sleep(max(EQUIP_INTERVAL, result["throttle_seconds"]))
            return result

        if action_type == "verify_restored":
            if not equipment_matches(
                source, character_id, expected["items"], compare_plugs=False
            ):
                raise LoadoutOperationError("Original equipment did not fully restore.")
            return {"message": "Original equipment restored and verified."}
        raise LoadoutOperationError(f"Unsupported operation action {action_type}.")

    async def _apply_socket_wave(
        self,
        owner: str,
        access_token: str,
        source: dict[str, Any],
        character_id: str,
        membership_type: int,
        changes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Apply independent socket writes in parallel retry waves."""

        pending = list(changes)
        latest_source = source
        last_errors: dict[tuple[str, int], Exception] = {}
        for wave_index in range(MAX_ACTION_ATTEMPTS):
            pending = self._pending_socket_changes(latest_source, pending)
            if not pending:
                return {
                    "message": (
                        f"Verified {len(changes)} parallel socket write(s)."
                    ),
                    "http_status": 200,
                    "error_code": 1,
                    "error_status": "Success",
                    "throttle_seconds": 0,
                }

            LOGGER.info(
                "Parallel socket wave %s/%s is applying %s unresolved "
                "write(s)",
                wave_index + 1,
                MAX_ACTION_ATTEMPTS,
                len(pending),
            )
            coroutines = []
            submitted: list[dict[str, Any]] = []
            for change in pending:
                item = item_by_instance(
                    latest_source, str(change["item_instance_id"])
                )
                if item is None:
                    raise LoadoutOperationError(
                        "An item required by the socket wave is missing."
                    )
                if (
                    item.get("character_id") != character_id
                    or item.get("source_kind")
                    not in {"character_inventory", "equipped"}
                ):
                    raise LoadoutOperationError(
                        "A socket-wave item is not on the target character."
                    )
                if self._free_socket_change(
                    latest_source,
                    item,
                    character_id,
                    socket_index=int(change["socket_index"]),
                    plug_hash=int(change["plug_hash"]),
                    item_name=str(change["item_name"]),
                ) is None:
                    raise LoadoutOperationError(
                        f"{change['item_name']} socket "
                        f"{int(change['socket_index']) + 1} is no longer "
                        "reported as freely insertable."
                    )
                submitted.append(change)
                coroutines.append(
                    self.bungie.insert_socket_plug_free(
                        access_token,
                        item_instance_id=str(change["item_instance_id"]),
                        socket_index=int(change["socket_index"]),
                        plug_hash=int(change["plug_hash"]),
                        character_id=character_id,
                        membership_type=membership_type,
                    )
                )
            results = await asyncio.gather(
                *coroutines, return_exceptions=True
            )
            throttle = 0.0
            for change, result in zip(submitted, results, strict=True):
                key = (
                    str(change["item_instance_id"]),
                    int(change["socket_index"]),
                )
                if isinstance(result, Exception):
                    if isinstance(result, BungieAuthenticationRejected):
                        raise result
                    last_errors[key] = result
                else:
                    last_errors.pop(key, None)
                    throttle = max(
                        throttle, float(result["throttle_seconds"])
                    )
            await asyncio.sleep(max(SOCKET_INTERVAL, throttle))
            latest_source = await self._fresh_source(owner, access_token)
            pending = self._pending_socket_changes(latest_source, pending)
            LOGGER.info(
                "Parallel socket wave %s verified; %s write(s) remain",
                wave_index + 1,
                len(pending),
            )
            if not pending:
                await asyncio.sleep(SOCKET_INTERVAL)
                settled_source = await self._fresh_source(owner, access_token)
                settled_pending = self._pending_socket_changes(
                    settled_source, changes
                )
                if not settled_pending:
                    return {
                        "message": (
                            f"Verified {len(changes)} parallel socket "
                            "write(s) twice."
                        ),
                        "http_status": 200,
                        "error_code": 1,
                        "error_status": "Success",
                        "throttle_seconds": 0,
                    }
                latest_source = settled_source
                pending = settled_pending
            if wave_index + 1 < MAX_ACTION_ATTEMPTS:
                await asyncio.sleep(RETRY_DELAYS[wave_index])

        failed = []
        for change in pending:
            key = (
                str(change["item_instance_id"]),
                int(change["socket_index"]),
            )
            detail = last_errors.get(key)
            failed.append(
                f"{change['item_name']} socket "
                f"{int(change['socket_index']) + 1}"
                + (f" ({detail})" if detail is not None else "")
            )
        raise BungieActionError(
            "Socket writes did not verify after parallel retries: "
            + "; ".join(failed),
            error_status="SocketWaveDidNotVerify",
            transient=False,
        )

    @staticmethod
    def _pending_socket_changes(
        source: dict[str, Any], changes: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        pending = []
        for change in changes:
            item = item_by_instance(
                source, str(change["item_instance_id"])
            )
            socket_index = int(change["socket_index"])
            plugs = component_plug_hashes(
                item.get("components") if item is not None else None
            )
            if (
                socket_index >= len(plugs)
                or plugs[socket_index] != int(change["plug_hash"])
            ):
                pending.append(change)
        return pending

    async def _prepare_loadout_wave(
        self,
        owner: str,
        access_token: str,
        source: dict[str, Any],
        character_id: str,
        membership_type: int,
        expected_items: list[dict[str, Any]],
        socket_clears: list[dict[str, Any]],
        socket_changes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Converge equipment and sockets using maximum independent writes."""

        latest_source = source
        item_ids = [str(item["item_instance_id"]) for item in expected_items]
        last_errors: list[str] = []
        initial_socket_pending = self._pending_socket_changes(
            latest_source, socket_changes
        )
        armor_items_to_rebuild = {
            str(change["item_instance_id"])
            for change in initial_socket_pending
            if change.get("socket_kind") == "armor mod"
        }
        required_clears = [
            change
            for change in socket_clears
            if str(change["item_instance_id"]) in armor_items_to_rebuild
        ]
        if required_clears:
            LOGGER.info(
                "Clearing %s armor socket(s) before parallel insertion",
                len(required_clears),
            )
            await self._apply_socket_wave(
                owner,
                access_token,
                latest_source,
                character_id,
                membership_type,
                required_clears,
            )
            # _apply_socket_wave force-refreshes and verifies every empty plug.
            latest_source = await self._available_source(owner, access_token)
        for wave_index in range(MAX_ACTION_ATTEMPTS):
            equipment_pending = not equipment_matches(
                latest_source,
                character_id,
                expected_items,
                compare_plugs=False,
            )
            socket_pending = self._pending_socket_changes(
                latest_source, socket_changes
            )
            if not equipment_pending and not socket_pending:
                return {
                    "message": (
                        "Exact equipment and all saved sockets verified after "
                        f"{wave_index} parallel wave(s)."
                    ),
                    "http_status": 200,
                    "error_code": 1,
                    "error_status": "Success",
                    "throttle_seconds": 0,
                }

            coroutines = []
            labels: list[str] = []

            for change in socket_pending:
                item_id = str(change["item_instance_id"])
                item = item_by_instance(latest_source, item_id)
                if item is None:
                    raise LoadoutOperationError(
                        f"{change['item_name']} disappeared while preparing "
                        "its sockets."
                    )
                if (
                    item.get("character_id") != character_id
                    or item.get("source_kind")
                    not in {"character_inventory", "equipped"}
                ):
                    raise LoadoutOperationError(
                        f"{change['item_name']} is not on the target "
                        "character."
                    )
                if self._free_socket_change(
                    latest_source,
                    item,
                    character_id,
                    socket_index=int(change["socket_index"]),
                    plug_hash=int(change["plug_hash"]),
                    item_name=str(change["item_name"]),
                ) is None:
                    raise LoadoutOperationError(
                        f"{change['item_name']} socket "
                        f"{int(change['socket_index']) + 1} is no longer "
                        "reported as freely insertable."
                    )
                labels.append(
                    f"socket {item_id}:{int(change['socket_index'])}"
                )
                coroutines.append(
                    self.bungie.insert_socket_plug_free(
                        access_token,
                        item_instance_id=item_id,
                        socket_index=int(change["socket_index"]),
                        plug_hash=int(change["plug_hash"]),
                        character_id=character_id,
                        membership_type=membership_type,
                    )
                )

            if equipment_pending:
                for item_id in item_ids:
                    item = item_by_instance(latest_source, item_id)
                    if (
                        item is None
                        or item.get("character_id") != character_id
                        or item.get("source_kind")
                        not in {"character_inventory", "equipped"}
                    ):
                        raise LoadoutOperationError(
                            "Every exact item must be on the target character "
                            "before parallel preparation."
                        )
                for replacement in self._exotic_replacements(
                    latest_source, character_id, expected_items
                ):
                    replacement_id = str(replacement["item_instance_id"])
                    labels.append(f"exotic replacement {replacement_id}")
                    coroutines.append(
                        self.bungie.equip_items(
                            access_token,
                            item_instance_ids=[replacement_id],
                            character_id=character_id,
                            membership_type=membership_type,
                        )
                    )
                labels.append("bulk exact equip")
                coroutines.append(
                    self.bungie.equip_items(
                        access_token,
                        item_instance_ids=item_ids,
                        character_id=character_id,
                        membership_type=membership_type,
                    )
                )

            LOGGER.info(
                "Parallel preparation wave %s/%s submitted %s write(s): "
                "%s socket target(s), equipment=%s",
                wave_index + 1,
                MAX_ACTION_ATTEMPTS,
                len(coroutines),
                len(socket_pending),
                equipment_pending,
            )
            write_started_at = utc_now()
            results = await asyncio.gather(
                *coroutines, return_exceptions=True
            )
            last_errors = []
            throttle = 0.0
            for label, result in zip(labels, results, strict=True):
                if isinstance(result, BungieAuthenticationRejected):
                    raise result
                if isinstance(result, Exception):
                    last_errors.append(f"{label}: {result}")
                else:
                    throttle = max(
                        throttle, float(result["throttle_seconds"])
                    )
            await asyncio.sleep(max(EQUIP_INTERVAL, SOCKET_INTERVAL, throttle))
            # Get evidence minted after this wave began. Bungie's profile
            # endpoint can return HTTP 200 with an older cached component;
            # treating that response as current made us replay successful
            # equips and eventually fail before SnapshotLoadout ran.
            if latest_source.get("snapshot", {}).get("source_minted_at"):
                latest_source = await self._fresh_source_after(
                    owner, access_token, write_started_at
                )
            else:
                # Lightweight unit/service callers may provide a normalized
                # source without persisted snapshot metadata.
                latest_source = await self._fresh_source(owner, access_token)
            remaining_sockets = self._pending_socket_changes(
                latest_source, socket_changes
            )
            remaining_equipment = not equipment_matches(
                latest_source,
                character_id,
                expected_items,
                compare_plugs=False,
            )
            # A newly minted profile can still represent an intermediate
            # state while Bungie finishes a bulk equip. Recheck that state a
            # few times before sending the same writes again.
            for verification_delay in PREPARATION_VERIFY_DELAYS:
                if not remaining_equipment and not remaining_sockets:
                    break
                LOGGER.info(
                    "Parallel preparation wave %s is not settled; checking "
                    "again in %.1f second(s) before retrying writes",
                    wave_index + 1,
                    verification_delay,
                )
                await asyncio.sleep(verification_delay)
                latest_source = await self._fresh_source(owner, access_token)
                remaining_sockets = self._pending_socket_changes(
                    latest_source, socket_changes
                )
                remaining_equipment = not equipment_matches(
                    latest_source,
                    character_id,
                    expected_items,
                    compare_plugs=False,
                )
            LOGGER.info(
                "Parallel preparation wave %s verified; %s socket target(s) "
                "remain, equipment=%s",
                wave_index + 1,
                len(remaining_sockets),
                "pending" if remaining_equipment else "verified",
            )
            if remaining_equipment:
                LOGGER.info(
                    "Parallel preparation wave %s still missing equipped "
                    "item(s): %s",
                    wave_index + 1,
                    ", ".join(
                        missing_equipment_labels(
                            latest_source, character_id, expected_items
                        )
                    ),
                )
            if not remaining_equipment and not remaining_sockets:
                # Require a second observation so a briefly successful
                # concurrent mutation cannot be snapshotted before Bungie's
                # eventual item state settles.
                await asyncio.sleep(SOCKET_INTERVAL)
                settled_source = await self._fresh_source(owner, access_token)
                settled_sockets = self._pending_socket_changes(
                    settled_source, socket_changes
                )
                settled_equipment = not equipment_matches(
                    settled_source,
                    character_id,
                    expected_items,
                    compare_plugs=False,
                )
                if not settled_equipment and not settled_sockets:
                    return {
                        "message": (
                            "Exact equipment and all saved sockets verified "
                            "twice after "
                            f"{wave_index + 1} parallel wave(s)."
                        ),
                        "http_status": 200,
                        "error_code": 1,
                        "error_status": "Success",
                        "throttle_seconds": throttle,
                    }
                latest_source = settled_source
            if wave_index + 1 < MAX_ACTION_ATTEMPTS:
                await asyncio.sleep(RETRY_DELAYS[wave_index])

        detail_parts = list(last_errors)
        missing = missing_equipment_labels(
            latest_source, character_id, expected_items
        )
        if missing:
            detail_parts.append(
                "equipment still not reported as equipped: "
                + ", ".join(missing)
            )
        detail = "; ".join(detail_parts)
        raise BungieActionError(
            "Parallel loadout preparation did not converge after retries."
            + (f" Last request errors: {detail}" if detail else ""),
            error_status="ParallelPreparationDidNotVerify",
            transient=False,
        )

    def _exotic_replacements(
        self,
        source: dict[str, Any],
        character_id: str,
        desired_items: list[dict[str, Any]],
        *,
        group: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return desired slot items that must first replace live Exotics."""
        desired_by_bucket = {
            int(item["bucket_hash"]): item for item in desired_items
        }
        item_definitions = self.manifest.resolve_many(
            "DestinyInventoryItemDefinition",
            (int(item["item_hash"]) for item in source["items"]),
        )
        replacements: list[dict[str, Any]] = []
        seen: set[str] = set()
        for current in source["items"]:
            if (
                current.get("source_kind") != "equipped"
                or current.get("character_id") != character_id
            ):
                continue
            definition = item_definitions.get(int(current["item_hash"]))
            inventory = (
                definition.get("inventory", {})
                if isinstance(definition, dict)
                else {}
            )
            if (
                not isinstance(inventory, dict)
                or inventory.get("tierType") != 6
            ):
                continue
            try:
                bucket_hash = intended_bucket_hash(definition)
            except LoadoutInspectionError:
                continue
            if bucket_hash not in (
                *REQUIRED_GAMEPLAY_BUCKET_ORDER[:3],
                *REQUIRED_GAMEPLAY_BUCKET_ORDER[3:8],
            ):
                continue
            bucket_group = (
                "weapon"
                if bucket_hash in REQUIRED_GAMEPLAY_BUCKET_ORDER[:3]
                else "armor"
            )
            if group is not None and bucket_group != group:
                continue
            desired = desired_by_bucket.get(bucket_hash)
            if (
                desired is None
                or str(desired["item_instance_id"])
                == str(current["item_instance_id"])
            ):
                continue
            instance_id = str(desired["item_instance_id"])
            if instance_id not in seen:
                replacements.append(desired)
                seen.add(instance_id)
        return replacements

    def _free_socket_change(
        self,
        source: dict[str, Any],
        item: dict[str, Any],
        target_character_id: str,
        *,
        socket_index: int,
        plug_hash: int,
        item_name: str,
    ) -> dict[str, Any] | None:
        """Describe a currently insertable free gameplay-plug change."""
        definition = self.manifest.resolve_many(
            "DestinyInventoryItemDefinition",
            (int(item["item_hash"]),),
        ).get(int(item["item_hash"]))
        sockets = (
            definition.get("sockets", {})
            if isinstance(definition, dict)
            else {}
        )
        entries = (
            sockets.get("socketEntries")
            if isinstance(sockets, dict)
            else None
        )
        if (
            not isinstance(entries, list)
            or socket_index < 0
            or socket_index >= len(entries)
            or not isinstance(entries[socket_index], dict)
        ):
            return None
        entry = entries[socket_index]
        socket_type_hash = valid_hash(entry.get("socketTypeHash"))
        if socket_type_hash is None:
            return None
        socket_type = self.manifest.resolve_many(
            "DestinySocketTypeDefinition",
            (socket_type_hash,),
        ).get(socket_type_hash)
        category_hash = valid_hash(
            socket_type.get("socketCategoryHash")
            if isinstance(socket_type, dict)
            else None
        )
        try:
            bucket_hash = intended_bucket_hash(definition)
        except LoadoutInspectionError:
            return None
        if category_hash == ARMOR_MOD_SOCKET_CATEGORY_HASH:
            socket_kind = "armor mod"
        elif bucket_hash == SUBCLASS_BUCKET_HASH:
            # Subclass abilities, Aspects, and Fragments use several socket
            # categories, and some subclass socket types have no category.
            # Bungie's live plug-set response is the authoritative evidence
            # that the exact saved plug is unlocked and insertable.
            socket_kind = "subclass plug"
        elif bucket_hash == REQUIRED_GAMEPLAY_BUCKET_ORDER[-1]:
            # Seasonal artifact perks are represented by normal artifact
            # sockets. Bungie's live plug-set response remains authoritative
            # for whether a saved perk is unlocked and freely insertable.
            socket_kind = "artifact perk"
        else:
            return None
        if not plug_is_live_insertable(
            source,
            item,
            target_character_id,
            socket_index=socket_index,
            plug_hash=plug_hash,
            socket_entry=entry,
        ):
            return None
        return {
            "item_instance_id": str(item["item_instance_id"]),
            "item_hash": int(item["item_hash"]),
            "item_name": item_name,
            "socket_index": socket_index,
            "plug_hash": plug_hash,
            "category_hash": category_hash,
            "socket_kind": socket_kind,
        }

    def _armor_socket_clear_targets(
        self,
        source: dict[str, Any],
        item: dict[str, Any],
        target_character_id: str,
        *,
        item_name: str,
    ) -> list[dict[str, Any]]:
        """Describe every writable empty armor-mod socket on one item."""

        definition = self.manifest.resolve_many(
            "DestinyInventoryItemDefinition",
            (int(item["item_hash"]),),
        ).get(int(item["item_hash"]))
        sockets = (
            definition.get("sockets")
            if isinstance(definition, dict)
            else None
        )
        entries = (
            sockets.get("socketEntries")
            if isinstance(sockets, dict)
            else None
        )
        if not isinstance(entries, list):
            return []
        targets = []
        for socket_index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            empty_hash = valid_hash(entry.get("singleInitialItemHash"))
            if empty_hash is None:
                continue
            change = self._free_socket_change(
                source,
                item,
                target_character_id,
                socket_index=socket_index,
                plug_hash=empty_hash,
                item_name=item_name,
            )
            if change is not None and change["socket_kind"] == "armor mod":
                targets.append(change)
        return targets

    def _source(self, owner: str) -> dict[str, Any]:
        source = self.database.load_active_loadout_source(owner)
        if source is None or not snapshot_is_fresh(source["snapshot"]):
            raise LoadoutPreviewError(
                "Refresh inventory before creating a live-state preview."
            )
        manifest_status = self.manifest.status()
        if not manifest_status.get("available") or not manifest_status.get("version"):
            raise LoadoutPreviewError("Prepare the current Destiny manifest first.")
        return source

    async def _fresh_source(self, owner: str, access_token: str) -> dict[str, Any]:
        await self.inventory.synchronize(
            bungie_membership_id=owner,
            access_token=access_token,
            force=True,
        )
        return self._source(owner)

    async def _available_source(
        self, owner: str, access_token: str
    ) -> dict[str, Any]:
        """Reuse verified fresh evidence; fetch only when it has gone stale."""

        await self.inventory.synchronize(
            bungie_membership_id=owner,
            access_token=access_token,
            force=False,
        )
        return self._source(owner)

    async def _fresh_source_after(
        self,
        owner: str,
        access_token: str,
        minted_after: datetime,
    ) -> dict[str, Any]:
        """Wait for Bungie to mint profile data after a confirmed write."""
        for delay in (0.0, *POST_WRITE_REFRESH_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            source = await self._fresh_source(owner, access_token)
            minted_at = parse_api_timestamp(
                source["snapshot"].get("source_minted_at")
            )
            if minted_at is not None and minted_at > minted_after:
                return source
        raise LoadoutOperationError(
            "Bungie has not returned profile data minted after the preceding "
            "write. The write is not being treated as failed; wait for "
            "Bungie's profile cache, then use Resume."
        )

    def _verification_minted_after(
        self,
        operation: dict[str, Any],
        action: dict[str, Any],
    ) -> datetime | None:
        verify_sources = {
            "verify_prepared": {"equip"},
            "verify_socket_plug": {"insert_socket_plug"},
            "verify_slot": {"snapshot", "identifiers"},
            "verify_clear": {"clear_slot"},
            "verify_restored": {"restore_equipment"},
        }
        allowed = verify_sources.get(str(action["action_type"]))
        if allowed is None:
            return None
        action_index = int(action["action_index"])
        target_slot = action.get("target_slot_index")
        checkpoints: list[datetime] = []
        for previous in operation["actions"]:
            if int(previous["action_index"]) >= action_index:
                break
            if previous["action_type"] not in allowed:
                continue
            if (
                target_slot is not None
                and previous.get("target_slot_index") != target_slot
            ):
                continue
            if previous.get("last_http_status") is None:
                continue
            completed_at = parse_api_timestamp(previous.get("completed_at"))
            if completed_at is not None:
                checkpoints.append(completed_at)
        return max(checkpoints) if checkpoints else None

    def _target_character(
        self,
        source: dict[str, Any],
        character_id: str,
        class_type: int,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        character = next(
            (row for row in source["characters"] if row["character_id"] == character_id),
            None,
        )
        if character is None:
            raise LoadoutPreviewError("The target character is unavailable.")
        if int(character.get("class_type", -1)) != class_type:
            raise LoadoutPreviewError("The loadout class does not match the target character.")
        component = (
            source["profile"].get("characterLoadouts", {}).get("data", {}).get(character_id)
        )
        slots = component.get("loadouts") if isinstance(component, dict) else None
        if not isinstance(slots, list):
            raise LoadoutPreviewError("The target character has no loadout slots.")
        return character, [slot if isinstance(slot, dict) else {} for slot in slots]

    def _replacement_job(
        self,
        source: dict[str, Any],
        character: dict[str, Any],
        loadout: dict[str, Any],
        slot_index: int,
        *,
        current_slot: dict[str, Any],
        encounter: dict[str, Any] | None,
        assignment: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], list[str]]:
        blockers: list[str] = []
        target_character_id = str(character["character_id"])
        active = {str(item["item_instance_id"]): item for item in source["items"]}
        item_defs = self.manifest.resolve_many(
            "DestinyInventoryItemDefinition",
            (int(item["item_hash"]) for item in source["items"]),
        )
        classifications = []
        transfers = []
        socket_clears = []
        socket_changes = []
        expected_items = []
        partial = bool(loadout.get("partial"))
        weapon_exotics = 0
        armor_exotics = 0
        for saved_item in loadout["items"]:
            instance_id = str(saved_item["item_instance_id"])
            item = active.get(instance_id)
            bucket_hash = int(saved_item["bucket_hash"])
            if item is None:
                blockers.append(
                    f"{saved_item['name']} ({instance_id[-8:]}) is no longer owned."
                )
                classifications.append({"name": saved_item["name"], "instance_id": instance_id, "location": "missing"})
                continue
            if int(item["item_hash"]) != int(saved_item["item_hash"]):
                blockers.append(f"{saved_item['name']} has incompatible identity data.")
                continue
            definition = item_defs.get(int(item["item_hash"]))
            try:
                live_bucket = intended_bucket_hash(definition)
            except LoadoutInspectionError:
                live_bucket = None
            if live_bucket != bucket_hash:
                blockers.append(f"{saved_item['name']} no longer resolves to its saved slot.")
            location = classify_location(item, target_character_id)
            instance_component = item.get("components", {}).get("instances", {})
            if (definition or {}).get("equippable") is False:
                blockers.append(
                    f"{saved_item['name']} cannot currently be equipped."
                )
            elif (
                isinstance(instance_component, dict)
                and instance_component.get("canEquip") is False
            ):
                cannot_reason = (
                    int(instance_component.get("cannotEquipReason") or 0)
                    if isinstance(instance_component, dict)
                    else 0
                )
                # EquipFailureReason 2 is a temporary unique-item conflict,
                # commonly an incoming Exotic while another Exotic is still
                # equipped. The complete item set is equipped together and
                # verified afterward, so this transient state is not a hard
                # preview blocker. All other failure reasons remain blocked.
                if not equip_failure_is_transient(
                    cannot_reason, location=location
                ):
                    blockers.append(
                        f"{saved_item['name']} cannot currently be equipped."
                    )
            class_type = (definition or {}).get("classType")
            if class_type not in (int(character["class_type"]), 3):
                blockers.append(f"{saved_item['name']} is incompatible with this class.")
            inventory = (definition or {}).get("inventory", {})
            if isinstance(inventory, dict) and inventory.get("tierType") == 6:
                if bucket_hash in REQUIRED_GAMEPLAY_BUCKET_ORDER[:3]:
                    weapon_exotics += 1
                elif bucket_hash in REQUIRED_GAMEPLAY_BUCKET_ORDER[3:8]:
                    armor_exotics += 1
            saved_plugs = [
                {
                    "socket_index": int(plug["socket_index"]),
                    "plug_hash": plug.get("plug_hash"),
                    "filtered": bool(plug.get("filtered_from_preview")),
                }
                for plug in saved_item["plugs"]
            ]
            current_plugs = component_plug_hashes(item.get("components"))
            armor_sockets_need_rebuild = False
            for plug in saved_plugs:
                if plug["filtered"] or plug["plug_hash"] in (
                    None,
                    INVALID_HASH_SENTINEL,
                ):
                    continue
                index = plug["socket_index"]
                current = current_plugs[index] if index < len(current_plugs) else None
                socket_change = self._free_socket_change(
                    source,
                    item,
                    target_character_id,
                    socket_index=index,
                    plug_hash=int(plug["plug_hash"]),
                    item_name=saved_item["name"],
                )
                if socket_change is None:
                    if current != plug["plug_hash"]:
                        blockers.append(
                            f"{saved_item['name']} socket {index + 1} differs; "
                            "this plug is not a currently insertable free "
                            "gameplay plug."
                        )
                    continue
                # Keep every writable saved socket as a final-state target,
                # even when it matches during preview creation. The executor
                # can then repair a socket that changes between preview and
                # execution while still skipping live matches.
                socket_changes.append(socket_change)
                if (
                    socket_change["category_hash"]
                    == ARMOR_MOD_SOCKET_CATEGORY_HASH
                ):
                    armor_sockets_need_rebuild = True
            if armor_sockets_need_rebuild:
                socket_clears.extend(
                    self._armor_socket_clear_targets(
                        source,
                        item,
                        target_character_id,
                        item_name=saved_item["name"],
                    )
                )
            classifications.append(
                {
                    "name": saved_item["name"],
                    "instance_id": instance_id,
                    "bucket_name": saved_item["bucket_name"],
                    "location": location,
                    "icon_path": saved_item.get("icon_path", ""),
                }
            )
            if location == "vault":
                transfers.append(
                    {
                        "direction": "from_vault",
                        "item_instance_id": instance_id,
                        "item_hash": int(item["item_hash"]),
                        "target_character_id": target_character_id,
                    }
                )
            elif location == "another_character":
                if item["source_kind"] == "equipped":
                    blockers.append(
                        f"{saved_item['name']} is equipped on another character and cannot be transferred safely."
                    )
                elif int(item.get("transfer_status") or 0) & 2:
                    blockers.append(f"{saved_item['name']} is non-transferable.")
                else:
                    transfers.extend(
                        [
                            {
                                "direction": "to_vault",
                                "item_instance_id": instance_id,
                                "item_hash": int(item["item_hash"]),
                                "source_character_id": str(item["character_id"]),
                                "target_character_id": target_character_id,
                            },
                            {
                                "direction": "from_vault",
                                "item_instance_id": instance_id,
                                "item_hash": int(item["item_hash"]),
                                "target_character_id": target_character_id,
                            },
                        ]
                    )
            elif location in {"postmaster", "shared_inventory", "non_transferable"}:
                blockers.append(
                    f"{saved_item['name']} is at unsupported location {location.replace('_', ' ')}."
                )
            expected_items.append(
                {
                    "item_instance_id": instance_id,
                    "item_hash": int(saved_item["item_hash"]),
                    "bucket_hash": bucket_hash,
                    "name": saved_item["name"],
                    "plugs": saved_plugs,
                }
            )
        if weapon_exotics > 1:
            blockers.append("The pinned revision contains multiple Exotic weapons.")
        if armor_exotics > 1:
            blockers.append("The pinned revision contains multiple Exotic armor pieces.")
        if (
            len(expected_items) != len(REQUIRED_GAMEPLAY_BUCKETS)
            and not partial
        ):
            blockers.append("The pinned revision is incomplete.")
        raw_source = loadout.get("source_payload", {})
        identifiers = {
            "name_hash": valid_hash(raw_source.get("nameHash")),
            "icon_hash": (
                valid_hash(loadout.get("cover_icon_hash"))
                or valid_hash(raw_source.get("iconHash"))
            ),
            "color_hash": valid_hash(raw_source.get("colorHash")),
        }
        if not any(identifiers.values()):
            identifiers = {
                "name_hash": valid_hash(current_slot.get("nameHash")),
                "icon_hash": valid_hash(current_slot.get("iconHash")),
                "color_hash": valid_hash(current_slot.get("colorHash")),
            }
        return (
            {
                "kind": "replace",
                "slot_index": slot_index,
                "display_index": slot_index + 1,
                "encounter": (
                    {"id": encounter["encounter_id"], "name": encounter["name"], "order": encounter["encounter_order"]}
                    if encounter else None
                ),
                "assignment": (
                    {"id": assignment["assignment_id"], "order": assignment["assignment_order"], "notes": assignment["notes"]}
                    if assignment else None
                ),
                "loadout": {
                    "loadout_id": loadout["loadout_id"],
                    "revision_id": loadout["revision_id"],
                    "revision_number": loadout["revision_number"],
                    "name": loadout["name"],
                    "class_name": loadout["class_name"],
                },
                "current_slot": summarize_slot(current_slot),
                "items": expected_items,
                "partial": partial,
                "classifications": classifications,
                "transfers": transfers,
                "socket_clears": socket_clears,
                "socket_changes": socket_changes,
                "identifiers": identifiers,
                "label": f"{loadout['name']} → slot {slot_index + 1}",
            },
            dedupe(blockers),
        )

    def _aggregate_plan_capacity_blockers(
        self,
        source: dict[str, Any],
        target_character_id: str,
        slot_jobs: list[dict[str, Any]],
        *,
        retain_only_desired: bool = False,
    ) -> list[str]:
        """Plan reversible vault staging for full character buckets."""
        item_defs = self.manifest.resolve_many(
            "DestinyInventoryItemDefinition",
            (int(item["item_hash"]) for item in source["items"]),
        )
        carried: dict[int, list[dict[str, Any]]] = {}
        for item in source["items"]:
            if (
                item["source_kind"] != "character_inventory"
                or item.get("character_id") != target_character_id
                or int(item.get("bucket_hash") or 0)
                == POSTMASTER_BUCKET_HASH
            ):
                continue
            try:
                bucket = intended_bucket_hash(
                    item_defs.get(int(item["item_hash"]))
                )
            except LoadoutInspectionError:
                continue
            if bucket not in REQUIRED_GAMEPLAY_BUCKET_ORDER[:8]:
                continue
            carried.setdefault(bucket, []).append(item)
        incoming: dict[int, dict[str, dict[str, Any]]] = {}
        desired_ids = {
            str(item["item_instance_id"])
            for job in slot_jobs
            if job["kind"] == "replace"
            for item in job["items"]
        }
        equipped_by_bucket: dict[int, dict[str, Any]] = {}
        for item in source["items"]:
            if (
                item.get("source_kind") != "equipped"
                or item.get("character_id") != target_character_id
            ):
                continue
            try:
                bucket = intended_bucket_hash(
                    item_defs.get(int(item["item_hash"]))
                )
            except LoadoutInspectionError:
                continue
            equipped_by_bucket[bucket] = item
        for job in slot_jobs:
            if job["kind"] != "replace":
                continue
            transferred = {
                row["item_instance_id"]
                for row in job["transfers"]
                if row["direction"] == "from_vault"
            }
            for item in job["items"]:
                if item["item_instance_id"] in transferred:
                    incoming.setdefault(item["bucket_hash"], {})[
                        item["item_instance_id"]
                    ] = item
        blockers = []
        evacuations: list[dict[str, Any]] = []
        cleanup_to_vault: list[dict[str, Any]] = []
        non_desired_carried: dict[int, list[dict[str, Any]]] = {
            bucket: [
                item
                for item in items
                if str(item["item_instance_id"]) not in desired_ids
            ]
            for bucket, items in carried.items()
        }
        for bucket, incoming_items in incoming.items():
            if retain_only_desired:
                # Set applications calculate peak resident incoming items
                # after deciding which completed set items must return to the
                # vault. That produces the exact number of early evacuations.
                continue
            free = CHARACTER_BUCKET_CAPACITY - len(carried.get(bucket, []))
            needed = max(
                0,
                len(incoming_items) - free,
            )
            candidates = [
                item for item in non_desired_carried.get(bucket, [])
                if not (int(item.get("transfer_status") or 0) & 2)
            ]
            candidates.sort(key=lambda item: str(item["item_instance_id"]))
            selected = candidates[:needed]
            if len(selected) < needed:
                blockers.append(
                    f"Across this full plan, {GAMEPLAY_BUCKET_NAMES.get(bucket, str(bucket))} needs "
                    f"{len(incoming_items)} unique incoming item(s), but only "
                    f"{max(0, free + len(selected))} safe carried slot(s) can be made available."
                )
            evacuations.extend(
                {
                    "item_instance_id": str(item["item_instance_id"]),
                    "item_hash": int(item["item_hash"]),
                    "item_name": inventory_item_name(
                        item_defs.get(int(item["item_hash"]))
                    ),
                    "target_character_id": target_character_id,
                }
                for item in selected
            )
        if retain_only_desired:
            all_non_desired = [
                item
                for items in non_desired_carried.values()
                for item in items
            ]
            non_transferable = [
                item
                for item in all_non_desired
                if int(item.get("transfer_status") or 0) & 2
            ]
            if non_transferable:
                blockers.append(
                    f"{len(non_transferable)} carried non-set gameplay item(s) "
                    "cannot be moved to the vault."
                )
            evacuated_ids = {
                row["item_instance_id"] for row in evacuations
            }
            cleanup_to_vault.extend(
                {
                    "item_instance_id": str(item["item_instance_id"]),
                    "item_hash": int(item["item_hash"]),
                    "target_character_id": target_character_id,
                }
                for item in all_non_desired
                if str(item["item_instance_id"]) not in evacuated_ids
                and not (int(item.get("transfer_status") or 0) & 2)
            )
            desired_buckets = {
                int(item["bucket_hash"])
                for job in slot_jobs
                if job["kind"] == "replace"
                for item in job["items"]
            }
            for item in source["items"]:
                if (
                    item.get("source_kind") != "equipped"
                    or item.get("character_id") != target_character_id
                    or str(item["item_instance_id"]) in desired_ids
                ):
                    continue
                try:
                    bucket = intended_bucket_hash(
                        item_defs.get(int(item["item_hash"]))
                    )
                except LoadoutInspectionError:
                    continue
                if bucket not in desired_buckets:
                    continue
                if int(item.get("transfer_status") or 0) & 2:
                    blockers.append(
                        "An equipped non-set gameplay item that will be "
                        "displaced cannot be moved to the vault."
                    )
                    continue
                cleanup_to_vault.append(
                    {
                        "item_instance_id": str(item["item_instance_id"]),
                        "item_hash": int(item["item_hash"]),
                        "target_character_id": target_character_id,
                    }
                )
        cross_character_to_vault = {
            row["item_instance_id"]
            for job in slot_jobs
            if job["kind"] == "replace"
            for row in job["transfers"]
            if row["direction"] == "to_vault"
        }
        vault_definition = self.manifest.resolve_many(
            "DestinyInventoryBucketDefinition",
            (GENERAL_VAULT_BUCKET_HASH,),
        ).get(GENERAL_VAULT_BUCKET_HASH)
        configured_capacity = (
            vault_definition.get("itemCount")
            if isinstance(vault_definition, dict)
            else None
        )
        vault_capacity = (
            int(configured_capacity)
            if isinstance(configured_capacity, int)
            and configured_capacity > 0
            else FALLBACK_VAULT_CAPACITY
        )
        vault_items = sum(
            item.get("source_kind") == "vault"
            and int(item.get("bucket_hash") or 0)
            == GENERAL_VAULT_BUCKET_HASH
            for item in source["items"]
        )
        available_vault = max(0, vault_capacity - vault_items)
        preloads: list[dict[str, Any]] = []
        if not retain_only_desired:
            preload_needed = max(
                0,
                len(evacuations)
                + bool(cross_character_to_vault)
                - available_vault,
            )
            for bucket, incoming_items in incoming.items():
                free = CHARACTER_BUCKET_CAPACITY - len(carried.get(bucket, []))
                for instance_id, item in list(incoming_items.items())[:max(0, free)]:
                    preloads.append(
                        {
                            "item_instance_id": instance_id,
                            "item_hash": int(item["item_hash"]),
                            "target_character_id": target_character_id,
                        }
                    )
                    if len(preloads) >= preload_needed:
                        break
                if len(preloads) >= preload_needed:
                    break
        if not retain_only_desired and (
            len(evacuations) + bool(cross_character_to_vault)
            > available_vault + len(preloads)
        ):
            blockers.append(
                "The vault and currently free character slots cannot stage "
                "the required inventory moves safely."
            )
        desired_items_by_id = {
            str(item["item_instance_id"]): item
            for job in slot_jobs
            if job["kind"] == "replace"
            for item in job["items"]
        }
        desired_by_bucket: dict[int, set[str]] = {}
        last_use: dict[str, int] = {}
        for job_index, job in enumerate(slot_jobs):
            if job["kind"] != "replace":
                continue
            for item in job["items"]:
                instance_id = str(item["item_instance_id"])
                desired_by_bucket.setdefault(
                    int(item["bucket_hash"]), set()
                ).add(instance_id)
                last_use[instance_id] = job_index
        simulated_equipped = {
            bucket: str(item["item_instance_id"])
            for bucket, item in equipped_by_bucket.items()
        }
        return_after_job: dict[str, int] = {}
        if retain_only_desired:
            for job_index, job in enumerate(slot_jobs):
                if job["kind"] != "replace":
                    continue
                for item in job["items"]:
                    bucket = int(item["bucket_hash"])
                    previous = simulated_equipped.get(bucket)
                    if (
                        previous is not None
                        and previous != str(item["item_instance_id"])
                        and last_use.get(previous, job_index) < job_index
                    ):
                        return_after_job[previous] = job_index
                    simulated_equipped[bucket] = str(
                        item["item_instance_id"]
                    )
        overflow_returns: set[str] = set()
        if retain_only_desired:
            total_bucket_capacity = CHARACTER_BUCKET_CAPACITY + 1
            for bucket, instance_ids in desired_by_bucket.items():
                overflow = max(0, len(instance_ids) - total_bucket_capacity)
                if not overflow:
                    continue
                final_equipped = simulated_equipped.get(bucket)
                candidates = [
                    instance_id
                    for instance_id in instance_ids
                    if instance_id != final_equipped
                    and instance_id in return_after_job
                ]
                candidates.sort(
                    key=lambda instance_id: (
                        (item_by_instance(source, instance_id) or {}).get(
                            "source_kind"
                        ) != "vault",
                        last_use.get(instance_id, -1),
                        instance_id,
                    )
                )
                overflow_returns.update(candidates[:overflow])
                if len(candidates) < overflow:
                    blockers.append(
                        f"{GAMEPLAY_BUCKET_NAMES.get(bucket, str(bucket))} "
                        "contains more unique set items than the character can "
                        "hold safely."
                    )
            returns_by_job: dict[int, list[dict[str, Any]]] = {}
            for instance_id in overflow_returns:
                desired = desired_items_by_id[instance_id]
                returns_by_job.setdefault(
                    return_after_job[instance_id], []
                ).append(
                    {
                        "item_instance_id": instance_id,
                        "item_hash": int(desired["item_hash"]),
                        "target_character_id": target_character_id,
                    }
                )
            for job_index, returns in returns_by_job.items():
                slot_jobs[job_index]["post_snapshot_vault_returns"] = returns
            arrivals_seen: set[str] = set()
            resident_delta: dict[int, int] = {}
            peak_resident_delta: dict[int, int] = {}
            returns_by_index = {
                job_index: {
                    row["item_instance_id"] for row in returns
                }
                for job_index, returns in returns_by_job.items()
            }
            for job_index, job in enumerate(slot_jobs):
                if job["kind"] != "replace":
                    continue
                for transfer in job["transfers"]:
                    instance_id = str(transfer["item_instance_id"])
                    if (
                        transfer["direction"] != "from_vault"
                        or instance_id in arrivals_seen
                    ):
                        continue
                    arrivals_seen.add(instance_id)
                    bucket = int(
                        desired_items_by_id[instance_id]["bucket_hash"]
                    )
                    resident_delta[bucket] = (
                        resident_delta.get(bucket, 0) + 1
                    )
                    peak_resident_delta[bucket] = max(
                        peak_resident_delta.get(bucket, 0),
                        resident_delta[bucket],
                    )
                for instance_id in returns_by_index.get(job_index, set()):
                    bucket = int(
                        desired_items_by_id[instance_id]["bucket_hash"]
                    )
                    resident_delta[bucket] = (
                        resident_delta.get(bucket, 0) - 1
                    )
            evacuations = []
            for bucket, peak in peak_resident_delta.items():
                free = max(
                    0,
                    CHARACTER_BUCKET_CAPACITY
                    - len(carried.get(bucket, [])),
                )
                needed = max(0, peak - free)
                candidates = [
                    item
                    for item in non_desired_carried.get(bucket, [])
                    if not (int(item.get("transfer_status") or 0) & 2)
                ]
                candidates.sort(
                    key=lambda item: str(item["item_instance_id"])
                )
                selected = candidates[:needed]
                if len(selected) < needed:
                    blockers.append(
                        f"{GAMEPLAY_BUCKET_NAMES.get(bucket, str(bucket))} "
                        f"needs {needed} temporary carried slot(s), but only "
                        f"{len(selected)} can be made safely."
                    )
                evacuations.extend(
                    {
                        "item_instance_id": str(item["item_instance_id"]),
                        "item_hash": int(item["item_hash"]),
                        "item_name": inventory_item_name(
                            item_defs.get(int(item["item_hash"]))
                        ),
                        "target_character_id": target_character_id,
                    }
                    for item in selected
                )
            evacuated_ids = {
                row["item_instance_id"] for row in evacuations
            }
            cleanup_to_vault = [
                row
                for row in cleanup_to_vault
                if row["item_instance_id"] not in evacuated_ids
            ]
        retained_original_vault_items = {
            instance_id
            for instance_id in desired_ids
            if (item_by_instance(source, instance_id) or {}).get("source_kind")
            == "vault"
            and instance_id not in overflow_returns
        }
        returned_non_vault_items = {
            instance_id
            for instance_id in overflow_returns
            if (item_by_instance(source, instance_id) or {}).get("source_kind")
            != "vault"
        }
        final_vault_delta = (
            len(evacuations)
            + len(cleanup_to_vault)
            + len(returned_non_vault_items)
            - len(retained_original_vault_items)
        )
        vault_offloads: list[dict[str, Any]] = []
        if retain_only_desired:
            required_vault_space = max(
                len(evacuations) + bool(cross_character_to_vault),
                final_vault_delta,
            )
            offload_needed = max(0, required_vault_space - available_vault)
            vault_offloads = self._plan_vault_capacity_offloads(
                source,
                item_defs,
                target_character_id,
                desired_ids,
                offload_needed,
            )
            if len(vault_offloads) < offload_needed:
                blockers.append(
                    "The full vault needs "
                    f"{offload_needed} spare inventory slot(s) on another "
                    f"character, but only {len(vault_offloads)} safe slot(s) "
                    "are available."
                )
        replace_jobs = [job for job in slot_jobs if job["kind"] == "replace"]
        if replace_jobs:
            replace_jobs[0]["capacity_staging"] = {
                "vault_offloads": vault_offloads,
                "preloads": preloads,
                "evacuations": evacuations,
                "return_to_vault": [
                    {
                        "item_instance_id": instance_id,
                        "item_hash": int(item["item_hash"]),
                        "target_character_id": target_character_id,
                    }
                    for bucket in incoming.values()
                    for instance_id, item in bucket.items()
                ] if not retain_only_desired else [],
                "restorations": evacuations if not retain_only_desired else [],
                "cleanup_to_vault": cleanup_to_vault,
                "retain_only_desired": retain_only_desired,
            }
        return blockers

    def _plan_vault_capacity_offloads(
        self,
        source: dict[str, Any],
        item_defs: dict[int, dict[str, Any]],
        target_character_id: str,
        desired_ids: set[str],
        needed: int,
    ) -> list[dict[str, Any]]:
        """Use spare inventory on other characters when the vault is full."""

        if needed <= 0:
            return []
        carried_counts: dict[tuple[str, int], int] = {}
        for item in source["items"]:
            character_id = str(item.get("character_id") or "")
            if (
                item.get("source_kind") != "character_inventory"
                or not character_id
                or character_id == target_character_id
                or int(item.get("bucket_hash") or 0)
                == POSTMASTER_BUCKET_HASH
            ):
                continue
            try:
                bucket = intended_bucket_hash(
                    item_defs.get(int(item["item_hash"]))
                )
            except LoadoutInspectionError:
                continue
            if bucket in REQUIRED_GAMEPLAY_BUCKET_ORDER[:8]:
                key = (character_id, bucket)
                carried_counts[key] = carried_counts.get(key, 0) + 1
        destinations: dict[int, list[str]] = {}
        for character in source["characters"]:
            character_id = str(character["character_id"])
            if character_id == target_character_id:
                continue
            for bucket in REQUIRED_GAMEPLAY_BUCKET_ORDER[:8]:
                free = max(
                    0,
                    CHARACTER_BUCKET_CAPACITY
                    - carried_counts.get((character_id, bucket), 0),
                )
                destinations.setdefault(bucket, []).extend(
                    [character_id] * free
                )
        candidates: dict[int, list[dict[str, Any]]] = {}
        for item in source["items"]:
            instance_id = str(item.get("item_instance_id") or "")
            if (
                item.get("source_kind") != "vault"
                or not instance_id
                or instance_id in desired_ids
                or int(item.get("transfer_status") or 0) & 2
            ):
                continue
            try:
                bucket = intended_bucket_hash(
                    item_defs.get(int(item["item_hash"]))
                )
            except LoadoutInspectionError:
                continue
            if bucket in destinations:
                candidates.setdefault(bucket, []).append(item)
        planned: list[dict[str, Any]] = []
        for bucket in REQUIRED_GAMEPLAY_BUCKET_ORDER[:8]:
            slots = destinations.get(bucket, [])
            items = sorted(
                candidates.get(bucket, []),
                key=lambda item: str(item["item_instance_id"]),
            )
            for character_id, item in zip(slots, items, strict=False):
                planned.append(
                    {
                        "item_instance_id": str(item["item_instance_id"]),
                        "item_hash": int(item["item_hash"]),
                        "target_character_id": character_id,
                    }
                )
                if len(planned) >= needed:
                    return planned
        return planned

    def _original_equipment(self, source: dict[str, Any], character_id: str) -> dict[str, Any]:
        wrapper = source["profile"].get("characterEquipment", {}).get("data", {}).get(character_id)
        active = {str(item["item_instance_id"]): item for item in source["items"]}
        definitions = self.manifest.resolve_many(
            "DestinyInventoryItemDefinition", (int(item["item_hash"]) for item in source["items"])
        )
        items = []
        issues = []
        for raw in component_items(wrapper):
            instance_id = valid_instance_id(raw.get("itemInstanceId"))
            item = active.get(instance_id or "")
            if item is None:
                continue
            definition = definitions.get(int(item["item_hash"]))
            try:
                bucket = intended_bucket_hash(definition)
            except LoadoutInspectionError:
                continue
            if bucket not in REQUIRED_GAMEPLAY_BUCKETS:
                continue
            items.append(
                {
                    "item_instance_id": instance_id,
                    "item_hash": int(item["item_hash"]),
                    "bucket_hash": bucket,
                    "plugs": [
                        {"socket_index": index, "plug_hash": plug, "filtered": False}
                        for index, plug in enumerate(component_plug_hashes(item.get("components")))
                    ],
                }
            )
        if {item["bucket_hash"] for item in items} != REQUIRED_GAMEPLAY_BUCKETS:
            issues.append("Original equipment is incomplete, so safe restoration cannot be guaranteed.")
        items.sort(key=lambda item: REQUIRED_GAMEPLAY_BUCKET_ORDER.index(item["bucket_hash"]))
        return {"items": items, "issues": issues}

    def _activity_blockers(self, source: dict[str, Any], character_id: str) -> list[str]:
        profile = source["profile"]
        transitory = profile.get("profileTransitoryData")
        if (
            isinstance(transitory, dict)
            and "privacy" in transitory
            and not isinstance(transitory.get("data"), dict)
        ):
            # Bungie can retain the last CharacterActivities record after a
            # player logs out. On an authenticated Transitory request, a
            # returned wrapper with no live data is the available offline
            # signal and takes precedence over that stale activity record.
            return []
        component = (
            profile.get("characterActivities", {})
            .get("data", {})
            .get(character_id)
        )
        if not isinstance(component, dict):
            return ["Live character activity state is unavailable."]
        activity_hash = valid_hash(component.get("currentActivityHash"))
        modes = component.get("currentActivityModeTypes")
        mode_values = modes if isinstance(modes, list) else []
        if activity_hash is None or SOCIAL_ACTIVITY_MODE_TYPE in mode_values:
            return []

        activity = self.manifest.resolve_many(
            "DestinyActivityDefinition", (activity_hash,)
        ).get(activity_hash)
        if isinstance(activity, dict):
            definition_modes = activity.get("activityModeTypes")
            if (
                isinstance(definition_modes, list)
                and SOCIAL_ACTIVITY_MODE_TYPE in definition_modes
            ):
                return []
            place_hash = valid_hash(activity.get("placeHash"))
            if place_hash in KNOWN_ORBIT_PLACE_HASHES:
                return []
            if place_hash is not None:
                place = self.manifest.resolve_many(
                    "DestinyPlaceDefinition", (place_hash,)
                ).get(place_hash)
                display = (
                    place.get("displayProperties", {})
                    if isinstance(place, dict)
                    else {}
                )
                if (
                    isinstance(display, dict)
                    and str(display.get("name") or "").strip().casefold()
                    == "orbit"
                ):
                    return []
        return [
            "The character is in an activity. Move to orbit, a social space, "
            "or offline before confirming."
        ]

    def _finalize_action_plan(
        self,
        source: dict[str, Any],
        character: dict[str, Any],
        slot_jobs: list[dict[str, Any]],
        original: dict[str, Any],
        *,
        title: str,
        activity_name: str | None,
    ) -> dict[str, Any]:
        actions: list[dict[str, Any]] = []
        capacity_staging = next(
            (
                job["capacity_staging"]
                for job in slot_jobs
                if job.get("capacity_staging")
            ),
            {
                "vault_offloads": [],
                "preloads": [],
                "evacuations": [],
                "return_to_vault": [],
                "restorations": [],
                "cleanup_to_vault": [],
                "retain_only_desired": False,
            },
        )
        for offload in capacity_staging["vault_offloads"]:
            actions.append(
                {
                    "action_type": "transfer_from_vault",
                    "phase": (
                        "Using spare inventory on another character to make "
                        "vault space"
                    ),
                    "item_instance_id": offload["item_instance_id"],
                    "request": {
                        **offload,
                        "mode": "vault_capacity_offload",
                    },
                    "expected": {},
                }
            )
        for preload in capacity_staging["preloads"]:
            actions.append(
                {
                    "action_type": "transfer_from_vault",
                    "phase": "Preloading a set item to make vault space",
                    "item_instance_id": preload["item_instance_id"],
                    "request": preload,
                    "expected": {},
                }
            )
        for staged in capacity_staging["evacuations"]:
            actions.append(
                {
                    "action_type": "transfer_to_vault",
                    "phase": (
                        "Making temporary character inventory space"
                        + (
                            f": {staged['item_name']}"
                            if staged.get("item_name")
                            else ""
                        )
                    ),
                    "item_instance_id": staged["item_instance_id"],
                    "request": {**staged, "mode": "capacity_stage"},
                    "expected": {},
                }
            )
        for job in slot_jobs:
            encounter_id = (job.get("encounter") or {}).get("id")
            assignment_id = (job.get("assignment") or {}).get("id")
            slot = int(job["slot_index"])
            if job["kind"] == "clear":
                actions.extend(
                    [
                        action("clear_slot", "Clearing unassigned slot", slot=slot),
                        action("verify_clear", "Verifying empty slot", slot=slot),
                    ]
                )
                continue
            common = {"encounter_id": encounter_id, "assignment_id": assignment_id, "target_slot_index": slot}
            for transfer in job["transfers"]:
                direction = transfer["direction"]
                actions.append(
                    {
                        "action_type": "transfer_to_vault" if direction == "to_vault" else "transfer_from_vault",
                        "phase": "Preparing exact items",
                        **common,
                        "item_instance_id": transfer["item_instance_id"],
                        "request": transfer,
                        "expected": {},
                    }
                )
            item_ids = [item["item_instance_id"] for item in job["items"]]
            actions.append(
                {
                    "action_type": "equip",
                    "phase": (
                        "Preparing all equipment and sockets in parallel"
                    ),
                    **common,
                    "request": {
                        "mode": "parallel_prepare",
                        "item_instance_ids": item_ids,
                        "socket_clears": job.get("socket_clears", []),
                        "socket_changes": job["socket_changes"],
                    },
                    "expected": {"items": job["items"]},
                }
            )
            actions.extend(
                [
                    {"action_type": "snapshot", "phase": "Snapshotting selected slot", **common, "request": job["identifiers"], "expected": {"items": job["items"], "partial": bool(job.get("partial"))}},
                ]
            )
            if any(job["identifiers"].values()):
                actions.append(
                    {"action_type": "identifiers", "phase": "Applying slot identifiers", **common, "request": {}, "expected": job["identifiers"]}
                )
            actions.append(
                {"action_type": "verify_slot", "phase": "Verifying in-game slot", **common, "request": {}, "expected": {"items": job["items"], "partial": bool(job.get("partial")), "identifiers": job["identifiers"] if any(job["identifiers"].values()) else {}}}
            )
            for returned in job.get("post_snapshot_vault_returns", []):
                actions.append(
                    {
                        "action_type": "transfer_to_vault",
                        "phase": "Returning a completed set item to the vault",
                        **common,
                        "item_instance_id": returned["item_instance_id"],
                        "request": {
                            **returned,
                            "mode": "restore_vault_origin",
                        },
                        "expected": {},
                    }
                )
        if not capacity_staging["retain_only_desired"]:
            restore_ids = [
                item["item_instance_id"] for item in original["items"]
            ]
            for replacement_group, replacement_label in (
                ("weapon", "weapon"),
                ("armor", "armor"),
            ):
                actions.append(
                    {
                        "action_type": "equip",
                        "phase": (
                            f"Replacing Exotic {replacement_label} before "
                            f"equipment restoration"
                        ),
                        "request": {
                            "mode": "exotic_replacements",
                            "group": replacement_group,
                        },
                        "expected": {"items": original["items"]},
                    }
                )
            actions.append(
                {
                    "action_type": "verify_prepared",
                    "phase": "Verifying restoration Exotic replacements",
                    "request": {
                        "mode": "exotic_replacements",
                        "group": "all",
                    },
                    "expected": {"items": original["items"]},
                }
            )
            actions.extend(
                [
                    {"action_type": "restore_equipment", "phase": "Restoring original equipment", "request": {"item_instance_ids": restore_ids}, "expected": {"items": original["items"]}},
                    {"action_type": "verify_restored", "phase": "Verifying restored equipment", "request": {}, "expected": {"items": original["items"]}},
                ]
            )
        for cleanup in capacity_staging["cleanup_to_vault"]:
            actions.append(
                {
                    "action_type": "transfer_to_vault",
                    "phase": "Moving non-set gameplay item to the vault",
                    "item_instance_id": cleanup["item_instance_id"],
                    "request": {**cleanup, "mode": "retain_set_only"},
                    "expected": {},
                }
            )
        for returned in capacity_staging["return_to_vault"]:
            actions.append(
                {
                    "action_type": "transfer_to_vault",
                    "phase": "Returning loadout item to its original vault location",
                    "item_instance_id": returned["item_instance_id"],
                    "request": {**returned, "mode": "restore_vault_origin"},
                    "expected": {},
                }
            )
        for restored in capacity_staging["restorations"]:
            actions.append(
                {
                    "action_type": "transfer_from_vault",
                    "phase": "Restoring temporary character inventory staging",
                    "item_instance_id": restored["item_instance_id"],
                    "request": restored,
                    "expected": {},
                }
            )
        write_types = {
            "transfer_to_vault",
            "transfer_from_vault",
            "equip",
            "snapshot",
            "identifiers",
            "clear_slot",
            "restore_equipment",
            "insert_socket_plug",
        }
        write_count = sum(row["action_type"] in write_types for row in actions)
        read_count = sum(row["action_type"] not in write_types for row in actions) + write_count
        minimum = sum(
            TRANSFER_INTERVAL if row["action_type"].startswith("transfer_") else EQUIP_INTERVAL if row["action_type"] in {"equip", "restore_equipment"} else SOCKET_INTERVAL if row["action_type"] == "insert_socket_plug" else LOADOUT_INTERVAL if row["action_type"] in {"snapshot", "identifiers", "clear_slot"} else 0
            for row in actions
        )
        return {
            "title": title,
            "activity_name": activity_name,
            "target_character_id": str(character["character_id"]),
            "target_character_name": CLASS_NAMES.get(character.get("class_type"), "Guardian"),
            "slot_capacity": len(source["profile"].get("characterLoadouts", {}).get("data", {}).get(str(character["character_id"]), {}).get("loadouts", [])),
            "slot_jobs": slot_jobs,
            "original_equipment": original["items"],
            "actions": actions,
            "write_request_count": write_count,
            "read_request_count": read_count,
            "request_count": write_count + read_count,
            "minimum_throttle_seconds": round(minimum, 1),
            "eligibility": "Character must be in orbit, a social space, or offline.",
            "inventory_policy": (
                "set_items_only"
                if capacity_staging["retain_only_desired"]
                else "restore_original"
            ),
            "initial_state_fingerprint": state_fingerprint(source),
        }

    def _persist_preview(
        self,
        owner: str,
        *,
        preview_type: str,
        source_entity_id: str,
        source_revision_id: str,
        target_character_id: str,
        target_slot_index: int | None,
        source: dict[str, Any],
        action_plan: dict[str, Any],
        blockers: list[str],
    ) -> dict[str, Any]:
        preview_id = secrets.token_hex(16)
        now = utc_now()
        status = "blocked" if blockers else "ready"
        warnings = [
            "Saved armor-mod and subclass differences that Bungie currently "
            "reports as free and insertable are applied and verified before "
            "the loadout snapshot. Empty plugs are preserved; unsupported "
            "socket differences remain blockers.",
            "Before each complete equip, the saved items for any slots "
            "currently occupied by Exotic weapon or armor pieces are "
            "equipped and verified first.",
        ]
        if any(job.get("partial") for job in action_plan["slot_jobs"]):
            warnings.append(
                "Partial imported loadouts intentionally leave unspecified "
                "items and blank sockets unchanged while preparing the slot."
            )
        if action_plan.get("inventory_policy") == "set_items_only":
            warnings.append(
                "After rebuilding the set, transferable carried weapon and "
                "armor items not used anywhere in the set are moved to the "
                "vault. The highest occupied board position remains equipped."
            )
        vault_offloads = sum(
            action.get("request", {}).get("mode")
            == "vault_capacity_offload"
            for action in action_plan["actions"]
        )
        if vault_offloads:
            warnings.append(
                f"Because the vault is full, {vault_offloads} transferable "
                "vault item(s) are moved into spare inventory slots on other "
                "characters. They remain there after this set finishes."
            )
        validation = {
            "blockers": dedupe(blockers),
            "warnings": warnings,
        }
        manifest_version = str(self.manifest.status()["version"])
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO loadout_previews (
                    preview_id, bungie_membership_id, preview_type,
                    source_entity_id, source_revision_id,
                    target_character_id, target_slot_index, snapshot_id,
                    manifest_version, state_fingerprint, status,
                    action_plan_json, validation_json, request_count,
                    minimum_throttle_seconds, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    preview_id, owner, preview_type, source_entity_id,
                    source_revision_id, target_character_id, target_slot_index,
                    source["snapshot"]["snapshot_id"], manifest_version,
                    state_fingerprint(source), status, compact_json(action_plan),
                    compact_json(validation), action_plan["request_count"],
                    action_plan["minimum_throttle_seconds"], as_iso(now),
                    as_iso(now + PREVIEW_LIFETIME),
                ),
            )
        result = self.preview(owner, preview_id)
        if result is None:
            raise RuntimeError("The preview could not be loaded.")
        return result

    def _set_preview_status(self, owner: str, preview_id: str, status: str) -> None:
        with self.database.connection() as connection:
            connection.execute(
                "UPDATE loadout_previews SET status = ? WHERE bungie_membership_id = ? AND preview_id = ?",
                (status, owner, preview_id),
            )

    def _recover_interrupted(self) -> None:
        now = as_iso(utc_now())
        retention_cutoff = as_iso(utc_now() - timedelta(days=1))
        with self.database.connection() as connection:
            connection.execute(
                """
                UPDATE loadout_sync_action_attempts
                SET status = 'failed',
                    message = 'Server stopped before this attempt completed.',
                    completed_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE loadout_sync_operations
                SET status = 'paused', updated_at = ?,
                    last_error = 'The server stopped during this operation. Review live state and explicitly resume.'
                WHERE status = 'running'
                """,
                (now,),
            )
            connection.execute(
                "UPDATE loadout_sync_actions SET status = 'pending' WHERE status = 'running'"
            )
            connection.execute(
                """
                UPDATE loadout_previews SET status = 'expired'
                WHERE status IN ('ready', 'blocked') AND expires_at <= ?
                """,
                (now,),
            )
            connection.execute(
                """
                DELETE FROM loadout_previews
                WHERE status IN ('expired', 'invalidated')
                  AND created_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM loadout_sync_operations AS operation
                      WHERE operation.preview_id = loadout_previews.preview_id
                  )
                """,
                (retention_cutoff,),
            )

    def _prepare_resume(self, owner: str, operation_id: str) -> None:
        now = as_iso(utc_now())
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            action_rows = connection.execute(
                """
                SELECT action_index, action_type, target_slot_index, status
                FROM loadout_sync_actions
                WHERE operation_id = ? ORDER BY action_index
                """,
                (operation_id,),
            ).fetchall()
            by_index = {
                int(row["action_index"]): dict(row) for row in action_rows
            }
            replay_indexes = {
                int(row["action_index"])
                for row in action_rows
                if row["status"] == "failed"
            }
            paired_write = {
                "verify_prepared": {"equip"},
                "verify_socket_plug": {"insert_socket_plug"},
                "verify_clear": {"clear_slot"},
                "verify_restored": {"restore_equipment"},
            }
            for failed_index in tuple(replay_indexes):
                failed = by_index[failed_index]
                allowed = paired_write.get(str(failed["action_type"]))
                previous = by_index.get(failed_index - 1)
                if (
                    allowed
                    and previous is not None
                    and previous["action_type"] in allowed
                ):
                    replay_indexes.add(failed_index - 1)
                if failed["action_type"] == "verify_slot":
                    cursor = failed_index - 1
                    while cursor in by_index:
                        candidate = by_index[cursor]
                        if (
                            candidate["target_slot_index"]
                            != failed["target_slot_index"]
                            or candidate["action_type"]
                            not in {"snapshot", "identifiers"}
                        ):
                            break
                        replay_indexes.add(cursor)
                        cursor -= 1
            if replay_indexes:
                placeholders = ",".join("?" for _ in replay_indexes)
                connection.execute(
                    f"""
                    UPDATE loadout_sync_actions
                    SET status = 'pending', completed_at = NULL
                    WHERE operation_id = ?
                      AND action_index IN ({placeholders})
                    """,
                    (operation_id, *sorted(replay_indexes)),
                )
            changed = connection.execute(
                """
                UPDATE loadout_sync_operations
                SET status = 'pending', last_error = NULL,
                    recovery_json = '{}', updated_at = ?,
                    completed_actions = (
                        SELECT COUNT(*) FROM loadout_sync_actions AS action
                        WHERE action.operation_id = loadout_sync_operations.operation_id
                          AND action.status = 'completed'
                    )
                WHERE bungie_membership_id = ? AND operation_id = ?
                  AND status IN ('paused', 'failed')
                """,
                (now, owner, operation_id),
            ).rowcount
        if not changed:
            raise LoadoutOperationError("The operation cannot be resumed.")

    def _set_operation_running(self, owner: str, operation_id: str) -> None:
        now = as_iso(utc_now())
        with self.database.connection() as connection:
            connection.execute(
                """
                UPDATE loadout_sync_operations
                SET status = 'running', started_at = COALESCE(started_at, ?),
                    updated_at = ?
                WHERE bungie_membership_id = ? AND operation_id = ?
                  AND status IN ('pending', 'running')
                """,
                (now, now, owner, operation_id),
            )

    def _start_action_attempt(self, operation_id: str, index: int) -> int:
        now = as_iso(utc_now())
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE loadout_sync_actions
                SET status = 'running', attempts = attempts + 1,
                    started_at = COALESCE(started_at, ?)
                WHERE operation_id = ? AND action_index = ?
                """,
                (now, operation_id, index),
            )
            attempt_number = int(
                connection.execute(
                    """
                    SELECT attempts FROM loadout_sync_actions
                    WHERE operation_id = ? AND action_index = ?
                    """,
                    (operation_id, index),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO loadout_sync_action_attempts (
                    operation_id, action_index, attempt_number,
                    status, started_at
                ) VALUES (?, ?, ?, 'running', ?)
                """,
                (operation_id, index, attempt_number, now),
            )
        return attempt_number

    def _record_action_error(
        self,
        operation_id: str,
        index: int,
        attempt_number: int,
        error: Exception,
    ) -> None:
        values = error_evidence(error)
        now = as_iso(utc_now())
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE loadout_sync_actions
                SET last_http_status = ?, last_error_code = ?,
                    last_error_status = ?, last_message = ?,
                    throttle_seconds = ?
                WHERE operation_id = ? AND action_index = ?
                """,
                (*values, operation_id, index),
            )
            connection.execute(
                """
                UPDATE loadout_sync_action_attempts
                SET status = 'failed', http_status = ?, error_code = ?,
                    error_status = ?, message = ?, throttle_seconds = ?,
                    completed_at = ?
                WHERE operation_id = ? AND action_index = ?
                  AND attempt_number = ?
                """,
                (*values, now, operation_id, index, attempt_number),
            )

    def _record_action_evidence(
        self,
        operation_id: str,
        index: int,
        attempt_number: int,
        evidence: dict[str, Any],
    ) -> None:
        now = as_iso(utc_now())
        values = (
            evidence.get("http_status"),
            evidence.get("error_code"),
            evidence.get("error_status", ""),
            str(evidence.get("message", ""))[:500],
            float(evidence.get("throttle_seconds", 0) or 0),
        )
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE loadout_sync_actions
                SET last_http_status = ?, last_error_code = ?,
                    last_error_status = ?, last_message = ?,
                    throttle_seconds = ?
                WHERE operation_id = ? AND action_index = ?
                """,
                (*values, operation_id, index),
            )
            connection.execute(
                """
                UPDATE loadout_sync_action_attempts
                SET status = 'succeeded', http_status = ?, error_code = ?,
                    error_status = ?, message = ?, throttle_seconds = ?,
                    completed_at = ?
                WHERE operation_id = ? AND action_index = ?
                  AND attempt_number = ?
                """,
                (*values, now, operation_id, index, attempt_number),
            )

    def _complete_action(self, owner: str, operation_id: str, action: dict[str, Any]) -> None:
        now = as_iso(utc_now())
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE loadout_sync_actions
                SET status = 'completed', completed_at = ?
                WHERE operation_id = ? AND action_index = ?
                """,
                (now, operation_id, action["action_index"]),
            )
            completed = connection.execute(
                "SELECT COUNT(*) FROM loadout_sync_actions WHERE operation_id = ? AND status IN ('completed', 'skipped')",
                (operation_id,),
            ).fetchone()[0]
            connection.execute(
                """
                UPDATE loadout_sync_operations
                SET completed_actions = ?, current_action_index = ?, updated_at = ?
                WHERE bungie_membership_id = ? AND operation_id = ?
                """,
                (completed, action["action_index"] + 1, now, owner, operation_id),
            )

    def _fail_action(self, owner: str, operation_id: str, action: dict[str, Any], error: Exception) -> None:
        now = as_iso(utc_now())
        message = " ".join(str(error).split())[:500]
        recovery = {
            "failed_action_index": action["action_index"],
            "failed_phase": action["phase"],
            "reason": message,
            "next_step": (
                "Refresh this report, resolve the stated condition, then use "
                "Resume. Completed checkpoints remain durable; when a "
                "verification fails, its paired idempotent write is rechecked "
                "and replayed only if still needed."
            ),
        }
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE loadout_sync_actions
                SET status = 'failed', completed_at = ?, last_message = ?
                WHERE operation_id = ? AND action_index = ?
                """,
                (now, message, operation_id, action["action_index"]),
            )
            connection.execute(
                """
                UPDATE loadout_sync_operations
                SET status = 'failed', last_error = ?, recovery_json = ?,
                    updated_at = ?
                WHERE bungie_membership_id = ? AND operation_id = ?
                """,
                (message, compact_json(recovery), now, owner, operation_id),
            )

    def _fail_resume_audit(
        self,
        owner: str,
        operation_id: str,
        error: Exception,
    ) -> None:
        now = as_iso(utc_now())
        message = " ".join(str(error).split())[:500]
        recovery = {
            "failed_phase": "Resume state comparison",
            "reason": message,
            "next_step": (
                "Inspect the reported slot in Destiny. Create a new preview "
                "if the completed result was intentionally changed."
            ),
        }
        with self.database.connection() as connection:
            connection.execute(
                """
                UPDATE loadout_sync_operations
                SET status = 'failed', last_error = ?, recovery_json = ?,
                    updated_at = ?
                WHERE bungie_membership_id = ? AND operation_id = ?
                """,
                (message, compact_json(recovery), now, owner, operation_id),
            )

    def _complete_operation(self, owner: str, operation_id: str) -> None:
        now = as_iso(utc_now())
        operation = self.operation(owner, operation_id)
        if operation is None:
            return
        report = {
            "completed_actions": operation["completed_actions"],
            "total_actions": operation["total_actions"],
            "slots": [
                {
                    "kind": job["kind"],
                    "slot_index": job["slot_index"],
                    "label": job["label"],
                    "encounter": job.get("encounter"),
                    "status": "succeeded",
                }
                for job in operation["action_plan"]["slot_jobs"]
            ],
            "original_equipment": "restored",
        }
        with self.database.connection() as connection:
            connection.execute(
                """
                UPDATE loadout_sync_operations
                SET status = 'completed', result_json = ?, recovery_json = '{}',
                    last_error = NULL, completed_actions = total_actions,
                    completed_at = ?, updated_at = ?
                WHERE bungie_membership_id = ? AND operation_id = ?
                """,
                (compact_json(report), now, now, owner, operation_id),
            )


def action(action_type: str, phase: str, *, slot: int) -> dict[str, Any]:
    return {"action_type": action_type, "phase": phase, "target_slot_index": slot, "request": {}, "expected": {}}


def state_fingerprint(source: dict[str, Any]) -> str:
    profile = source["profile"]
    transitory = profile.get("profileTransitoryData")
    payload = {
        "membership_type": source["snapshot"].get("membership_type"),
        "items": [
            {
                "id": str(item["item_instance_id"]),
                "hash": int(item["item_hash"]),
                "source": item["source_kind"],
                "character": item.get("character_id"),
                "state": int(item.get("state") or 0),
                "transfer": int(item.get("transfer_status") or 0),
                "plugs": component_plug_hashes(item.get("components")),
            }
            for item in sorted(source["items"], key=lambda row: str(row["item_instance_id"]))
        ],
        "equipment": profile.get("characterEquipment", {}).get("data", {}),
        "loadouts": profile.get("characterLoadouts", {}).get("data", {}),
        "activities": profile.get("characterActivities", {}).get("data", {}),
        "transitory_live_data": (
            isinstance(transitory, dict)
            and isinstance(transitory.get("data"), dict)
        ),
    }
    return hashlib.sha256(compact_json(payload).encode("utf-8")).hexdigest()


def classify_location(item: dict[str, Any], target_character_id: str) -> str:
    source = item.get("source_kind")
    if source == "equipped" and item.get("character_id") == target_character_id:
        return "already_equipped"
    if source == "character_inventory" and item.get("character_id") == target_character_id:
        return "target_character"
    if source == "vault":
        return "vault"
    if source in {"equipped", "character_inventory"}:
        return "another_character"
    if source == "postmaster":
        return "postmaster"
    if source == "profile_inventory":
        return "shared_inventory"
    return "non_transferable"


def item_by_instance(source: dict[str, Any], instance_id: str) -> dict[str, Any] | None:
    return next((item for item in source["items"] if str(item["item_instance_id"]) == str(instance_id)), None)


def inventory_item_name(definition: dict[str, Any] | None) -> str:
    display = (
        definition.get("displayProperties")
        if isinstance(definition, dict)
        else None
    )
    name = display.get("name") if isinstance(display, dict) else None
    return str(name).strip() if isinstance(name, str) else ""


def equip_failure_is_transient(reason: int, *, location: str) -> bool:
    """Recognize equip states that this operation resolves before equipping."""

    if reason <= 0:
        return False
    transient_reasons = 2  # ItemUniqueEquipRestricted.
    if location in {"vault", "another_character"}:
        # Bungie commonly adds ItemWrapped (16) while an otherwise equippable
        # item is outside the target character. The planned transfer makes
        # Bungie re-evaluate that state before the equip request.
        transient_reasons |= 16
    return reason & ~transient_reasons == 0


def plug_is_live_insertable(
    source: dict[str, Any],
    item: dict[str, Any],
    character_id: str,
    *,
    socket_index: int,
    plug_hash: int,
    socket_entry: dict[str, Any],
) -> bool:
    """Require Bungie's live plug-set evidence before planning a free write."""
    candidate_rows: list[dict[str, Any]] = []
    reusable = item.get("components", {}).get("reusablePlugs", {})
    reusable_by_socket = (
        reusable.get("plugs") if isinstance(reusable, dict) else None
    )
    if isinstance(reusable_by_socket, dict):
        rows = reusable_by_socket.get(str(socket_index))
        if rows is None:
            rows = reusable_by_socket.get(socket_index)
        if isinstance(rows, list):
            candidate_rows.extend(
                row for row in rows if isinstance(row, dict)
            )

    set_hashes = {
        value
        for field in ("reusablePlugSetHash", "randomizedPlugSetHash")
        if (value := valid_hash(socket_entry.get(field))) is not None
    }
    profile = source.get("profile", {})
    profile_sets = (
        profile.get("profilePlugSets", {}).get("data", {}).get("plugs", {})
        if isinstance(profile, dict)
        else {}
    )
    character_sets = (
        profile.get("characterPlugSets", {})
        .get("data", {})
        .get(character_id, {})
        .get("plugs", {})
        if isinstance(profile, dict)
        else {}
    )
    for plug_sets in (profile_sets, character_sets):
        if not isinstance(plug_sets, dict):
            continue
        for set_hash in set_hashes:
            rows = plug_sets.get(str(set_hash))
            if rows is None:
                rows = plug_sets.get(set_hash)
            if isinstance(rows, list):
                candidate_rows.extend(
                    row for row in rows if isinstance(row, dict)
                )

    return any(
        valid_hash(row.get("plugItemHash")) == plug_hash
        and row.get("canInsert") is True
        and row.get("enabled") is not False
        for row in candidate_rows
    )


def character_slot(source: dict[str, Any], character_id: str, slot_index: int) -> dict[str, Any]:
    component = source["profile"].get("characterLoadouts", {}).get("data", {}).get(character_id)
    slots = component.get("loadouts") if isinstance(component, dict) else None
    if not isinstance(slots, list) or slot_index < 0 or slot_index >= len(slots):
        raise LoadoutOperationError("The target in-game slot is unavailable.")
    slot = slots[slot_index]
    return slot if isinstance(slot, dict) else {}


def slot_empty(slot: dict[str, Any]) -> bool:
    items = slot.get("items") if isinstance(slot, dict) else None
    return not isinstance(items, list) or not items


def slot_matches(
    slot: dict[str, Any],
    expected_items: list[dict[str, Any]],
    *,
    allow_extra: bool = False,
) -> bool:
    return not slot_mismatch_reasons(
        slot, expected_items, allow_extra=allow_extra
    )


def slot_mismatch_reasons(
    slot: dict[str, Any],
    expected_items: list[dict[str, Any]],
    *,
    allow_extra: bool = False,
) -> list[str]:
    raw_items = slot.get("items") if isinstance(slot, dict) else None
    if not isinstance(raw_items, list):
        return ["Bungie returned no item list"]
    actual = {
        str(item.get("itemInstanceId")): item
        for item in raw_items if isinstance(item, dict) and valid_instance_id(item.get("itemInstanceId"))
    }
    expected_ids = {
        str(item["item_instance_id"]) for item in expected_items
    }
    differences = []
    missing = expected_ids - set(actual)
    unexpected = set(actual) - expected_ids
    if missing:
        differences.append(
            "missing item(s) " + ", ".join(sorted(missing))
        )
    if unexpected and not allow_extra:
        differences.append(
            "unexpected item(s) " + ", ".join(sorted(unexpected))
        )
    for expected in expected_items:
        instance_id = str(expected["item_instance_id"])
        raw = actual.get(instance_id)
        if raw is None:
            continue
        plugs = raw.get("plugItemHashes")
        actual_plugs = plugs if isinstance(plugs, list) else []
        for plug in expected.get("plugs", []):
            if plug.get("filtered") or plug.get("plug_hash") in (
                None,
                INVALID_HASH_SENTINEL,
            ):
                continue
            index = int(plug["socket_index"])
            current = valid_hash(actual_plugs[index]) if index < len(actual_plugs) else None
            if current != plug.get("plug_hash"):
                differences.append(
                    f"{expected.get('name') or instance_id} socket "
                    f"{index + 1} stored {current}, expected "
                    f"{plug.get('plug_hash')}"
                )
    return differences


def missing_equipment_labels(
    source: dict[str, Any],
    character_id: str,
    expected_items: list[dict[str, Any]],
) -> list[str]:
    wrapper = (
        source["profile"]
        .get("characterEquipment", {})
        .get("data", {})
        .get(character_id)
    )
    equipped_ids = {
        str(item.get("itemInstanceId")) for item in component_items(wrapper)
    }
    return [
        str(item.get("name") or item["item_instance_id"])
        for item in expected_items
        if str(item["item_instance_id"]) not in equipped_ids
    ]


def equipment_matches(
    source: dict[str, Any],
    character_id: str,
    expected_items: list[dict[str, Any]],
    *,
    compare_plugs: bool = True,
) -> bool:
    if missing_equipment_labels(source, character_id, expected_items):
        return False
    if not compare_plugs:
        return True
    live = {str(item["item_instance_id"]): item for item in source["items"]}
    for expected in expected_items:
        item = live.get(str(expected["item_instance_id"]))
        if item is None or item["source_kind"] != "equipped" or item.get("character_id") != character_id:
            return False
        current_plugs = component_plug_hashes(item.get("components"))
        for plug in expected.get("plugs", []):
            if plug.get("filtered") or plug.get("plug_hash") in (
                None,
                INVALID_HASH_SENTINEL,
            ):
                continue
            index = int(plug["socket_index"])
            current = current_plugs[index] if index < len(current_plugs) else None
            if current != plug.get("plug_hash"):
                return False
    return True


def identifiers_match(slot: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(
        value is None or valid_hash(slot.get(field)) == value
        for field, value in (
            ("nameHash", expected.get("name_hash")),
            ("iconHash", expected.get("icon_hash")),
            ("colorHash", expected.get("color_hash")),
        )
    )


def summarize_slot(slot: dict[str, Any]) -> dict[str, Any]:
    items = slot.get("items") if isinstance(slot, dict) else []
    return {
        "populated": isinstance(items, list) and bool(items),
        "item_count": len(items) if isinstance(items, list) else 0,
        "item_instance_ids": [
            str(item.get("itemInstanceId"))
            for item in (items if isinstance(items, list) else [])
            if isinstance(item, dict) and valid_instance_id(item.get("itemInstanceId"))
        ],
        "name_hash": valid_hash(slot.get("nameHash")) if isinstance(slot, dict) else None,
        "icon_hash": valid_hash(slot.get("iconHash")) if isinstance(slot, dict) else None,
        "color_hash": valid_hash(slot.get("colorHash")) if isinstance(slot, dict) else None,
    }


def expired(preview: dict[str, Any]) -> bool:
    try:
        value = datetime.fromisoformat(preview["expires_at"])
    except (KeyError, TypeError, ValueError):
        return True
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value <= datetime.now(UTC)


def parse_api_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def error_evidence(error: Exception) -> tuple[int | None, int | None, str, str, float]:
    if isinstance(error, BungieActionError):
        return (
            error.http_status,
            error.error_code,
            error.error_status,
            " ".join(str(error).split())[:500],
            error.throttle_seconds,
        )
    return (None, None, type(error).__name__, " ".join(str(error).split())[:500], 0.0)


def compact_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
