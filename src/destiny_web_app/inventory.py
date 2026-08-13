"""Full-inventory synchronization and Bungie profile normalization."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from destiny_web_app.bungie import BungieClient, BungieError
from destiny_web_app.database import (
    CharacterRecord,
    Database,
    InventoryItemRecord,
    InventorySnapshot,
    utc_now,
)


# One GetProfile response forms one coherent inventory snapshot.
INVENTORY_COMPONENTS = (
    100,  # Profiles
    102,  # ProfileInventories (includes the vault)
    103,  # ProfileCurrencies
    200,  # Characters
    201,  # CharacterInventories
    204,  # CharacterActivities (orbit/social-space safety evidence)
    205,  # CharacterEquipment
    206,  # CharacterLoadouts
    300,  # ItemInstances
    301,  # ItemObjectives
    302,  # ItemPerks
    303,  # ItemRenderData
    304,  # ItemStats
    305,  # ItemSockets
    306,  # ItemTalentGrids
    308,  # ItemPlugStates
    309,  # ItemPlugObjectives
    310,  # ItemReusablePlugs
    1000,  # Transitory (live/offline activity evidence)
)

VAULT_LOCATION = 2
POSTMASTER_LOCATION = 4
POSTMASTER_BUCKET_HASH = 215593132
REQUIRED_ITEM_COMPONENTS = (
    "instances",
    "objectives",
    "perks",
    "renderData",
    "stats",
    "sockets",
    "talentGrids",
    "plugStates",
    "plugObjectives",
    "reusablePlugs",
)


class InventoryDataError(ValueError):
    """Bungie's response could not form a complete inventory snapshot."""


@dataclass(frozen=True, slots=True)
class DestinyMembership:
    membership_id: str
    membership_type: int
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class InventorySyncResult:
    snapshot_id: str | None
    used_cache: bool
    status: dict[str, Any]


class InventoryService:
    def __init__(
        self,
        database: Database,
        bungie: BungieClient,
        *,
        stale_seconds: int,
    ) -> None:
        self.database = database
        self.bungie = bungie
        self.stale_seconds = stale_seconds
        self._locks: dict[str, asyncio.Lock] = {}

    async def synchronize(
        self,
        *,
        bungie_membership_id: str,
        access_token: str,
        force: bool = False,
    ) -> InventorySyncResult:
        lock = self._locks.setdefault(bungie_membership_id, asyncio.Lock())
        async with lock:
            current = await asyncio.to_thread(
                self.database.inventory_status,
                bungie_membership_id,
            )
            if (
                not force
                and current.get("sync_status") == "fresh"
                and current.get("snapshot_id")
            ):
                await asyncio.to_thread(
                    self.database.record_inventory_cache_hit,
                    bungie_membership_id,
                )
                status = await asyncio.to_thread(
                    self.database.inventory_status,
                    bungie_membership_id,
                )
                return InventorySyncResult(
                    snapshot_id=status.get("snapshot_id"),
                    used_cache=True,
                    status=status,
                )

            try:
                membership_resource = await asyncio.to_thread(
                    self.database.membership_resource,
                    bungie_membership_id,
                )
                membership_data = membership_resource["payload"]
                # A forced inventory refresh must bypass the profile cache,
                # but the selected Destiny membership is independent account
                # metadata. Reuse it until its own expiry instead of adding a
                # second Bungie request to every operation verification.
                if not membership_resource["fresh"]:
                    membership_data = await self.bungie.get_current_memberships(
                        access_token
                    )
                    await asyncio.to_thread(
                        self.database.save_membership_data,
                        bungie_membership_id,
                        membership_data,
                    )
                membership = select_destiny_membership(membership_data)
                profile = await self.bungie.get_profile(
                    access_token,
                    membership_type=membership.membership_type,
                    membership_id=membership.membership_id,
                    components=INVENTORY_COMPONENTS,
                )
                snapshot = build_inventory_snapshot(
                    bungie_membership_id=bungie_membership_id,
                    membership=membership,
                    profile=profile,
                    stale_seconds=self.stale_seconds,
                )
                snapshot_id = await asyncio.to_thread(
                    self.database.save_inventory_snapshot,
                    snapshot,
                )
            except Exception as error:
                await asyncio.to_thread(
                    self.database.record_inventory_failure,
                    bungie_membership_id,
                    public_sync_error(error),
                )
                raise

            status = await asyncio.to_thread(
                self.database.inventory_status,
                bungie_membership_id,
            )
            return InventorySyncResult(
                snapshot_id=snapshot_id,
                used_cache=False,
                status=status,
            )

    async def simulate_failed_refresh(
        self,
        bungie_membership_id: str,
    ) -> None:
        await asyncio.to_thread(
            self.database.record_inventory_failure,
            bungie_membership_id,
            "Controlled development failure; the last complete snapshot was retained.",
        )


def select_destiny_membership(
    membership_data: dict[str, Any],
) -> DestinyMembership:
    raw_memberships = membership_data.get("destinyMemberships")
    if not isinstance(raw_memberships, list) or not raw_memberships:
        raise InventoryDataError(
            "Bungie did not return a linked Destiny membership."
        )

    memberships: list[DestinyMembership] = []
    for raw in raw_memberships:
        if not isinstance(raw, dict):
            continue
        membership_id = raw.get("membershipId")
        membership_type = raw.get("membershipType")
        if (
            isinstance(membership_id, (str, int))
            and str(membership_id)
            and isinstance(membership_type, int)
            and membership_type != 254
        ):
            memberships.append(
                DestinyMembership(
                    membership_id=str(membership_id),
                    membership_type=membership_type,
                    payload=raw,
                )
            )
    if not memberships:
        raise InventoryDataError(
            "Bungie did not return a usable Destiny 2 membership."
        )

    primary_id = membership_data.get("primaryMembershipId")
    if primary_id is not None:
        primary_text = str(primary_id)
        for membership in memberships:
            if membership.membership_id == primary_text:
                return membership
        raise InventoryDataError(
            "Bungie's cross-save primary membership was not in the linked "
            "membership list."
        )

    # Without cross-save, Bungie does not define a primary membership. Prefer a
    # membership that is not shadowed by a cross-save override, preserving the
    # response order as the final deterministic tie-breaker.
    for membership in memberships:
        override = membership.payload.get("crossSaveOverride")
        if override in (None, 0, membership.membership_type):
            return membership
    return memberships[0]


def build_inventory_snapshot(
    *,
    bungie_membership_id: str,
    membership: DestinyMembership,
    profile: dict[str, Any],
    stale_seconds: int,
) -> InventorySnapshot:
    fetched_at = utc_now()
    character_data = required_component_dictionary(profile, "characters")
    characters = tuple(
        build_character(character_id, payload)
        for character_id, payload in character_data.items()
    )
    character_ids = {character.character_id for character in characters}
    if not characters:
        raise InventoryDataError(
            "The selected Destiny profile did not return any characters."
        )

    item_components = profile.get("itemComponents")
    if not isinstance(item_components, dict):
        raise InventoryDataError(
            "Bungie did not return the requested item components."
        )
    component_maps = {
        name: required_component_dictionary(item_components, name)
        for name in REQUIRED_ITEM_COMPONENTS
    }
    instance_components = component_maps["instances"]

    items: list[InventoryItemRecord] = []
    seen_instances: set[str] = set()
    append_inventory_items(
        items,
        required_inventory_items(profile, "profileInventory"),
        source_kind=None,
        character_id=None,
        item_components=item_components,
        seen_instances=seen_instances,
    )

    for component_name, source_kind in (
        ("characterInventories", "character_inventory"),
        ("characterEquipment", "equipped"),
    ):
        inventories = required_component_dictionary(profile, component_name)
        if set(inventories) != character_ids:
            raise InventoryDataError(
                f"{component_name} did not contain the complete character set."
            )
        for character_id in character_ids:
            append_inventory_items(
                items,
                inventory_items(inventories.get(character_id)),
                source_kind=source_kind,
                character_id=character_id,
                item_components=item_components,
                seen_instances=seen_instances,
            )

    missing_instance_components = seen_instances - set(instance_components)
    if missing_instance_components:
        raise InventoryDataError(
            "Bungie omitted instance data for one or more inventory items."
        )

    profile_component = component_data(profile.get("profile"), "profile")
    expected_character_ids = profile_component.get("characterIds")
    if isinstance(expected_character_ids, list):
        expected = {str(value) for value in expected_character_ids}
        if expected != character_ids:
            raise InventoryDataError(
                "The profile and character components disagree about the "
                "complete character list."
            )

    source_minted_at = profile.get("responseMintedTimestamp")
    if not isinstance(source_minted_at, str):
        source_minted_at = None

    return InventorySnapshot(
        bungie_membership_id=bungie_membership_id,
        destiny_membership_id=membership.membership_id,
        membership_type=membership.membership_type,
        account=membership.payload,
        characters=characters,
        items=tuple(items),
        raw_response=profile,
        fetched_at=fetched_at,
        stale_at=fetched_at + timedelta(seconds=stale_seconds),
        source_minted_at=source_minted_at,
    )


def build_character(
    character_id: str,
    payload: Any,
) -> CharacterRecord:
    if not isinstance(payload, dict):
        raise InventoryDataError("Bungie returned invalid character data.")
    return CharacterRecord(
        character_id=str(character_id),
        class_type=optional_int(payload.get("classType")),
        class_hash=optional_int(payload.get("classHash")),
        light=optional_int(payload.get("light")),
        emblem_hash=optional_int(payload.get("emblemHash")),
        payload=payload,
    )


def append_inventory_items(
    destination: list[InventoryItemRecord],
    raw_items: list[dict[str, Any]],
    *,
    source_kind: str | None,
    character_id: str | None,
    item_components: dict[str, Any],
    seen_instances: set[str],
) -> None:
    for ordinal, item in enumerate(raw_items):
        item_hash = required_uint32(item, "itemHash")
        bucket_hash = required_uint32(item, "bucketHash")
        item_instance_id = parse_item_instance_id(item.get("itemInstanceId"))
        location = optional_int(item.get("location"))
        actual_source = source_kind
        if actual_source is None:
            actual_source = (
                "vault"
                if location == VAULT_LOCATION
                else "profile_inventory"
            )
        elif actual_source == "character_inventory" and (
            location == POSTMASTER_LOCATION
            or bucket_hash == POSTMASTER_BUCKET_HASH
        ):
            actual_source = "postmaster"

        if item_instance_id:
            if item_instance_id in seen_instances:
                raise InventoryDataError(
                    "Bungie returned one item instance in multiple inventory "
                    "locations; the incomplete snapshot was rejected."
                )
            seen_instances.add(item_instance_id)
            record_key = f"instance:{item_instance_id}"
        else:
            owner = character_id or "profile"
            record_key = (
                f"stack:{actual_source}:{owner}:{item_hash}:{bucket_hash}:{ordinal}"
            )

        destination.append(
            InventoryItemRecord(
                record_key=record_key,
                item_instance_id=item_instance_id,
                item_hash=item_hash,
                bucket_hash=bucket_hash,
                quantity=positive_int(item.get("quantity", 1), "quantity"),
                state=nonnegative_int(item.get("state", 0), "state"),
                bind_status=optional_int(item.get("bindStatus")),
                location=location,
                transfer_status=optional_int(item.get("transferStatus")),
                lockable=optional_bool(item.get("lockable")),
                source_kind=actual_source,
                character_id=character_id,
                payload=item,
                components=components_for_instance(
                    item_components,
                    item_instance_id,
                ),
            )
        )


def components_for_instance(
    item_components: dict[str, Any],
    item_instance_id: str | None,
) -> dict[str, Any]:
    if item_instance_id is None:
        return {}
    components: dict[str, Any] = {}
    for component_name, wrapper in item_components.items():
        if not isinstance(component_name, str) or not isinstance(wrapper, dict):
            continue
        data = wrapper.get("data")
        if not isinstance(data, dict):
            continue
        value = data.get(item_instance_id)
        if value is not None:
            components[component_name] = value
    return components


def component_dictionary(
    profile: dict[str, Any],
    field: str,
) -> dict[str, Any]:
    wrapper = profile.get(field)
    if wrapper is None:
        return {}
    data = component_data(wrapper, field)
    if not isinstance(data, dict):
        raise InventoryDataError(f"Bungie returned invalid {field} data.")
    return {str(key): value for key, value in data.items()}


def required_component_dictionary(
    profile: dict[str, Any],
    field: str,
) -> dict[str, Any]:
    if field not in profile or not isinstance(profile[field], dict):
        raise InventoryDataError(
            f"Bungie did not return the requested {field} component."
        )
    return component_dictionary(profile, field)


def required_inventory_items(
    profile: dict[str, Any],
    field: str,
) -> list[dict[str, Any]]:
    if field not in profile or not isinstance(profile[field], dict):
        raise InventoryDataError(
            f"Bungie did not return the requested {field} component."
        )
    return inventory_items(profile[field])


def component_data(wrapper: Any, field: str) -> Any:
    if not isinstance(wrapper, dict):
        raise InventoryDataError(f"Bungie did not return the {field} component.")
    if "data" not in wrapper:
        raise InventoryDataError(f"Bungie's {field} component had no data.")
    return wrapper["data"]


def inventory_items(wrapper: Any) -> list[dict[str, Any]]:
    if wrapper is None:
        return []
    if not isinstance(wrapper, dict):
        raise InventoryDataError("Bungie returned invalid inventory data.")
    # Single-component responses wrap the inventory in ``data``. Values inside
    # a character dictionary are already the inventory component itself.
    data = wrapper if "items" in wrapper else component_data(wrapper, "inventory")
    if not isinstance(data, dict):
        raise InventoryDataError("Bungie returned invalid inventory data.")
    items = data.get("items")
    if items is None:
        return []
    if not isinstance(items, list) or any(
        not isinstance(item, dict) for item in items
    ):
        raise InventoryDataError("Bungie returned an invalid inventory list.")
    return items


def required_uint32(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 < value < 2**32
    ):
        raise InventoryDataError(
            f"An inventory item has an invalid {field}."
        )
    return value


def parse_item_instance_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise InventoryDataError("An inventory item has an invalid instance ID.")
    text = str(value)
    if not text.isdecimal() or int(text) <= 0:
        raise InventoryDataError("An inventory item has an invalid instance ID.")
    return text


def positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise InventoryDataError(f"An inventory item has an invalid {field}.")
    return value


def nonnegative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InventoryDataError(f"An inventory item has an invalid {field}.")
    return value


def optional_int(value: Any, *, default: int | None = None) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def public_sync_error(error: Exception) -> str:
    if isinstance(error, (BungieError, InventoryDataError, LookupError)):
        return str(error)
    return "Inventory synchronization failed before a complete snapshot was saved."
