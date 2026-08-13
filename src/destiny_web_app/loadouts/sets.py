"""Create and edit class-bound 20-position loadout sets."""

from __future__ import annotations

from typing import Any, Mapping

from .runtime import LoadoutFunctions


def create_loadout_set(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    name: str,
    character_class: int,
) -> dict[str, Any]:
    return functions.sets.create(
        user_id, name=name, character_class_type=character_class
    )


def create_loadout_set_from_character(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    name: str,
    character_id: str,
) -> dict[str, Any]:
    """Import every populated in-game slot into its matching set position."""

    return functions.sets.create_from_character(
        user_id,
        name=name,
        character_id=character_id,
    )


def list_loadout_sets(
    functions: LoadoutFunctions, user_id: str
) -> list[dict[str, Any]]:
    return functions.sets.list(user_id)


def get_loadout_set(
    functions: LoadoutFunctions, user_id: str, *, set_id: str
) -> dict[str, Any] | None:
    return functions.sets.get(user_id, set_id)


def rename_loadout_set(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    set_id: str,
    name: str,
) -> dict[str, Any]:
    return functions.sets.rename(user_id, set_id, name=name)


def save_loadout_set_board(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    set_id: str,
    slots: Mapping[int, str],
    expected_version: int,
) -> dict[str, Any]:
    return functions.sets.save_board(
        user_id,
        set_id,
        slots=slots,
        expected_version=expected_version,
    )


def list_loadout_set_usages(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    loadout_id: str,
) -> list[dict[str, Any]]:
    return functions.sets.usages(user_id, loadout_id)


def replace_loadout_in_set(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    set_id: str,
    old_loadout_id: str,
    new_loadout_id: str,
) -> int:
    return functions.sets.replace_all(
        user_id,
        set_id,
        old_loadout_id=old_loadout_id,
        new_loadout_id=new_loadout_id,
    )


def delete_loadout_set(
    functions: LoadoutFunctions, user_id: str, *, set_id: str
) -> None:
    functions.sets.delete(user_id, set_id)
