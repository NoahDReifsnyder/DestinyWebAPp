"""Current-state, class-bound 20-slot loadout boards."""

from __future__ import annotations

from typing import Any, Mapping

from destiny_web_app.loadout_manager import CLASS_NAMES, LoadoutManagerService
from destiny_web_app.loadouts.storage import LoadoutStore


class LoadoutSetError(ValueError):
    """A loadout-set change violates a board invariant."""


class LoadoutSetService:
    def __init__(
        self, store: LoadoutStore, library: LoadoutManagerService
    ) -> None:
        self.store = store
        self.library = library

    def create(
        self,
        user_id: str,
        *,
        name: str,
        character_class_type: int,
    ) -> dict[str, Any]:
        return self.store.create_loadout_set(
            user_id,
            name=set_name(name),
            character_class_type=class_type(character_class_type),
        )

    def create_from_character(
        self,
        user_id: str,
        *,
        name: str,
        character_id: str,
    ) -> dict[str, Any]:
        normalized_name = set_name(name)
        prepared = self.library.prepare_character_loadout_set(
            user_id,
            character_id=character_id,
        )
        return self._enrich(
            user_id,
            self.store.create_loadout_set_from_captures(
                user_id,
                name=normalized_name,
                character_class_type=class_type(
                    prepared["character_class_type"]
                ),
                loadouts=prepared["loadouts"],
            ),
        )

    def list(self, user_id: str) -> list[dict[str, Any]]:
        return [self._enrich(user_id, value) for value in self.store.list_loadout_sets(user_id)]

    def get(self, user_id: str, set_id: str) -> dict[str, Any] | None:
        value = self.store.load_loadout_set(user_id, set_id)
        return self._enrich(user_id, value) if value is not None else None

    def rename(
        self, user_id: str, set_id: str, *, name: str
    ) -> dict[str, Any]:
        return self._enrich(
            user_id,
            self.store.rename_loadout_set(user_id, set_id, name=set_name(name)),
        )

    def save_board(
        self,
        user_id: str,
        set_id: str,
        *,
        slots: Mapping[int, str],
        expected_version: int,
    ) -> dict[str, Any]:
        normalized: dict[int, str] = {}
        for raw_position, raw_loadout_id in slots.items():
            position = int(raw_position)
            loadout_id = str(raw_loadout_id).strip()
            if not loadout_id:
                continue
            if position < 0 or position >= 20:
                raise LoadoutSetError("Set positions must be between 1 and 20.")
            normalized[position] = loadout_id
        try:
            value = self.store.save_loadout_set_slots(
                user_id,
                set_id,
                slots=normalized,
                expected_version=int(expected_version),
            )
        except (LookupError, ValueError) as error:
            raise LoadoutSetError(str(error)) from error
        return self._enrich(user_id, value)

    def usages(self, user_id: str, loadout_id: str) -> list[dict[str, Any]]:
        return self.store.loadout_set_usages(user_id, loadout_id)

    def replace_all(
        self,
        user_id: str,
        set_id: str,
        *,
        old_loadout_id: str,
        new_loadout_id: str,
    ) -> int:
        try:
            return self.store.replace_loadout_in_set(
                user_id,
                set_id,
                old_loadout_id=old_loadout_id,
                new_loadout_id=new_loadout_id,
            )
        except (LookupError, ValueError) as error:
            raise LoadoutSetError(str(error)) from error

    def delete(self, user_id: str, set_id: str) -> None:
        self.store.delete_loadout_set(user_id, set_id)

    def _enrich(self, user_id: str, board: dict[str, Any]) -> dict[str, Any]:
        loadout_by_id = {
            loadout["loadout_id"]: loadout
            for loadout in self.library.saved_loadouts(
                user_id, include_archived=True
            )
        }
        slots = []
        for row in board["slots"]:
            loadout = loadout_by_id.get(row["loadout_id"])
            if loadout is None:
                continue
            slots.append({**row, "loadout": loadout})
        return {
            **board,
            "class_name": CLASS_NAMES.get(
                int(board["character_class_type"]), "Guardian"
            ),
            "slots": slots,
            "filled_count": len(slots),
        }


def set_name(value: str) -> str:
    normalized = " ".join(str(value).split())
    if not normalized:
        raise LoadoutSetError("Set name is required.")
    if len(normalized) > 100:
        raise LoadoutSetError("Set name must be 100 characters or fewer.")
    return normalized


def class_type(value: int) -> int:
    result = int(value)
    if result not in (0, 1, 2):
        raise LoadoutSetError("Choose Titan, Hunter, or Warlock.")
    return result
