"""Local-only immutable activity loadout plans."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any, Callable

from destiny_web_app.loadout_manager import (
    CLASS_NAMES,
    LoadoutInspectionError,
    LoadoutManagerService,
)

if TYPE_CHECKING:
    from destiny_web_app.loadouts.storage import LoadoutStore


class ActivityPlanError(ValueError):
    """An activity plan change would violate a local invariant."""


class ActivityPlanService:
    def __init__(
        self,
        database: LoadoutStore,
        loadouts: LoadoutManagerService,
    ) -> None:
        self.database = database
        self.loadouts = loadouts

    def create(
        self,
        bungie_membership_id: str,
        *,
        name: str,
        description: str,
        game: str,
        activity_name: str,
        activity_version: str,
    ) -> dict[str, Any]:
        values = validate_plan_metadata(
            name,
            description,
            game,
            activity_name,
            activity_version,
        )
        return self._write(
            self.database.create_loadout_plan,
            bungie_membership_id,
            **values,
        )

    def plans(
        self,
        bungie_membership_id: str,
        *,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        return self.database.list_loadout_plans(
            bungie_membership_id,
            include_archived=include_archived,
        )

    def workspace(
        self,
        bungie_membership_id: str,
        plan_id: str,
    ) -> dict[str, Any] | None:
        plan = self.database.load_loadout_plan(
            bungie_membership_id,
            plan_id,
            include_archived=True,
        )
        if plan is None:
            return None
        inspection = self.loadouts.inspect(bungie_membership_id)
        characters = inspection["characters"] if inspection else []
        character_map = {
            character["character_id"]: character for character in characters
        }
        for assignment in plan["assignments"]:
            character = character_map.get(assignment["target_character_id"])
            assignment["target_character_name"] = (
                character["class_name"] if character else "Unavailable character"
            )
            assignment["slot_display_index"] = (
                int(assignment["target_slot_index"]) + 1
            )
            assignment["class_name"] = CLASS_NAMES.get(
                int(assignment["character_class_type"]), "Guardian"
            )
        revision_options = []
        for loadout in self.loadouts.saved_loadouts(
            bungie_membership_id,
            include_archived=True,
        ):
            for revision in self.loadouts.loadout_revisions(
                bungie_membership_id,
                loadout["loadout_id"],
            ):
                revision_options.append(
                    {
                        "loadout_id": loadout["loadout_id"],
                        "loadout_name": loadout["name"],
                        "revision_id": revision["revision_id"],
                        "revision_number": revision["revision_number"],
                        "class_name": revision["class_name"],
                        "class_type": revision["character_class_type"],
                        "archived": bool(loadout["archived_at"]),
                    }
                )
        return {
            "plan": plan,
            "plan_revisions": self.database.list_loadout_plan_revisions(
                bungie_membership_id,
                plan_id,
            ),
            "characters": characters,
            "revision_options": revision_options,
        }

    def update_metadata(
        self,
        bungie_membership_id: str,
        plan_id: str,
        *,
        name: str,
        description: str,
        game: str,
        activity_name: str,
        activity_version: str,
        change_note: str,
    ) -> dict[str, Any]:
        values = validate_plan_metadata(
            name,
            description,
            game,
            activity_name,
            activity_version,
        )
        return self._write(
            self.database.update_loadout_plan_metadata,
            bungie_membership_id,
            plan_id,
            **values,
            change_note=limited_text(change_note, 500, "Change note"),
        )

    def add_encounter(
        self,
        bungie_membership_id: str,
        plan_id: str,
        *,
        encounter_order: int,
        name: str,
        notes: str,
    ) -> dict[str, Any]:
        if encounter_order < 0:
            raise ActivityPlanError("Encounter order cannot be negative.")
        return self._write(
            self.database.add_loadout_plan_encounter,
            bungie_membership_id,
            plan_id,
            encounter_order=encounter_order,
            name=required_text(name, 100, "Encounter name"),
            notes=limited_text(notes, 2000, "Encounter notes"),
        )

    def remove_encounter(
        self,
        bungie_membership_id: str,
        plan_id: str,
        encounter_id: str,
    ) -> dict[str, Any]:
        return self._write(
            self.database.remove_loadout_plan_encounter,
            bungie_membership_id,
            plan_id,
            encounter_id,
        )

    def update_encounter(
        self,
        bungie_membership_id: str,
        plan_id: str,
        encounter_id: str,
        *,
        encounter_order: int,
        name: str,
        notes: str,
    ) -> dict[str, Any]:
        if encounter_order < 0:
            raise ActivityPlanError("Encounter order cannot be negative.")
        return self._write(
            self.database.update_loadout_plan_encounter,
            bungie_membership_id,
            plan_id,
            encounter_id,
            encounter_order=encounter_order,
            name=required_text(name, 100, "Encounter name"),
            notes=limited_text(notes, 2000, "Encounter notes"),
        )

    def add_assignment(
        self,
        bungie_membership_id: str,
        plan_id: str,
        *,
        encounter_id: str,
        assignment_order: int,
        loadout_revision_id: str,
        target_character_id: str,
        target_slot_index: int,
        notes: str,
    ) -> dict[str, Any]:
        if assignment_order < 0 or target_slot_index < 0:
            raise ActivityPlanError("Assignment order and slot must be positive.")
        inspection = self.loadouts.inspect(bungie_membership_id)
        character = next(
            (
                row
                for row in inspection["characters"]
                if row["character_id"] == target_character_id
            ),
            None,
        ) if inspection else None
        if character is None:
            raise ActivityPlanError("The target character is unavailable.")
        if target_slot_index >= int(character["slot_count"]):
            raise ActivityPlanError(
                f"{character['class_name']} exposes only "
                f"{character['slot_count']} loadout slots."
            )
        return self._write(
            self.database.add_loadout_plan_assignment,
            bungie_membership_id,
            plan_id,
            encounter_id=encounter_id,
            assignment_order=assignment_order,
            loadout_revision_id=loadout_revision_id,
            target_character_id=target_character_id,
            target_slot_index=target_slot_index,
            notes=limited_text(notes, 1000, "Assignment notes"),
        )

    def remove_assignment(
        self,
        bungie_membership_id: str,
        plan_id: str,
        assignment_id: str,
    ) -> dict[str, Any]:
        return self._write(
            self.database.remove_loadout_plan_assignment,
            bungie_membership_id,
            plan_id,
            assignment_id,
        )

    def set_archived(
        self,
        bungie_membership_id: str,
        plan_id: str,
        *,
        archived: bool,
    ) -> None:
        self.database.set_loadout_plan_archived(
            bungie_membership_id,
            plan_id,
            archived=archived,
        )

    def delete(
        self,
        bungie_membership_id: str,
        plan_id: str,
    ) -> None:
        self.database.delete_loadout_plan(bungie_membership_id, plan_id)

    def export_bundle(
        self,
        bungie_membership_id: str,
        plan_id: str,
    ) -> dict[str, Any]:
        plan = self.database.load_loadout_plan(
            bungie_membership_id, plan_id, include_archived=True
        )
        if plan is None:
            raise ActivityPlanError("The activity plan is unavailable.")
        loadout_bundles = {}
        for assignment in plan["assignments"]:
            revision_id = assignment["loadout_revision_id"]
            if revision_id in loadout_bundles:
                continue
            loadout_bundles[revision_id] = self.loadouts.export_bundle(
                bungie_membership_id,
                assignment["loadout_id"],
                revision_id=revision_id,
            )
        return {
            "format": "destiny-web-app/activity-plan",
            "version": 1,
            "name": plan["name"],
            "description": plan["description"],
            "game": plan["game"],
            "activityName": plan["activity_name"],
            "activityVersion": plan["activity_version"],
            "loadouts": loadout_bundles,
            "encounters": [
                {
                    "sourceEncounterId": encounter["encounter_id"],
                    "order": int(encounter["encounter_order"]),
                    "name": encounter["name"],
                    "notes": encounter["notes"],
                    "assignments": [
                        {
                            "order": int(assignment["assignment_order"]),
                            "loadoutRevisionRef": assignment[
                                "loadout_revision_id"
                            ],
                            "targetCharacterId": assignment[
                                "target_character_id"
                            ],
                            "targetSlotIndex": int(
                                assignment["target_slot_index"]
                            ),
                            "notes": assignment["notes"],
                        }
                        for assignment in encounter["assignments"]
                    ],
                }
                for encounter in plan["encounters"]
            ],
        }

    def import_bundle(
        self,
        bungie_membership_id: str,
        bundle: dict[str, Any],
    ) -> dict[str, Any]:
        if (
            bundle.get("format") != "destiny-web-app/activity-plan"
            or bundle.get("version") != 1
        ):
            raise ActivityPlanError(
                "Only destiny-web-app/activity-plan version 1 can be imported."
            )
        raw_loadouts = bundle.get("loadouts")
        raw_encounters = bundle.get("encounters")
        if not isinstance(raw_loadouts, dict) or not isinstance(
            raw_encounters, list
        ):
            raise ActivityPlanError("The activity-plan bundle is incomplete.")
        # Validate every exact item and socket before creating any local row.
        for ref, loadout_bundle in raw_loadouts.items():
            if not isinstance(ref, str) or not isinstance(loadout_bundle, dict):
                raise ActivityPlanError("The embedded loadout map is invalid.")
            self.loadouts.validate_import_bundle(
                bungie_membership_id, loadout_bundle
            )
        validate_plan_metadata(
            str(bundle.get("name") or "Imported activity plan"),
            str(bundle.get("description") or ""),
            str(bundle.get("game") or "Destiny 2"),
            str(bundle.get("activityName") or "Imported activity"),
            str(bundle.get("activityVersion") or ""),
        )
        inspection = self.loadouts.inspect(bungie_membership_id)
        characters = {
            row["character_id"]: row
            for row in (inspection["characters"] if inspection else [])
        }
        encounter_orders: set[int] = set()
        target_slots: set[tuple[str, int]] = set()
        for encounter in raw_encounters:
            if not isinstance(encounter, dict) or not isinstance(
                encounter.get("assignments"), list
            ):
                raise ActivityPlanError("An imported encounter is invalid.")
            encounter_order = int(encounter.get("order", -1))
            if encounter_order < 0 or encounter_order in encounter_orders:
                raise ActivityPlanError(
                    "Imported encounter orders must be unique and non-negative."
                )
            encounter_orders.add(encounter_order)
            assignment_orders: set[int] = set()
            for assignment in encounter["assignments"]:
                if (
                    not isinstance(assignment, dict)
                    or assignment.get("loadoutRevisionRef")
                    not in raw_loadouts
                ):
                    raise ActivityPlanError(
                        "An imported assignment has an unknown loadout reference."
                    )
                assignment_order = int(assignment.get("order", -1))
                character_id = str(
                    assignment.get("targetCharacterId") or ""
                )
                slot_index = int(assignment.get("targetSlotIndex", -1))
                character = characters.get(character_id)
                if (
                    assignment_order < 0
                    or assignment_order in assignment_orders
                ):
                    raise ActivityPlanError(
                        "Imported assignment orders must be unique within an encounter."
                    )
                assignment_orders.add(assignment_order)
                if character is None or not 0 <= slot_index < int(
                    character["slot_count"]
                ):
                    raise ActivityPlanError(
                        "An imported target character or slot is unavailable to this account."
                    )
                target = (character_id, slot_index)
                if target in target_slots:
                    raise ActivityPlanError(
                        "The imported plan maps one target slot more than once."
                    )
                target_slots.add(target)
                embedded = raw_loadouts[
                    assignment["loadoutRevisionRef"]
                ]
                target_class_type = next(
                    (
                        value
                        for value, name in CLASS_NAMES.items()
                        if name == character["class_name"]
                    ),
                    -1,
                )
                if int(embedded["characterClassType"]) != target_class_type:
                    raise ActivityPlanError(
                        "An imported loadout class does not match its target character."
                    )
        imported_revisions = {}
        for ref, loadout_bundle in raw_loadouts.items():
            saved = self.loadouts.import_bundle(
                bungie_membership_id, loadout_bundle
            )
            imported_revisions[ref] = saved["revision_id"]
        plan = self.create(
            bungie_membership_id,
            name=str(bundle.get("name") or "Imported activity plan"),
            description=str(bundle.get("description") or ""),
            game=str(bundle.get("game") or "Destiny 2"),
            activity_name=str(bundle.get("activityName") or "Imported activity"),
            activity_version=str(bundle.get("activityVersion") or ""),
        )
        for raw_encounter in raw_encounters:
            plan = self.add_encounter(
                bungie_membership_id,
                plan["plan_id"],
                encounter_order=int(raw_encounter.get("order", 0)),
                name=str(raw_encounter.get("name") or "Encounter"),
                notes=str(raw_encounter.get("notes") or ""),
            )
            new_encounter = next(
                row
                for row in plan["encounters"]
                if row["encounter_order"] == int(raw_encounter.get("order", 0))
            )
            for raw_assignment in raw_encounter["assignments"]:
                plan = self.add_assignment(
                    bungie_membership_id,
                    plan["plan_id"],
                    encounter_id=new_encounter["encounter_id"],
                    assignment_order=int(raw_assignment.get("order", 0)),
                    loadout_revision_id=imported_revisions[
                        raw_assignment["loadoutRevisionRef"]
                    ],
                    target_character_id=str(
                        raw_assignment.get("targetCharacterId") or ""
                    ),
                    target_slot_index=int(
                        raw_assignment.get("targetSlotIndex", -1)
                    ),
                    notes=str(raw_assignment.get("notes") or ""),
                )
        return plan

    @staticmethod
    def _write(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except sqlite3.IntegrityError as error:
            message = str(error)
            if "target_character_id" in message or "target_slot_index" in message:
                raise ActivityPlanError(
                    "That character slot already has an assignment in this plan."
                ) from error
            if "encounter_order" in message:
                raise ActivityPlanError(
                    "That encounter order is already used."
                ) from error
            if "assignment_order" in message:
                raise ActivityPlanError(
                    "That assignment order is already used in this encounter."
                ) from error
            raise ActivityPlanError(
                "The plan change conflicts with its current revision."
            ) from error
        except (LoadoutInspectionError, LookupError, ValueError) as error:
            raise ActivityPlanError(str(error)) from error


def validate_plan_metadata(
    name: str,
    description: str,
    game: str,
    activity_name: str,
    activity_version: str,
) -> dict[str, str]:
    return {
        "name": required_text(name, 100, "Plan name"),
        "description": limited_text(description, 3000, "Plan description"),
        "game": required_text(game or "Destiny 2", 80, "Game"),
        "activity_name": required_text(
            activity_name, 120, "Activity name"
        ),
        "activity_version": limited_text(
            activity_version, 80, "Activity version"
        ),
    }


def required_text(value: str, limit: int, label: str) -> str:
    normalized = " ".join(str(value).split())
    if not normalized:
        raise ActivityPlanError(f"{label} is required.")
    if len(normalized) > limit:
        raise ActivityPlanError(f"{label} must be {limit} characters or fewer.")
    return normalized


def limited_text(value: str, limit: int, label: str) -> str:
    normalized = str(value).strip()
    if len(normalized) > limit:
        raise ActivityPlanError(f"{label} must be {limit} characters or fewer.")
    return normalized
