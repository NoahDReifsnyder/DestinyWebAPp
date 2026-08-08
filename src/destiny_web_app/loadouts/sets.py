"""Build and edit ordered sets of pinned loadout revisions."""

from __future__ import annotations

from typing import Any, Mapping

from .runtime import LoadoutFunctions


def create_loadout_set(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    name: str,
    activity_name: str,
    description: str = "",
    game: str = "Destiny 2",
    activity_version: str = "",
) -> dict[str, Any]:
    """Create an empty activity-oriented loadout set."""

    return functions.sets.create(
        user_id,
        name=name,
        description=description,
        game=game,
        activity_name=activity_name,
        activity_version=activity_version,
    )


def list_loadout_sets(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """List saved loadout sets."""

    return functions.sets.plans(user_id, include_archived=include_archived)


def get_loadout_set(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    set_id: str,
) -> dict[str, Any] | None:
    """Return a set, encounter hierarchy, assignments, and editing options."""

    return functions.sets.workspace(user_id, set_id)


def update_loadout_set(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    set_id: str,
    name: str,
    activity_name: str,
    description: str = "",
    game: str = "Destiny 2",
    activity_version: str = "",
    note: str = "",
) -> dict[str, Any]:
    """Append a metadata revision to a loadout set."""

    return functions.sets.update_metadata(
        user_id,
        set_id,
        name=name,
        description=description,
        game=game,
        activity_name=activity_name,
        activity_version=activity_version,
        change_note=note,
    )


def add_encounter(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    set_id: str,
    order: int,
    name: str,
    notes: str = "",
) -> dict[str, Any]:
    """Append a set revision containing a new ordered encounter group."""

    return functions.sets.add_encounter(
        user_id,
        set_id,
        encounter_order=order,
        name=name,
        notes=notes,
    )


def update_encounter(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    set_id: str,
    encounter_id: str,
    order: int,
    name: str,
    notes: str = "",
) -> dict[str, Any]:
    """Append a set revision with one encounter changed."""

    return functions.sets.update_encounter(
        user_id,
        set_id,
        encounter_id,
        encounter_order=order,
        name=name,
        notes=notes,
    )


def remove_encounter(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    set_id: str,
    encounter_id: str,
) -> dict[str, Any]:
    """Append a set revision without the selected encounter and assignments."""

    return functions.sets.remove_encounter(user_id, set_id, encounter_id)


def assign_loadout(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    set_id: str,
    encounter_id: str,
    order: int,
    loadout_revision_id: str,
    character_id: str,
    slot_index: int,
    notes: str = "",
) -> dict[str, Any]:
    """Pin an immutable loadout revision to an exact zero-based game slot."""

    return functions.sets.add_assignment(
        user_id,
        set_id,
        encounter_id=encounter_id,
        assignment_order=order,
        loadout_revision_id=loadout_revision_id,
        target_character_id=character_id,
        target_slot_index=slot_index,
        notes=notes,
    )


def unassign_loadout(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    set_id: str,
    assignment_id: str,
) -> dict[str, Any]:
    """Append a set revision without one slot assignment."""

    return functions.sets.remove_assignment(user_id, set_id, assignment_id)


def set_loadout_set_archived(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    set_id: str,
    archived: bool,
) -> None:
    """Archive or restore a set."""

    functions.sets.set_archived(user_id, set_id, archived=archived)


def delete_loadout_set(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    set_id: str,
) -> None:
    """Delete a loadout set and its local immutable history."""

    functions.sets.delete(user_id, set_id)


def export_loadout_set(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    set_id: str,
) -> dict[str, Any]:
    """Export a set together with every pinned loadout revision."""

    return functions.sets.export_bundle(user_id, set_id)


def import_loadout_set(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    document: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and import a set and all embedded exact loadouts."""

    return functions.sets.import_bundle(user_id, dict(document))
