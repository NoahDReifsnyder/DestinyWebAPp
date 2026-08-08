"""Save, read, revise, import, and export individual loadouts."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .runtime import LoadoutFunctions


def save_equipped_loadout(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    character_id: str,
    name: str,
    description: str = "",
    tags: Sequence[str] = (),
) -> dict[str, Any]:
    """Save one character's complete equipped gameplay state as a new loadout."""

    return functions.library.capture_current_equipment(
        user_id,
        character_id=character_id,
        name=name,
        description=description,
        tags=list(tags),
    )


def save_selected_loadout(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    character_class: int,
    items_by_bucket: Mapping[int, str],
    name: str,
    description: str = "",
    tags: Sequence[str] = (),
) -> dict[str, Any]:
    """Save one complete loadout assembled from exact item instance IDs."""

    return functions.library.save_builder_loadout(
        user_id,
        class_type=character_class,
        selections=dict(items_by_bucket),
        name=name,
        description=description,
        tags=list(tags),
    )


def save_in_game_slot(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    character_id: str,
    slot_index: int,
    name: str,
    description: str = "",
    tags: Sequence[str] = (),
) -> dict[str, Any]:
    """Import one populated zero-based Bungie slot into the local library."""

    return functions.library.import_in_game_slot(
        user_id,
        character_id=character_id,
        slot_index=slot_index,
        name=name,
        description=description,
        tags=list(tags),
    )


def list_loadouts(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """List application-owned loadouts using their current revisions."""

    return functions.library.saved_loadouts(
        user_id,
        include_archived=include_archived,
    )


def get_loadout(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    loadout_id: str,
    revision_id: str | None = None,
    include_archived: bool = False,
) -> dict[str, Any] | None:
    """Load one current or explicitly pinned immutable revision."""

    return functions.library.saved_loadout(
        user_id,
        loadout_id,
        revision_id=revision_id,
        include_archived=include_archived,
    )


def list_revisions(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    loadout_id: str,
) -> list[dict[str, Any]]:
    """List every immutable revision for one loadout."""

    return functions.library.loadout_revisions(user_id, loadout_id)


def revise_from_equipped(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    loadout_id: str,
    character_id: str,
    note: str = "",
) -> dict[str, Any]:
    """Append a complete equipped-state revision; never saves a partial state."""

    return functions.library.revise_with_current_equipment(
        user_id,
        loadout_id,
        character_id=character_id,
        revision_note=note,
    )


def update_loadout_details(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    loadout_id: str,
    name: str,
    description: str = "",
    tags: Sequence[str] = (),
) -> dict[str, Any]:
    """Update mutable library metadata without changing the pinned revision."""

    return functions.library.update_metadata(
        user_id,
        loadout_id,
        name=name,
        description=description,
        tags=list(tags),
    )


def clone_loadout(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    loadout_id: str,
    name: str,
) -> dict[str, Any]:
    """Clone the current immutable revision into a new library loadout."""

    return functions.library.clone_loadout(user_id, loadout_id, name=name)


def restore_revision(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    loadout_id: str,
    revision_id: str,
    note: str = "",
) -> dict[str, Any]:
    """Restore old content by appending it as a new immutable revision."""

    return functions.library.restore_revision(
        user_id,
        loadout_id,
        revision_id,
        revision_note=note,
    )


def compare_revisions(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    loadout_id: str,
    left_revision_id: str,
    right_revision_id: str,
) -> dict[str, Any]:
    """Compare exact items and sockets between two revisions."""

    return functions.library.compare_revisions(
        user_id,
        loadout_id,
        left_revision_id,
        right_revision_id,
    )


def set_loadout_archived(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    loadout_id: str,
    archived: bool,
) -> None:
    """Archive or restore one loadout without deleting pinned history."""

    functions.library.set_archived(user_id, loadout_id, archived=archived)


def set_loadout_favorite(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    loadout_id: str,
    favorite: bool,
) -> None:
    """Set the library favorite flag."""

    functions.library.set_favorite(user_id, loadout_id, favorite=favorite)


def delete_loadout(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    loadout_id: str,
) -> None:
    """Delete an unreferenced loadout and its revisions."""

    functions.library.delete_loadout(user_id, loadout_id)


def export_loadout(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    loadout_id: str,
    revision_id: str | None = None,
) -> dict[str, Any]:
    """Export one versioned portable loadout document."""

    return functions.library.export_bundle(
        user_id,
        loadout_id,
        revision_id=revision_id,
    )


def import_loadout(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    document: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate exact ownership and import one versioned loadout document."""

    return functions.library.import_bundle(user_id, dict(document))
