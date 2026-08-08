"""Refresh and verify live state without prescribing a write flow."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from destiny_web_app.loadout_sync import (
    POST_WRITE_REFRESH_DELAYS,
    character_slot,
    equipment_matches,
    item_by_instance,
    parse_api_timestamp,
    slot_empty,
    slot_matches,
    state_fingerprint,
)

from .runtime import LoadoutFunctions


def get_live_state(
    functions: LoadoutFunctions,
    user_id: str,
) -> dict[str, Any] | None:
    """Read the active coherent inventory/profile snapshot from SQLite."""

    return functions.store.load_active_loadout_source(user_id)


async def refresh_live_state(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    access_token: str,
) -> dict[str, Any]:
    """Force one Bungie profile refresh and return the stored coherent state."""

    await functions.inventory.synchronize(
        bungie_membership_id=user_id,
        access_token=access_token,
        force=True,
    )
    source = get_live_state(functions, user_id)
    if source is None:
        raise RuntimeError("Bungie refresh produced no active loadout state.")
    return source


async def wait_for_post_write_state(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    access_token: str,
    written_at: datetime,
) -> dict[str, Any]:
    """Wait until Bungie returns profile data minted after a write checkpoint."""

    for delay in (0.0, *POST_WRITE_REFRESH_DELAYS):
        if delay:
            await asyncio.sleep(delay)
        source = await refresh_live_state(
            functions,
            user_id,
            access_token=access_token,
        )
        minted_at = parse_api_timestamp(
            source["snapshot"].get("source_minted_at")
        )
        if minted_at is not None and minted_at > written_at:
            return source
    raise RuntimeError(
        "Bungie did not return profile data minted after the write checkpoint."
    )


def get_item(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    item_instance_id: str,
) -> dict[str, Any] | None:
    """Find one exact instance in the current coherent state."""

    source = get_live_state(functions, user_id)
    return (
        item_by_instance(source, item_instance_id)
        if source is not None
        else None
    )


def get_slot(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    character_id: str,
    slot_index: int,
) -> dict[str, Any]:
    """Read one zero-based in-game loadout slot from current state."""

    source = get_live_state(functions, user_id)
    if source is None:
        raise LookupError("Current loadout state is unavailable.")
    return character_slot(source, character_id, slot_index)


def equipment_is_equipped(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    character_id: str,
    expected_items: list[dict[str, Any]],
) -> bool:
    """Return whether exact items and selected sockets match current equipment."""

    source = get_live_state(functions, user_id)
    return bool(
        source is not None
        and equipment_matches(source, character_id, expected_items)
    )


def slot_matches_loadout(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    character_id: str,
    slot_index: int,
    expected_items: list[dict[str, Any]],
) -> bool:
    """Return whether one in-game slot matches exact expected item/socket state."""

    return slot_matches(
        get_slot(
            functions,
            user_id,
            character_id=character_id,
            slot_index=slot_index,
        ),
        expected_items,
    )


def slot_is_empty(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    character_id: str,
    slot_index: int,
) -> bool:
    """Return whether one in-game loadout slot has no saved items."""

    return slot_empty(
        get_slot(
            functions,
            user_id,
            character_id=character_id,
            slot_index=slot_index,
        )
    )


def fingerprint_live_state(
    functions: LoadoutFunctions,
    user_id: str,
) -> str:
    """Return a deterministic fingerprint for confirmation/state-change checks."""

    source = get_live_state(functions, user_id)
    if source is None:
        raise LookupError("Current loadout state is unavailable.")
    return state_fingerprint(source)
