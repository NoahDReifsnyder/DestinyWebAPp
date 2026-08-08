"""Atomic Bungie loadout actions with no application-flow assumptions.

These functions perform writes. They intentionally do not refresh inventory,
build previews, retry, restore equipment, or persist checkpoints. Compose them
directly only when your caller provides those policies.
"""

from __future__ import annotations

from typing import Any, Sequence

from .runtime import LoadoutFunctions


async def transfer_item(
    functions: LoadoutFunctions,
    access_token: str,
    *,
    character_id: str,
    membership_type: int,
    item_instance_id: str,
    item_hash: int,
    to_vault: bool,
) -> dict[str, Any]:
    """Move one exact item between a character and the vault."""

    return await functions.bungie.transfer_item(
        access_token,
        item_instance_id=item_instance_id,
        item_hash=item_hash,
        character_id=character_id,
        membership_type=membership_type,
        transfer_to_vault=to_vault,
    )


async def equip_items(
    functions: LoadoutFunctions,
    access_token: str,
    *,
    character_id: str,
    membership_type: int,
    item_instance_ids: Sequence[str],
) -> dict[str, Any]:
    """Equip exact items and reject any non-success per-item equip status."""

    return await functions.bungie.equip_items(
        access_token,
        item_instance_ids=list(item_instance_ids),
        character_id=character_id,
        membership_type=membership_type,
    )


async def insert_free_plug(
    functions: LoadoutFunctions,
    access_token: str,
    *,
    character_id: str,
    membership_type: int,
    item_instance_id: str,
    socket_index: int,
    plug_hash: int,
) -> dict[str, Any]:
    """Insert one plug that live Bungie data reports as free and insertable."""

    return await functions.bungie.insert_socket_plug_free(
        access_token,
        item_instance_id=item_instance_id,
        socket_index=socket_index,
        plug_hash=plug_hash,
        character_id=character_id,
        membership_type=membership_type,
    )


async def save_equipped_to_slot(
    functions: LoadoutFunctions,
    access_token: str,
    *,
    character_id: str,
    membership_type: int,
    slot_index: int,
) -> dict[str, Any]:
    """Snapshot equipped state into one zero-based Bungie loadout slot."""

    return await functions.bungie.snapshot_loadout(
        access_token,
        loadout_index=slot_index,
        character_id=character_id,
        membership_type=membership_type,
    )


async def set_slot_identifiers(
    functions: LoadoutFunctions,
    access_token: str,
    *,
    character_id: str,
    membership_type: int,
    slot_index: int,
    name_hash: int | None,
    icon_hash: int | None,
    color_hash: int | None,
) -> dict[str, Any]:
    """Set Bungie's constrained name, icon, and color for a zero-based slot."""

    return await functions.bungie.update_loadout_identifiers(
        access_token,
        loadout_index=slot_index,
        character_id=character_id,
        membership_type=membership_type,
        name_hash=name_hash,
        icon_hash=icon_hash,
        color_hash=color_hash,
    )


async def clear_slot(
    functions: LoadoutFunctions,
    access_token: str,
    *,
    character_id: str,
    membership_type: int,
    slot_index: int,
) -> dict[str, Any]:
    """Clear one zero-based Bungie loadout slot."""

    return await functions.bungie.clear_loadout(
        access_token,
        loadout_index=slot_index,
        character_id=character_id,
        membership_type=membership_type,
    )
