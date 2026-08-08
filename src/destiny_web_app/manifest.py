"""Version-aware local access to Bungie's official SQLite manifest."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

from destiny_web_app.bungie import BungieClient, BungieError
from destiny_web_app.database import Database


REQUIRED_TABLES = {
    "DestinyInventoryItemDefinition",
    "DestinyInventoryBucketDefinition",
    "DestinyClassDefinition",
    "DestinyStatDefinition",
    "DestinySandboxPerkDefinition",
}
MAX_UNPACKED_MANIFEST_BYTES = 2 * 1024 * 1024 * 1024
ALLOWED_TABLES = REQUIRED_TABLES | {
    "DestinyActivityDefinition",
    "DestinyEquipableItemSetDefinition",
    "DestinyLoadoutColorDefinition",
    "DestinyLoadoutConstantsDefinition",
    "DestinyLoadoutIconDefinition",
    "DestinyLoadoutNameDefinition",
    "DestinyPlugSetDefinition",
    "DestinyItemCategoryDefinition",
    "DestinyPlaceDefinition",
    "DestinySocketCategoryDefinition",
    "DestinySocketTypeDefinition",
}


class ManifestError(RuntimeError):
    """The local or remote Destiny manifest could not be used."""


class ManifestService:
    def __init__(
        self,
        database: Database,
        bungie: BungieClient,
        *,
        path: Path,
        language: str,
    ) -> None:
        self.database = database
        self.bungie = bungie
        self.path = path
        self.language = language
        self._lock = asyncio.Lock()
        self._validated_signature: tuple[int, int] | None = None
        self._definition_cache: dict[str, dict[int, dict[str, Any]]] = {}
        self._missing_definition_cache: dict[str, set[int]] = {}
        self._cache_signature: tuple[int, int] | None = None
        self._cache_lock = threading.Lock()

    def status(self) -> dict[str, Any]:
        status = self.database.manifest_status()
        configured_path = self.path.resolve()
        stored_path = status.get("content_path")
        metadata_matches = (
            status.get("language") == self.language
            and isinstance(stored_path, str)
            and Path(stored_path).resolve() == configured_path
        )
        candidate_available = (
            status.get("status") in {"ready", "update_failed"}
            and self.path.is_file()
            and metadata_matches
        )
        local_error: str | None = None
        if candidate_available:
            signature = manifest_signature(self.path)
            if signature != self._validated_signature:
                try:
                    validate_manifest_schema(self.path)
                except ManifestError as error:
                    local_error = str(error)
                    self._validated_signature = None
                else:
                    self._validated_signature = signature
        status["available"] = candidate_available and local_error is None
        status["metadata_matches_configuration"] = metadata_matches
        status["local_error"] = local_error
        status["configured_path"] = str(self.path)
        return status

    async def synchronize(self, *, force: bool = False) -> dict[str, Any]:
        async with self._lock:
            try:
                metadata = await self.bungie.get_manifest_metadata()
                version = metadata.get("version")
                if not isinstance(version, str) or not version:
                    raise ManifestError(
                        "Bungie did not provide a manifest version."
                    )
                paths = metadata.get("mobileWorldContentPaths")
                if not isinstance(paths, dict):
                    raise ManifestError(
                        "Bungie did not provide SQLite manifest paths."
                    )
                content_path = paths.get(self.language)
                if not isinstance(content_path, str) or not content_path:
                    raise ManifestError(
                        f"Bungie has no SQLite manifest for {self.language!r}."
                    )

                current = self.status()
                if (
                    not force
                    and current["available"]
                    and current.get("version") == version
                ):
                    return {**current, "downloaded": False}

                await self._download_and_install(content_path)
                await asyncio.to_thread(
                    self.database.save_manifest_ready,
                    version=version,
                    language=self.language,
                    content_path=self.path,
                )
            except Exception as error:
                await asyncio.to_thread(
                    self.database.save_manifest_failure,
                    public_manifest_error(error),
                )
                raise

            return {**self.status(), "downloaded": True}

    async def _download_and_install(self, content_path: str) -> None:
        download_path = self.path.with_name(self.path.name + ".download")
        candidate_path = self.path.with_name(self.path.name + ".candidate")
        for temporary in (download_path, candidate_path):
            temporary.unlink(missing_ok=True)
        try:
            await self.bungie.download_manifest_content(
                content_path,
                download_path,
            )
            await asyncio.to_thread(
                unpack_manifest,
                download_path,
                candidate_path,
            )
            await asyncio.to_thread(validate_manifest, candidate_path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(candidate_path, self.path)
            self._validated_signature = manifest_signature(self.path)
            with self._cache_lock:
                self._definition_cache.clear()
                self._missing_definition_cache.clear()
                self._cache_signature = self._validated_signature
        finally:
            download_path.unlink(missing_ok=True)
            candidate_path.unlink(missing_ok=True)

    def resolve_many(
        self,
        table: str,
        hashes: Iterable[int],
    ) -> dict[int, dict[str, Any]]:
        requested = set(hashes)
        if not requested:
            return {}
        status = self.status()
        if not status["available"]:
            raise ManifestError("The local Destiny definitions are unavailable.")
        signature = manifest_signature(self.path)
        with self._cache_lock:
            if self._cache_signature != signature:
                self._definition_cache.clear()
                self._missing_definition_cache.clear()
                self._cache_signature = signature
            table_cache = self._definition_cache.setdefault(table, {})
            known_missing = self._missing_definition_cache.setdefault(
                table,
                set(),
            )
            missing = requested - set(table_cache) - known_missing

        if missing:
            resolved = ManifestRepository(self.path).resolve_many(table, missing)
            with self._cache_lock:
                table_cache.update(resolved)
                known_missing.update(missing - set(resolved))

        with self._cache_lock:
            return {
                item_hash: table_cache[item_hash]
                for item_hash in requested
                if item_hash in table_cache
            }

    def resolve_all(self, table: str) -> dict[int, dict[str, Any]]:
        status = self.status()
        if not status["available"]:
            raise ManifestError("The local Destiny definitions are unavailable.")
        return ManifestRepository(self.path).resolve_all(table)


class ManifestRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def available(self) -> bool:
        return self.path.is_file()

    def resolve_many(
        self,
        table: str,
        hashes: Iterable[int],
    ) -> dict[int, dict[str, Any]]:
        if table not in ALLOWED_TABLES:
            raise ValueError(f"Unsupported manifest table: {table}")
        unique_hashes = sorted(set(hashes))
        if not unique_hashes:
            return {}

        resolved: dict[int, dict[str, Any]] = {}
        try:
            with closing(manifest_connection(self.path)) as connection:
                for start in range(0, len(unique_hashes), 500):
                    batch = unique_hashes[start : start + 500]
                    signed = [signed_hash(value) for value in batch]
                    placeholders = ",".join("?" for _ in signed)
                    rows = connection.execute(
                        f'SELECT id, json FROM "{table}" '
                        f"WHERE id IN ({placeholders})",
                        signed,
                    ).fetchall()
                    for row in rows:
                        key = unsigned_hash(row["id"])
                        try:
                            payload = json.loads(row["json"])
                        except (TypeError, json.JSONDecodeError):
                            continue
                        if isinstance(payload, dict):
                            resolved[key] = payload
        except sqlite3.Error as error:
            raise ManifestError(
                "The local Destiny definitions could not be read."
            ) from error
        return resolved

    def resolve_all(self, table: str) -> dict[int, dict[str, Any]]:
        if table not in ALLOWED_TABLES:
            raise ValueError(f"Unsupported manifest table: {table}")
        resolved: dict[int, dict[str, Any]] = {}
        try:
            with closing(manifest_connection(self.path)) as connection:
                rows = connection.execute(
                    f'SELECT id, json FROM "{table}"'
                ).fetchall()
                for row in rows:
                    try:
                        payload = json.loads(row["json"])
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if isinstance(payload, dict):
                        resolved[unsigned_hash(row["id"])] = payload
        except sqlite3.Error as error:
            raise ManifestError(
                "The local Destiny definitions could not be read."
            ) from error
        return resolved


def manifest_signature(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except OSError as error:
        raise ManifestError("The local Destiny manifest is unavailable.") from error
    return (stat.st_mtime_ns, stat.st_size)


def validate_manifest_schema(path: Path) -> None:
    if not path.is_file():
        raise ManifestError("The local Destiny manifest is missing.")
    try:
        with closing(manifest_connection(path)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            missing = REQUIRED_TABLES - tables
            if missing:
                raise ManifestError(
                    "The local Destiny manifest is missing required "
                    "definitions: " + ", ".join(sorted(missing))
                )
            for table in REQUIRED_TABLES:
                connection.execute(
                    f'SELECT id, json FROM "{table}" LIMIT 1'
                ).fetchone()
    except sqlite3.Error as error:
        raise ManifestError(
            "The local Destiny manifest is not valid SQLite."
        ) from error


def unpack_manifest(download_path: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(download_path) as archive:
            files = [entry for entry in archive.infolist() if not entry.is_dir()]
            if len(files) != 1:
                raise ManifestError(
                    "The Bungie manifest archive did not contain one database."
                )
            if files[0].file_size > MAX_UNPACKED_MANIFEST_BYTES:
                raise ManifestError(
                    "The Bungie manifest archive is unexpectedly large."
                )
            with archive.open(files[0]) as source, destination.open("wb") as output:
                copied = 0
                while chunk := source.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > MAX_UNPACKED_MANIFEST_BYTES:
                        raise ManifestError(
                            "The Bungie manifest archive is unexpectedly large."
                        )
                    output.write(chunk)
    except zipfile.BadZipFile as error:
        raise ManifestError(
            "Bungie returned an invalid manifest archive."
        ) from error


def validate_manifest(path: Path) -> None:
    validate_manifest_schema(path)
    try:
        with closing(manifest_connection(path)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.Error as error:
        raise ManifestError("The local Destiny manifest is not valid SQLite.") from error
    if integrity != "ok":
        raise ManifestError("The local Destiny manifest failed its integrity check.")


def manifest_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def signed_hash(value: int) -> int:
    return value - 2**32 if value >= 2**31 else value


def unsigned_hash(value: int) -> int:
    return value & 0xFFFFFFFF


def public_manifest_error(error: Exception) -> str:
    if isinstance(error, (BungieError, ManifestError)):
        return str(error)
    return "The Destiny manifest could not be updated."
