"""SQLite persistence for authentication, resources, and inventory snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Sequence


# Loadout-specific migrations 8–12 are initialized by LoadoutStore. Keeping
# the aggregate version here lets the generic database safely open an existing
# loadout-enabled database before that subsystem is constructed.
SCHEMA_VERSION = 14
# Inventory is a live cache. Retain only the active snapshot; saved loadouts
# are rebased to it during refresh and independently validated by item ID.
INVENTORY_HISTORY_LIMIT = 1
CLEANER_HISTORY_LIMIT = 10
GENERAL_VAULT_BUCKET_HASH = 138197802


MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (
        1,
        "authentication foundation",
        """
        CREATE TABLE IF NOT EXISTS users (
            bungie_membership_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            membership_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS oauth_tokens (
            bungie_membership_id TEXT PRIMARY KEY,
            access_token TEXT NOT NULL,
            refresh_token TEXT NOT NULL,
            access_expires_at TEXT NOT NULL,
            refresh_expires_at TEXT NOT NULL,
            refreshed_at TEXT NOT NULL,
            FOREIGN KEY (bungie_membership_id)
                REFERENCES users(bungie_membership_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS web_sessions (
            session_hash TEXT PRIMARY KEY,
            bungie_membership_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY (bungie_membership_id)
                REFERENCES users(bungie_membership_id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_web_sessions_user
            ON web_sessions(bungie_membership_id);
        CREATE INDEX IF NOT EXISTS idx_web_sessions_expiry
            ON web_sessions(expires_at);
        """,
    ),
    (
        2,
        "resource cache and atomic inventory snapshots",
        """
        CREATE TABLE IF NOT EXISTS destiny_accounts (
            bungie_membership_id TEXT NOT NULL,
            destiny_membership_id TEXT NOT NULL,
            membership_type INTEGER NOT NULL,
            display_name TEXT,
            is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
            account_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (bungie_membership_id, destiny_membership_id),
            FOREIGN KEY (bungie_membership_id)
                REFERENCES users(bungie_membership_id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_destiny_accounts_primary
            ON destiny_accounts(bungie_membership_id, is_primary);

        CREATE TABLE IF NOT EXISTS user_resources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bungie_membership_id TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_key TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            source_version TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('fresh', 'stale', 'refresh_failed')
            ),
            fetched_at TEXT NOT NULL,
            stale_at TEXT NOT NULL,
            last_attempt_at TEXT NOT NULL,
            last_success_at TEXT NOT NULL,
            last_error TEXT,
            UNIQUE (bungie_membership_id, resource_type, resource_key),
            FOREIGN KEY (bungie_membership_id)
                REFERENCES users(bungie_membership_id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_user_resources_lookup
            ON user_resources(
                bungie_membership_id, resource_type, resource_key
            );
        CREATE INDEX IF NOT EXISTS idx_user_resources_stale
            ON user_resources(stale_at);

        INSERT OR IGNORE INTO user_resources (
            bungie_membership_id, resource_type, resource_key,
            payload_json, source_version, status, fetched_at, stale_at,
            last_attempt_at, last_success_at, last_error
        )
        SELECT
            bungie_membership_id,
            'bungie_current_memberships',
            'current',
            membership_json,
            NULL,
            'stale',
            updated_at,
            updated_at,
            updated_at,
            updated_at,
            NULL
        FROM users;

        CREATE TABLE IF NOT EXISTS inventory_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            bungie_membership_id TEXT NOT NULL,
            destiny_membership_id TEXT NOT NULL,
            membership_type INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('complete', 'failed')),
            is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
            fetched_at TEXT NOT NULL,
            source_minted_at TEXT,
            stale_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            total_item_count INTEGER NOT NULL,
            vault_item_count INTEGER NOT NULL,
            profile_item_count INTEGER NOT NULL,
            character_inventory_count INTEGER NOT NULL,
            equipped_item_count INTEGER NOT NULL,
            raw_response_json TEXT NOT NULL,
            FOREIGN KEY (bungie_membership_id)
                REFERENCES users(bungie_membership_id)
                ON DELETE CASCADE,
            FOREIGN KEY (bungie_membership_id, destiny_membership_id)
                REFERENCES destiny_accounts(
                    bungie_membership_id, destiny_membership_id
                )
                ON DELETE CASCADE
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_inventory_one_active
            ON inventory_snapshots(bungie_membership_id)
            WHERE is_active = 1;
        CREATE INDEX IF NOT EXISTS idx_inventory_snapshots_user_time
            ON inventory_snapshots(bungie_membership_id, fetched_at DESC);

        CREATE TABLE IF NOT EXISTS inventory_characters (
            snapshot_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            class_type INTEGER,
            class_hash INTEGER,
            light INTEGER,
            emblem_hash INTEGER,
            character_json TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, character_id),
            FOREIGN KEY (snapshot_id)
                REFERENCES inventory_snapshots(snapshot_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS item_instances (
            bungie_membership_id TEXT NOT NULL,
            item_instance_id TEXT NOT NULL,
            item_hash INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_snapshot_id TEXT NOT NULL,
            PRIMARY KEY (bungie_membership_id, item_instance_id),
            FOREIGN KEY (bungie_membership_id)
                REFERENCES users(bungie_membership_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS inventory_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id TEXT NOT NULL,
            record_key TEXT NOT NULL,
            item_instance_id TEXT,
            item_hash INTEGER NOT NULL,
            bucket_hash INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            state INTEGER NOT NULL,
            bind_status INTEGER,
            location INTEGER,
            transfer_status INTEGER,
            transfer_statuses INTEGER,
            lockable INTEGER,
            source_kind TEXT NOT NULL CHECK (
                source_kind IN (
                    'vault', 'profile_inventory',
                    'character_inventory', 'equipped'
                )
            ),
            character_id TEXT,
            item_json TEXT NOT NULL,
            components_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE (snapshot_id, record_key),
            FOREIGN KEY (snapshot_id)
                REFERENCES inventory_snapshots(snapshot_id)
                ON DELETE CASCADE,
            FOREIGN KEY (snapshot_id, character_id)
                REFERENCES inventory_characters(snapshot_id, character_id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_inventory_items_instance
            ON inventory_items(snapshot_id, item_instance_id);
        CREATE INDEX IF NOT EXISTS idx_inventory_items_hash
            ON inventory_items(snapshot_id, item_hash);
        CREATE INDEX IF NOT EXISTS idx_inventory_items_owner
            ON inventory_items(snapshot_id, character_id, source_kind);
        CREATE INDEX IF NOT EXISTS idx_inventory_items_bucket
            ON inventory_items(snapshot_id, bucket_hash);

        CREATE TABLE IF NOT EXISTS inventory_sync_state (
            bungie_membership_id TEXT PRIMARY KEY,
            active_snapshot_id TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('missing', 'fresh', 'stale', 'refresh_failed')
            ),
            last_attempt_at TEXT,
            last_success_at TEXT,
            stale_at TEXT,
            last_error TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            api_fetch_count INTEGER NOT NULL DEFAULT 0,
            cache_hit_count INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (bungie_membership_id)
                REFERENCES users(bungie_membership_id)
                ON DELETE CASCADE,
            FOREIGN KEY (active_snapshot_id)
                REFERENCES inventory_snapshots(snapshot_id)
                ON DELETE SET NULL
        );
        """,
    ),
    (
        3,
        "manifest cache state",
        """
        CREATE TABLE IF NOT EXISTS manifest_state (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            version TEXT,
            language TEXT NOT NULL,
            content_path TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('missing', 'ready', 'update_failed')
            ),
            last_attempt_at TEXT,
            downloaded_at TEXT,
            last_error TEXT
        );

        INSERT OR IGNORE INTO manifest_state (
            singleton_id, version, language, content_path, status,
            last_attempt_at, downloaded_at, last_error
        ) VALUES (1, NULL, 'en', NULL, 'missing', NULL, NULL, NULL);
        """,
    ),
    (
        4,
        "inventory ownership and history invariants",
        """
        ALTER TABLE inventory_snapshots
            ADD COLUMN postmaster_item_count INTEGER NOT NULL DEFAULT 0;

        ALTER TABLE item_instances RENAME TO item_instances_v3;

        CREATE TABLE item_instances (
            bungie_membership_id TEXT NOT NULL,
            item_instance_id TEXT NOT NULL,
            item_hash INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_snapshot_id TEXT,
            PRIMARY KEY (bungie_membership_id, item_instance_id),
            FOREIGN KEY (bungie_membership_id)
                REFERENCES users(bungie_membership_id)
                ON DELETE CASCADE,
            FOREIGN KEY (last_snapshot_id)
                REFERENCES inventory_snapshots(snapshot_id)
                ON DELETE SET NULL
        );

        INSERT INTO item_instances (
            bungie_membership_id, item_instance_id, item_hash,
            first_seen_at, last_seen_at, last_snapshot_id
        )
        SELECT
            bungie_membership_id, item_instance_id, item_hash,
            first_seen_at, last_seen_at,
            CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM inventory_snapshots
                    WHERE snapshot_id = item_instances_v3.last_snapshot_id
                )
                THEN last_snapshot_id
                ELSE NULL
            END
        FROM item_instances_v3;

        DROP TABLE item_instances_v3;

        CREATE TRIGGER item_instance_hash_is_immutable
        BEFORE UPDATE OF item_hash ON item_instances
        WHEN OLD.item_hash != NEW.item_hash
        BEGIN
            SELECT RAISE(
                ABORT,
                'an item instance cannot change definition hash'
            );
        END;

        ALTER TABLE inventory_items RENAME TO inventory_items_v3;

        CREATE TABLE inventory_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id TEXT NOT NULL,
            record_key TEXT NOT NULL,
            item_instance_id TEXT,
            item_hash INTEGER NOT NULL,
            bucket_hash INTEGER NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            state INTEGER NOT NULL CHECK (state >= 0),
            bind_status INTEGER,
            location INTEGER,
            transfer_status INTEGER,
            lockable INTEGER CHECK (lockable IN (0, 1) OR lockable IS NULL),
            source_kind TEXT NOT NULL CHECK (
                source_kind IN (
                    'vault', 'profile_inventory', 'character_inventory',
                    'equipped', 'postmaster'
                )
            ),
            character_id TEXT,
            item_json TEXT NOT NULL,
            components_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE (snapshot_id, record_key),
            CHECK (
                (
                    source_kind IN ('vault', 'profile_inventory')
                    AND character_id IS NULL
                )
                OR
                (
                    source_kind IN (
                        'character_inventory', 'equipped', 'postmaster'
                    )
                    AND character_id IS NOT NULL
                )
            ),
            FOREIGN KEY (snapshot_id)
                REFERENCES inventory_snapshots(snapshot_id)
                ON DELETE CASCADE,
            FOREIGN KEY (snapshot_id, character_id)
                REFERENCES inventory_characters(snapshot_id, character_id)
                ON DELETE CASCADE
        );

        INSERT INTO inventory_items (
            id, snapshot_id, record_key, item_instance_id, item_hash,
            bucket_hash, quantity, state, bind_status, location,
            transfer_status, lockable, source_kind,
            character_id, item_json, components_json,
            first_seen_at, last_seen_at
        )
        SELECT
            id, snapshot_id, record_key, item_instance_id, item_hash,
            bucket_hash, quantity, state, bind_status, location,
            transfer_status, lockable,
            CASE
                WHEN source_kind = 'character_inventory' AND location = 4
                THEN 'postmaster'
                ELSE source_kind
            END,
            character_id, item_json, components_json,
            first_seen_at, last_seen_at
        FROM inventory_items_v3;

        DROP TABLE inventory_items_v3;

        UPDATE inventory_snapshots
        SET postmaster_item_count = (
                SELECT COUNT(*)
                FROM inventory_items
                WHERE inventory_items.snapshot_id =
                    inventory_snapshots.snapshot_id
                  AND source_kind = 'postmaster'
            ),
            character_inventory_count = (
                SELECT COUNT(*)
                FROM inventory_items
                WHERE inventory_items.snapshot_id =
                    inventory_snapshots.snapshot_id
                  AND source_kind = 'character_inventory'
            );

        CREATE INDEX idx_inventory_items_instance
            ON inventory_items(snapshot_id, item_instance_id);
        CREATE UNIQUE INDEX idx_inventory_items_unique_instance
            ON inventory_items(snapshot_id, item_instance_id)
            WHERE item_instance_id IS NOT NULL;
        CREATE INDEX idx_inventory_items_hash
            ON inventory_items(snapshot_id, item_hash);
        CREATE INDEX idx_inventory_items_owner
            ON inventory_items(snapshot_id, character_id, source_kind);
        CREATE INDEX idx_inventory_items_bucket
            ON inventory_items(snapshot_id, bucket_hash);

        CREATE TRIGGER inventory_sync_snapshot_owner_insert
        BEFORE INSERT ON inventory_sync_state
        WHEN NEW.active_snapshot_id IS NOT NULL
         AND NOT EXISTS (
            SELECT 1
            FROM inventory_snapshots
            WHERE snapshot_id = NEW.active_snapshot_id
              AND bungie_membership_id = NEW.bungie_membership_id
              AND status = 'complete'
         )
        BEGIN
            SELECT RAISE(ABORT, 'active snapshot has the wrong owner');
        END;

        CREATE TRIGGER inventory_sync_snapshot_owner_update
        BEFORE UPDATE OF active_snapshot_id, bungie_membership_id
        ON inventory_sync_state
        WHEN NEW.active_snapshot_id IS NOT NULL
         AND NOT EXISTS (
            SELECT 1
            FROM inventory_snapshots
            WHERE snapshot_id = NEW.active_snapshot_id
              AND bungie_membership_id = NEW.bungie_membership_id
              AND status = 'complete'
         )
        BEGIN
            SELECT RAISE(ABORT, 'active snapshot has the wrong owner');
        END;
        """,
    ),
    (
        5,
        "snapshot-bound cleaner analysis",
        """
        CREATE TABLE cleaner_analysis_runs (
            run_id TEXT PRIMARY KEY,
            bungie_membership_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            analysis_kind TEXT NOT NULL CHECK (
                analysis_kind IN ('weapon')
            ),
            ruleset_version TEXT NOT NULL,
            manifest_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            weapon_count INTEGER NOT NULL CHECK (weapon_count >= 0),
            group_count INTEGER NOT NULL CHECK (group_count >= 0),
            keep_count INTEGER NOT NULL CHECK (keep_count >= 0),
            candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
            result_json TEXT NOT NULL,
            UNIQUE (
                bungie_membership_id, snapshot_id, analysis_kind,
                ruleset_version, manifest_version
            ),
            FOREIGN KEY (bungie_membership_id)
                REFERENCES users(bungie_membership_id)
                ON DELETE CASCADE,
            FOREIGN KEY (snapshot_id)
                REFERENCES inventory_snapshots(snapshot_id)
                ON DELETE RESTRICT
        );

        CREATE INDEX idx_cleaner_runs_user_time
            ON cleaner_analysis_runs(
                bungie_membership_id, analysis_kind, created_at DESC
            );
        """,
    ),
    (
        6,
        "cleaner ownership and armor-ready analysis kinds",
        """
        ALTER TABLE cleaner_analysis_runs
            RENAME TO cleaner_analysis_runs_v5;

        CREATE TABLE cleaner_analysis_runs (
            run_id TEXT PRIMARY KEY,
            bungie_membership_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            analysis_kind TEXT NOT NULL CHECK (
                analysis_kind IN ('weapon', 'armor')
            ),
            ruleset_version TEXT NOT NULL,
            manifest_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            item_count INTEGER NOT NULL CHECK (item_count >= 0),
            group_count INTEGER NOT NULL CHECK (group_count >= 0),
            keep_count INTEGER NOT NULL CHECK (keep_count >= 0),
            candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
            result_json TEXT NOT NULL,
            UNIQUE (
                bungie_membership_id, snapshot_id, analysis_kind,
                ruleset_version, manifest_version
            ),
            FOREIGN KEY (bungie_membership_id)
                REFERENCES users(bungie_membership_id)
                ON DELETE CASCADE,
            FOREIGN KEY (snapshot_id)
                REFERENCES inventory_snapshots(snapshot_id)
                ON DELETE RESTRICT
        );

        INSERT INTO cleaner_analysis_runs (
            run_id, bungie_membership_id, snapshot_id, analysis_kind,
            ruleset_version, manifest_version, created_at, item_count,
            group_count, keep_count, candidate_count, result_json
        )
        SELECT
            run_id, bungie_membership_id, snapshot_id, analysis_kind,
            ruleset_version, manifest_version, created_at, weapon_count,
            group_count, keep_count, candidate_count, result_json
        FROM cleaner_analysis_runs_v5;

        DROP TABLE cleaner_analysis_runs_v5;

        CREATE INDEX idx_cleaner_runs_user_time
            ON cleaner_analysis_runs(
                bungie_membership_id, analysis_kind, created_at DESC
            );

        CREATE TRIGGER cleaner_run_snapshot_owner_insert
        BEFORE INSERT ON cleaner_analysis_runs
        WHEN NOT EXISTS (
            SELECT 1
            FROM inventory_snapshots
            WHERE snapshot_id = NEW.snapshot_id
              AND bungie_membership_id = NEW.bungie_membership_id
              AND status = 'complete'
        )
        BEGIN
            SELECT RAISE(ABORT, 'cleaner snapshot has the wrong owner');
        END;

        CREATE TRIGGER cleaner_run_snapshot_owner_update
        BEFORE UPDATE OF snapshot_id, bungie_membership_id
        ON cleaner_analysis_runs
        WHEN NOT EXISTS (
            SELECT 1
            FROM inventory_snapshots
            WHERE snapshot_id = NEW.snapshot_id
              AND bungie_membership_id = NEW.bungie_membership_id
              AND status = 'complete'
        )
        BEGIN
            SELECT RAISE(ABORT, 'cleaner snapshot has the wrong owner');
        END;
        """,
    ),
    (
        7,
        "saved armor cleaner policy and manual keeps",
        """
        CREATE TABLE armor_cleaner_policies (
            bungie_membership_id TEXT PRIMARY KEY,
            revision INTEGER NOT NULL CHECK (revision > 0),
            policy_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (bungie_membership_id)
                REFERENCES users(bungie_membership_id)
                ON DELETE CASCADE
        );

        CREATE TABLE armor_manual_keeps (
            bungie_membership_id TEXT NOT NULL,
            item_instance_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (bungie_membership_id, item_instance_id),
            FOREIGN KEY (bungie_membership_id)
                REFERENCES users(bungie_membership_id)
                ON DELETE CASCADE
        );

        CREATE INDEX idx_armor_manual_keeps_user
            ON armor_manual_keeps(bungie_membership_id, created_at);
        """,
    ),
)


@dataclass(frozen=True, slots=True)
class TokenRecord:
    bungie_membership_id: str
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    access_expires_at: datetime
    refresh_expires_at: datetime
    refreshed_at: datetime

    @property
    def access_needs_refresh(self) -> bool:
        return self.access_expires_at <= utc_now() + timedelta(minutes=5)

    @property
    def refresh_is_expired(self) -> bool:
        return self.refresh_expires_at <= utc_now()


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    session_id: str = field(repr=False)
    bungie_membership_id: str
    display_name: str
    token: TokenRecord
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CharacterRecord:
    character_id: str
    class_type: int | None
    class_hash: int | None
    light: int | None
    emblem_hash: int | None
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class InventoryItemRecord:
    record_key: str
    item_instance_id: str | None
    item_hash: int
    bucket_hash: int
    quantity: int
    state: int
    bind_status: int | None
    location: int | None
    transfer_status: int | None
    lockable: bool | None
    source_kind: str
    character_id: str | None
    payload: dict[str, Any]
    components: dict[str, Any]


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    bungie_membership_id: str
    destiny_membership_id: str
    membership_type: int
    account: dict[str, Any]
    characters: Sequence[CharacterRecord]
    items: Sequence[InventoryItemRecord]
    raw_response: dict[str, Any]
    fetched_at: datetime
    stale_at: datetime
    source_minted_at: str | None


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )

        for version, name, sql in MIGRATIONS:
            with self.connection() as connection:
                # Keep the migration body and its ledger entry in one
                # transaction. BEGIN IMMEDIATE also serializes two application
                # processes that happen to start against the same database.
                connection.execute("BEGIN IMMEDIATE")
                applied = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?",
                    (version,),
                ).fetchone()
                if applied is not None:
                    continue
                for statement in sql_statements(sql):
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO schema_migrations(version, name, applied_at)
                    VALUES (?, ?, ?)
                    """,
                    (version, name, as_iso(utc_now())),
                )

        with self.connection() as connection:
            current_version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(
                "The database schema is newer than this application "
                f"({current_version} > {SCHEMA_VERSION})."
            )

        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def save_login(
        self,
        bungie_membership_id: str,
        display_name: str,
        membership_data: dict[str, Any],
        token_payload: dict[str, Any],
    ) -> tuple[str, int]:
        now = utc_now()
        token = token_from_payload(
            bungie_membership_id,
            token_payload,
            now=now,
        )
        raw_session_id = secrets.token_urlsafe(48)
        session_expires_at = token.refresh_expires_at

        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO users (
                    bungie_membership_id, display_name, membership_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(bungie_membership_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    membership_json = excluded.membership_json,
                    updated_at = excluded.updated_at
                """,
                (
                    bungie_membership_id,
                    display_name,
                    compact_json(membership_data),
                    as_iso(now),
                    as_iso(now),
                ),
            )
            self._save_token(connection, token)
            connection.execute(
                """
                INSERT INTO web_sessions (
                    session_hash, bungie_membership_id, created_at,
                    last_seen_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    hash_session_id(raw_session_id),
                    bungie_membership_id,
                    as_iso(now),
                    as_iso(now),
                    as_iso(session_expires_at),
                ),
            )
            self._upsert_resource(
                connection,
                bungie_membership_id=bungie_membership_id,
                resource_type="bungie_current_memberships",
                resource_key="current",
                payload=membership_data,
                fetched_at=now,
                stale_at=now + timedelta(hours=24),
                source_version=None,
            )

        max_age = max(0, int((session_expires_at - now).total_seconds()))
        return raw_session_id, max_age

    def get_session(self, raw_session_id: str) -> AuthenticatedSession | None:
        session_hash = hash_session_id(raw_session_id)
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    s.expires_at AS session_expires_at,
                    s.last_seen_at AS session_last_seen_at,
                    u.bungie_membership_id,
                    u.display_name,
                    t.access_token,
                    t.refresh_token,
                    t.access_expires_at,
                    t.refresh_expires_at,
                    t.refreshed_at
                FROM web_sessions AS s
                JOIN users AS u
                    ON u.bungie_membership_id = s.bungie_membership_id
                JOIN oauth_tokens AS t
                    ON t.bungie_membership_id = u.bungie_membership_id
                WHERE s.session_hash = ?
                """,
                (session_hash,),
            ).fetchone()
            if row is None:
                return None

            expires_at = from_iso(row["session_expires_at"])
            if expires_at <= utc_now():
                connection.execute(
                    "DELETE FROM web_sessions WHERE session_hash = ?",
                    (session_hash,),
                )
                return None

            now = utc_now()
            if from_iso(row["session_last_seen_at"]) <= now - timedelta(
                minutes=5
            ):
                connection.execute(
                    """
                    UPDATE web_sessions
                    SET last_seen_at = ?
                    WHERE session_hash = ?
                    """,
                    (as_iso(now), session_hash),
                )

        token = TokenRecord(
            bungie_membership_id=row["bungie_membership_id"],
            access_token=row["access_token"],
            refresh_token=row["refresh_token"],
            access_expires_at=from_iso(row["access_expires_at"]),
            refresh_expires_at=from_iso(row["refresh_expires_at"]),
            refreshed_at=from_iso(row["refreshed_at"]),
        )
        return AuthenticatedSession(
            session_id=raw_session_id,
            bungie_membership_id=row["bungie_membership_id"],
            display_name=row["display_name"],
            token=token,
            expires_at=expires_at,
        )

    def get_membership_data(
        self,
        bungie_membership_id: str,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT membership_json
                FROM users
                WHERE bungie_membership_id = ?
                """,
                (bungie_membership_id,),
            ).fetchone()
        if row is None:
            raise LookupError("The signed-in Bungie user is not stored.")
        payload = json.loads(row["membership_json"])
        if not isinstance(payload, dict):
            raise ValueError("Stored Bungie membership data is invalid.")
        return payload

    def membership_resource(
        self,
        bungie_membership_id: str,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    user.membership_json,
                    resource.status,
                    resource.stale_at
                FROM users AS user
                LEFT JOIN user_resources AS resource
                    ON resource.bungie_membership_id =
                        user.bungie_membership_id
                   AND resource.resource_type =
                       'bungie_current_memberships'
                   AND resource.resource_key = 'current'
                WHERE user.bungie_membership_id = ?
                """,
                (bungie_membership_id,),
            ).fetchone()
        if row is None:
            raise LookupError("The signed-in Bungie user is not stored.")
        payload = json.loads(row["membership_json"])
        if not isinstance(payload, dict):
            raise ValueError("Stored Bungie membership data is invalid.")
        stale_at = row["stale_at"]
        fresh = (
            row["status"] == "fresh"
            and isinstance(stale_at, str)
            and from_iso(stale_at) > utc_now()
        )
        return {"payload": payload, "fresh": fresh}

    def save_membership_data(
        self,
        bungie_membership_id: str,
        membership_data: dict[str, Any],
    ) -> None:
        now = utc_now()
        with self.connection() as connection:
            updated = connection.execute(
                """
                UPDATE users
                SET membership_json = ?, updated_at = ?
                WHERE bungie_membership_id = ?
                """,
                (
                    compact_json(membership_data),
                    as_iso(now),
                    bungie_membership_id,
                ),
            ).rowcount
            if not updated:
                raise LookupError("The signed-in Bungie user is not stored.")
            self._upsert_resource(
                connection,
                bungie_membership_id=bungie_membership_id,
                resource_type="bungie_current_memberships",
                resource_key="current",
                payload=membership_data,
                fetched_at=now,
                stale_at=now + timedelta(hours=24),
                source_version=None,
            )

    def update_tokens(
        self,
        bungie_membership_id: str,
        payload: dict[str, Any],
        *,
        previous_refresh_token: str,
    ) -> TokenRecord:
        token = token_from_payload(
            bungie_membership_id,
            payload,
            previous_refresh_token=previous_refresh_token,
        )
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT
                    bungie_membership_id, access_token, refresh_token,
                    access_expires_at, refresh_expires_at, refreshed_at
                FROM oauth_tokens
                WHERE bungie_membership_id = ?
                """,
                (bungie_membership_id,),
            ).fetchone()
            if current is None:
                raise LookupError("The stored Bungie token no longer exists.")
            if not secrets.compare_digest(
                current["refresh_token"],
                previous_refresh_token,
            ):
                return token_record_from_row(current)
            self._save_token(connection, token)
            connection.execute(
                """
                UPDATE web_sessions
                SET expires_at = ?
                WHERE bungie_membership_id = ?
                """,
                (as_iso(token.refresh_expires_at), bungie_membership_id),
            )
        return token

    def delete_session(self, raw_session_id: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "DELETE FROM web_sessions WHERE session_hash = ?",
                (hash_session_id(raw_session_id),),
            )

    def remove_expired_sessions(self) -> None:
        with self.connection() as connection:
            connection.execute(
                "DELETE FROM web_sessions WHERE expires_at <= ?",
                (as_iso(utc_now()),),
            )

    def save_inventory_snapshot(self, snapshot: InventorySnapshot) -> str:
        snapshot_id = secrets.token_hex(16)
        now_text = as_iso(snapshot.fetched_at)
        stale_text = as_iso(snapshot.stale_at)
        source_counts = {
            "vault": 0,
            "profile_inventory": 0,
            "character_inventory": 0,
            "equipped": 0,
            "postmaster": 0,
        }
        for item in snapshot.items:
            source_counts[item.source_kind] += 1
        # ProfileInventory includes currencies and consumable containers that
        # do not consume slots in Destiny's General vault bucket. Only rows in
        # that bucket count against the vault capacity shown in game.
        source_counts["vault"] = sum(
            item.source_kind == "vault"
            and item.bucket_hash == GENERAL_VAULT_BUCKET_HASH
            for item in snapshot.items
        )

        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO destiny_accounts (
                    bungie_membership_id, destiny_membership_id,
                    membership_type, display_name, is_primary, account_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(
                    bungie_membership_id, destiny_membership_id
                ) DO UPDATE SET
                    membership_type = excluded.membership_type,
                    display_name = excluded.display_name,
                    is_primary = 1,
                    account_json = excluded.account_json,
                    updated_at = excluded.updated_at
                """,
                (
                    snapshot.bungie_membership_id,
                    snapshot.destiny_membership_id,
                    snapshot.membership_type,
                    membership_display_name(snapshot.account),
                    compact_json(snapshot.account),
                    now_text,
                    now_text,
                ),
            )
            connection.execute(
                """
                UPDATE destiny_accounts
                SET is_primary = 0
                WHERE bungie_membership_id = ?
                  AND destiny_membership_id != ?
                """,
                (
                    snapshot.bungie_membership_id,
                    snapshot.destiny_membership_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO inventory_snapshots (
                    snapshot_id, bungie_membership_id,
                    destiny_membership_id, membership_type, status, is_active,
                    fetched_at, source_minted_at, stale_at, completed_at,
                    total_item_count, vault_item_count,
                    profile_item_count, character_inventory_count,
                    equipped_item_count, postmaster_item_count,
                    raw_response_json
                ) VALUES (
                    ?, ?, ?, ?, 'complete', 0, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    snapshot_id,
                    snapshot.bungie_membership_id,
                    snapshot.destiny_membership_id,
                    snapshot.membership_type,
                    now_text,
                    snapshot.source_minted_at,
                    stale_text,
                    now_text,
                    len(snapshot.items),
                    source_counts["vault"],
                    source_counts["profile_inventory"],
                    source_counts["character_inventory"],
                    source_counts["equipped"],
                    source_counts["postmaster"],
                    compact_json(snapshot.raw_response),
                ),
            )

            for character in snapshot.characters:
                connection.execute(
                    """
                    INSERT INTO inventory_characters (
                        snapshot_id, character_id, class_type, class_hash,
                        light, emblem_hash, character_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        character.character_id,
                        character.class_type,
                        character.class_hash,
                        character.light,
                        character.emblem_hash,
                        compact_json(character.payload),
                    ),
                )

            for item in snapshot.items:
                first_seen_at = now_text
                if item.item_instance_id:
                    previous = connection.execute(
                        """
                        SELECT first_seen_at
                        FROM item_instances
                        WHERE bungie_membership_id = ?
                          AND item_instance_id = ?
                        """,
                        (
                            snapshot.bungie_membership_id,
                            item.item_instance_id,
                        ),
                    ).fetchone()
                    if previous is not None:
                        first_seen_at = previous["first_seen_at"]
                    connection.execute(
                        """
                        INSERT INTO item_instances (
                            bungie_membership_id, item_instance_id, item_hash,
                            first_seen_at, last_seen_at, last_snapshot_id
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(
                            bungie_membership_id, item_instance_id
                        ) DO UPDATE SET
                            item_hash = excluded.item_hash,
                            last_seen_at = excluded.last_seen_at,
                            last_snapshot_id = excluded.last_snapshot_id
                        """,
                        (
                            snapshot.bungie_membership_id,
                            item.item_instance_id,
                            item.item_hash,
                            first_seen_at,
                            now_text,
                            snapshot_id,
                        ),
                    )

                connection.execute(
                    """
                    INSERT INTO inventory_items (
                        snapshot_id, record_key, item_instance_id, item_hash,
                        bucket_hash, quantity, state, bind_status, location,
                        transfer_status, lockable,
                        source_kind, character_id, item_json, components_json,
                        first_seen_at, last_seen_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        snapshot_id,
                        item.record_key,
                        item.item_instance_id,
                        item.item_hash,
                        item.bucket_hash,
                        item.quantity,
                        item.state,
                        item.bind_status,
                        item.location,
                        item.transfer_status,
                        bool_as_int(item.lockable),
                        item.source_kind,
                        item.character_id,
                        compact_json(item.payload),
                        compact_json(item.components),
                        first_seen_at,
                        now_text,
                    ),
                )

            connection.execute(
                """
                UPDATE inventory_snapshots
                SET is_active = 0
                WHERE bungie_membership_id = ?
                  AND is_active = 1
                """,
                (snapshot.bungie_membership_id,),
            )
            connection.execute(
                """
                UPDATE inventory_snapshots
                SET is_active = 1
                WHERE snapshot_id = ?
                """,
                (snapshot_id,),
            )
            connection.execute(
                """
                INSERT INTO inventory_sync_state (
                    bungie_membership_id, active_snapshot_id, status,
                    last_attempt_at, last_success_at, stale_at, last_error,
                    consecutive_failures, api_fetch_count, cache_hit_count
                ) VALUES (?, ?, 'fresh', ?, ?, ?, NULL, 0, 1, 0)
                ON CONFLICT(bungie_membership_id) DO UPDATE SET
                    active_snapshot_id = excluded.active_snapshot_id,
                    status = excluded.status,
                    last_attempt_at = excluded.last_attempt_at,
                    last_success_at = excluded.last_success_at,
                    stale_at = excluded.stale_at,
                    last_error = NULL,
                    consecutive_failures = 0,
                    api_fetch_count =
                        inventory_sync_state.api_fetch_count + 1
                """,
                (
                    snapshot.bungie_membership_id,
                    snapshot_id,
                    now_text,
                    now_text,
                    stale_text,
                ),
            )
            self._upsert_resource(
                connection,
                bungie_membership_id=snapshot.bungie_membership_id,
                resource_type="destiny_profile",
                resource_key=snapshot.destiny_membership_id,
                payload=snapshot.raw_response,
                fetched_at=snapshot.fetched_at,
                stale_at=snapshot.stale_at,
                source_version=snapshot.source_minted_at,
            )
            self._prune_inventory_history(
                connection,
                snapshot.bungie_membership_id,
                current_snapshot_id=snapshot_id,
                keep=INVENTORY_HISTORY_LIMIT,
            )
        return snapshot_id

    def record_inventory_failure(
        self,
        bungie_membership_id: str,
        message: str,
    ) -> None:
        now_text = as_iso(utc_now())
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO inventory_sync_state (
                    bungie_membership_id, active_snapshot_id, status,
                    last_attempt_at, last_success_at, stale_at, last_error,
                    consecutive_failures, api_fetch_count, cache_hit_count
                ) VALUES (
                    ?, NULL, 'refresh_failed', ?, NULL, NULL, ?, 1, 1, 0
                )
                ON CONFLICT(bungie_membership_id) DO UPDATE SET
                    status = 'refresh_failed',
                    last_attempt_at = excluded.last_attempt_at,
                    last_error = excluded.last_error,
                    consecutive_failures =
                        inventory_sync_state.consecutive_failures + 1,
                    api_fetch_count =
                        inventory_sync_state.api_fetch_count + 1
                """,
                (bungie_membership_id, now_text, safe_error(message)),
            )
            connection.execute(
                """
                UPDATE user_resources
                SET status = 'refresh_failed',
                    last_attempt_at = ?,
                    last_error = ?
                WHERE bungie_membership_id = ?
                  AND resource_type = 'destiny_profile'
                """,
                (
                    now_text,
                    safe_error(message),
                    bungie_membership_id,
                ),
            )

    def mark_inventory_stale(self, bungie_membership_id: str) -> bool:
        now_text = as_iso(utc_now() - timedelta(seconds=1))
        with self.connection() as connection:
            changed = connection.execute(
                """
                UPDATE inventory_sync_state
                SET status = 'stale', stale_at = ?
                WHERE bungie_membership_id = ?
                  AND active_snapshot_id IS NOT NULL
                """,
                (now_text, bungie_membership_id),
            ).rowcount
            connection.execute(
                """
                UPDATE inventory_snapshots
                SET stale_at = ?
                WHERE snapshot_id = (
                    SELECT active_snapshot_id
                    FROM inventory_sync_state
                    WHERE bungie_membership_id = ?
                )
                """,
                (now_text, bungie_membership_id),
            )
            connection.execute(
                """
                UPDATE user_resources
                SET status = 'stale', stale_at = ?
                WHERE bungie_membership_id = ?
                  AND resource_type = 'destiny_profile'
                """,
                (now_text, bungie_membership_id),
            )
        return bool(changed)

    def record_inventory_cache_hit(self, bungie_membership_id: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE inventory_sync_state
                SET cache_hit_count = cache_hit_count + 1
                WHERE bungie_membership_id = ?
                  AND active_snapshot_id IS NOT NULL
                """,
                (bungie_membership_id,),
            )

    def inventory_status(self, bungie_membership_id: str) -> dict[str, Any]:
        started = datetime.now(UTC)
        with self.connection() as connection:
            schema_version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
            latest_migration = connection.execute(
                """
                SELECT version, name, applied_at
                FROM schema_migrations
                ORDER BY version DESC
                LIMIT 1
                """
            ).fetchone()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            sync = connection.execute(
                """
                SELECT
                    state.status AS sync_status,
                    state.last_attempt_at,
                    state.last_success_at,
                    state.stale_at,
                    state.last_error,
                    state.consecutive_failures,
                    snapshot.*,
                    account.display_name AS destiny_display_name
                FROM inventory_sync_state AS state
                LEFT JOIN inventory_snapshots AS snapshot
                    ON snapshot.snapshot_id = state.active_snapshot_id
                LEFT JOIN destiny_accounts AS account
                    ON account.bungie_membership_id =
                        snapshot.bungie_membership_id
                   AND account.destiny_membership_id =
                        snapshot.destiny_membership_id
                WHERE state.bungie_membership_id = ?
                """,
                (bungie_membership_id,),
            ).fetchone()
            resources = connection.execute(
                """
                SELECT resource_type, resource_key, status, fetched_at,
                       stale_at, last_error
                FROM user_resources
                WHERE bungie_membership_id = ?
                ORDER BY resource_type, resource_key
                """,
                (bungie_membership_id,),
            ).fetchall()

            characters: list[dict[str, Any]] = []
            duplicate_instances = 0
            broken_owners = 0
            if sync is not None and sync["snapshot_id"]:
                snapshot_id = sync["snapshot_id"]
                character_rows = connection.execute(
                    """
                    SELECT
                        character.character_id,
                        character.class_type,
                        character.class_hash,
                        character.light,
                        SUM(CASE WHEN item.source_kind =
                            'character_inventory' THEN 1 ELSE 0 END)
                            AS carried_count,
                        SUM(CASE WHEN item.source_kind =
                            'equipped' THEN 1 ELSE 0 END)
                            AS equipped_count,
                        SUM(CASE WHEN item.source_kind =
                            'postmaster' THEN 1 ELSE 0 END)
                            AS postmaster_count
                    FROM inventory_characters AS character
                    LEFT JOIN inventory_items AS item
                        ON item.snapshot_id = character.snapshot_id
                       AND item.character_id = character.character_id
                    WHERE character.snapshot_id = ?
                    GROUP BY character.character_id
                    ORDER BY character.character_id
                    """,
                    (snapshot_id,),
                ).fetchall()
                characters = [dict(row) for row in character_rows]
                duplicate_instances = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM (
                        SELECT item_instance_id
                        FROM inventory_items
                        WHERE snapshot_id = ?
                          AND item_instance_id IS NOT NULL
                        GROUP BY item_instance_id
                        HAVING COUNT(*) > 1
                    )
                    """,
                    (snapshot_id,),
                ).fetchone()[0]
                broken_owners = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM inventory_items AS item
                    WHERE item.snapshot_id = ?
                      AND item.character_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM inventory_characters AS character
                          WHERE character.snapshot_id = item.snapshot_id
                            AND character.character_id = item.character_id
                      )
                    """,
                    (snapshot_id,),
                ).fetchone()[0]

        status = dict(sync) if sync is not None else {
            "sync_status": "missing",
            "last_attempt_at": None,
            "last_success_at": None,
            "stale_at": None,
            "last_error": None,
            "consecutive_failures": 0,
        }
        stale_at = status.get("stale_at")
        if (
            status.get("sync_status") == "fresh"
            and stale_at
            and from_iso(stale_at) <= utc_now()
        ):
            status["sync_status"] = "stale"
        resource_statuses = [dict(row) for row in resources]
        for resource in resource_statuses:
            if (
                resource["status"] == "fresh"
                and from_iso(resource["stale_at"]) <= utc_now()
            ):
                resource["status"] = "stale"

        status.update(
            {
                "schema_version": schema_version,
                "expected_schema_version": SCHEMA_VERSION,
                "last_migration": (
                    dict(latest_migration) if latest_migration is not None else None
                ),
                "database_bytes": self.path.stat().st_size,
                "integrity": integrity,
                "duplicate_instance_count": duplicate_instances,
                "broken_owner_reference_count": broken_owners,
                "characters": characters,
                "resources": resource_statuses,
                "read_duration_ms": round(
                    (datetime.now(UTC) - started).total_seconds() * 1000,
                    2,
                ),
            }
        )
        return status

    def load_active_inventory(
        self,
        bungie_membership_id: str,
    ) -> dict[str, Any] | None:
        """Load the active snapshot without its large raw Bungie response."""
        with self.connection() as connection:
            snapshot = connection.execute(
                """
                SELECT
                    snapshot.snapshot_id,
                    snapshot.destiny_membership_id,
                    snapshot.membership_type,
                    snapshot.fetched_at,
                    snapshot.source_minted_at,
                    snapshot.stale_at,
                    snapshot.total_item_count,
                    snapshot.vault_item_count,
                    snapshot.profile_item_count,
                    snapshot.character_inventory_count,
                    snapshot.equipped_item_count,
                    snapshot.postmaster_item_count,
                    sync.status AS sync_status,
                    sync.last_attempt_at,
                    sync.last_error
                FROM inventory_snapshots AS snapshot
                LEFT JOIN inventory_sync_state AS sync
                    ON sync.active_snapshot_id = snapshot.snapshot_id
                   AND sync.bungie_membership_id =
                       snapshot.bungie_membership_id
                WHERE snapshot.bungie_membership_id = ?
                  AND snapshot.is_active = 1
                  AND snapshot.status = 'complete'
                """,
                (bungie_membership_id,),
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
                SELECT
                    id, record_key, item_instance_id, item_hash, bucket_hash,
                    quantity, state, bind_status, location, transfer_status,
                    lockable, source_kind, character_id,
                    json_extract(
                        components_json,
                        '$.instances.primaryStat.value'
                    ) AS primary_power,
                    first_seen_at, last_seen_at
                FROM inventory_items
                WHERE snapshot_id = ?
                ORDER BY source_kind, character_id, bucket_hash, item_hash, id
                """,
                (snapshot["snapshot_id"],),
            ).fetchall()

        characters = []
        for row in character_rows:
            character = dict(row)
            character["payload"] = json.loads(character.pop("character_json"))
            characters.append(character)
        items = []
        for row in item_rows:
            items.append(dict(row))
        return {
            "snapshot": dict(snapshot),
            "characters": characters,
            "items": items,
        }

    def load_active_inventory_item(
        self,
        bungie_membership_id: str,
        item_row_id: int,
    ) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    item.id, item.record_key, item.item_instance_id,
                    item.item_hash, item.bucket_hash, item.quantity,
                    item.state, item.bind_status, item.location,
                    item.transfer_status, item.lockable,
                    item.source_kind, item.character_id,
                    item.item_json, item.components_json,
                    item.first_seen_at, item.last_seen_at
                FROM inventory_items AS item
                JOIN inventory_snapshots AS snapshot
                    ON snapshot.snapshot_id = item.snapshot_id
                WHERE snapshot.bungie_membership_id = ?
                  AND snapshot.is_active = 1
                  AND item.id = ?
                """,
                (bungie_membership_id, item_row_id),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item.pop("item_json"))
        item["components"] = json.loads(item.pop("components_json"))
        return item

    def load_active_inventory_for_analysis(
        self,
        bungie_membership_id: str,
    ) -> dict[str, Any] | None:
        """Load the active snapshot with the component data analyzers need."""
        with self.connection() as connection:
            snapshot = connection.execute(
                """
                SELECT
                    snapshot.snapshot_id, snapshot.fetched_at,
                    snapshot.stale_at, snapshot.total_item_count,
                    snapshot.destiny_membership_id,
                    snapshot.membership_type,
                    snapshot.raw_response_json,
                    sync.status AS sync_status
                FROM inventory_snapshots AS snapshot
                JOIN inventory_sync_state AS sync
                    ON sync.active_snapshot_id = snapshot.snapshot_id
                   AND sync.bungie_membership_id =
                       snapshot.bungie_membership_id
                WHERE snapshot.bungie_membership_id = ?
                  AND snapshot.is_active = 1
                  AND snapshot.status = 'complete'
                """,
                (bungie_membership_id,),
            ).fetchone()
            if snapshot is None:
                return None
            rows = connection.execute(
                """
                SELECT
                    id, item_instance_id, item_hash, state, source_kind,
                    character_id, lockable, components_json,
                    first_seen_at, last_seen_at
                FROM inventory_items
                WHERE snapshot_id = ?
                ORDER BY item_hash, id
                """,
                (snapshot["snapshot_id"],),
            ).fetchall()

        snapshot_data = dict(snapshot)
        profile = json.loads(snapshot_data.pop("raw_response_json"))
        character_loadouts = profile.get("characterLoadouts", {}).get(
            "data",
            {},
        )
        character_ids = sorted(
            str(character_id)
            for character_id in profile.get("characters", {}).get("data", {})
        )
        items = []
        for row in rows:
            item = dict(row)
            item["components"] = json.loads(item.pop("components_json"))
            items.append(item)
        return {
            "snapshot": snapshot_data,
            "items": items,
            "character_loadouts": character_loadouts,
            "character_ids": character_ids,
        }

    def save_cleaner_analysis(
        self,
        bungie_membership_id: str,
        *,
        snapshot_id: str,
        analysis_kind: str,
        ruleset_version: str,
        manifest_version: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        summary = result["summary"]
        run_id = secrets.token_hex(16)
        created_at = as_iso(utc_now())
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO cleaner_analysis_runs (
                    run_id, bungie_membership_id, snapshot_id,
                    analysis_kind, ruleset_version, manifest_version,
                    created_at, item_count, group_count, keep_count,
                    candidate_count, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (
                    bungie_membership_id, snapshot_id, analysis_kind,
                    ruleset_version, manifest_version
                ) DO UPDATE SET
                    created_at = excluded.created_at,
                    item_count = excluded.item_count,
                    group_count = excluded.group_count,
                    keep_count = excluded.keep_count,
                    candidate_count = excluded.candidate_count,
                    result_json = excluded.result_json
                """,
                (
                    run_id,
                    bungie_membership_id,
                    snapshot_id,
                    analysis_kind,
                    ruleset_version,
                    manifest_version,
                    created_at,
                    int(summary["item_count"]),
                    int(summary["group_count"]),
                    int(summary["keep_count"]),
                    int(summary["candidate_count"]),
                    compact_json(result),
                ),
            )
            saved = connection.execute(
                """
                SELECT run_id, created_at
                FROM cleaner_analysis_runs
                WHERE bungie_membership_id = ?
                  AND snapshot_id = ?
                  AND analysis_kind = ?
                  AND ruleset_version = ?
                  AND manifest_version = ?
                """,
                (
                    bungie_membership_id,
                    snapshot_id,
                    analysis_kind,
                    ruleset_version,
                    manifest_version,
                ),
            ).fetchone()
            connection.execute(
                """
                DELETE FROM cleaner_analysis_runs
                WHERE run_id IN (
                    SELECT run_id
                    FROM cleaner_analysis_runs
                    WHERE bungie_membership_id = ?
                      AND analysis_kind = ?
                    ORDER BY created_at DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (
                    bungie_membership_id,
                    analysis_kind,
                    CLEANER_HISTORY_LIMIT,
                ),
            )
        return {
            "run_id": saved["run_id"],
            "created_at": saved["created_at"],
            "snapshot_id": snapshot_id,
            "manifest_version": manifest_version,
            "ruleset_version": ruleset_version,
            "result": result,
        }

    def load_armor_cleaner_policy(
        self,
        bungie_membership_id: str,
    ) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT revision, policy_json, updated_at
                FROM armor_cleaner_policies
                WHERE bungie_membership_id = ?
                """,
                (bungie_membership_id,),
            ).fetchone()
        if row is None:
            return None
        policy = json.loads(row["policy_json"])
        if not isinstance(policy, dict):
            raise ValueError("Stored armor cleaner policy is invalid.")
        return {
            "revision": int(row["revision"]),
            "updated_at": row["updated_at"],
            "policy": policy,
        }

    def save_armor_cleaner_policy(
        self,
        bungie_membership_id: str,
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        updated_at = as_iso(utc_now())
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO armor_cleaner_policies (
                    bungie_membership_id, revision, policy_json, updated_at
                ) VALUES (?, 1, ?, ?)
                ON CONFLICT(bungie_membership_id) DO UPDATE SET
                    revision = armor_cleaner_policies.revision + 1,
                    policy_json = excluded.policy_json,
                    updated_at = excluded.updated_at
                """,
                (bungie_membership_id, compact_json(policy), updated_at),
            )
            row = connection.execute(
                """
                SELECT revision, updated_at
                FROM armor_cleaner_policies
                WHERE bungie_membership_id = ?
                """,
                (bungie_membership_id,),
            ).fetchone()
        return {
            "revision": int(row["revision"]),
            "updated_at": row["updated_at"],
            "policy": policy,
        }

    def armor_manual_keeps(self, bungie_membership_id: str) -> set[str]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT item_instance_id
                FROM armor_manual_keeps
                WHERE bungie_membership_id = ?
                """,
                (bungie_membership_id,),
            ).fetchall()
        return {str(row["item_instance_id"]) for row in rows}

    def set_armor_manual_keep(
        self,
        bungie_membership_id: str,
        item_instance_id: str,
        *,
        keep: bool,
    ) -> None:
        with self.connection() as connection:
            if keep:
                connection.execute(
                    """
                    INSERT INTO armor_manual_keeps (
                        bungie_membership_id, item_instance_id, created_at
                    ) VALUES (?, ?, ?)
                    ON CONFLICT(bungie_membership_id, item_instance_id)
                    DO NOTHING
                    """,
                    (bungie_membership_id, item_instance_id, as_iso(utc_now())),
                )
            else:
                connection.execute(
                    """
                    DELETE FROM armor_manual_keeps
                    WHERE bungie_membership_id = ? AND item_instance_id = ?
                    """,
                    (bungie_membership_id, item_instance_id),
                )

    def latest_cleaner_analysis(
        self,
        bungie_membership_id: str,
        *,
        analysis_kind: str,
    ) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    run_id, snapshot_id, manifest_version, ruleset_version,
                    created_at, result_json
                FROM cleaner_analysis_runs
                WHERE bungie_membership_id = ?
                  AND analysis_kind = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (bungie_membership_id, analysis_kind),
            ).fetchone()
        if row is None:
            return None
        analysis = dict(row)
        result = json.loads(analysis.pop("result_json"))
        if not isinstance(result, dict):
            raise ValueError("Stored cleaner analysis is invalid.")
        analysis["result"] = result
        return analysis

    def manifest_status(self) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT version, language, content_path, status,
                       last_attempt_at, downloaded_at, last_error
                FROM manifest_state
                WHERE singleton_id = 1
                """
            ).fetchone()
        if row is None:
            return {
                "version": None,
                "language": "en",
                "content_path": None,
                "status": "missing",
                "last_attempt_at": None,
                "downloaded_at": None,
                "last_error": None,
            }
        return dict(row)

    def save_manifest_ready(
        self,
        *,
        version: str,
        language: str,
        content_path: Path,
    ) -> None:
        now_text = as_iso(utc_now())
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO manifest_state (
                    singleton_id, version, language, content_path, status,
                    last_attempt_at, downloaded_at, last_error
                ) VALUES (1, ?, ?, ?, 'ready', ?, ?, NULL)
                ON CONFLICT(singleton_id) DO UPDATE SET
                    version = excluded.version,
                    language = excluded.language,
                    content_path = excluded.content_path,
                    status = 'ready',
                    last_attempt_at = excluded.last_attempt_at,
                    downloaded_at = excluded.downloaded_at,
                    last_error = NULL
                """,
                (
                    version,
                    language,
                    str(content_path),
                    now_text,
                    now_text,
                ),
            )

    def save_manifest_failure(self, message: str) -> None:
        now_text = as_iso(utc_now())
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE manifest_state
                SET status = CASE
                        WHEN content_path IS NULL THEN 'missing'
                        ELSE 'update_failed'
                    END,
                    last_attempt_at = ?,
                    last_error = ?
                WHERE singleton_id = 1
                """,
                (now_text, safe_error(message)),
            )

    @staticmethod
    def _save_token(
        connection: sqlite3.Connection,
        token: TokenRecord,
    ) -> None:
        connection.execute(
            """
            INSERT INTO oauth_tokens (
                bungie_membership_id, access_token, refresh_token,
                access_expires_at, refresh_expires_at, refreshed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(bungie_membership_id) DO UPDATE SET
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                access_expires_at = excluded.access_expires_at,
                refresh_expires_at = excluded.refresh_expires_at,
                refreshed_at = excluded.refreshed_at
            """,
            (
                token.bungie_membership_id,
                token.access_token,
                token.refresh_token,
                as_iso(token.access_expires_at),
                as_iso(token.refresh_expires_at),
                as_iso(token.refreshed_at),
            ),
        )

    @staticmethod
    def _upsert_resource(
        connection: sqlite3.Connection,
        *,
        bungie_membership_id: str,
        resource_type: str,
        resource_key: str,
        payload: dict[str, Any],
        fetched_at: datetime,
        stale_at: datetime,
        source_version: str | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO user_resources (
                bungie_membership_id, resource_type, resource_key,
                payload_json, source_version, status, fetched_at, stale_at,
                last_attempt_at, last_success_at, last_error
            ) VALUES (?, ?, ?, ?, ?, 'fresh', ?, ?, ?, ?, NULL)
            ON CONFLICT(
                bungie_membership_id, resource_type, resource_key
            ) DO UPDATE SET
                payload_json = excluded.payload_json,
                source_version = excluded.source_version,
                status = 'fresh',
                fetched_at = excluded.fetched_at,
                stale_at = excluded.stale_at,
                last_attempt_at = excluded.last_attempt_at,
                last_success_at = excluded.last_success_at,
                last_error = NULL
            """,
            (
                bungie_membership_id,
                resource_type,
                resource_key,
                compact_json(payload),
                source_version,
                as_iso(fetched_at),
                as_iso(stale_at),
                as_iso(fetched_at),
                as_iso(fetched_at),
            ),
        )

    @staticmethod
    def _prune_inventory_history(
        connection: sqlite3.Connection,
        bungie_membership_id: str,
        *,
        current_snapshot_id: str,
        keep: int,
    ) -> None:
        # Loadout captures are validated against the active inventory, not an
        # archival snapshot. Repoint their provenance to the new active
        # snapshot before pruning so saved loadouts never prevent inventory
        # refresh. Their existing item-instance validation will mark them
        # invalid when an item is no longer present.
        tables = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table'
                  AND name IN (
                      'loadout_revisions', 'loadout_previews',
                      'cleaner_analysis_runs'
                  )
                """
            ).fetchall()
        }
        if "loadout_revisions" in tables:
            connection.execute(
                """
                UPDATE loadout_revisions
                SET snapshot_id = ?
                WHERE snapshot_id IN (
                    SELECT snapshot_id
                    FROM inventory_snapshots
                    WHERE bungie_membership_id = ?
                )
                """,
                (current_snapshot_id, bungie_membership_id),
            )
        if "loadout_previews" in tables:
            # A refresh changes the state fingerprint, so existing previews
            # must not remain confirmable even though their FK is rebased.
            connection.execute(
                """
                UPDATE loadout_previews
                SET snapshot_id = ?, status = 'invalidated'
                WHERE bungie_membership_id = ?
                """,
                (current_snapshot_id, bungie_membership_id),
            )
        # Cleaner results are derived from a snapshot and are not archival
        # data. Remove results tied to older snapshots before deleting those
        # snapshots; the cleaner services regenerate the current result.
        if "cleaner_analysis_runs" in tables:
            connection.execute(
                """
                DELETE FROM cleaner_analysis_runs
                WHERE bungie_membership_id = ?
                  AND snapshot_id != ?
                """,
                (bungie_membership_id, current_snapshot_id),
            )
        old_rows = connection.execute(
            """
            SELECT snapshot_id
            FROM inventory_snapshots
            WHERE bungie_membership_id = ?
            ORDER BY fetched_at DESC
            LIMIT -1 OFFSET ?
            """,
            (bungie_membership_id, keep),
        ).fetchall()
        connection.executemany(
            "DELETE FROM inventory_snapshots WHERE snapshot_id = ?",
            ((row["snapshot_id"],) for row in old_rows),
        )


def token_from_payload(
    bungie_membership_id: str,
    payload: dict[str, Any],
    *,
    previous_refresh_token: str | None = None,
    now: datetime | None = None,
) -> TokenRecord:
    now = now or utc_now()
    access_token = required_string(payload, "access_token")
    refresh_token = payload.get("refresh_token") or previous_refresh_token
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ValueError("Bungie did not return a refresh token")

    expires_in = required_positive_int(payload, "expires_in")
    refresh_expires_in = required_positive_int(payload, "refresh_expires_in")
    return TokenRecord(
        bungie_membership_id=bungie_membership_id,
        access_token=access_token,
        refresh_token=refresh_token,
        access_expires_at=now + timedelta(seconds=expires_in),
        refresh_expires_at=now + timedelta(seconds=refresh_expires_in),
        refreshed_at=now,
    )


def token_record_from_row(row: sqlite3.Row) -> TokenRecord:
    return TokenRecord(
        bungie_membership_id=row["bungie_membership_id"],
        access_token=row["access_token"],
        refresh_token=row["refresh_token"],
        access_expires_at=from_iso(row["access_expires_at"]),
        refresh_expires_at=from_iso(row["refresh_expires_at"]),
        refreshed_at=from_iso(row["refreshed_at"]),
    )


def required_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Bungie token response is missing {field}")
    return value


def required_positive_int(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"Bungie token response has invalid {field}")
    return value


def hash_session_id(raw_session_id: str) -> str:
    return hashlib.sha256(raw_session_id.encode("utf-8")).hexdigest()


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def compact_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def sql_statements(script: str) -> Iterator[str]:
    """Split a trusted migration script without losing transaction control."""
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                yield statement
            pending = ""
    if pending.strip():
        raise ValueError("Migration script ended with an incomplete statement.")


def membership_display_name(account: dict[str, Any]) -> str | None:
    value = account.get("bungieGlobalDisplayName") or account.get("displayName")
    return value.strip() if isinstance(value, str) and value.strip() else None


def bool_as_int(value: bool | None) -> int | None:
    return None if value is None else int(value)


def safe_error(message: str) -> str:
    return " ".join(message.split())[:500]
