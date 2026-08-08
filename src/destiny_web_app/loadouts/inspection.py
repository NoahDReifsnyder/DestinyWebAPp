"""Read-only inspection and exact-item selection functions."""

from __future__ import annotations

from typing import Any

from .runtime import LoadoutFunctions


def inspect_in_game_loadouts(
    functions: LoadoutFunctions,
    user_id: str,
) -> dict[str, Any] | None:
    """Return every character's current Bungie loadout slots and capabilities."""

    return functions.library.inspect(user_id)


def build_item_catalog(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    character_class: int,
) -> dict[str, Any]:
    """Return selectable exact owned items for all gameplay equipment slots."""

    return functions.library.builder_catalog(
        user_id,
        class_type=character_class,
    )
