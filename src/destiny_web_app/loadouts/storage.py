"""SQLite persistence owned exclusively by the loadout subsystem."""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import AbstractContextManager
from typing import Any, Callable

from destiny_web_app.database import Database, as_iso, compact_json, utc_now


LOADOUT_SCHEMA_VERSION = 15


class LoadoutStore:
    """Persistence adapter used by all loadout capability modules.

    The generic :class:`Database` owns authentication and inventory. This
    adapter owns loadout tables and exposes only loadout-oriented operations.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    def connection(self) -> AbstractContextManager[sqlite3.Connection]:
        """Expose a transaction for the durable synchronization engine."""

        return self.database.connection()

    def initialize(self) -> None:
        """Create the loadout schema and apply idempotent local upgrades."""

        with self.connection() as connection:
            connection.executescript(LOADOUT_SCHEMA)
            action_table = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'loadout_sync_actions'"
            ).fetchone()
            if action_table is not None and "cleanup_inventory" not in str(
                action_table["sql"]
            ):
                # SQLite cannot add a value to a CHECK constraint in place.
                # Rebuild both the action table and its attempt child while
                # retaining every durable operation and audit record.
                connection.executescript(
                    """
                    CREATE TABLE loadout_sync_actions_new (
                        operation_id TEXT NOT NULL,
                        action_index INTEGER NOT NULL CHECK (action_index >= 0),
                        action_type TEXT NOT NULL CHECK (action_type IN (
                            'transfer_to_vault', 'transfer_from_vault', 'equip',
                            'verify_prepared', 'snapshot', 'identifiers',
                            'verify_slot', 'clear_slot', 'verify_clear',
                            'restore_equipment', 'verify_restored',
                            'insert_socket_plug', 'verify_socket_plug',
                            'cleanup_inventory'
                        )),
                        phase TEXT NOT NULL,
                        encounter_id TEXT,
                        assignment_id TEXT,
                        target_slot_index INTEGER CHECK (target_slot_index >= 0),
                        item_instance_id TEXT,
                        request_json TEXT NOT NULL DEFAULT '{}',
                        expected_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL CHECK (status IN (
                            'pending', 'running', 'completed', 'failed', 'skipped'
                        )),
                        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                        throttle_seconds REAL NOT NULL DEFAULT 0 CHECK (
                            throttle_seconds >= 0
                        ),
                        last_http_status INTEGER,
                        last_error_code INTEGER,
                        last_error_status TEXT,
                        last_message TEXT,
                        started_at TEXT,
                        completed_at TEXT,
                        PRIMARY KEY (operation_id, action_index),
                        FOREIGN KEY (operation_id)
                            REFERENCES loadout_sync_operations(operation_id)
                            ON DELETE CASCADE
                    );

                    INSERT INTO loadout_sync_actions_new
                    SELECT * FROM loadout_sync_actions;

                    CREATE TABLE loadout_sync_action_attempts_new (
                        operation_id TEXT NOT NULL,
                        action_index INTEGER NOT NULL,
                        attempt_number INTEGER NOT NULL CHECK (
                            attempt_number > 0
                        ),
                        status TEXT NOT NULL CHECK (status IN (
                            'running', 'succeeded', 'failed'
                        )),
                        http_status INTEGER,
                        error_code INTEGER,
                        error_status TEXT,
                        message TEXT NOT NULL DEFAULT '',
                        throttle_seconds REAL NOT NULL DEFAULT 0 CHECK (
                            throttle_seconds >= 0
                        ),
                        started_at TEXT NOT NULL,
                        completed_at TEXT,
                        PRIMARY KEY (
                            operation_id, action_index, attempt_number
                        ),
                        FOREIGN KEY (operation_id, action_index)
                            REFERENCES loadout_sync_actions_new(
                                operation_id, action_index
                            ) ON DELETE CASCADE
                    );

                    INSERT INTO loadout_sync_action_attempts_new
                    SELECT * FROM loadout_sync_action_attempts;
                    DROP TABLE loadout_sync_action_attempts;
                    DROP TABLE loadout_sync_actions;
                    ALTER TABLE loadout_sync_actions_new
                        RENAME TO loadout_sync_actions;
                    ALTER TABLE loadout_sync_action_attempts_new
                        RENAME TO loadout_sync_action_attempts;
                    """
                )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(loadouts)")
            }
            if "cover_icon_hash" not in columns:
                connection.execute(
                    "ALTER TABLE loadouts ADD COLUMN cover_icon_hash INTEGER"
                )
                connection.execute(
                    """
                    UPDATE loadouts
                    SET cover_icon_hash = CAST(
                        json_extract(
                            (SELECT source_payload_json
                             FROM loadout_revisions
                             WHERE revision_id = loadouts.current_revision_id),
                            '$.iconHash'
                        ) AS INTEGER
                    )
                    WHERE json_extract(
                        (SELECT source_payload_json
                         FROM loadout_revisions
                         WHERE revision_id = loadouts.current_revision_id),
                        '$.iconHash'
                    ) IS NOT NULL
                    """
                )
            # Archive is no longer part of the set-oriented library flow.
            # Make any legacy soft-hidden loadouts visible before the controls
            # are retired so no saved content becomes unreachable.
            connection.execute(
                "UPDATE loadouts SET archived_at = NULL WHERE archived_at IS NOT NULL"
            )
            # Version 12 created an all-column immutability trigger. Snapshot
            # references are now intentionally rebased during inventory
            # refresh, while the captured loadout content remains immutable.
            connection.executescript(
                """
                DROP TRIGGER IF EXISTS loadout_revision_is_immutable;
                CREATE TRIGGER loadout_revision_is_immutable
                BEFORE UPDATE OF revision_id, loadout_id, revision_number,
                    manifest_version, capture_source, source_character_id,
                    source_slot_index, captured_at, capture_validation_status,
                    capture_issues_json, canonical_payload_json,
                    source_payload_json, parent_revision_id, revision_action,
                    revision_note
                ON loadout_revisions
                BEGIN
                    SELECT RAISE(ABORT, 'loadout revisions are immutable');
                END;

                DROP TRIGGER IF EXISTS loadout_preview_source_owner_insert;
                CREATE TRIGGER loadout_preview_source_owner_insert
                BEFORE INSERT ON loadout_previews
                WHEN (
                    NEW.preview_type = 'single_slot' AND NOT EXISTS (
                        SELECT 1 FROM loadout_revisions AS revision
                        JOIN loadouts AS loadout
                          ON loadout.loadout_id = revision.loadout_id
                        WHERE revision.revision_id = NEW.source_revision_id
                          AND loadout.loadout_id = NEW.source_entity_id
                          AND loadout.bungie_membership_id = NEW.bungie_membership_id
                    )
                ) OR (
                    NEW.preview_type = 'activity_plan'
                    AND NOT EXISTS (
                        SELECT 1 FROM loadout_plan_revisions AS revision
                        JOIN loadout_plans AS plan
                          ON plan.plan_id = revision.plan_id
                        WHERE revision.plan_revision_id = NEW.source_revision_id
                          AND plan.plan_id = NEW.source_entity_id
                          AND plan.bungie_membership_id = NEW.bungie_membership_id
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM loadout_sets AS board
                        WHERE board.set_id = NEW.source_entity_id
                          AND CAST(board.version AS TEXT) = NEW.source_revision_id
                          AND board.bungie_membership_id = NEW.bungie_membership_id
                    )
                )
                BEGIN
                    SELECT RAISE(ABORT, 'preview source has the wrong owner');
                END;
                """
            )
            now = as_iso(utc_now())
            for version, name in (
                (8, "application-owned exact loadouts"),
                (9, "immutable loadout revisions and activity sets"),
                (10, "loadout previews and durable operations"),
                (11, "loadout operation evidence"),
                (12, "free socket synchronization actions"),
                (13, "rebased ephemeral inventory snapshots"),
                (14, "current-state class loadout sets and cover icons"),
                (15, "durable set inventory cleanup actions"),
            ):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO schema_migrations(version, name, applied_at)
                    VALUES (?, ?, ?)
                    """,
                    (version, name, now),
                )

    def load_active_loadout_source(self, user_id: str) -> dict[str, Any] | None:
        """Load one coherent active profile, characters, items, and components."""

        with self.connection() as connection:
            snapshot = connection.execute(
                """
                SELECT snapshot.snapshot_id, snapshot.destiny_membership_id,
                       snapshot.membership_type, snapshot.fetched_at,
                       snapshot.source_minted_at, snapshot.stale_at,
                       snapshot.total_item_count, snapshot.vault_item_count,
                       snapshot.profile_item_count,
                       snapshot.character_inventory_count,
                       snapshot.equipped_item_count,
                       snapshot.postmaster_item_count,
                       snapshot.raw_response_json,
                       sync.status AS sync_status, sync.last_error
                FROM inventory_snapshots AS snapshot
                LEFT JOIN inventory_sync_state AS sync
                  ON sync.active_snapshot_id = snapshot.snapshot_id
                 AND sync.bungie_membership_id = snapshot.bungie_membership_id
                WHERE snapshot.bungie_membership_id = ?
                  AND snapshot.is_active = 1
                  AND snapshot.status = 'complete'
                """,
                (user_id,),
            ).fetchone()
            if snapshot is None:
                return None
            character_rows = connection.execute(
                """
                SELECT character_id, class_type, class_hash, light, emblem_hash,
                       character_json
                FROM inventory_characters
                WHERE snapshot_id = ?
                ORDER BY class_type, character_id
                """,
                (snapshot["snapshot_id"],),
            ).fetchall()
            item_rows = connection.execute(
                """
                SELECT id, record_key, item_instance_id, item_hash, bucket_hash,
                       quantity, state, bind_status, location, transfer_status,
                       lockable, source_kind, character_id, item_json,
                       components_json, first_seen_at, last_seen_at
                FROM inventory_items
                WHERE snapshot_id = ?
                ORDER BY source_kind, character_id, bucket_hash, item_hash, id
                """,
                (snapshot["snapshot_id"],),
            ).fetchall()

        snapshot_data = dict(snapshot)
        profile = _json_object(snapshot_data.pop("raw_response_json"))
        characters = []
        for row in character_rows:
            value = dict(row)
            value["payload"] = _json_object(value.pop("character_json"))
            characters.append(value)
        items = []
        for row in item_rows:
            value = dict(row)
            value["payload"] = _json_object(value.pop("item_json"))
            value["components"] = _json_object(value.pop("components_json"))
            items.append(value)
        return {
            "snapshot": snapshot_data,
            "profile": profile,
            "characters": characters,
            "items": items,
        }

    def save_captured_loadout(
        self,
        user_id: str,
        *,
        name: str,
        description: str,
        tags: list[str],
        capture: dict[str, Any],
        revision_action: str = "capture",
        revision_note: str = "",
        cover_icon_hash: int | None = None,
        replace_in_set_id: str | None = None,
        replace_loadout_id: str | None = None,
    ) -> dict[str, Any]:
        """Create one loadout and its immutable first revision atomically."""

        loadout_id = secrets.token_hex(16)
        revision_id = secrets.token_hex(16)
        now = as_iso(utc_now())
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO loadouts(
                    loadout_id, bungie_membership_id, name, description,
                    tags_json, character_class_type, cover_icon_hash,
                    current_revision_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    loadout_id, user_id, name, description, compact_json(tags),
                    int(capture["character_class_type"]), cover_icon_hash,
                    now, now,
                ),
            )
            self._insert_revision(
                connection,
                loadout_id=loadout_id,
                revision_id=revision_id,
                revision_number=1,
                capture=capture,
                parent_revision_id=None,
                revision_action=revision_action,
                revision_note=revision_note,
            )
            connection.execute(
                "UPDATE loadouts SET current_revision_id = ? WHERE loadout_id = ?",
                (revision_id, loadout_id),
            )
            if replace_in_set_id is not None:
                board = connection.execute(
                    """
                    SELECT character_class_type FROM loadout_sets
                    WHERE set_id = ? AND bungie_membership_id = ?
                    """,
                    (replace_in_set_id, user_id),
                ).fetchone()
                if board is None or int(board["character_class_type"]) != int(
                    capture["character_class_type"]
                ):
                    raise ValueError(
                        "The set is unavailable or has the wrong class."
                    )
                changed = connection.execute(
                    """
                    UPDATE loadout_set_slots SET loadout_id = ?
                    WHERE set_id = ? AND loadout_id = ?
                    """,
                    (loadout_id, replace_in_set_id, replace_loadout_id),
                ).rowcount
                if not changed:
                    raise ValueError(
                        "The original loadout is no longer used by that set."
                    )
                connection.execute(
                    """
                    UPDATE loadout_sets
                    SET version = version + 1, updated_at = ?
                    WHERE set_id = ?
                    """,
                    (now, replace_in_set_id),
                )
        saved = self.load_saved_loadout(user_id, loadout_id)
        if saved is None:
            raise RuntimeError("Saved loadout could not be reloaded.")
        return saved

    def append_captured_loadout_revision(
        self,
        user_id: str,
        loadout_id: str,
        *,
        capture: dict[str, Any],
        revision_note: str = "",
        revision_action: str = "recapture",
    ) -> dict[str, Any]:
        """Append a complete immutable revision and make it current."""

        revision_id = secrets.token_hex(16)
        now = as_iso(utc_now())
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT current_revision_id, character_class_type
                FROM loadouts
                WHERE bungie_membership_id = ? AND loadout_id = ?
                """,
                (user_id, loadout_id),
            ).fetchone()
            if row is None:
                raise LookupError("The saved loadout is unavailable.")
            if int(row["character_class_type"]) != int(
                capture["character_class_type"]
            ):
                raise ValueError("A revision cannot change character class.")
            number = connection.execute(
                "SELECT COALESCE(MAX(revision_number), 0) + 1 FROM loadout_revisions WHERE loadout_id = ?",
                (loadout_id,),
            ).fetchone()[0]
            self._insert_revision(
                connection,
                loadout_id=loadout_id,
                revision_id=revision_id,
                revision_number=int(number),
                capture=capture,
                parent_revision_id=row["current_revision_id"],
                revision_action=revision_action,
                revision_note=revision_note,
            )
            connection.execute(
                """
                UPDATE loadouts
                SET current_revision_id = ?, updated_at = ?
                WHERE bungie_membership_id = ? AND loadout_id = ?
                """,
                (revision_id, now, user_id, loadout_id),
            )
        saved = self.load_saved_loadout(user_id, loadout_id)
        if saved is None:
            raise RuntimeError("Revised loadout could not be reloaded.")
        return saved

    def load_saved_loadout(
        self,
        user_id: str,
        loadout_id: str,
        *,
        revision_id: str | None = None,
        include_archived: bool = False,
    ) -> dict[str, Any] | None:
        """Load one loadout with all exact items and socket rows."""

        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT loadout.*, revision.*
                FROM loadouts AS loadout
                JOIN loadout_revisions AS revision
                  ON revision.revision_id = COALESCE(?, loadout.current_revision_id)
                 AND revision.loadout_id = loadout.loadout_id
                WHERE loadout.bungie_membership_id = ?
                  AND loadout.loadout_id = ?
                  AND (? OR loadout.archived_at IS NULL)
                """,
                (revision_id, user_id, loadout_id, int(include_archived)),
            ).fetchone()
            if row is None:
                return None
            result = self._decode_loadout(connection, dict(row))
        return result

    def list_saved_loadouts(
        self,
        user_id: str,
        *,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """List current loadout revisions newest first."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT loadout_id FROM loadouts
                WHERE bungie_membership_id = ?
                  AND (? OR archived_at IS NULL)
                ORDER BY favorite DESC, updated_at DESC, name COLLATE NOCASE
                """,
                (user_id, int(include_archived)),
            ).fetchall()
        return [
            value
            for row in rows
            if (
                value := self.load_saved_loadout(
                    user_id,
                    row["loadout_id"],
                    include_archived=include_archived,
                )
            ) is not None
        ]

    def list_loadout_revisions(
        self,
        user_id: str,
        loadout_id: str,
    ) -> list[dict[str, Any]]:
        """List every revision newest first."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT revision_id FROM loadout_revisions AS revision
                WHERE revision.loadout_id = ?
                  AND EXISTS (
                    SELECT 1 FROM loadouts
                    WHERE loadout_id = revision.loadout_id
                      AND bungie_membership_id = ?
                  )
                ORDER BY revision_number DESC
                """,
                (loadout_id, user_id),
            ).fetchall()
        return [
            value
            for row in rows
            if (
                value := self.load_saved_loadout(
                    user_id,
                    loadout_id,
                    revision_id=row["revision_id"],
                    include_archived=True,
                )
            ) is not None
        ]

    def update_loadout_metadata(
        self,
        user_id: str,
        loadout_id: str,
        *,
        name: str,
        description: str,
        tags: list[str],
        cover_icon_hash: int | None = None,
    ) -> None:
        """Update mutable display metadata only."""

        with self.connection() as connection:
            changed = connection.execute(
                """
                UPDATE loadouts
                SET name = ?, description = ?, tags_json = ?,
                    cover_icon_hash = ?, updated_at = ?
                WHERE bungie_membership_id = ? AND loadout_id = ?
                """,
                (
                    name, description, compact_json(tags), cover_icon_hash,
                    as_iso(utc_now()),
                    user_id, loadout_id,
                ),
            ).rowcount
        if not changed:
            raise LookupError("The saved loadout is unavailable.")

    def clone_loadout(
        self,
        user_id: str,
        loadout_id: str,
        *,
        name: str,
    ) -> dict[str, Any]:
        """Clone the current complete revision into a new loadout."""

        source = self.load_saved_loadout(
            user_id, loadout_id, include_archived=True
        )
        if source is None:
            raise LookupError("The saved loadout is unavailable.")
        return self.save_captured_loadout(
            user_id,
            name=name,
            description=source["description"],
            tags=list(source["tags"]),
            capture=_capture_from_saved(source),
            cover_icon_hash=source.get("cover_icon_hash"),
            revision_action="clone",
            revision_note=f"Cloned from {loadout_id}",
        )

    def restore_loadout_revision(
        self,
        user_id: str,
        loadout_id: str,
        revision_id: str,
        *,
        revision_note: str,
    ) -> dict[str, Any]:
        """Append old content as a new revision; never mutates history."""

        source = self.load_saved_loadout(
            user_id,
            loadout_id,
            revision_id=revision_id,
            include_archived=True,
        )
        if source is None:
            raise LookupError("The selected revision is unavailable.")
        return self.append_captured_loadout_revision(
            user_id,
            loadout_id,
            capture=_capture_from_saved(source),
            revision_note=revision_note,
            revision_action="restore",
        )

    def set_loadout_archived(
        self,
        user_id: str,
        loadout_id: str,
        *,
        archived: bool,
    ) -> None:
        """Set or clear the archive timestamp."""

        value = as_iso(utc_now()) if archived else None
        with self.connection() as connection:
            changed = connection.execute(
                """
                UPDATE loadouts SET archived_at = ?, updated_at = ?
                WHERE bungie_membership_id = ? AND loadout_id = ?
                """,
                (value, as_iso(utc_now()), user_id, loadout_id),
            ).rowcount
        if not changed:
            raise LookupError("The saved loadout is unavailable.")

    def set_loadout_favorite(
        self,
        user_id: str,
        loadout_id: str,
        *,
        favorite: bool,
    ) -> None:
        """Set the favorite flag."""

        with self.connection() as connection:
            changed = connection.execute(
                """
                UPDATE loadouts SET favorite = ?, updated_at = ?
                WHERE bungie_membership_id = ? AND loadout_id = ?
                """,
                (int(favorite), as_iso(utc_now()), user_id, loadout_id),
            ).rowcount
        if not changed:
            raise LookupError("The saved loadout is unavailable.")

    def loadout_set_membership_names(self, user_id: str) -> dict[str, list[str]]:
        """Map every pinned loadout to the names of the sets that pin it."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT slot.loadout_id, board.name
                FROM loadout_set_slots AS slot
                JOIN loadout_sets AS board ON board.set_id = slot.set_id
                WHERE board.bungie_membership_id = ?
                ORDER BY board.name
                """,
                (user_id,),
            ).fetchall()
        memberships: dict[str, list[str]] = {}
        for row in rows:
            memberships.setdefault(row["loadout_id"], []).append(row["name"])
        return memberships

    def loadout_set_memberships(
        self, user_id: str, loadout_id: str
    ) -> list[dict[str, Any]]:
        """List every set position that currently pins one loadout."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT board.set_id, board.name, slot.position
                FROM loadout_set_slots AS slot
                JOIN loadout_sets AS board ON board.set_id = slot.set_id
                WHERE board.bungie_membership_id = ? AND slot.loadout_id = ?
                ORDER BY board.name, slot.position
                """,
                (user_id, loadout_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_loadout(
        self, user_id: str, loadout_id: str, *, detach_from_sets: bool = False
    ) -> None:
        """Delete a loadout unless a current set or legacy plan pins it."""

        with self.connection() as connection:
            current_set = connection.execute(
                """
                SELECT board.name
                FROM loadout_set_slots AS slot
                JOIN loadout_sets AS board ON board.set_id = slot.set_id
                WHERE board.bungie_membership_id = ? AND slot.loadout_id = ?
                LIMIT 1
                """,
                (user_id, loadout_id),
            ).fetchone()
            if current_set is not None and not detach_from_sets:
                raise ValueError(
                    f"Remove this loadout from {current_set['name']} before deleting it."
                )
            if current_set is not None:
                connection.execute(
                    """
                    UPDATE loadout_sets SET updated_at = ?
                    WHERE bungie_membership_id = ? AND set_id IN (
                        SELECT set_id FROM loadout_set_slots WHERE loadout_id = ?
                    )
                    """,
                    (as_iso(utc_now()), user_id, loadout_id),
                )
                connection.execute(
                    """
                    DELETE FROM loadout_set_slots
                    WHERE loadout_id = ? AND set_id IN (
                        SELECT set_id FROM loadout_sets
                        WHERE bungie_membership_id = ?
                    )
                    """,
                    (loadout_id, user_id),
                )
            changed = connection.execute(
                "DELETE FROM loadouts WHERE bungie_membership_id = ? AND loadout_id = ?",
                (user_id, loadout_id),
            ).rowcount
        if not changed:
            raise LookupError("The saved loadout is unavailable.")

    def create_loadout_set(
        self,
        user_id: str,
        *,
        name: str,
        character_class_type: int,
    ) -> dict[str, Any]:
        """Create one empty, class-bound 20-position board."""

        set_id = secrets.token_hex(16)
        now = as_iso(utc_now())
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO loadout_sets(
                    set_id, bungie_membership_id, name,
                    character_class_type, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (set_id, user_id, name, character_class_type, now, now),
            )
        result = self.load_loadout_set(user_id, set_id)
        if result is None:
            raise RuntimeError("The loadout set could not be reloaded.")
        return result

    def create_loadout_set_from_captures(
        self,
        user_id: str,
        *,
        name: str,
        character_class_type: int,
        loadouts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Atomically create a set and one new loadout per populated position."""

        positions = [int(value["position"]) for value in loadouts]
        if not loadouts:
            raise ValueError("At least one populated loadout slot is required.")
        if len(positions) != len(set(positions)) or any(
            position < 0 or position >= 20 for position in positions
        ):
            raise ValueError(
                "Imported loadout positions must be unique and between 0 and 19."
            )
        if any(
            int(value["capture"]["character_class_type"])
            != int(character_class_type)
            for value in loadouts
        ):
            raise ValueError("Every imported loadout must match the set class.")

        set_id = secrets.token_hex(16)
        now = as_iso(utc_now())
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO loadout_sets(
                    set_id, bungie_membership_id, name,
                    character_class_type, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (set_id, user_id, name, character_class_type, now, now),
            )
            for value in loadouts:
                loadout_id = secrets.token_hex(16)
                revision_id = secrets.token_hex(16)
                capture = value["capture"]
                connection.execute(
                    """
                    INSERT INTO loadouts(
                        loadout_id, bungie_membership_id, name, description,
                        tags_json, character_class_type, cover_icon_hash,
                        current_revision_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        loadout_id,
                        user_id,
                        value["name"],
                        value.get("description", ""),
                        compact_json(value.get("tags", [])),
                        character_class_type,
                        value.get("cover_icon_hash"),
                        now,
                        now,
                    ),
                )
                self._insert_revision(
                    connection,
                    loadout_id=loadout_id,
                    revision_id=revision_id,
                    revision_number=1,
                    capture=capture,
                    parent_revision_id=None,
                    revision_action="set_character_import",
                    revision_note=(
                        f"Imported from in-game slot {int(value['position']) + 1}"
                    ),
                )
                connection.execute(
                    "UPDATE loadouts SET current_revision_id = ? WHERE loadout_id = ?",
                    (revision_id, loadout_id),
                )
                connection.execute(
                    """
                    INSERT INTO loadout_set_slots(set_id, position, loadout_id)
                    VALUES (?, ?, ?)
                    """,
                    (set_id, int(value["position"]), loadout_id),
                )
        result = self.load_loadout_set(user_id, set_id)
        if result is None:
            raise RuntimeError("The imported loadout set could not be reloaded.")
        return result

    def list_loadout_sets(self, user_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT set_id FROM loadout_sets
                WHERE bungie_membership_id = ?
                ORDER BY updated_at DESC, name COLLATE NOCASE
                """,
                (user_id,),
            ).fetchall()
        return [
            value
            for row in rows
            if (value := self.load_loadout_set(user_id, row["set_id"]))
            is not None
        ]

    def load_loadout_set(
        self, user_id: str, set_id: str
    ) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM loadout_sets
                WHERE bungie_membership_id = ? AND set_id = ?
                """,
                (user_id, set_id),
            ).fetchone()
            if row is None:
                return None
            slots = connection.execute(
                """
                SELECT slot.position, slot.loadout_id, loadout.name,
                       loadout.character_class_type, loadout.cover_icon_hash,
                       loadout.current_revision_id, loadout.updated_at
                FROM loadout_set_slots AS slot
                JOIN loadouts AS loadout ON loadout.loadout_id = slot.loadout_id
                WHERE slot.set_id = ?
                ORDER BY slot.position
                """,
                (set_id,),
            ).fetchall()
        result = dict(row)
        result["slots"] = [dict(slot) for slot in slots]
        result["filled_count"] = len(slots)
        return result

    def rename_loadout_set(
        self, user_id: str, set_id: str, *, name: str
    ) -> dict[str, Any]:
        with self.connection() as connection:
            changed = connection.execute(
                """
                UPDATE loadout_sets
                SET name = ?, version = version + 1, updated_at = ?
                WHERE bungie_membership_id = ? AND set_id = ?
                """,
                (name, as_iso(utc_now()), user_id, set_id),
            ).rowcount
        if not changed:
            raise LookupError("The loadout set is unavailable.")
        result = self.load_loadout_set(user_id, set_id)
        assert result is not None
        return result

    def save_loadout_set_slots(
        self,
        user_id: str,
        set_id: str,
        *,
        slots: dict[int, str],
        expected_version: int,
    ) -> dict[str, Any]:
        """Replace the complete board atomically with optimistic locking."""

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT character_class_type, version FROM loadout_sets
                WHERE bungie_membership_id = ? AND set_id = ?
                """,
                (user_id, set_id),
            ).fetchone()
            if row is None:
                raise LookupError("The loadout set is unavailable.")
            if int(row["version"]) != int(expected_version):
                raise ValueError(
                    "The loadout set changed while you were editing it. Reload and try again."
                )
            if any(position < 0 or position >= 20 for position in slots):
                raise ValueError("Loadout set positions must be between 0 and 19.")
            if slots:
                placeholders = ",".join("?" for _ in slots)
                found = connection.execute(
                    f"""
                    SELECT loadout_id, character_class_type FROM loadouts
                    WHERE bungie_membership_id = ?
                      AND loadout_id IN ({placeholders})
                    """,
                    (user_id, *slots.values()),
                ).fetchall()
                classes = {
                    str(value["loadout_id"]): int(value["character_class_type"])
                    for value in found
                }
                if set(classes) != set(slots.values()):
                    raise ValueError("One or more selected loadouts are unavailable.")
                if any(
                    class_type != int(row["character_class_type"])
                    for class_type in classes.values()
                ):
                    raise ValueError("Every loadout in a set must match its class.")
            connection.execute(
                "DELETE FROM loadout_set_slots WHERE set_id = ?", (set_id,)
            )
            connection.executemany(
                """
                INSERT INTO loadout_set_slots(set_id, position, loadout_id)
                VALUES (?, ?, ?)
                """,
                ((set_id, position, loadout_id) for position, loadout_id in slots.items()),
            )
            connection.execute(
                """
                UPDATE loadout_sets
                SET version = version + 1, updated_at = ?
                WHERE set_id = ?
                """,
                (as_iso(utc_now()), set_id),
            )
        result = self.load_loadout_set(user_id, set_id)
        assert result is not None
        return result

    def loadout_set_usages(
        self, user_id: str, loadout_id: str
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT board.set_id, board.name, board.character_class_type,
                       GROUP_CONCAT(slot.position) AS positions
                FROM loadout_set_slots AS slot
                JOIN loadout_sets AS board ON board.set_id = slot.set_id
                WHERE board.bungie_membership_id = ? AND slot.loadout_id = ?
                GROUP BY board.set_id, board.name, board.character_class_type
                ORDER BY board.name COLLATE NOCASE
                """,
                (user_id, loadout_id),
            ).fetchall()
        return [
            {
                **dict(row),
                "positions": sorted(
                    int(value) for value in str(row["positions"]).split(",")
                ),
            }
            for row in rows
        ]

    def replace_loadout_in_set(
        self,
        user_id: str,
        set_id: str,
        *,
        old_loadout_id: str,
        new_loadout_id: str,
    ) -> int:
        """Replace every occurrence in one set and advance its version."""

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            board = connection.execute(
                """
                SELECT character_class_type FROM loadout_sets
                WHERE bungie_membership_id = ? AND set_id = ?
                """,
                (user_id, set_id),
            ).fetchone()
            loadout = connection.execute(
                """
                SELECT character_class_type FROM loadouts
                WHERE bungie_membership_id = ? AND loadout_id = ?
                """,
                (user_id, new_loadout_id),
            ).fetchone()
            if board is None or loadout is None:
                raise LookupError("The set or replacement loadout is unavailable.")
            if int(board["character_class_type"]) != int(loadout["character_class_type"]):
                raise ValueError("The replacement loadout has the wrong class.")
            changed = connection.execute(
                """
                UPDATE loadout_set_slots SET loadout_id = ?
                WHERE set_id = ? AND loadout_id = ?
                """,
                (new_loadout_id, set_id, old_loadout_id),
            ).rowcount
            if not changed:
                raise LookupError("That loadout is not used by this set.")
            connection.execute(
                """
                UPDATE loadout_sets SET version = version + 1, updated_at = ?
                WHERE set_id = ?
                """,
                (as_iso(utc_now()), set_id),
            )
        return int(changed)

    def delete_loadout_set(self, user_id: str, set_id: str) -> None:
        with self.connection() as connection:
            changed = connection.execute(
                """
                DELETE FROM loadout_sets
                WHERE bungie_membership_id = ? AND set_id = ?
                """,
                (user_id, set_id),
            ).rowcount
        if not changed:
            raise LookupError("The loadout set is unavailable.")

    def application_loadout_instance_ids(self, user_id: str) -> set[str]:
        """Return exact instances protected by current loadouts and pinned sets."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT item.item_instance_id
                FROM loadout_revision_items AS item
                JOIN loadout_revisions AS revision
                  ON revision.revision_id = item.revision_id
                JOIN loadouts AS loadout
                  ON loadout.loadout_id = revision.loadout_id
                WHERE loadout.bungie_membership_id = ?
                  AND (
                    revision.revision_id = loadout.current_revision_id
                    OR EXISTS (
                      SELECT 1
                      FROM loadout_plan_assignments AS assignment
                      JOIN loadout_plan_revisions AS plan_revision
                        ON plan_revision.plan_revision_id = assignment.plan_revision_id
                      JOIN loadout_plans AS plan
                        ON plan.plan_id = plan_revision.plan_id
                      WHERE assignment.loadout_revision_id = revision.revision_id
                        AND plan.bungie_membership_id = ?
                        AND plan_revision.plan_revision_id = plan.current_revision_id
                    )
                  )
                """,
                (user_id, user_id),
            ).fetchall()
        return {str(row["item_instance_id"]) for row in rows}

    def create_loadout_plan(
        self,
        user_id: str,
        *,
        name: str,
        description: str,
        game: str,
        activity_name: str,
        activity_version: str,
    ) -> dict[str, Any]:
        """Create an empty set with immutable revision one."""

        plan_id = secrets.token_hex(16)
        revision_id = secrets.token_hex(16)
        now = as_iso(utc_now())
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO loadout_plans(
                    plan_id, bungie_membership_id, current_revision_id,
                    created_at, updated_at
                ) VALUES (?, ?, NULL, ?, ?)
                """,
                (plan_id, user_id, now, now),
            )
            self._insert_plan_revision(
                connection,
                plan_id=plan_id,
                revision_id=revision_id,
                revision_number=1,
                metadata={
                    "name": name,
                    "description": description,
                    "game": game,
                    "activity_name": activity_name,
                    "activity_version": activity_version,
                },
                change_note="Created loadout set",
                encounters=[],
            )
            connection.execute(
                "UPDATE loadout_plans SET current_revision_id = ? WHERE plan_id = ?",
                (revision_id, plan_id),
            )
        result = self.load_loadout_plan(user_id, plan_id)
        if result is None:
            raise RuntimeError("Created loadout set could not be reloaded.")
        return result

    def list_loadout_plans(
        self,
        user_id: str,
        *,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """List current set revisions newest first."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT plan_id FROM loadout_plans
                WHERE bungie_membership_id = ?
                  AND (? OR archived_at IS NULL)
                ORDER BY updated_at DESC
                """,
                (user_id, int(include_archived)),
            ).fetchall()
        return [
            value
            for row in rows
            if (
                value := self.load_loadout_plan(
                    user_id,
                    row["plan_id"],
                    include_archived=include_archived,
                )
            ) is not None
        ]

    def load_loadout_plan(
        self,
        user_id: str,
        plan_id: str,
        *,
        include_archived: bool = False,
        revision_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Load one set revision with nested encounters and assignments."""

        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT plan.*, revision.*
                FROM loadout_plans AS plan
                JOIN loadout_plan_revisions AS revision
                  ON revision.plan_revision_id = COALESCE(?, plan.current_revision_id)
                 AND revision.plan_id = plan.plan_id
                WHERE plan.bungie_membership_id = ? AND plan.plan_id = ?
                  AND (? OR plan.archived_at IS NULL)
                """,
                (revision_id, user_id, plan_id, int(include_archived)),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["canonical_payload"] = _json_object(
                result.pop("canonical_payload_json")
            )
            encounter_rows = connection.execute(
                """
                SELECT * FROM loadout_plan_encounters
                WHERE plan_revision_id = ? ORDER BY encounter_order
                """,
                (result["plan_revision_id"],),
            ).fetchall()
            assignment_rows = connection.execute(
                """
                SELECT assignment.*, revision.loadout_id,
                       revision.revision_number AS loadout_revision_number,
                       loadout.name AS loadout_name,
                       loadout.character_class_type,
                       loadout.archived_at AS loadout_archived_at
                FROM loadout_plan_assignments AS assignment
                JOIN loadout_revisions AS revision
                  ON revision.revision_id = assignment.loadout_revision_id
                JOIN loadouts AS loadout ON loadout.loadout_id = revision.loadout_id
                WHERE assignment.plan_revision_id = ?
                ORDER BY assignment.encounter_id, assignment.assignment_order
                """,
                (result["plan_revision_id"],),
            ).fetchall()
        assignments = []
        for row in assignment_rows:
            assignment = dict(row)
            assignment["loadout_archived"] = bool(
                assignment.pop("loadout_archived_at")
            )
            assignments.append(assignment)
        encounters = []
        for row in encounter_rows:
            encounter = dict(row)
            encounter["assignments"] = [
                assignment
                for assignment in assignments
                if assignment["encounter_id"] == encounter["encounter_id"]
            ]
            encounters.append(encounter)
        result["encounters"] = encounters
        result["assignments"] = assignments
        result["encounter_count"] = len(encounters)
        result["assignment_count"] = len(assignments)
        result["target_character_count"] = len(
            {row["target_character_id"] for row in assignments}
        )
        return result

    def list_loadout_plan_revisions(
        self,
        user_id: str,
        plan_id: str,
    ) -> list[dict[str, Any]]:
        """List immutable set revision headers."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT revision.*,
                       (
                         SELECT COUNT(*) FROM loadout_plan_encounters
                         WHERE plan_revision_id = revision.plan_revision_id
                       ) AS encounter_count,
                       (
                         SELECT COUNT(*) FROM loadout_plan_assignments
                         WHERE plan_revision_id = revision.plan_revision_id
                       ) AS assignment_count
                FROM loadout_plan_revisions AS revision
                JOIN loadout_plans AS plan ON plan.plan_id = revision.plan_id
                WHERE plan.bungie_membership_id = ? AND plan.plan_id = ?
                ORDER BY revision.revision_number DESC
                """,
                (user_id, plan_id),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["canonical_payload"] = _json_object(
                value.pop("canonical_payload_json")
            )
            result.append(value)
        return result

    def update_loadout_plan_metadata(
        self,
        user_id: str,
        plan_id: str,
        *,
        name: str,
        description: str,
        game: str,
        activity_name: str,
        activity_version: str,
        change_note: str,
    ) -> dict[str, Any]:
        """Append a set revision with changed metadata."""

        return self._revise_plan(
            user_id,
            plan_id,
            metadata={
                "name": name,
                "description": description,
                "game": game,
                "activity_name": activity_name,
                "activity_version": activity_version,
            },
            change_note=change_note or "Updated set metadata",
        )

    def add_loadout_plan_encounter(
        self,
        user_id: str,
        plan_id: str,
        *,
        encounter_order: int,
        name: str,
        notes: str,
    ) -> dict[str, Any]:
        """Append a set revision with one new encounter."""

        encounter_id = secrets.token_hex(16)

        def mutate(encounters: list[dict[str, Any]]) -> None:
            encounters.append(
                {
                    "encounter_id": encounter_id,
                    "encounter_order": encounter_order,
                    "name": name,
                    "notes": notes,
                    "assignments": [],
                }
            )

        return self._revise_plan(
            user_id,
            plan_id,
            mutate=mutate,
            change_note=f"Added encounter: {name}",
        )

    def remove_loadout_plan_encounter(
        self,
        user_id: str,
        plan_id: str,
        encounter_id: str,
    ) -> dict[str, Any]:
        """Append a set revision without one encounter."""

        def mutate(encounters: list[dict[str, Any]]) -> None:
            original = len(encounters)
            encounters[:] = [
                row for row in encounters if row["encounter_id"] != encounter_id
            ]
            if len(encounters) == original:
                raise LookupError("The encounter is unavailable.")

        return self._revise_plan(
            user_id,
            plan_id,
            mutate=mutate,
            change_note="Removed encounter",
        )

    def update_loadout_plan_encounter(
        self,
        user_id: str,
        plan_id: str,
        encounter_id: str,
        *,
        encounter_order: int,
        name: str,
        notes: str,
    ) -> dict[str, Any]:
        """Append a set revision with one encounter changed."""

        def mutate(encounters: list[dict[str, Any]]) -> None:
            for row in encounters:
                if row["encounter_id"] == encounter_id:
                    row.update(
                        encounter_order=encounter_order,
                        name=name,
                        notes=notes,
                    )
                    return
            raise LookupError("The encounter is unavailable.")

        return self._revise_plan(
            user_id,
            plan_id,
            mutate=mutate,
            change_note=f"Updated encounter: {name}",
        )

    def add_loadout_plan_assignment(
        self,
        user_id: str,
        plan_id: str,
        *,
        encounter_id: str,
        assignment_order: int,
        loadout_revision_id: str,
        target_character_id: str,
        target_slot_index: int,
        notes: str,
    ) -> dict[str, Any]:
        """Append a set revision with one pinned slot assignment."""

        assignment_id = secrets.token_hex(16)

        def mutate(encounters: list[dict[str, Any]]) -> None:
            for row in encounters:
                if row["encounter_id"] == encounter_id:
                    row["assignments"].append(
                        {
                            "assignment_id": assignment_id,
                            "encounter_id": encounter_id,
                            "assignment_order": assignment_order,
                            "loadout_revision_id": loadout_revision_id,
                            "target_character_id": target_character_id,
                            "target_slot_index": target_slot_index,
                            "notes": notes,
                        }
                    )
                    return
            raise LookupError("The encounter is unavailable.")

        return self._revise_plan(
            user_id,
            plan_id,
            mutate=mutate,
            change_note="Assigned pinned loadout revision",
        )

    def remove_loadout_plan_assignment(
        self,
        user_id: str,
        plan_id: str,
        assignment_id: str,
    ) -> dict[str, Any]:
        """Append a set revision without one assignment."""

        def mutate(encounters: list[dict[str, Any]]) -> None:
            for row in encounters:
                original = len(row["assignments"])
                row["assignments"] = [
                    value
                    for value in row["assignments"]
                    if value["assignment_id"] != assignment_id
                ]
                if len(row["assignments"]) != original:
                    return
            raise LookupError("The assignment is unavailable.")

        return self._revise_plan(
            user_id,
            plan_id,
            mutate=mutate,
            change_note="Removed slot assignment",
        )

    def set_loadout_plan_archived(
        self,
        user_id: str,
        plan_id: str,
        *,
        archived: bool,
    ) -> None:
        """Archive or restore a set."""

        with self.connection() as connection:
            changed = connection.execute(
                """
                UPDATE loadout_plans SET archived_at = ?, updated_at = ?
                WHERE bungie_membership_id = ? AND plan_id = ?
                """,
                (
                    as_iso(utc_now()) if archived else None,
                    as_iso(utc_now()), user_id, plan_id,
                ),
            ).rowcount
        if not changed:
            raise LookupError("The loadout set is unavailable.")

    def delete_loadout_plan(self, user_id: str, plan_id: str) -> None:
        """Delete a local set and its immutable history."""

        with self.connection() as connection:
            changed = connection.execute(
                "DELETE FROM loadout_plans WHERE bungie_membership_id = ? AND plan_id = ?",
                (user_id, plan_id),
            ).rowcount
        if not changed:
            raise LookupError("The loadout set is unavailable.")

    def _insert_revision(
        self,
        connection: sqlite3.Connection,
        *,
        loadout_id: str,
        revision_id: str,
        revision_number: int,
        capture: dict[str, Any],
        parent_revision_id: str | None,
        revision_action: str,
        revision_note: str,
    ) -> None:
        items = list(capture["items"])
        canonical = {
            "character_class_type": int(capture["character_class_type"]),
            "items": items,
            "reference_items": list(capture.get("reference_items", [])),
            "partial": bool(capture.get("partial")),
            "unresolved_item_instance_ids": list(
                capture.get("unresolved_item_instance_ids", [])
            ),
        }
        connection.execute(
            """
            INSERT INTO loadout_revisions(
                revision_id, loadout_id, revision_number, snapshot_id,
                manifest_version, capture_source, source_character_id,
                source_slot_index, captured_at, capture_validation_status,
                capture_issues_json, canonical_payload_json,
                source_payload_json, parent_revision_id, revision_action,
                revision_note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'valid', '[]', ?, ?, ?, ?, ?)
            """,
            (
                revision_id, loadout_id, revision_number,
                capture["snapshot_id"], capture["manifest_version"],
                capture["capture_source"], capture["source_character_id"],
                capture.get("source_slot_index"), as_iso(utc_now()),
                compact_json(canonical),
                compact_json(capture.get("source_payload", {})),
                parent_revision_id, revision_action, revision_note,
            ),
        )
        for item in items:
            order = int(item["equipment_order"])
            connection.execute(
                """
                INSERT INTO loadout_revision_items(
                    revision_id, equipment_order, item_instance_id, item_hash,
                    bucket_hash, intended_class_type, captured_source_kind,
                    captured_character_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id, order, item["item_instance_id"],
                    int(item["item_hash"]), int(item["bucket_hash"]),
                    int(capture["character_class_type"]),
                    item["captured_source_kind"],
                    item.get("captured_character_id"),
                ),
            )
            for plug in item.get("plugs", []):
                connection.execute(
                    """
                    INSERT INTO loadout_revision_plugs(
                        revision_id, equipment_order, socket_index, plug_hash,
                        sync_capability, filtered_from_preview
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id, order, int(plug["socket_index"]),
                        plug.get("plug_hash"),
                        str(plug.get("sync_capability") or "unsupported"),
                        int(bool(plug.get("filtered_from_preview"))),
                    ),
                )

    def _decode_loadout(
        self,
        connection: sqlite3.Connection,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        value["tags"] = _json_list(value.pop("tags_json"))
        value["capture_issues"] = _json_list(
            value.pop("capture_issues_json")
        )
        value["canonical_payload"] = _json_object(
            value.pop("canonical_payload_json")
        )
        value["source_payload"] = _json_object(
            value.pop("source_payload_json")
        )
        item_rows = connection.execute(
            """
            SELECT * FROM loadout_revision_items
            WHERE revision_id = ? ORDER BY equipment_order
            """,
            (value["revision_id"],),
        ).fetchall()
        plug_rows = connection.execute(
            """
            SELECT * FROM loadout_revision_plugs
            WHERE revision_id = ? ORDER BY equipment_order, socket_index
            """,
            (value["revision_id"],),
        ).fetchall()
        plugs = [dict(row) for row in plug_rows]
        value["items"] = []
        for row in item_rows:
            item = dict(row)
            item["plugs"] = [
                plug
                for plug in plugs
                if plug["equipment_order"] == item["equipment_order"]
            ]
            value["items"].append(item)
        return value

    def _revise_plan(
        self,
        user_id: str,
        plan_id: str,
        *,
        metadata: dict[str, str] | None = None,
        mutate: Callable[[list[dict[str, Any]]], None] | None = None,
        change_note: str,
    ) -> dict[str, Any]:
        current = self.load_loadout_plan(
            user_id, plan_id, include_archived=True
        )
        if current is None:
            raise LookupError("The loadout set is unavailable.")
        encounters = [
            {
                "encounter_id": row["encounter_id"],
                "encounter_order": int(row["encounter_order"]),
                "name": row["name"],
                "notes": row["notes"],
                "assignments": [
                    {
                        "assignment_id": assignment["assignment_id"],
                        "encounter_id": row["encounter_id"],
                        "assignment_order": int(
                            assignment["assignment_order"]
                        ),
                        "loadout_revision_id": assignment[
                            "loadout_revision_id"
                        ],
                        "target_character_id": assignment[
                            "target_character_id"
                        ],
                        "target_slot_index": int(
                            assignment["target_slot_index"]
                        ),
                        "notes": assignment["notes"],
                    }
                    for assignment in row["assignments"]
                ],
            }
            for row in current["encounters"]
        ]
        if mutate is not None:
            mutate(encounters)
        values = metadata or {
            key: current[key]
            for key in (
                "name", "description", "game", "activity_name",
                "activity_version",
            )
        }
        revision_id = secrets.token_hex(16)
        now = as_iso(utc_now())
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT current_revision_id FROM loadout_plans
                WHERE bungie_membership_id = ? AND plan_id = ?
                """,
                (user_id, plan_id),
            ).fetchone()
            if row is None or row["current_revision_id"] != current[
                "plan_revision_id"
            ]:
                raise sqlite3.IntegrityError(
                    "The set changed while its revision was being prepared."
                )
            number = connection.execute(
                "SELECT COALESCE(MAX(revision_number), 0) + 1 FROM loadout_plan_revisions WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()[0]
            self._insert_plan_revision(
                connection,
                plan_id=plan_id,
                revision_id=revision_id,
                revision_number=int(number),
                metadata=values,
                change_note=change_note,
                encounters=encounters,
            )
            connection.execute(
                """
                UPDATE loadout_plans
                SET current_revision_id = ?, updated_at = ?
                WHERE plan_id = ?
                """,
                (revision_id, now, plan_id),
            )
        result = self.load_loadout_plan(
            user_id, plan_id, include_archived=True
        )
        if result is None:
            raise RuntimeError("Revised loadout set could not be reloaded.")
        return result

    def _insert_plan_revision(
        self,
        connection: sqlite3.Connection,
        *,
        plan_id: str,
        revision_id: str,
        revision_number: int,
        metadata: dict[str, str],
        change_note: str,
        encounters: list[dict[str, Any]],
    ) -> None:
        canonical = {
            **metadata,
            "encounters": encounters,
        }
        connection.execute(
            """
            INSERT INTO loadout_plan_revisions(
                plan_revision_id, plan_id, revision_number, name, description,
                game, activity_name, activity_version, change_note,
                canonical_payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id, plan_id, revision_number, metadata["name"],
                metadata["description"], metadata["game"],
                metadata["activity_name"], metadata["activity_version"],
                change_note, compact_json(canonical), as_iso(utc_now()),
            ),
        )
        for encounter in encounters:
            connection.execute(
                """
                INSERT INTO loadout_plan_encounters(
                    plan_revision_id, encounter_id, encounter_order, name, notes
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    revision_id, encounter["encounter_id"],
                    int(encounter["encounter_order"]), encounter["name"],
                    encounter["notes"],
                ),
            )
            for assignment in encounter.get("assignments", []):
                connection.execute(
                    """
                    INSERT INTO loadout_plan_assignments(
                        plan_revision_id, assignment_id, encounter_id,
                        assignment_order, loadout_revision_id,
                        target_character_id, target_slot_index, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id, assignment["assignment_id"],
                        encounter["encounter_id"],
                        int(assignment["assignment_order"]),
                        assignment["loadout_revision_id"],
                        assignment["target_character_id"],
                        int(assignment["target_slot_index"]),
                        assignment["notes"],
                    ),
                )


def _capture_from_saved(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "snapshot_id": value["snapshot_id"],
        "manifest_version": value["manifest_version"],
        "capture_source": value["capture_source"],
        "source_character_id": value["source_character_id"],
        "source_slot_index": value.get("source_slot_index"),
        "character_class_type": int(value["character_class_type"]),
        "items": [
            {
                "equipment_order": int(item["equipment_order"]),
                "item_instance_id": item["item_instance_id"],
                "item_hash": int(item["item_hash"]),
                "bucket_hash": int(item["bucket_hash"]),
                "captured_source_kind": item["captured_source_kind"],
                "captured_character_id": item.get("captured_character_id"),
                "plugs": [dict(plug) for plug in item["plugs"]],
            }
            for item in value["items"]
        ],
        "reference_items": list(
            value["canonical_payload"].get("reference_items", [])
        ),
        "partial": bool(value["canonical_payload"].get("partial")),
        "unresolved_item_instance_ids": list(
            value["canonical_payload"].get(
                "unresolved_item_instance_ids", []
            )
        ),
        "source_payload": dict(value["source_payload"]),
    }


def _json_object(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("Stored loadout object JSON is invalid.")
    return parsed


def _json_list(value: str) -> list[Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise ValueError("Stored loadout list JSON is invalid.")
    return parsed


LOADOUT_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS loadouts (
    loadout_id TEXT PRIMARY KEY,
    bungie_membership_id TEXT NOT NULL,
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 2000),
    tags_json TEXT NOT NULL DEFAULT '[]',
    character_class_type INTEGER NOT NULL CHECK (character_class_type BETWEEN 0 AND 3),
    cover_icon_hash INTEGER CHECK (
        cover_icon_hash IS NULL OR
        (cover_icon_hash > 0 AND cover_icon_hash < 4294967296)
    ),
    current_revision_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    favorite INTEGER NOT NULL DEFAULT 0 CHECK (favorite IN (0, 1)),
    FOREIGN KEY (bungie_membership_id) REFERENCES users(bungie_membership_id) ON DELETE CASCADE,
    FOREIGN KEY (current_revision_id) REFERENCES loadout_revisions(revision_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_loadouts_owner_time
    ON loadouts(bungie_membership_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS loadout_sets (
    set_id TEXT PRIMARY KEY,
    bungie_membership_id TEXT NOT NULL,
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    character_class_type INTEGER NOT NULL CHECK (
        character_class_type BETWEEN 0 AND 2
    ),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (bungie_membership_id)
        REFERENCES users(bungie_membership_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_loadout_sets_owner_time
    ON loadout_sets(bungie_membership_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS loadout_set_slots (
    set_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position BETWEEN 0 AND 19),
    loadout_id TEXT NOT NULL,
    PRIMARY KEY (set_id, position),
    FOREIGN KEY (set_id) REFERENCES loadout_sets(set_id) ON DELETE CASCADE,
    FOREIGN KEY (loadout_id) REFERENCES loadouts(loadout_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_loadout_set_slots_loadout
    ON loadout_set_slots(loadout_id);

CREATE TRIGGER IF NOT EXISTS loadout_set_slot_owner_class_insert
BEFORE INSERT ON loadout_set_slots
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM loadout_sets AS board
        JOIN loadouts AS loadout
          ON loadout.loadout_id = NEW.loadout_id
         AND loadout.bungie_membership_id = board.bungie_membership_id
         AND loadout.character_class_type = board.character_class_type
        WHERE board.set_id = NEW.set_id
    ) THEN RAISE(ABORT, 'loadout set slot owner or class mismatch') END;
END;

CREATE TRIGGER IF NOT EXISTS loadout_set_slot_owner_class_update
BEFORE UPDATE OF set_id, loadout_id ON loadout_set_slots
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM loadout_sets AS board
        JOIN loadouts AS loadout
          ON loadout.loadout_id = NEW.loadout_id
         AND loadout.bungie_membership_id = board.bungie_membership_id
         AND loadout.character_class_type = board.character_class_type
        WHERE board.set_id = NEW.set_id
    ) THEN RAISE(ABORT, 'loadout set slot owner or class mismatch') END;
END;

CREATE TABLE IF NOT EXISTS loadout_revisions (
    revision_id TEXT PRIMARY KEY,
    loadout_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    snapshot_id TEXT NOT NULL,
    manifest_version TEXT NOT NULL,
    capture_source TEXT NOT NULL CHECK (capture_source IN ('current_equipment', 'in_game_slot')),
    source_character_id TEXT NOT NULL,
    source_slot_index INTEGER CHECK (source_slot_index >= 0),
    captured_at TEXT NOT NULL,
    capture_validation_status TEXT NOT NULL CHECK (capture_validation_status IN ('valid', 'invalid')),
    capture_issues_json TEXT NOT NULL DEFAULT '[]',
    canonical_payload_json TEXT NOT NULL,
    source_payload_json TEXT NOT NULL,
    parent_revision_id TEXT REFERENCES loadout_revisions(revision_id) ON DELETE CASCADE,
    revision_action TEXT NOT NULL DEFAULT 'capture',
    revision_note TEXT NOT NULL DEFAULT '',
    UNIQUE (loadout_id, revision_number),
    FOREIGN KEY (loadout_id) REFERENCES loadouts(loadout_id) ON DELETE CASCADE,
    FOREIGN KEY (snapshot_id) REFERENCES inventory_snapshots(snapshot_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_loadout_revisions_snapshot
    ON loadout_revisions(snapshot_id);

CREATE TABLE IF NOT EXISTS loadout_revision_items (
    revision_id TEXT NOT NULL,
    equipment_order INTEGER NOT NULL CHECK (equipment_order >= 0),
    item_instance_id TEXT NOT NULL,
    item_hash INTEGER NOT NULL CHECK (item_hash > 0 AND item_hash < 4294967296),
    bucket_hash INTEGER NOT NULL CHECK (bucket_hash > 0 AND bucket_hash < 4294967296),
    intended_class_type INTEGER NOT NULL CHECK (intended_class_type BETWEEN 0 AND 3),
    captured_source_kind TEXT NOT NULL CHECK (captured_source_kind IN ('vault', 'profile_inventory', 'character_inventory', 'equipped', 'postmaster')),
    captured_character_id TEXT,
    PRIMARY KEY (revision_id, equipment_order),
    UNIQUE (revision_id, item_instance_id),
    UNIQUE (revision_id, bucket_hash),
    FOREIGN KEY (revision_id) REFERENCES loadout_revisions(revision_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_loadout_items_instance
    ON loadout_revision_items(item_instance_id);

CREATE TABLE IF NOT EXISTS loadout_revision_plugs (
    revision_id TEXT NOT NULL,
    equipment_order INTEGER NOT NULL,
    socket_index INTEGER NOT NULL CHECK (socket_index >= 0),
    plug_hash INTEGER CHECK (plug_hash IS NULL OR (plug_hash > 0 AND plug_hash < 4294967296)),
    sync_capability TEXT NOT NULL CHECK (sync_capability IN ('verified', 'experimental', 'unsupported')),
    filtered_from_preview INTEGER NOT NULL DEFAULT 0 CHECK (filtered_from_preview IN (0, 1)),
    PRIMARY KEY (revision_id, equipment_order, socket_index),
    FOREIGN KEY (revision_id, equipment_order) REFERENCES loadout_revision_items(revision_id, equipment_order) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS loadout_plans (
    plan_id TEXT PRIMARY KEY,
    bungie_membership_id TEXT NOT NULL,
    current_revision_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    FOREIGN KEY (bungie_membership_id) REFERENCES users(bungie_membership_id) ON DELETE CASCADE,
    FOREIGN KEY (current_revision_id) REFERENCES loadout_plan_revisions(plan_revision_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_loadout_plan_owner_time
    ON loadout_plans(bungie_membership_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS loadout_plan_revisions (
    plan_revision_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 3000),
    game TEXT NOT NULL DEFAULT 'Destiny 2' CHECK (length(game) BETWEEN 1 AND 80),
    activity_name TEXT NOT NULL CHECK (length(activity_name) BETWEEN 1 AND 120),
    activity_version TEXT NOT NULL DEFAULT '' CHECK (length(activity_version) <= 80),
    change_note TEXT NOT NULL DEFAULT '' CHECK (length(change_note) <= 500),
    canonical_payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (plan_id, revision_number),
    FOREIGN KEY (plan_id) REFERENCES loadout_plans(plan_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS loadout_plan_encounters (
    plan_revision_id TEXT NOT NULL,
    encounter_id TEXT NOT NULL,
    encounter_order INTEGER NOT NULL CHECK (encounter_order >= 0),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    notes TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 2000),
    PRIMARY KEY (plan_revision_id, encounter_id),
    UNIQUE (plan_revision_id, encounter_order),
    FOREIGN KEY (plan_revision_id) REFERENCES loadout_plan_revisions(plan_revision_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS loadout_plan_assignments (
    plan_revision_id TEXT NOT NULL,
    assignment_id TEXT NOT NULL,
    encounter_id TEXT NOT NULL,
    assignment_order INTEGER NOT NULL CHECK (assignment_order >= 0),
    loadout_revision_id TEXT NOT NULL,
    target_character_id TEXT NOT NULL,
    target_slot_index INTEGER NOT NULL CHECK (target_slot_index >= 0),
    notes TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 1000),
    PRIMARY KEY (plan_revision_id, assignment_id),
    UNIQUE (plan_revision_id, encounter_id, assignment_order),
    UNIQUE (plan_revision_id, target_character_id, target_slot_index),
    FOREIGN KEY (plan_revision_id, encounter_id) REFERENCES loadout_plan_encounters(plan_revision_id, encounter_id) ON DELETE CASCADE,
    FOREIGN KEY (loadout_revision_id) REFERENCES loadout_revisions(revision_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_loadout_plan_assignments_revision
    ON loadout_plan_assignments(loadout_revision_id);

CREATE TABLE IF NOT EXISTS loadout_previews (
    preview_id TEXT PRIMARY KEY,
    bungie_membership_id TEXT NOT NULL,
    preview_type TEXT NOT NULL CHECK (preview_type IN ('single_slot', 'activity_plan')),
    source_entity_id TEXT NOT NULL,
    source_revision_id TEXT NOT NULL,
    target_character_id TEXT NOT NULL,
    target_slot_index INTEGER CHECK (target_slot_index >= 0),
    snapshot_id TEXT NOT NULL,
    manifest_version TEXT NOT NULL,
    state_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ready', 'blocked', 'invalidated', 'expired', 'confirmed')),
    action_plan_json TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    request_count INTEGER NOT NULL CHECK (request_count >= 0),
    minimum_throttle_seconds REAL NOT NULL CHECK (minimum_throttle_seconds >= 0),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    confirmed_at TEXT,
    FOREIGN KEY (bungie_membership_id) REFERENCES users(bungie_membership_id) ON DELETE CASCADE,
    FOREIGN KEY (snapshot_id) REFERENCES inventory_snapshots(snapshot_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_loadout_previews_owner_time
    ON loadout_previews(bungie_membership_id, created_at DESC);

CREATE TABLE IF NOT EXISTS loadout_sync_operations (
    operation_id TEXT PRIMARY KEY,
    preview_id TEXT NOT NULL UNIQUE,
    bungie_membership_id TEXT NOT NULL,
    operation_type TEXT NOT NULL CHECK (operation_type IN ('single_slot', 'activity_plan')),
    target_character_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'paused', 'failed', 'completed')),
    backup_choice TEXT NOT NULL CHECK (backup_choice IN ('import', 'skip')),
    current_action_index INTEGER NOT NULL DEFAULT 0 CHECK (current_action_index >= 0),
    total_actions INTEGER NOT NULL CHECK (total_actions >= 0),
    completed_actions INTEGER NOT NULL DEFAULT 0 CHECK (completed_actions >= 0),
    original_equipment_json TEXT NOT NULL,
    backup_json TEXT NOT NULL DEFAULT '{}',
    recovery_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    last_error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (preview_id) REFERENCES loadout_previews(preview_id) ON DELETE RESTRICT,
    FOREIGN KEY (bungie_membership_id) REFERENCES users(bungie_membership_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_loadout_operations_owner_time
    ON loadout_sync_operations(bungie_membership_id, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_loadout_operation_active_character
    ON loadout_sync_operations(bungie_membership_id, target_character_id)
    WHERE status IN ('pending', 'running', 'paused');

CREATE TABLE IF NOT EXISTS loadout_sync_actions (
    operation_id TEXT NOT NULL,
    action_index INTEGER NOT NULL CHECK (action_index >= 0),
    action_type TEXT NOT NULL CHECK (action_type IN (
        'transfer_to_vault', 'transfer_from_vault', 'equip',
        'verify_prepared', 'snapshot', 'identifiers', 'verify_slot',
        'clear_slot', 'verify_clear', 'restore_equipment', 'verify_restored',
        'insert_socket_plug', 'verify_socket_plug', 'cleanup_inventory'
    )),
    phase TEXT NOT NULL,
    encounter_id TEXT,
    assignment_id TEXT,
    target_slot_index INTEGER CHECK (target_slot_index >= 0),
    item_instance_id TEXT,
    request_json TEXT NOT NULL DEFAULT '{}',
    expected_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed', 'skipped')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    throttle_seconds REAL NOT NULL DEFAULT 0 CHECK (throttle_seconds >= 0),
    last_http_status INTEGER,
    last_error_code INTEGER,
    last_error_status TEXT,
    last_message TEXT,
    started_at TEXT,
    completed_at TEXT,
    PRIMARY KEY (operation_id, action_index),
    FOREIGN KEY (operation_id) REFERENCES loadout_sync_operations(operation_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS loadout_sync_action_attempts (
    operation_id TEXT NOT NULL,
    action_index INTEGER NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    http_status INTEGER,
    error_code INTEGER,
    error_status TEXT,
    message TEXT NOT NULL DEFAULT '',
    throttle_seconds REAL NOT NULL DEFAULT 0 CHECK (throttle_seconds >= 0),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (operation_id, action_index, attempt_number),
    FOREIGN KEY (operation_id, action_index) REFERENCES loadout_sync_actions(operation_id, action_index) ON DELETE CASCADE
);

CREATE TRIGGER IF NOT EXISTS loadout_current_revision_update
BEFORE UPDATE OF current_revision_id ON loadouts
WHEN NEW.current_revision_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM loadout_revisions
    WHERE revision_id = NEW.current_revision_id AND loadout_id = NEW.loadout_id
)
BEGIN
    SELECT RAISE(ABORT, 'current revision belongs to another loadout');
END;

CREATE TRIGGER IF NOT EXISTS loadout_revision_snapshot_owner_insert
BEFORE INSERT ON loadout_revisions
WHEN NOT EXISTS (
    SELECT 1 FROM loadouts AS loadout
    JOIN inventory_snapshots AS snapshot ON snapshot.snapshot_id = NEW.snapshot_id
    WHERE loadout.loadout_id = NEW.loadout_id
      AND snapshot.bungie_membership_id = loadout.bungie_membership_id
      AND snapshot.status = 'complete'
)
BEGIN
    SELECT RAISE(ABORT, 'loadout snapshot has the wrong owner');
END;

CREATE TRIGGER IF NOT EXISTS loadout_revision_is_immutable
BEFORE UPDATE OF revision_id, loadout_id, revision_number, manifest_version,
    capture_source, source_character_id, source_slot_index, captured_at,
    capture_validation_status, capture_issues_json, canonical_payload_json,
    source_payload_json, parent_revision_id, revision_action, revision_note
ON loadout_revisions
BEGIN
    SELECT RAISE(ABORT, 'loadout revisions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS loadout_revision_item_is_immutable
BEFORE UPDATE ON loadout_revision_items
BEGIN
    SELECT RAISE(ABORT, 'loadout revision items are immutable');
END;

CREATE TRIGGER IF NOT EXISTS loadout_revision_plug_is_immutable
BEFORE UPDATE ON loadout_revision_plugs
BEGIN
    SELECT RAISE(ABORT, 'loadout revision plugs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS loadout_plan_current_revision_update
BEFORE UPDATE OF current_revision_id ON loadout_plans
WHEN NEW.current_revision_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM loadout_plan_revisions
    WHERE plan_revision_id = NEW.current_revision_id AND plan_id = NEW.plan_id
)
BEGIN
    SELECT RAISE(ABORT, 'current plan revision belongs to another plan');
END;

CREATE TRIGGER IF NOT EXISTS loadout_plan_revision_is_immutable
BEFORE UPDATE ON loadout_plan_revisions
BEGIN
    SELECT RAISE(ABORT, 'loadout plan revisions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS loadout_plan_encounter_is_immutable
BEFORE UPDATE ON loadout_plan_encounters
BEGIN
    SELECT RAISE(ABORT, 'loadout plan encounters are immutable');
END;

CREATE TRIGGER IF NOT EXISTS loadout_plan_assignment_owner_insert
BEFORE INSERT ON loadout_plan_assignments
WHEN NOT EXISTS (
    SELECT 1 FROM loadout_plan_revisions AS plan_revision
    JOIN loadout_plans AS plan ON plan.plan_id = plan_revision.plan_id
    JOIN loadout_revisions AS loadout_revision
      ON loadout_revision.revision_id = NEW.loadout_revision_id
    JOIN loadouts AS loadout ON loadout.loadout_id = loadout_revision.loadout_id
    WHERE plan_revision.plan_revision_id = NEW.plan_revision_id
      AND plan.bungie_membership_id = loadout.bungie_membership_id
)
BEGIN
    SELECT RAISE(ABORT, 'plan assignment has the wrong owner');
END;

CREATE TRIGGER IF NOT EXISTS loadout_plan_assignment_is_immutable
BEFORE UPDATE ON loadout_plan_assignments
BEGIN
    SELECT RAISE(ABORT, 'loadout plan assignments are immutable');
END;

CREATE TRIGGER IF NOT EXISTS loadout_preview_source_owner_insert
BEFORE INSERT ON loadout_previews
WHEN (
    NEW.preview_type = 'single_slot' AND NOT EXISTS (
        SELECT 1 FROM loadout_revisions AS revision
        JOIN loadouts AS loadout ON loadout.loadout_id = revision.loadout_id
        WHERE revision.revision_id = NEW.source_revision_id
          AND loadout.loadout_id = NEW.source_entity_id
          AND loadout.bungie_membership_id = NEW.bungie_membership_id
    )
) OR (
    NEW.preview_type = 'activity_plan' AND NOT EXISTS (
        SELECT 1 FROM loadout_plan_revisions AS revision
        JOIN loadout_plans AS plan ON plan.plan_id = revision.plan_id
        WHERE revision.plan_revision_id = NEW.source_revision_id
          AND plan.plan_id = NEW.source_entity_id
          AND plan.bungie_membership_id = NEW.bungie_membership_id
    ) AND NOT EXISTS (
        SELECT 1 FROM loadout_sets AS board
        WHERE board.set_id = NEW.source_entity_id
          AND CAST(board.version AS TEXT) = NEW.source_revision_id
          AND board.bungie_membership_id = NEW.bungie_membership_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'preview source has the wrong owner');
END;

CREATE TRIGGER IF NOT EXISTS loadout_operation_owner_insert
BEFORE INSERT ON loadout_sync_operations
WHEN NOT EXISTS (
    SELECT 1 FROM loadout_previews
    WHERE preview_id = NEW.preview_id
      AND bungie_membership_id = NEW.bungie_membership_id
      AND preview_type = NEW.operation_type
      AND target_character_id = NEW.target_character_id
)
BEGIN
    SELECT RAISE(ABORT, 'operation preview has the wrong owner');
END;
"""
