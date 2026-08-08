"""Create and inspect read-only safety previews for game mutations."""

from __future__ import annotations

from typing import Any

from .runtime import LoadoutFunctions


def preview_loadout_application(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    loadout_id: str,
    revision_id: str,
    character_id: str,
    slot_index: int,
) -> dict[str, Any]:
    """Preview applying one pinned revision to one zero-based in-game slot."""

    return functions.synchronization.create_single_preview(
        user_id,
        loadout_id=loadout_id,
        revision_id=revision_id,
        target_character_id=character_id,
        target_slot_index=slot_index,
    )


def preview_loadout_set_application(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    set_id: str,
) -> dict[str, Any]:
    """Preview applying every assigned slot and clearing every unassigned slot."""

    return functions.synchronization.create_plan_preview(
        user_id,
        plan_id=set_id,
    )


def get_preview(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    preview_id: str,
) -> dict[str, Any] | None:
    """Read one persisted preview without performing a Bungie write."""

    return functions.synchronization.preview(user_id, preview_id)


def validate_preview(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    preview_id: str,
) -> dict[str, Any]:
    """Recheck that a preview is unexpired and its confirmed state is unchanged."""

    return functions.synchronization.validate_preview_state(user_id, preview_id)
