"""Confirm, start, observe, and resume durable loadout applications."""

from __future__ import annotations

from typing import Any, Literal, Mapping

from .runtime import LoadoutFunctions


BackupChoice = Literal["import", "skip"]


def confirm_application(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    preview_id: str,
    backup_choice: BackupChoice,
    backup: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert a valid preview into a durable operation; sends no Bungie write."""

    return functions.synchronization.create_operation(
        user_id,
        preview_id,
        backup_choice=backup_choice,
        backup=dict(backup) if backup is not None else None,
    )


def backup_current_slots(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    character_id: str,
    label: str,
) -> dict[str, Any]:
    """Import every populated in-game slot into the local library."""

    return functions.synchronization.import_current_slot_backup(
        user_id,
        character_id=character_id,
        label=label,
    )


def start_application(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    operation_id: str,
    access_token: str,
) -> None:
    """Start or explicitly resume the durable background Bungie operation."""

    functions.synchronization.start(user_id, operation_id, access_token)


def get_application(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    operation_id: str,
) -> dict[str, Any] | None:
    """Read durable progress, attempt evidence, and recovery information."""

    return functions.synchronization.operation(user_id, operation_id)


def list_recent_applications(
    functions: LoadoutFunctions,
    user_id: str,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """List recent durable loadout applications."""

    return functions.synchronization.recent_operations(user_id, limit=limit)
