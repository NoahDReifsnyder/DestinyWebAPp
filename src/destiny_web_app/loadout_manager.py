"""Read-only inspection of Bungie character loadouts and API capabilities."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Iterable

from destiny_web_app.manifest import ManifestError, ManifestService

if TYPE_CHECKING:
    from destiny_web_app.loadouts.storage import LoadoutStore


CLASS_NAMES = {0: "Titan", 1: "Hunter", 2: "Warlock", 3: "Guardian"}
LOCATION_NAMES = {
    "equipped": "Equipped",
    "character_inventory": "Carried",
    "vault": "Vault",
    "profile_inventory": "Shared inventory",
    "postmaster": "Postmaster",
}
REQUIRED_GAMEPLAY_BUCKET_ORDER = (
    1498876634,  # Kinetic weapon
    2465295065,  # Energy weapon
    953998645,  # Power weapon
    3448274439,  # Helmet
    3551918588,  # Gauntlets
    14239492,  # Chest armor
    20886954,  # Leg armor
    1585787867,  # Class armor
    3284755031,  # Subclass
    1506418338,  # Seasonal artifact
)
REQUIRED_GAMEPLAY_BUCKETS = set(REQUIRED_GAMEPLAY_BUCKET_ORDER)
# Bungie uses the FNV offset basis as an invalid/empty hash sentinel.
INVALID_HASH_SENTINEL = 2166136261
GAMEPLAY_BUCKET_NAMES = {
    1498876634: "Kinetic weapon",
    2465295065: "Energy weapon",
    953998645: "Power weapon",
    3448274439: "Helmet",
    3551918588: "Gauntlets",
    14239492: "Chest armor",
    20886954: "Leg armor",
    1585787867: "Class armor",
    3284755031: "Subclass",
    1506418338: "Seasonal artifact",
}

CAPABILITY_MATRIX = (
    {
        "field": "Slot index",
        "read": "Observed",
        "write": "Contract available",
        "status": "verify",
        "detail": (
            "The CharacterLoadouts array provides the observed index. Bungie "
            "loadout actions accept loadoutIndex, but index behavior remains "
            "read-only until the controlled single-slot test."
        ),
    },
    {
        "field": "Name, icon, and color",
        "read": "Resolved",
        "write": "Contract available",
        "status": "verify",
        "detail": (
            "Hashes resolve through the official manifest. "
            "UpdateLoadoutIdentifiers accepts these hashes."
        ),
    },
    {
        "field": "Exact item instances",
        "read": "Observed",
        "write": "Preparation required",
        "status": "verify",
        "detail": (
            "Each stored entry identifies an exact item instance. Arbitrary "
            "application loadouts must equip those instances before snapshot."
        ),
    },
    {
        "field": "Ordered socket plugs",
        "read": "Observed",
        "write": "Free gameplay plugs supported",
        "status": "verify",
        "detail": (
            "Ordered plug hashes are readable. Armor mods and subclass plugs "
            "that Bungie's live plug sets report as free and insertable are "
            "restored with InsertSocketPlugFree and verified. Other socket "
            "types remain blocked until separately supported."
        ),
    },
    {
        "field": "Subclass configuration",
        "read": "Item and plugs",
        "write": "Free plugs supported",
        "status": "verify",
        "detail": (
            "Subclass state appears as a loadout item with ordered plugs. "
            "Saved differences reported by Bungie's live plug sets as free, "
            "enabled, and insertable are restored and verified."
        ),
    },
    {
        "field": "Artifact configuration",
        "read": "Observed item and plugs",
        "write": "Experimental",
        "status": "experimental",
        "detail": (
            "The live component represents the equipped artifact as an exact "
            "item with ordered plugs. Arbitrary restoration still requires a "
            "controlled capability test."
        ),
    },
    {
        "field": "Activity eligibility",
        "read": "CharacterActivities",
        "write": "Enforced before actions",
        "status": "verify",
        "detail": (
            "Fresh previews fetch CharacterActivities and block confirmation "
            "unless the character is in orbit, a social space, or offline."
        ),
    },
)


class LoadoutInspectionError(ValueError):
    """Stored loadout data cannot be inspected safely."""


class LoadoutManagerService:
    """Build a manifest-backed view of the active loadout snapshot."""

    def __init__(
        self,
        database: LoadoutStore,
        manifest: ManifestService,
    ) -> None:
        self.database = database
        self.manifest = manifest

    def inspect(self, bungie_membership_id: str) -> dict[str, Any] | None:
        source = self.database.load_active_loadout_source(
            bungie_membership_id
        )
        if source is None:
            return None

        profile = source["profile"]
        loadout_wrapper = profile.get("characterLoadouts")
        if not isinstance(loadout_wrapper, dict):
            raise LoadoutInspectionError(
                "The active snapshot has no CharacterLoadouts component."
            )
        loadout_data = loadout_wrapper.get("data")
        if not isinstance(loadout_data, dict):
            raise LoadoutInspectionError(
                "The active CharacterLoadouts component is invalid."
            )

        items_by_instance = {
            str(item["item_instance_id"]): item
            for item in source["items"]
            if item.get("item_instance_id")
        }
        raw_characters = []
        identifier_hashes = {"name": set(), "icon": set(), "color": set()}
        item_hashes: set[int] = set()
        plug_hashes: set[int] = set()
        warnings: list[str] = []

        for character in source["characters"]:
            character_id = character["character_id"]
            component = loadout_data.get(character_id)
            if not isinstance(component, dict):
                warnings.append(
                    f"{character_label(character)} has no loadout component."
                )
                loadouts: list[Any] = []
            else:
                loadouts = component.get("loadouts", [])
                if not isinstance(loadouts, list):
                    raise LoadoutInspectionError(
                        "Bungie returned an invalid character loadout list."
                    )

            parsed_slots = []
            for slot_index, raw_slot in enumerate(loadouts):
                slot = raw_slot if isinstance(raw_slot, dict) else {}
                identifiers = {
                    kind: valid_hash(slot.get(f"{kind}Hash"))
                    for kind in ("name", "icon", "color")
                }
                for kind, value in identifiers.items():
                    if value is not None:
                        identifier_hashes[kind].add(value)

                raw_items = slot.get("items", [])
                if not isinstance(raw_items, list):
                    raw_items = []
                    warnings.append(
                        f"{character_label(character)} slot {slot_index + 1} "
                        "has an invalid item list."
                    )
                else:
                    # Cleared Bungie loadout slots contain ten sentinel rows
                    # rather than an empty array. They are placeholders, not
                    # unresolved saved items.
                    raw_items = [
                        item
                        for item in raw_items
                        if isinstance(item, dict)
                        and valid_instance_id(item.get("itemInstanceId"))
                        is not None
                    ]
                parsed_items = []
                for raw_item in raw_items:
                    entry = raw_item if isinstance(raw_item, dict) else {}
                    instance_id = valid_instance_id(
                        entry.get("itemInstanceId")
                    )
                    inventory_item = (
                        items_by_instance.get(instance_id)
                        if instance_id is not None
                        else None
                    )
                    if inventory_item is not None:
                        item_hashes.add(int(inventory_item["item_hash"]))
                    plugs = ordered_hashes(entry.get("plugItemHashes"))
                    plug_hashes.update(
                        value for value in plugs if value is not None
                    )
                    parsed_items.append(
                        {
                            "instance_id": instance_id,
                            "inventory_item": inventory_item,
                            "plug_hashes": plugs,
                        }
                    )
                parsed_slots.append(
                    {
                        "slot_index": slot_index,
                        "display_index": slot_index + 1,
                        "identifiers": identifiers,
                        "raw": slot,
                        "items": parsed_items,
                    }
                )
            raw_characters.append(
                {"character": character, "slots": parsed_slots}
            )

        definitions = self._definitions(
            identifier_hashes=identifier_hashes,
            item_hashes=item_hashes,
            plug_hashes=plug_hashes,
        )
        if definitions["error"]:
            warnings.append(definitions["error"])

        constants = definitions["constants"]
        manifest_capacity = loadout_capacity(constants)
        filtered_socket_types, filtered_socket_categories = loadout_filters(
            constants
        )

        characters = []
        all_unresolved: set[str] = set()
        observed_counts: set[int] = set()
        for raw_character in raw_characters:
            character = raw_character["character"]
            slots = []
            character_unresolved: set[str] = set()
            for parsed_slot in raw_character["slots"]:
                identifiers = parsed_slot["identifiers"]
                items = []
                for parsed_item in parsed_slot["items"]:
                    inventory_item = parsed_item["inventory_item"]
                    instance_id = parsed_item["instance_id"]
                    if inventory_item is None and instance_id is not None:
                        character_unresolved.add(instance_id)
                        all_unresolved.add(instance_id)
                    item_definition = (
                        definitions["items"].get(
                            int(inventory_item["item_hash"])
                        )
                        if inventory_item is not None
                        else None
                    )
                    plugs = build_plugs(
                        parsed_item["plug_hashes"],
                        item_definition,
                        definitions["plugs"],
                        definitions["socket_types"],
                        definitions["socket_categories"],
                        filtered_socket_types,
                        filtered_socket_categories,
                    )
                    items.append(
                        build_item(
                            instance_id,
                            inventory_item,
                            item_definition,
                            plugs,
                        )
                    )

                name_hash = identifiers["name"]
                icon_hash = identifiers["icon"]
                color_hash = identifiers["color"]
                slots.append(
                    {
                        **parsed_slot,
                        "items": items,
                        "name": loadout_name(
                            definitions["names"].get(name_hash),
                            parsed_slot["display_index"],
                        ),
                        "icon_path": definition_path(
                            definitions["icons"].get(icon_hash),
                            "iconImagePath",
                        ),
                        "color_path": definition_path(
                            definitions["colors"].get(color_hash),
                            "colorImagePath",
                        ),
                        "unresolved_count": sum(
                            1 for item in items if not item["resolved"]
                        ),
                    }
                )
            observed_counts.add(len(slots))
            characters.append(
                {
                    "character_id": character["character_id"],
                    "class_name": character_label(character),
                    "light": character.get("light"),
                    "emblem_path": character["payload"].get(
                        "emblemBackgroundPath"
                    ),
                    "slots": slots,
                    "slot_count": len(slots),
                    "populated_count": sum(
                        1 for slot in slots if slot["items"]
                    ),
                    "unresolved_count": len(character_unresolved),
                }
            )

        if len(observed_counts) > 1:
            warnings.append(
                "Characters returned different numbers of loadout entries."
            )
        observed_capacity = max(observed_counts, default=0)
        if (
            manifest_capacity is not None
            and observed_capacity != manifest_capacity
        ):
            warnings.append(
                "The active profile exposes "
                f"{observed_capacity} entries per character, while the local "
                f"manifest declares a maximum of {manifest_capacity}. The "
                "observed entries are displayed, but write behavior remains "
                "unverified."
            )

        snapshot = source["snapshot"]
        return {
            "snapshot": snapshot,
            "characters": characters,
            "capabilities": CAPABILITY_MATRIX,
            "warnings": warnings,
            "summary": {
                "character_count": len(characters),
                "observed_slots_per_character": observed_capacity,
                "manifest_capacity": manifest_capacity,
                "populated_slot_count": sum(
                    character["populated_count"] for character in characters
                ),
                "unique_unresolved_count": len(all_unresolved),
            },
            "manifest": {
                "version": self.manifest.status().get("version"),
                "available": self.manifest.status().get("available", False),
            },
        }

    def capture_current_equipment(
        self,
        bungie_membership_id: str,
        *,
        character_id: str,
        name: str,
        description: str = "",
        tags: list[str] | None = None,
        loadout_id: str | None = None,
        revision_note: str = "",
        cover_icon_hash: int | None = None,
    ) -> dict[str, Any]:
        """Save one character's complete current equipment as revision one."""
        name, description, tags = validate_metadata(
            name,
            description,
            tags or [],
        )
        source, character, manifest_version = self._capture_source(
            bungie_membership_id,
            character_id,
        )
        equipment_wrapper = (
            source["profile"]
            .get("characterEquipment", {})
            .get("data", {})
            .get(character_id)
        )
        raw_equipment = component_items(equipment_wrapper)
        if not raw_equipment:
            raise LoadoutInspectionError(
                "The selected character has no captured equipment."
            )
        item_index = inventory_item_index(source["items"])
        captured = []
        item_hashes: set[int] = set()
        plug_hashes: set[int] = set()
        raw_plugs_by_instance: dict[str, list[int | None]] = {}
        for equipment_order, raw_item in enumerate(raw_equipment):
            instance_id = valid_instance_id(raw_item.get("itemInstanceId"))
            inventory_item = item_index.get(instance_id or "")
            if inventory_item is None:
                raise LoadoutInspectionError(
                    "Current equipment contains an unresolved exact item."
                )
            raw_plugs = component_plug_hashes(inventory_item["components"])
            raw_plugs_by_instance[instance_id] = raw_plugs
            plug_hashes.update(
                value for value in raw_plugs if value is not None
            )
            item_hashes.add(int(inventory_item["item_hash"]))
            captured.append(
                capture_item(
                    equipment_order,
                    inventory_item,
                    plugs=[],
                )
            )
        definitions = self._definitions(
            identifier_hashes={"name": set(), "icon": set(), "color": set()},
            item_hashes=item_hashes,
            plug_hashes=plug_hashes,
        )
        if definitions["error"]:
            raise LoadoutInspectionError(definitions["error"])
        filtered_types, filtered_categories = loadout_filters(
            definitions["constants"]
        )
        reference_items = []
        gameplay_items = []
        for item in captured:
            item_definition = definitions["items"].get(item["item_hash"])
            item["bucket_hash"] = intended_bucket_hash(item_definition)
            if item["bucket_hash"] not in REQUIRED_GAMEPLAY_BUCKETS:
                reference_items.append(
                    reference_item(item, item_definition)
                )
                continue
            plug_rows = build_plugs(
                raw_plugs_by_instance[item["item_instance_id"]],
                item_definition,
                definitions["plugs"],
                definitions["socket_types"],
                definitions["socket_categories"],
                filtered_types,
                filtered_categories,
            )
            item["plugs"] = captured_plugs(plug_rows)
            gameplay_items.append(item)
        captured = gameplay_items
        for gameplay_order, item in enumerate(captured):
            item["equipment_order"] = gameplay_order
        ensure_complete_capture(captured)

        capture = {
            "snapshot_id": source["snapshot"]["snapshot_id"],
            "manifest_version": manifest_version,
            "capture_source": "current_equipment",
            "source_character_id": character_id,
            "source_slot_index": None,
            "character_class_type": int(character["class_type"]),
            "items": captured,
            "reference_items": reference_items,
            "source_payload": equipment_wrapper,
        }
        if loadout_id is None:
            saved = self.database.save_captured_loadout(
                bungie_membership_id,
                name=name,
                description=description,
                tags=tags,
                capture=capture,
                cover_icon_hash=self.validate_cover_icon(cover_icon_hash),
            )
        else:
            saved = self.database.append_captured_loadout_revision(
                bungie_membership_id,
                loadout_id,
                capture=capture,
                revision_note=revision_note,
            )
        return self._enrich_saved(saved)

    def builder_catalog(
        self,
        bungie_membership_id: str,
        *,
        class_type: int,
    ) -> dict[str, Any]:
        """Return exact compatible items for a complete local-only builder."""
        if class_type not in (0, 1, 2):
            raise LoadoutInspectionError("Choose Titan, Hunter, or Warlock.")
        source = self.database.load_active_loadout_source(
            bungie_membership_id
        )
        if source is None or not snapshot_is_fresh(source["snapshot"]):
            raise LoadoutInspectionError(
                "Refresh inventory before building a loadout."
            )
        character = next(
            (
                row
                for row in source["characters"]
                if row.get("class_type") == class_type
            ),
            None,
        )
        if character is None:
            raise LoadoutInspectionError(
                "The active inventory has no character for that class."
            )
        manifest_status = self.manifest.status()
        if not manifest_status.get("available"):
            raise LoadoutInspectionError(
                "Prepare the Destiny manifest before using the builder."
            )
        item_hashes = {int(row["item_hash"]) for row in source["items"]}
        definitions = self.manifest.resolve_many(
            "DestinyInventoryItemDefinition", item_hashes
        )
        relevant = []
        plug_hashes: set[int] = set()
        stat_hashes: set[int] = set()
        for row in source["items"]:
            definition = definitions.get(int(row["item_hash"]))
            try:
                bucket_hash = intended_bucket_hash(definition)
            except LoadoutInspectionError:
                continue
            definition_class = (
                definition.get("classType") if definition else None
            )
            if (
                bucket_hash not in REQUIRED_GAMEPLAY_BUCKETS
                or definition_class not in (class_type, 3)
                or not row.get("item_instance_id")
            ):
                continue
            raw_plugs = component_plug_hashes(row.get("components"))
            raw_stat_rows = row.get("components", {}).get("stats", {}).get(
                "stats", {}
            )
            if isinstance(raw_stat_rows, dict):
                stat_hashes.update(
                    int(value)
                    for value in raw_stat_rows
                    if str(value).isdecimal()
                )
            plug_hashes.update(
                value for value in raw_plugs if value is not None
            )
            relevant.append((row, definition, bucket_hash, raw_plugs))
        enriched_definitions = self._definitions(
            identifier_hashes={"name": set(), "icon": set(), "color": set()},
            item_hashes=item_hashes,
            plug_hashes=plug_hashes,
        )
        if enriched_definitions["error"]:
            raise LoadoutInspectionError(enriched_definitions["error"])
        stat_definitions = self.manifest.resolve_many(
            "DestinyStatDefinition", stat_hashes
        )
        filtered_types, filtered_categories = loadout_filters(
            enriched_definitions["constants"]
        )
        protected = self.database.application_loadout_instance_ids(
            bungie_membership_id
        )
        buckets = []
        for bucket_hash in REQUIRED_GAMEPLAY_BUCKET_ORDER:
            choices = []
            for row, definition, actual_bucket, raw_plugs in relevant:
                if actual_bucket != bucket_hash:
                    continue
                components = row.get("components", {})
                instance_component = components.get("instances", {})
                primary_stat = (
                    instance_component.get("primaryStat", {})
                    if isinstance(instance_component, dict)
                    else {}
                )
                stat_component = components.get("stats", {})
                raw_stats = (
                    stat_component.get("stats", {})
                    if isinstance(stat_component, dict)
                    else {}
                )
                stats = sorted(
                    (
                        {
                            "hash": str(stat_hash),
                            "name": display_name(
                                stat_definitions.get(int(stat_hash))
                            ) or str(stat_hash),
                            "value": int(stat.get("value", 0)),
                        }
                        for stat_hash, stat in raw_stats.items()
                        if isinstance(stat, dict)
                        and isinstance(stat.get("value"), int)
                    ),
                    key=lambda value: value["value"],
                    reverse=True,
                )
                inventory = (
                    definition.get("inventory", {}) if definition else {}
                )
                tier_type = (
                    inventory.get("tierType")
                    if isinstance(inventory, dict)
                    else None
                )
                can_equip = (
                    instance_component.get("canEquip")
                    if isinstance(instance_component, dict)
                    else None
                )
                unsupported_reason = ""
                if (definition or {}).get("equippable") is False:
                    unsupported_reason = "Definition marks this item unequippable"
                elif can_equip is False:
                    reason = instance_component.get("cannotEquipReason", 0)
                    unsupported_reason = f"Cannot currently equip (reason {reason})"
                elif row.get("source_kind") == "postmaster":
                    unsupported_reason = "Retrieve this item from the Postmaster first"
                elif (
                    bool(int(row.get("transfer_status") or 0) & 2)
                    and row.get("character_id") != character["character_id"]
                ):
                    unsupported_reason = "Cannot transfer to this class character"
                choices.append(
                    {
                        "instance_id": str(row["item_instance_id"]),
                        "item_hash": int(row["item_hash"]),
                        "bucket_hash": bucket_hash,
                        "name": display_name(definition) or "Unknown item",
                        "type": (
                            definition.get("itemTypeAndTierDisplayName")
                            if definition
                            else "Unknown item"
                        ) or "Unknown item",
                        "icon_path": display_icon(definition),
                        "location": LOCATION_NAMES.get(
                            str(row.get("source_kind")), "Unknown"
                        ),
                        "source_kind": row.get("source_kind"),
                        "character_id": row.get("character_id"),
                        "locked": bool(int(row.get("state") or 0) & 1),
                        "power": (
                            primary_stat.get("value")
                            if isinstance(primary_stat, dict)
                            else None
                        ),
                        "stats": stats[:6],
                        "protected": str(row["item_instance_id"]) in protected,
                        "transfer_status": int(row.get("transfer_status") or 0),
                        "non_transferable": bool(
                            (int(row.get("transfer_status") or 0) & 2)
                            or (definition or {}).get("nonTransferrable")
                        ),
                        "exotic": tier_type == 6,
                        "selectable": not unsupported_reason,
                        "unsupported_reason": unsupported_reason,
                        "plugs": build_plugs(
                            raw_plugs,
                            definition,
                            enriched_definitions["plugs"],
                            enriched_definitions["socket_types"],
                            enriched_definitions["socket_categories"],
                            filtered_types,
                            filtered_categories,
                        ),
                    }
                )
            choices.sort(
                key=lambda value: (
                    not value["selectable"],
                    not value["protected"],
                    value["name"].lower(),
                    -int(value["power"] or 0),
                    value["instance_id"],
                )
            )
            buckets.append(
                {
                    "bucket_hash": bucket_hash,
                    "name": GAMEPLAY_BUCKET_NAMES[bucket_hash],
                    "choices": choices,
                }
            )
        return {
            "class_type": class_type,
            "class_name": CLASS_NAMES[class_type],
            "character_id": str(character["character_id"]),
            "snapshot": source["snapshot"],
            "manifest_version": str(manifest_status["version"]),
            "buckets": buckets,
            "complete_catalog": all(
                any(choice["selectable"] for choice in bucket["choices"])
                for bucket in buckets
            ),
        }

    def save_builder_loadout(
        self,
        bungie_membership_id: str,
        *,
        class_type: int,
        selections: dict[int, str],
        name: str,
        description: str = "",
        tags: list[str] | None = None,
        revision_action: str = "builder",
        source_identifiers: dict[str, int | None] | None = None,
        cover_icon_hash: int | None = None,
        loadout_id: str | None = None,
        set_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate and persist one complete exact builder selection."""
        name, description, tags = validate_metadata(
            name, description, tags or []
        )
        catalog = self.builder_catalog(
            bungie_membership_id, class_type=class_type
        )
        if set(selections) != REQUIRED_GAMEPLAY_BUCKETS:
            raise LoadoutInspectionError(
                "Select one exact item in every gameplay slot."
            )
        choice_index = {
            (bucket["bucket_hash"], choice["instance_id"]): choice
            for bucket in catalog["buckets"]
            for choice in bucket["choices"]
            if choice["selectable"]
        }
        selected = []
        seen: set[str] = set()
        for equipment_order, bucket_hash in enumerate(
            REQUIRED_GAMEPLAY_BUCKET_ORDER
        ):
            instance_id = valid_instance_id(selections.get(bucket_hash))
            choice = choice_index.get((bucket_hash, instance_id or ""))
            if choice is None:
                raise LoadoutInspectionError(
                    f"{GAMEPLAY_BUCKET_NAMES[bucket_hash]} selection is no "
                    "longer compatible or owned."
                )
            if instance_id in seen:
                raise LoadoutInspectionError(
                    "One exact item instance cannot fill multiple slots."
                )
            if choice["source_kind"] == "postmaster":
                raise LoadoutInspectionError(
                    f"{choice['name']} is in the Postmaster and cannot be "
                    "used by the automated loadout workflow."
                )
            if choice["non_transferable"] and choice.get(
                "character_id"
            ) != catalog["character_id"]:
                raise LoadoutInspectionError(
                    f"{choice['name']} cannot be transferred to the selected "
                    "class character."
                )
            seen.add(instance_id)
            selected.append(
                {
                    "equipment_order": equipment_order,
                    "item_instance_id": instance_id,
                    "item_hash": choice["item_hash"],
                    "bucket_hash": bucket_hash,
                    "captured_source_kind": choice["source_kind"],
                    "captured_character_id": choice.get("character_id"),
                    "plugs": captured_plugs(choice["plugs"]),
                    "exotic": choice["exotic"],
                }
            )
        weapon_exotics = sum(
            1 for item in selected[:3] if item.pop("exotic")
        )
        armor_exotics = sum(
            1 for item in selected[3:8] if item.pop("exotic")
        )
        for item in selected[8:]:
            item.pop("exotic")
        if weapon_exotics > 1:
            raise LoadoutInspectionError(
                "A loadout can equip at most one Exotic weapon."
            )
        if armor_exotics > 1:
            raise LoadoutInspectionError(
                "A loadout can equip at most one Exotic armor piece."
            )
        ensure_complete_capture(selected)
        capture = {
            "snapshot_id": catalog["snapshot"]["snapshot_id"],
            "manifest_version": catalog["manifest_version"],
            # Schema 8's evidence category remains current equipment; the
            # explicit revision action and payload distinguish builder saves.
            "capture_source": "current_equipment",
            "capture_method": revision_action,
            "source_character_id": catalog["character_id"],
            "source_slot_index": None,
            "character_class_type": class_type,
            "items": selected,
            "source_payload": {
                "method": revision_action,
                "selectedInstanceIds": [
                    item["item_instance_id"] for item in selected
                ],
                **(source_identifiers or {}),
            },
        }
        icon_hash = self.validate_cover_icon(
            cover_icon_hash
            if cover_icon_hash is not None
            else valid_hash((source_identifiers or {}).get("iconHash"))
        )
        if loadout_id is not None and set_id is None:
            saved = self.database.append_captured_loadout_revision(
                bungie_membership_id,
                loadout_id,
                capture=capture,
                revision_note="Changed exact items in the loadout editor",
                revision_action="builder_edit",
            )
            self.database.update_loadout_metadata(
                bungie_membership_id,
                loadout_id,
                name=name,
                description=description,
                tags=tags,
                cover_icon_hash=icon_hash,
            )
            saved = self.database.load_saved_loadout(
                bungie_membership_id, loadout_id
            )
            assert saved is not None
        else:
            saved = self.database.save_captured_loadout(
                bungie_membership_id,
                name=name,
                description=description,
                tags=tags,
                capture=capture,
                revision_action=(
                    "set_fork" if set_id is not None else revision_action
                ),
                revision_note=(
                    "Forked by an exact-item edit within one loadout set"
                    if set_id is not None
                    else "Imported from versioned application format"
                    if revision_action == "import"
                    else "Created from exact stored inventory selections"
                ),
                cover_icon_hash=icon_hash,
                replace_in_set_id=set_id,
                replace_loadout_id=loadout_id,
            )
        return self._enrich_saved(saved)

    def export_bundle(
        self,
        bungie_membership_id: str,
        loadout_id: str,
        *,
        revision_id: str | None = None,
    ) -> dict[str, Any]:
        loadout = self.saved_loadout(
            bungie_membership_id,
            loadout_id,
            revision_id=revision_id,
            include_archived=True,
        )
        if loadout is None:
            raise LoadoutInspectionError("The saved loadout is unavailable.")
        return {
            "format": "destiny-web-app/loadout",
            "version": 1,
            "name": loadout["name"],
            "description": loadout["description"],
            "tags": loadout["tags"],
            "characterClassType": int(loadout["character_class_type"]),
            "revision": {
                "revisionNumber": int(loadout["revision_number"]),
                "manifestVersion": loadout["manifest_version"],
                "identifiers": {
                    "nameHash": valid_hash(
                        loadout["source_payload"].get("nameHash")
                    ),
                    "iconHash": (
                        valid_hash(loadout.get("cover_icon_hash"))
                        or valid_hash(loadout["source_payload"].get("iconHash"))
                    ),
                    "colorHash": valid_hash(
                        loadout["source_payload"].get("colorHash")
                    ),
                },
                "items": [
                    {
                        "equipmentOrder": int(item["equipment_order"]),
                        "itemInstanceId": item["item_instance_id"],
                        "itemHash": int(item["item_hash"]),
                        "bucketHash": int(item["bucket_hash"]),
                        "plugs": [
                            {
                                "socketIndex": int(plug["socket_index"]),
                                "plugHash": plug.get("plug_hash"),
                                "filteredFromPreview": bool(
                                    plug["filtered_from_preview"]
                                ),
                            }
                            for plug in item["plugs"]
                        ],
                    }
                    for item in loadout["items"]
                ],
            },
        }

    def import_bundle(
        self,
        bungie_membership_id: str,
        bundle: dict[str, Any],
    ) -> dict[str, Any]:
        """Import only when every exact instance is currently owned."""
        values = self.validate_import_bundle(bungie_membership_id, bundle)
        return self.save_builder_loadout(
            bungie_membership_id,
            **values,
            revision_action="import",
        )

    def validate_import_bundle(
        self,
        bungie_membership_id: str,
        bundle: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate a versioned loadout without creating database rows."""
        if bundle.get("format") != "destiny-web-app/loadout" or bundle.get(
            "version"
        ) != 1:
            raise LoadoutInspectionError(
                "Only destiny-web-app/loadout version 1 can be imported."
            )
        try:
            class_type = int(bundle["characterClassType"])
            raw_items = bundle["revision"]["items"]
            raw_identifiers = bundle["revision"].get("identifiers", {})
        except (KeyError, TypeError, ValueError) as error:
            raise LoadoutInspectionError("The import bundle is incomplete.") from error
        if not isinstance(raw_items, list):
            raise LoadoutInspectionError("The import item list is invalid.")
        selections: dict[int, str] = {}
        expected_plugs: dict[str, list[tuple[int, int | None, bool]]] = {}
        expected_hashes: dict[str, int] = {}
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise LoadoutInspectionError("The import contains an invalid item.")
            try:
                bucket_hash = int(raw["bucketHash"])
                instance_id = str(raw["itemInstanceId"])
                item_hash = int(raw["itemHash"])
                equipment_order = int(raw["equipmentOrder"])
                plug_rows = raw.get("plugs", [])
            except (KeyError, TypeError, ValueError) as error:
                raise LoadoutInspectionError("An imported item lacks exact identity data.") from error
            if bucket_hash in selections:
                raise LoadoutInspectionError("The import assigns one slot more than once.")
            if instance_id in expected_hashes:
                raise LoadoutInspectionError(
                    "The import reuses one exact instance in multiple slots."
                )
            if (
                bucket_hash not in REQUIRED_GAMEPLAY_BUCKETS
                or equipment_order
                != REQUIRED_GAMEPLAY_BUCKET_ORDER.index(bucket_hash)
            ):
                raise LoadoutInspectionError(
                    "An imported item has an invalid gameplay slot order."
                )
            selections[bucket_hash] = instance_id
            expected_hashes[instance_id] = item_hash
            if not isinstance(plug_rows, list):
                raise LoadoutInspectionError("Imported socket evidence is invalid.")
            expected_plugs[instance_id] = [
                (
                    int(plug["socketIndex"]),
                    valid_hash(plug.get("plugHash")),
                    bool(plug.get("filteredFromPreview")),
                )
                for plug in plug_rows
                if isinstance(plug, dict)
            ]
        catalog = self.builder_catalog(
            bungie_membership_id, class_type=class_type
        )
        if set(selections) != REQUIRED_GAMEPLAY_BUCKETS:
            raise LoadoutInspectionError(
                "The import must contain all ten gameplay slots exactly once."
            )
        choices = {
            choice["instance_id"]: choice
            for bucket in catalog["buckets"]
            for choice in bucket["choices"]
        }
        for instance_id, expected in expected_plugs.items():
            choice = choices.get(instance_id)
            if choice is None:
                raise LoadoutInspectionError(
                    f"Exact imported instance {instance_id} is not owned or compatible."
                )
            if int(choice["item_hash"]) != expected_hashes[instance_id]:
                raise LoadoutInspectionError(
                    f"Exact imported instance {instance_id} has a different item hash."
                )
            actual = {
                int(plug["socket_index"]): plug.get("plug_hash")
                for plug in choice["plugs"]
            }
            for index, plug_hash, filtered in expected:
                if plug_hash is None:
                    continue
                if actual.get(index) != plug_hash:
                    raise LoadoutInspectionError(
                        f"Imported socket {index + 1} on {choice['name']} no longer matches."
                    )
        return {
            "class_type": class_type,
            "selections": selections,
            "name": str(bundle.get("name") or "Imported loadout"),
            "description": str(bundle.get("description") or ""),
            "tags": (
                bundle.get("tags")
                if isinstance(bundle.get("tags"), list)
                else []
            ),
            "source_identifiers": (
                {
                    field: valid_hash(raw_identifiers.get(field))
                    for field in ("nameHash", "iconHash", "colorHash")
                }
                if isinstance(raw_identifiers, dict)
                else {}
            ),
        }

    def import_in_game_slot(
        self,
        bungie_membership_id: str,
        *,
        character_id: str,
        slot_index: int,
        name: str,
        description: str = "",
        tags: list[str] | None = None,
        cover_icon_hash: int | None = None,
    ) -> dict[str, Any]:
        """Import one complete, currently resolvable in-game loadout slot."""
        name, description, tags = validate_metadata(
            name,
            description,
            tags or [],
        )
        source, character, manifest_version = self._capture_source(
            bungie_membership_id,
            character_id,
        )
        capture, raw_slot = self._prepare_in_game_slot_capture(
            source,
            character,
            manifest_version=manifest_version,
            character_id=character_id,
            slot_index=slot_index,
        )
        saved = self.database.save_captured_loadout(
            bungie_membership_id,
            name=name,
            description=description,
            tags=tags,
            capture=capture,
            cover_icon_hash=self.validate_cover_icon(
                cover_icon_hash
                if cover_icon_hash is not None
                else valid_hash(raw_slot.get("iconHash"))
            ),
        )
        return self._enrich_saved(saved)

    def prepare_character_loadout_set(
        self,
        bungie_membership_id: str,
        *,
        character_id: str,
    ) -> dict[str, Any]:
        """Prepare every populated slot for one atomic set import."""

        source, character, manifest_version = self._capture_source(
            bungie_membership_id,
            character_id,
        )
        component = (
            source["profile"]
            .get("characterLoadouts", {})
            .get("data", {})
            .get(character_id)
        )
        slots = component.get("loadouts") if isinstance(component, dict) else None
        if not isinstance(slots, list):
            raise LoadoutInspectionError(
                "The selected character has no loadout component."
            )
        if len(slots) > 20:
            raise LoadoutInspectionError(
                "The selected character returned more than 20 loadout slots."
            )
        inspection = self.inspect(bungie_membership_id)
        inspected_character = next(
            (
                value
                for value in (inspection or {}).get("characters", [])
                if value["character_id"] == character_id
            ),
            None,
        )
        inspected_slots = {
            int(value["slot_index"]): value
            for value in (
                inspected_character["slots"] if inspected_character else []
            )
        }
        official_icons = {row["hash"] for row in self.loadout_icons()}
        prepared = []
        for position, raw_slot in enumerate(slots):
            if raw_loadout_slot_empty(raw_slot):
                continue
            display_slot = inspected_slots.get(position, {})
            capture, captured_slot = self._prepare_in_game_slot_capture(
                source,
                character,
                manifest_version=manifest_version,
                character_id=character_id,
                slot_index=position,
            )
            icon_hash = valid_hash(captured_slot.get("iconHash"))
            prepared.append(
                {
                    "position": position,
                    "name": validate_metadata(
                        str(display_slot.get("name") or f"Slot {position + 1}"),
                        "",
                        [],
                    )[0],
                    "description": "",
                    "tags": [],
                    "cover_icon_hash": (
                        icon_hash if icon_hash in official_icons else None
                    ),
                    "capture": capture,
                }
            )
        if not prepared:
            raise LoadoutInspectionError(
                "The selected character has no populated in-game loadouts."
            )
        return {
            "character_class_type": int(character["class_type"]),
            "loadouts": prepared,
        }

    def _prepare_in_game_slot_capture(
        self,
        source: dict[str, Any],
        character: dict[str, Any],
        *,
        manifest_version: str,
        character_id: str,
        slot_index: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build immutable capture data without writing it to SQLite."""

        component = (
            source["profile"]
            .get("characterLoadouts", {})
            .get("data", {})
            .get(character_id)
        )
        if not isinstance(component, dict):
            raise LoadoutInspectionError(
                "The selected character has no loadout component."
            )
        slots = component.get("loadouts")
        if (
            not isinstance(slots, list)
            or slot_index < 0
            or slot_index >= len(slots)
        ):
            raise LoadoutInspectionError(
                "The selected in-game loadout slot is unavailable."
            )
        raw_slot = slots[slot_index]
        if not isinstance(raw_slot, dict):
            raise LoadoutInspectionError(
                "The selected in-game loadout slot is invalid."
            )
        raw_items = raw_slot.get("items")
        if raw_loadout_slot_empty(raw_slot):
            raise LoadoutInspectionError(
                "An empty in-game slot cannot be saved as a loadout."
            )
        item_index = inventory_item_index(source["items"])
        item_hashes: set[int] = set()
        plug_hashes: set[int] = set()
        captured = []
        unresolved_instance_ids: list[str] = []
        parsed_plugs: dict[str, list[int | None]] = {}
        for equipment_order, raw_item in enumerate(raw_items):
            if not isinstance(raw_item, dict):
                raise LoadoutInspectionError(
                    "The selected slot contains an invalid item entry."
                )
            instance_id = valid_instance_id(raw_item.get("itemInstanceId"))
            inventory_item = item_index.get(instance_id or "")
            if inventory_item is None:
                if instance_id is not None:
                    unresolved_instance_ids.append(instance_id)
                continue
            raw_plugs = ordered_hashes(raw_item.get("plugItemHashes"))
            parsed_plugs[instance_id] = raw_plugs
            plug_hashes.update(
                value for value in raw_plugs if value is not None
            )
            item_hashes.add(int(inventory_item["item_hash"]))
            captured.append(
                capture_item(equipment_order, inventory_item, plugs=[])
            )
        definitions = self._definitions(
            identifier_hashes={"name": set(), "icon": set(), "color": set()},
            item_hashes=item_hashes,
            plug_hashes=plug_hashes,
        )
        if definitions["error"]:
            raise LoadoutInspectionError(definitions["error"])
        filtered_types, filtered_categories = loadout_filters(
            definitions["constants"]
        )
        for item in captured:
            item_definition = definitions["items"].get(item["item_hash"])
            item["bucket_hash"] = intended_bucket_hash(item_definition)
            plug_rows = build_plugs(
                parsed_plugs[item["item_instance_id"]],
                item_definition,
                definitions["plugs"],
                definitions["socket_types"],
                definitions["socket_categories"],
                filtered_types,
                filtered_categories,
            )
            item["plugs"] = captured_plugs(plug_rows)
        for equipment_order, item in enumerate(captured):
            item["equipment_order"] = equipment_order
        ensure_complete_capture(captured, allow_partial=True)
        partial = (
            bool(unresolved_instance_ids)
            or {int(item["bucket_hash"]) for item in captured}
            != REQUIRED_GAMEPLAY_BUCKETS
        )

        capture = {
            "snapshot_id": source["snapshot"]["snapshot_id"],
            "manifest_version": manifest_version,
            "capture_source": "in_game_slot",
            "source_character_id": character_id,
            "source_slot_index": slot_index,
            "character_class_type": int(character["class_type"]),
            "items": captured,
            "partial": partial,
            "unresolved_item_instance_ids": unresolved_instance_ids,
            "source_payload": {
                **raw_slot,
                "_partialCapture": partial,
                "_unresolvedItemInstanceIds": unresolved_instance_ids,
            },
        }
        return capture, raw_slot

    def saved_loadouts(
        self,
        bungie_membership_id: str,
        *,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        source = self.database.load_active_loadout_source(
            bungie_membership_id
        )
        return [
            self._enrich_saved(loadout, source=source)
            for loadout in self.database.list_saved_loadouts(
                bungie_membership_id,
                include_archived=include_archived,
            )
        ]

    def saved_loadout(
        self,
        bungie_membership_id: str,
        loadout_id: str,
        *,
        revision_id: str | None = None,
        include_archived: bool = False,
    ) -> dict[str, Any] | None:
        loadout = self.database.load_saved_loadout(
            bungie_membership_id,
            loadout_id,
            revision_id=revision_id,
            include_archived=include_archived,
        )
        return self._enrich_saved(loadout) if loadout is not None else None

    def loadout_revisions(
        self,
        bungie_membership_id: str,
        loadout_id: str,
    ) -> list[dict[str, Any]]:
        source = self.database.load_active_loadout_source(
            bungie_membership_id
        )
        return [
            self._enrich_saved(revision, source=source)
            for revision in self.database.list_loadout_revisions(
                bungie_membership_id,
                loadout_id,
            )
        ]

    def update_metadata(
        self,
        bungie_membership_id: str,
        loadout_id: str,
        *,
        name: str,
        description: str,
        tags: list[str],
        cover_icon_hash: int | None = None,
    ) -> dict[str, Any]:
        name, description, tags = validate_metadata(name, description, tags)
        if cover_icon_hash is None:
            current = self.database.load_saved_loadout(
                bungie_membership_id, loadout_id, include_archived=True
            )
            if current is None:
                raise LoadoutInspectionError("The saved loadout is unavailable.")
            cover_icon_hash = valid_hash(current.get("cover_icon_hash"))
        self.database.update_loadout_metadata(
            bungie_membership_id,
            loadout_id,
            name=name,
            description=description,
            tags=tags,
            cover_icon_hash=self.validate_cover_icon(cover_icon_hash),
        )
        saved = self.saved_loadout(
            bungie_membership_id,
            loadout_id,
            include_archived=True,
        )
        if saved is None:
            raise LoadoutInspectionError("The saved loadout is unavailable.")
        return saved

    def revise_with_current_equipment(
        self,
        bungie_membership_id: str,
        loadout_id: str,
        *,
        character_id: str,
        revision_note: str,
    ) -> dict[str, Any]:
        current = self.saved_loadout(bungie_membership_id, loadout_id)
        if current is None:
            raise LoadoutInspectionError("The saved loadout is unavailable.")
        return self.capture_current_equipment(
            bungie_membership_id,
            character_id=character_id,
            name=current["name"],
            description=current["description"],
            tags=current["tags"],
            loadout_id=loadout_id,
            revision_note=revision_note,
        )

    def clone_loadout(
        self,
        bungie_membership_id: str,
        loadout_id: str,
        *,
        name: str,
    ) -> dict[str, Any]:
        name, _description, _tags = validate_metadata(name, "", [])
        return self._enrich_saved(
            self.database.clone_loadout(
                bungie_membership_id,
                loadout_id,
                name=name,
            )
        )

    def restore_revision(
        self,
        bungie_membership_id: str,
        loadout_id: str,
        revision_id: str,
        *,
        revision_note: str,
    ) -> dict[str, Any]:
        return self._enrich_saved(
            self.database.restore_loadout_revision(
                bungie_membership_id,
                loadout_id,
                revision_id,
                revision_note=revision_note,
            )
        )

    def set_archived(
        self,
        bungie_membership_id: str,
        loadout_id: str,
        *,
        archived: bool,
    ) -> None:
        self.database.set_loadout_archived(
            bungie_membership_id,
            loadout_id,
            archived=archived,
        )

    def set_favorite(
        self,
        bungie_membership_id: str,
        loadout_id: str,
        *,
        favorite: bool,
    ) -> None:
        self.database.set_loadout_favorite(
            bungie_membership_id, loadout_id, favorite=favorite
        )

    def delete_loadout(
        self,
        bungie_membership_id: str,
        loadout_id: str,
    ) -> None:
        self.database.delete_loadout(bungie_membership_id, loadout_id)

    def compare_revisions(
        self,
        bungie_membership_id: str,
        loadout_id: str,
        left_revision_id: str,
        right_revision_id: str,
    ) -> dict[str, Any]:
        left = self.saved_loadout(
            bungie_membership_id,
            loadout_id,
            revision_id=left_revision_id,
            include_archived=True,
        )
        right = self.saved_loadout(
            bungie_membership_id,
            loadout_id,
            revision_id=right_revision_id,
            include_archived=True,
        )
        if left is None or right is None:
            raise LoadoutInspectionError(
                "Both selected revisions must belong to this loadout."
            )
        left_by_bucket = {item["bucket_hash"]: item for item in left["items"]}
        right_by_bucket = {item["bucket_hash"]: item for item in right["items"]}
        rows = []
        for bucket_hash in sorted(set(left_by_bucket) | set(right_by_bucket)):
            left_item = left_by_bucket.get(bucket_hash)
            right_item = right_by_bucket.get(bucket_hash)
            if left_item is None or right_item is None:
                status = "item changed"
            elif left_item["item_instance_id"] != right_item["item_instance_id"]:
                status = "item changed"
            elif [plug["plug_hash"] for plug in left_item["plugs"]] != [
                plug["plug_hash"] for plug in right_item["plugs"]
            ]:
                status = "sockets changed"
            else:
                status = "unchanged"
            rows.append(
                {
                    "bucket_hash": bucket_hash,
                    "bucket_name": (
                        left_item or right_item or {}
                    ).get("bucket_name", str(bucket_hash)),
                    "left": left_item,
                    "right": right_item,
                    "status": status,
                }
            )
        return {"left": left, "right": right, "rows": rows}

    def loadout_icons(self) -> list[dict[str, Any]]:
        """Return Bungie's current official loadout icon choices."""

        if not self.manifest.status().get("available"):
            raise LoadoutInspectionError(
                "Prepare the current Destiny definitions before choosing an icon."
            )
        constants = self.manifest.resolve_all(
            "DestinyLoadoutConstantsDefinition"
        )
        hashes = {
            int(value)
            for definition in constants.values()
            for value in definition.get("loadoutIconHashes", [])
            if isinstance(value, int)
        }
        definitions = self.manifest.resolve_many(
            "DestinyLoadoutIconDefinition", hashes
        )
        return [
            {
                "hash": icon_hash,
                "icon_path": (
                    definition_path(definition, "iconImagePath")
                    or display_icon(definition)
                ),
                "name": display_name(definition) or f"Icon {icon_hash}",
            }
            for icon_hash, definition in sorted(definitions.items())
            if icon_hash in hashes
        ]

    def validate_cover_icon(self, value: int | None) -> int | None:
        if value is None:
            return None
        icon_hash = valid_hash(value)
        if icon_hash is None or icon_hash not in {
            row["hash"] for row in self.loadout_icons()
        }:
            raise LoadoutInspectionError(
                "Choose an icon from the current Destiny loadout icon catalog."
            )
        return icon_hash

    def _capture_source(
        self,
        bungie_membership_id: str,
        character_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        source = self.database.load_active_loadout_source(
            bungie_membership_id
        )
        if source is None:
            raise LoadoutInspectionError(
                "Synchronize inventory before saving a loadout."
            )
        if not snapshot_is_fresh(source["snapshot"]):
            raise LoadoutInspectionError(
                "Refresh inventory before saving a loadout."
            )
        character = next(
            (
                value
                for value in source["characters"]
                if value["character_id"] == character_id
            ),
            None,
        )
        if character is None or not isinstance(character.get("class_type"), int):
            raise LoadoutInspectionError(
                "The selected character is unavailable."
            )
        manifest_status = self.manifest.status()
        manifest_version = manifest_status.get("version")
        if not manifest_status.get("available") or not isinstance(
            manifest_version, str
        ):
            raise LoadoutInspectionError(
                "Prepare the current Destiny definitions before saving."
            )
        return source, character, manifest_version

    def _enrich_saved(
        self,
        loadout: dict[str, Any],
        *,
        source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if source is None:
            source = self.database.load_active_loadout_source(
                str(loadout["bungie_membership_id"])
            )
        live_items = {
            str(item["item_instance_id"]): item
            for item in (source["items"] if source is not None else [])
            if item.get("item_instance_id")
        }
        validation_issues: list[str] = []
        item_hashes = {int(item["item_hash"]) for item in loadout["items"]}
        plug_hashes = {
            int(plug["plug_hash"])
            for item in loadout["items"]
            for plug in item["plugs"]
            if plug.get("plug_hash") is not None
        }
        item_definitions: dict[int, dict[str, Any]] = {}
        plug_definitions: dict[int, dict[str, Any]] = {}
        bucket_definitions: dict[int, dict[str, Any]] = {}
        if self.manifest.status().get("available"):
            item_definitions = self.manifest.resolve_many(
                "DestinyInventoryItemDefinition", item_hashes
            )
            plug_definitions = self.manifest.resolve_many(
                "DestinyInventoryItemDefinition", plug_hashes
            )
            bucket_definitions = self.manifest.resolve_many(
                "DestinyInventoryBucketDefinition",
                (int(item["bucket_hash"]) for item in loadout["items"]),
            )
        for item in loadout["items"]:
            definition = item_definitions.get(int(item["item_hash"]))
            item["name"] = display_name(definition) or "Unknown item"
            item["icon_path"] = display_icon(definition)
            item["type"] = (
                definition.get("itemTypeAndTierDisplayName")
                if definition
                else "Unknown item"
            ) or "Unknown item"
            item["bucket_name"] = (
                display_name(
                    bucket_definitions.get(int(item["bucket_hash"]))
                )
                or f"Bucket {item['bucket_hash']}"
            )
            live = live_items.get(str(item["item_instance_id"]))
            item_issues: list[str] = []
            if source is None:
                item_issues.append("Live inventory is unavailable.")
            elif live is None:
                item_issues.append(
                    f"Instance {item['item_instance_id']} is no longer owned."
                )
            else:
                if int(live["item_hash"]) != int(item["item_hash"]):
                    item_issues.append(
                        f"Instance {item['item_instance_id']} now has a different item hash."
                    )
                if (
                    live.get("source_kind") != item["captured_source_kind"]
                    or live.get("character_id")
                    != item.get("captured_character_id")
                ):
                    item_issues.append(
                        f"Instance {item['item_instance_id']} moved from its captured location."
                    )
            item["validation_issues"] = item_issues
            item["live_status"] = "invalid" if item_issues else "valid"
            validation_issues.extend(item_issues)
            for plug in item["plugs"]:
                plug_definition = (
                    plug_definitions.get(int(plug["plug_hash"]))
                    if plug.get("plug_hash") is not None
                    else None
                )
                plug["name"] = (
                    display_name(plug_definition)
                    or (
                        "Unresolved plug"
                        if plug.get("plug_hash") is not None
                        else "Missing plug hash"
                    )
                )
                plug["icon_path"] = display_icon(plug_definition)
        loadout["class_name"] = CLASS_NAMES.get(
            int(loadout["character_class_type"]), "Guardian"
        )
        loadout["item_count"] = len(loadout["items"])
        loadout["partial"] = bool(
            loadout["canonical_payload"].get("partial")
        )
        loadout["unresolved_item_count"] = len(
            loadout["canonical_payload"].get(
                "unresolved_item_instance_ids", []
            )
        )
        loadout["plug_count"] = sum(
            len(item["plugs"]) for item in loadout["items"]
        )
        loadout["reference_item_count"] = len(
            loadout["canonical_payload"].get("reference_items", [])
        )
        loadout["validation_issues"] = validation_issues
        loadout["live_status"] = (
            "invalid" if validation_issues else "valid"
        )
        icon_hash = valid_hash(loadout.get("cover_icon_hash"))
        icon_definition = (
            self.manifest.resolve_many(
                "DestinyLoadoutIconDefinition", (icon_hash,)
            ).get(icon_hash)
            if icon_hash is not None and self.manifest.status().get("available")
            else None
        )
        loadout["cover_icon_path"] = (
            definition_path(icon_definition, "iconImagePath")
            or display_icon(icon_definition)
        )
        return loadout

    def _definitions(
        self,
        *,
        identifier_hashes: dict[str, set[int]],
        item_hashes: set[int],
        plug_hashes: set[int],
    ) -> dict[str, Any]:
        empty = {
            "names": {},
            "icons": {},
            "colors": {},
            "items": {},
            "plugs": {},
            "socket_types": {},
            "socket_categories": {},
            "constants": {},
            "error": "",
        }
        if not self.manifest.status().get("available"):
            empty["error"] = (
                "The local manifest is unavailable; hashes are shown without "
                "names or artwork."
            )
            return empty
        try:
            names = self.manifest.resolve_many(
                "DestinyLoadoutNameDefinition", identifier_hashes["name"]
            )
            icons = self.manifest.resolve_many(
                "DestinyLoadoutIconDefinition", identifier_hashes["icon"]
            )
            colors = self.manifest.resolve_many(
                "DestinyLoadoutColorDefinition", identifier_hashes["color"]
            )
            items = self.manifest.resolve_many(
                "DestinyInventoryItemDefinition", item_hashes
            )
            plugs = self.manifest.resolve_many(
                "DestinyInventoryItemDefinition", plug_hashes
            )
            socket_type_hashes = hashes_from_socket_entries(items.values())
            socket_types = self.manifest.resolve_many(
                "DestinySocketTypeDefinition", socket_type_hashes
            )
            socket_category_hashes = {
                value
                for definition in socket_types.values()
                if (value := valid_hash(definition.get("socketCategoryHash")))
                is not None
            }
            socket_categories = self.manifest.resolve_many(
                "DestinySocketCategoryDefinition", socket_category_hashes
            )
            constants = self.manifest.resolve_all(
                "DestinyLoadoutConstantsDefinition"
            )
        except ManifestError as error:
            empty["error"] = str(error)
            return empty
        return {
            "names": names,
            "icons": icons,
            "colors": colors,
            "items": items,
            "plugs": plugs,
            "socket_types": socket_types,
            "socket_categories": socket_categories,
            "constants": constants,
            "error": "",
        }


def build_item(
    instance_id: str | None,
    inventory_item: dict[str, Any] | None,
    definition: dict[str, Any] | None,
    plugs: list[dict[str, Any]],
) -> dict[str, Any]:
    display = definition.get("displayProperties", {}) if definition else {}
    name = display.get("name") if isinstance(display, dict) else None
    icon = display.get("icon") if isinstance(display, dict) else None
    return {
        "instance_id": instance_id,
        "resolved": inventory_item is not None,
        "item_hash": (
            int(inventory_item["item_hash"])
            if inventory_item is not None
            else None
        ),
        "name": name or "Unresolved item instance",
        "type": (
            definition.get("itemTypeAndTierDisplayName")
            if definition
            else "Unavailable"
        )
        or "Unknown item",
        "icon_path": icon if isinstance(icon, str) else "",
        "location": (
            LOCATION_NAMES.get(
                str(inventory_item.get("source_kind")), "Unknown location"
            )
            if inventory_item is not None
            else "Missing from active inventory"
        ),
        "owner_character_id": (
            inventory_item.get("character_id")
            if inventory_item is not None
            else None
        ),
        "plugs": plugs,
        "plug_count": len(plugs),
        "resolved_plug_count": sum(
            1 for plug in plugs if plug["resolved"]
        ),
    }


def validate_metadata(
    name: str,
    description: str,
    tags: list[str],
) -> tuple[str, str, list[str]]:
    """Normalize user-owned metadata before it reaches persistence."""
    normalized_name = " ".join(str(name).split())
    normalized_description = str(description).strip()
    if not normalized_name:
        raise LoadoutInspectionError("Give the saved loadout a name.")
    if len(normalized_name) > 80:
        raise LoadoutInspectionError(
            "Loadout names must be 80 characters or fewer."
        )
    if len(normalized_description) > 2000:
        raise LoadoutInspectionError(
            "Loadout descriptions must be 2,000 characters or fewer."
        )
    normalized_tags = []
    seen = set()
    for raw_tag in tags:
        tag = " ".join(str(raw_tag).split()).lower()
        if not tag or tag in seen:
            continue
        if len(tag) > 40:
            raise LoadoutInspectionError(
                "Each loadout tag must be 40 characters or fewer."
            )
        seen.add(tag)
        normalized_tags.append(tag)
    if len(normalized_tags) > 20:
        raise LoadoutInspectionError(
            "A loadout can have at most 20 tags."
        )
    return normalized_name, normalized_description, normalized_tags


def component_items(value: Any) -> list[dict[str, Any]]:
    """Return the ordered item array from a Destiny component wrapper."""
    if not isinstance(value, dict):
        return []
    candidate = value.get("items")
    if not isinstance(candidate, list):
        return []
    return [item for item in candidate if isinstance(item, dict)]


def inventory_item_index(
    items: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    indexed = {}
    for item in items:
        instance_id = valid_instance_id(item.get("item_instance_id"))
        if instance_id is not None:
            indexed[instance_id] = item
    return indexed


def component_plug_hashes(components: Any) -> list[int | None]:
    """Read equipped plugs while retaining their socket positions."""
    if not isinstance(components, dict):
        return []
    sockets = components.get("sockets")
    if not isinstance(sockets, dict):
        return []
    entries = sockets.get("sockets")
    if not isinstance(entries, list):
        return []
    return [
        valid_hash(entry.get("plugHash"))
        if isinstance(entry, dict)
        else None
        for entry in entries
    ]


def capture_item(
    equipment_order: int,
    inventory_item: dict[str, Any],
    *,
    plugs: list[dict[str, Any]],
) -> dict[str, Any]:
    instance_id = valid_instance_id(inventory_item.get("item_instance_id"))
    item_hash = valid_hash(inventory_item.get("item_hash"))
    bucket_hash = valid_hash(inventory_item.get("bucket_hash"))
    source_kind = inventory_item.get("source_kind")
    if instance_id is None or item_hash is None or bucket_hash is None:
        raise LoadoutInspectionError(
            "A captured equipment item has invalid identity data."
        )
    if source_kind not in LOCATION_NAMES:
        raise LoadoutInspectionError(
            "A captured equipment item has an unsupported location."
        )
    return {
        "equipment_order": int(equipment_order),
        "item_instance_id": instance_id,
        "item_hash": item_hash,
        "bucket_hash": bucket_hash,
        "captured_source_kind": source_kind,
        "captured_character_id": inventory_item.get("character_id"),
        "plugs": plugs,
    }


def intended_bucket_hash(definition: dict[str, Any] | None) -> int:
    """Resolve the equip slot, not the item's current storage container."""
    inventory = definition.get("inventory") if definition else None
    bucket_hash = (
        valid_hash(inventory.get("bucketTypeHash"))
        if isinstance(inventory, dict)
        else None
    )
    if bucket_hash is None:
        raise LoadoutInspectionError(
            "A captured item has no resolvable equipment bucket."
        )
    return bucket_hash


def reference_item(
    item: dict[str, Any],
    definition: dict[str, Any] | None,
) -> dict[str, Any]:
    """Preserve non-gameplay equipment explicitly without making it actionable."""
    return {
        "equipment_order": int(item["equipment_order"]),
        "item_instance_id": item["item_instance_id"],
        "item_hash": int(item["item_hash"]),
        "bucket_hash": int(item["bucket_hash"]),
        "name": display_name(definition) or "Unknown reference item",
        "sync_capability": "reference_only",
        "reason": (
            "Equipped cosmetic or account utility state is outside the "
            "gameplay loadout contract."
        ),
    }


def ensure_complete_capture(
    items: list[dict[str, Any]],
    *,
    allow_partial: bool = False,
) -> None:
    """Reject structurally ambiguous captures and optional partial state."""
    if not items and not allow_partial:
        raise LoadoutInspectionError("An empty loadout cannot be saved.")
    orders = [int(item["equipment_order"]) for item in items]
    if orders != list(range(len(items))):
        raise LoadoutInspectionError(
            "The captured equipment order is incomplete."
        )
    instance_ids = [item["item_instance_id"] for item in items]
    if len(instance_ids) != len(set(instance_ids)):
        raise LoadoutInspectionError(
            "The captured equipment contains a duplicate exact instance."
        )
    bucket_hashes = [int(item["bucket_hash"]) for item in items]
    if len(bucket_hashes) != len(set(bucket_hashes)):
        raise LoadoutInspectionError(
            "The captured equipment contains multiple items for one slot."
        )
    missing_buckets = REQUIRED_GAMEPLAY_BUCKETS - set(bucket_hashes)
    if missing_buckets and not allow_partial:
        raise LoadoutInspectionError(
            "The capture is incomplete: one or more required gameplay slots "
            "are missing."
        )


def captured_plugs(
    plugs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reduce enriched socket rows to the immutable synchronization record."""
    return [
        {
            "socket_index": int(plug["socket_index"]),
            "plug_hash": plug.get("plug_hash"),
            "sync_capability": (
                "unsupported"
                if plug.get("plug_hash") is None
                else "experimental"
            ),
            "filtered_from_preview": bool(
                plug.get("filtered_from_preview")
            ),
        }
        for plug in plugs
    ]


def build_plugs(
    plug_hashes: list[int | None],
    item_definition: dict[str, Any] | None,
    plug_definitions: dict[int, dict[str, Any]],
    socket_types: dict[int, dict[str, Any]],
    socket_categories: dict[int, dict[str, Any]],
    filtered_types: set[int],
    filtered_categories: set[int],
) -> list[dict[str, Any]]:
    entries: list[Any] = []
    if item_definition:
        sockets = item_definition.get("sockets")
        if isinstance(sockets, dict):
            candidate = sockets.get("socketEntries")
            if isinstance(candidate, list):
                entries = candidate

    plugs = []
    for socket_index, plug_hash in enumerate(plug_hashes):
        entry = (
            entries[socket_index]
            if socket_index < len(entries)
            and isinstance(entries[socket_index], dict)
            else {}
        )
        socket_type_hash = valid_hash(entry.get("socketTypeHash"))
        socket_type = socket_types.get(socket_type_hash, {})
        category_hash = valid_hash(socket_type.get("socketCategoryHash"))
        category = socket_categories.get(category_hash)
        category_name = display_name(category) or "Unmapped socket"
        definition = (
            plug_definitions.get(plug_hash)
            if plug_hash is not None
            else None
        )
        plugs.append(
            {
                "socket_index": socket_index,
                "plug_hash": plug_hash,
                "name": (
                    display_name(definition)
                    or (
                        "Unresolved plug"
                        if plug_hash is not None
                        else "Missing or invalid plug hash"
                    )
                ),
                "description": display_description(definition),
                "icon_path": display_icon(definition),
                "category": category_name,
                "socket_type_hash": socket_type_hash,
                "category_hash": category_hash,
                "filtered_from_preview": (
                    socket_type_hash in filtered_types
                    or category_hash in filtered_categories
                ),
                "resolved": definition is not None,
            }
        )
    return plugs


def character_label(character: dict[str, Any]) -> str:
    return CLASS_NAMES.get(character.get("class_type"), "Guardian")


def loadout_name(
    definition: dict[str, Any] | None,
    display_index: int,
) -> str:
    if definition:
        value = definition.get("name")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"Loadout {display_index}"


def loadout_capacity(definitions: dict[int, dict[str, Any]]) -> int | None:
    values = [
        value
        for definition in definitions.values()
        if isinstance(
            value := definition.get("loadoutCountPerCharacter"), int
        )
        and not isinstance(value, bool)
        and value >= 0
    ]
    return max(values) if values else None


def loadout_filters(
    definitions: dict[int, dict[str, Any]],
) -> tuple[set[int], set[int]]:
    socket_types: set[int] = set()
    socket_categories: set[int] = set()
    for definition in definitions.values():
        socket_types.update(
            valid_hashes(
                definition.get("loadoutPreviewFilterOutSocketTypeHashes")
            )
        )
        socket_categories.update(
            valid_hashes(
                definition.get("loadoutPreviewFilterOutSocketCategoryHashes")
            )
        )
    return socket_types, socket_categories


def hashes_from_socket_entries(
    definitions: Iterable[dict[str, Any]],
) -> set[int]:
    hashes: set[int] = set()
    for definition in definitions:
        sockets = definition.get("sockets")
        entries = sockets.get("socketEntries") if isinstance(sockets, dict) else []
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                value = valid_hash(entry.get("socketTypeHash"))
                if value is not None:
                    hashes.add(value)
    return hashes


def valid_hash(value: Any) -> int | None:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 < value < 2**32
        and value != INVALID_HASH_SENTINEL
    ):
        return value
    return None


def valid_hashes(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    return [item for item in value if valid_hash(item) is not None]


def ordered_hashes(value: Any) -> list[int | None]:
    """Preserve plug-array position even when a hash is absent or invalid."""
    if not isinstance(value, list):
        return []
    return [valid_hash(item) for item in value]


def valid_instance_id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value)
    return text if text.isdecimal() and int(text) > 0 else None


def raw_loadout_slot_empty(slot: Any) -> bool:
    """Recognize both empty arrays and Bungie's sentinel-filled clear slots."""

    if not isinstance(slot, dict):
        return True
    items = slot.get("items")
    if not isinstance(items, list):
        return True
    return not any(
        isinstance(item, dict)
        and valid_instance_id(item.get("itemInstanceId")) is not None
        for item in items
    )


def definition_path(
    definition: dict[str, Any] | None,
    field: str,
) -> str:
    if not definition:
        return ""
    value = definition.get(field)
    return value if isinstance(value, str) else ""


def display_name(definition: dict[str, Any] | None) -> str:
    if not definition:
        return ""
    display = definition.get("displayProperties")
    if not isinstance(display, dict):
        return ""
    value = display.get("name")
    return value.strip() if isinstance(value, str) else ""


def display_description(definition: dict[str, Any] | None) -> str:
    if not definition:
        return ""
    display = definition.get("displayProperties")
    if not isinstance(display, dict):
        return ""
    value = display.get("description")
    return value.strip() if isinstance(value, str) else ""


def display_icon(definition: dict[str, Any] | None) -> str:
    if not definition:
        return ""
    display = definition.get("displayProperties")
    if not isinstance(display, dict):
        return ""
    value = display.get("icon")
    return value if isinstance(value, str) else ""


def snapshot_is_fresh(snapshot: dict[str, Any]) -> bool:
    value = snapshot.get("stale_at")
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed > datetime.now(UTC) and snapshot.get("sync_status") == "fresh"
