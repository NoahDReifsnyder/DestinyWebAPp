"""Verified Bungie lock-state organization for cleaner reviews."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from destiny_web_app.bungie import (
    BungieClient,
    BungieError,
    BungieTemporarilyUnavailable,
)
from destiny_web_app.armor_cleaner import ArmorCleanerService
from destiny_web_app.cleaner import WeaponCleanerService
from destiny_web_app.database import Database
from destiny_web_app.inventory import InventoryService
from destiny_web_app.manifest import ManifestService


ACTION_INTERVAL_SECONDS = 0.15
RETRY_DELAYS = (1.0, 2.0, 4.0)


class WeaponOrganizerError(RuntimeError):
    """Weapons could not be safely organized against a current analysis."""


@dataclass(frozen=True, slots=True)
class WeaponOrganizerResult:
    candidate_count: int
    keeper_count: int
    changed_count: int
    already_correct_count: int
    verified_count: int
    failed_count: int


@dataclass(slots=True)
class WeaponOrganizerProgress:
    status: str = "preparing"
    message: str = "Refreshing inventory and computing a safe plan…"
    completed: int = 0
    total: int = 0
    retry_completed: int = 0
    retry_total: int = 0
    started_at: float = 0.0
    result: WeaponOrganizerResult | None = None
    error: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "completed": self.completed,
            "total": self.total,
            "retry_completed": self.retry_completed,
            "retry_total": self.retry_total,
            "elapsed_seconds": max(0, round(time.monotonic() - self.started_at)),
            "result": (
                {
                    field: getattr(self.result, field)
                    for field in self.result.__dataclass_fields__
                }
                if self.result
                else None
            ),
            "error": self.error,
        }


class WeaponOrganizerService:
    def __init__(
        self,
        database: Database,
        bungie: BungieClient,
        inventory: InventoryService,
        manifest: ManifestService,
        cleaner: WeaponCleanerService | ArmorCleanerService,
        *,
        item_type: int = 3,
        label: str = "weapon",
        vault_only: bool = True,
    ) -> None:
        self.database = database
        self.bungie = bungie
        self.inventory = inventory
        self.manifest = manifest
        self.cleaner = cleaner
        self.item_type = item_type
        self.label = label
        self.vault_only = vault_only
        self._locks: dict[str, asyncio.Lock] = {}
        self._jobs: dict[str, WeaponOrganizerProgress] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self, *, bungie_membership_id: str, access_token: str) -> None:
        current = self._jobs.get(bungie_membership_id)
        if current and current.status not in {"complete", "partial", "failed"}:
            raise WeaponOrganizerError(
                f"{self.label.title()} organization is already running for "
                "this account."
            )
        progress = WeaponOrganizerProgress(started_at=time.monotonic())
        self._jobs[bungie_membership_id] = progress
        task = asyncio.create_task(
            self._run_job(bungie_membership_id, access_token, progress)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def progress(self, bungie_membership_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(bungie_membership_id)
        return job.public() if job else None

    async def _run_job(
        self,
        bungie_membership_id: str,
        access_token: str,
        progress: WeaponOrganizerProgress,
    ) -> None:
        try:
            result = await self.organize(
                bungie_membership_id=bungie_membership_id,
                access_token=access_token,
                progress=progress,
            )
        except Exception as error:
            progress.status = "failed"
            progress.message = f"{self.label.title()} organization stopped."
            progress.error = " ".join(str(error).split())[:300]
        else:
            progress.result = result
            progress.status = "partial" if result.failed_count else "complete"
            progress.message = (
                f"Verified {result.verified_count} {self.label} items; "
                f"{result.failed_count} still need attention."
                if result.failed_count
                else (
                    f"Organized {result.candidate_count} candidates and "
                    f"{result.keeper_count} keepers; every state was verified."
                )
            )

    async def organize(
        self,
        *,
        bungie_membership_id: str,
        access_token: str,
        progress: WeaponOrganizerProgress | None = None,
    ) -> WeaponOrganizerResult:
        operation_lock = self._locks.setdefault(
            bungie_membership_id,
            asyncio.Lock(),
        )
        if operation_lock.locked():
            raise WeaponOrganizerError(
                f"{self.label.title()} organization is already running for "
                "this account."
            )
        async with operation_lock:
            return await self._organize(
                bungie_membership_id=bungie_membership_id,
                access_token=access_token,
                progress=progress,
            )

    async def _organize(
        self,
        *,
        bungie_membership_id: str,
        access_token: str,
        progress: WeaponOrganizerProgress | None,
    ) -> WeaponOrganizerResult:
        # Freeze a fresh inventory and recompute the analysis immediately before
        # any write. This prevents an older browser page from driving actions.
        await self.inventory.synchronize(
            bungie_membership_id=bungie_membership_id,
            access_token=access_token,
            force=True,
        )
        analysis = await asyncio.to_thread(
            self.cleaner.analyze,
            bungie_membership_id,
        )
        source = await asyncio.to_thread(
            self.database.load_active_inventory_for_analysis,
            bungie_membership_id,
        )
        if (
            source is None
            or analysis["snapshot_id"] != source["snapshot"]["snapshot_id"]
        ):
            raise WeaponOrganizerError(
                f"The inventory changed before {self.label} organization "
                "could start."
            )

        item_definitions = await asyncio.to_thread(
            self.manifest.resolve_many,
            "DestinyInventoryItemDefinition",
            (item["item_hash"] for item in source["items"]),
        )
        candidate_row_ids = {
            item["item_row_id"]
            for group in analysis["result"]["groups"]
            for item in group["items"]
            if item["decision"] == "candidate"
        }
        organized_items = [
            item
            for item in source["items"]
            if (not self.vault_only or item["source_kind"] == "vault")
            and item.get("item_instance_id")
            and bool(item.get("lockable"))
            and item_definitions.get(item["item_hash"], {}).get("itemType")
            == self.item_type
        ]
        organized_row_ids = {item["id"] for item in organized_items}
        if not candidate_row_ids <= organized_row_ids:
            raise WeaponOrganizerError(
                f"One or more review candidates are no longer lockable "
                f"{self.label} items."
            )
        fallback_character_id = first_character_id(source)
        membership_type = source["snapshot"].get("membership_type")
        if not isinstance(membership_type, int):
            raise WeaponOrganizerError("The Destiny membership type is unavailable.")

        desired_by_instance = {
            item["item_instance_id"]: item["id"] not in candidate_row_ids
            for item in organized_items
        }
        character_by_instance = {
            item["item_instance_id"]: (
                str(item["character_id"])
                if item.get("character_id")
                else fallback_character_id
            )
            for item in organized_items
        }
        actions = [
            item
            for item in organized_items
            if bool(item["state"] & 1)
            != desired_by_instance[item["item_instance_id"]]
        ]
        if progress:
            progress.status = "running"
            progress.message = f"Applying {self.label} lock states…"
            progress.total = len(actions)
        consecutive_errors = 0
        for item in actions:
            instance_id = item["item_instance_id"]
            desired_locked = desired_by_instance[instance_id]
            try:
                await self._set_with_retry(
                    access_token=access_token,
                    item_instance_id=instance_id,
                    character_id=character_by_instance[instance_id],
                    membership_type=membership_type,
                    locked=desired_locked,
                )
                consecutive_errors = 0
            except BungieError:
                consecutive_errors += 1
                await asyncio.sleep(ACTION_INTERVAL_SECONDS)
                if consecutive_errors >= 3:
                    break
            finally:
                if progress:
                    progress.completed += 1

        # A successful action response is not treated as proof. Refresh the
        # complete profile and compare every exact instance with its target.
        if progress:
            progress.status = "verifying"
            progress.message = "Refreshing inventory to verify every item…"
        await asyncio.sleep(1.0)
        await self.inventory.synchronize(
            bungie_membership_id=bungie_membership_id,
            access_token=access_token,
            force=True,
        )
        verified_source = await asyncio.to_thread(
            self.database.load_active_inventory_for_analysis,
            bungie_membership_id,
        )
        if verified_source is None:
            raise WeaponOrganizerError(
                "Bungie actions completed, but verification could not load inventory."
            )
        actual_by_instance = {
            item["item_instance_id"]: bool(item["state"] & 1)
            for item in verified_source["items"]
            if item.get("item_instance_id")
        }
        mismatches = {
            instance_id
            for instance_id, desired_locked in desired_by_instance.items()
            if actual_by_instance.get(instance_id) != desired_locked
        }

        # A success response can occasionally race the profile state seen by a
        # subsequent fetch. Retry only observed mismatches, then verify once
        # more instead of asking the user to trust the action responses.
        if mismatches:
            if progress:
                progress.status = "retrying"
                progress.message = "Retrying states that did not verify…"
                progress.retry_total = len(mismatches)
            consecutive_errors = 0
            for instance_id in sorted(mismatches):
                try:
                    await self._set_with_retry(
                        access_token=access_token,
                        item_instance_id=instance_id,
                        character_id=character_by_instance[instance_id],
                        membership_type=membership_type,
                        locked=desired_by_instance[instance_id],
                    )
                    consecutive_errors = 0
                except BungieError:
                    consecutive_errors += 1
                    await asyncio.sleep(ACTION_INTERVAL_SECONDS)
                    if consecutive_errors >= 3:
                        break
                finally:
                    if progress:
                        progress.retry_completed += 1
            await asyncio.sleep(1.0)
            await self.inventory.synchronize(
                bungie_membership_id=bungie_membership_id,
                access_token=access_token,
                force=True,
            )
            verified_source = await asyncio.to_thread(
                self.database.load_active_inventory_for_analysis,
                bungie_membership_id,
            )
            if verified_source is None:
                raise WeaponOrganizerError(
                    "The second lock verification could not load inventory."
                )
            actual_by_instance = {
                item["item_instance_id"]: bool(item["state"] & 1)
                for item in verified_source["items"]
                if item.get("item_instance_id")
            }
            mismatches = {
                instance_id
                for instance_id, desired_locked in desired_by_instance.items()
                if actual_by_instance.get(instance_id) != desired_locked
            }

        # Leave the visible analysis current against the verified lock state.
        await asyncio.to_thread(self.cleaner.analyze, bungie_membership_id)
        return WeaponOrganizerResult(
            candidate_count=len(candidate_row_ids),
            keeper_count=len(organized_items) - len(candidate_row_ids),
            changed_count=sum(
                actual_by_instance.get(item["item_instance_id"])
                == desired_by_instance[item["item_instance_id"]]
                for item in actions
            ),
            already_correct_count=len(organized_items) - len(actions),
            verified_count=len(desired_by_instance) - len(mismatches),
            failed_count=len(mismatches),
        )

    async def _set_with_retry(
        self,
        *,
        access_token: str,
        item_instance_id: str,
        character_id: str,
        membership_type: int,
        locked: bool,
    ) -> None:
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                throttle = await self.bungie.set_item_lock_state(
                    access_token,
                    item_instance_id=item_instance_id,
                    character_id=character_id,
                    membership_type=membership_type,
                    locked=locked,
                )
                await asyncio.sleep(max(ACTION_INTERVAL_SECONDS, throttle))
                return
            except BungieTemporarilyUnavailable:
                if attempt >= len(RETRY_DELAYS):
                    raise
                await asyncio.sleep(RETRY_DELAYS[attempt])


class ArmorOrganizerService(WeaponOrganizerService):
    def __init__(
        self,
        database: Database,
        bungie: BungieClient,
        inventory: InventoryService,
        manifest: ManifestService,
        cleaner: ArmorCleanerService,
    ) -> None:
        super().__init__(
            database,
            bungie,
            inventory,
            manifest,
            cleaner,
            item_type=2,
            label="armor",
            vault_only=False,
        )


def first_character_id(source: dict[str, Any]) -> str:
    character_ids = source.get("character_ids")
    if not character_ids:
        raise WeaponOrganizerError(
            "No Destiny character is available for item actions."
        )
    return str(character_ids[0])
