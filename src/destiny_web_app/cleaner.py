"""Conservative, snapshot-bound vault-cleaner analysis."""

from __future__ import annotations

import itertools
from collections import defaultdict
from typing import Any, Iterable

from destiny_web_app.database import Database
from destiny_web_app.manifest import ManifestError, ManifestService


WEAPON_RULESET_VERSION = "weapon-gameplay-coverage-v2"
GAMEPLAY_CATEGORIES = {
    "arrows",
    "barrels",
    "batteries",
    "blades",
    "bolts",
    "bowstrings",
    "frames",
    "grips",
    "guards",
    "hafts",
    "intrinsics",
    "magazines",
    "magazines_gl",
    "origins",
    "rails",
    "stocks",
    "tubes",
    "v300.weapon.damage_type.energy",
}
CATEGORY_LABELS = {
    "arrows": "Arrow",
    "barrels": "Barrel",
    "batteries": "Battery",
    "blades": "Blade",
    "bolts": "Bolt",
    "bowstrings": "Bowstring",
    "grips": "Grip",
    "guards": "Guard",
    "hafts": "Haft",
    "intrinsics": "Intrinsic",
    "magazines": "Magazine",
    "magazines_gl": "Magazine",
    "origins": "Origin trait",
    "rails": "Rail",
    "stocks": "Stock",
    "tubes": "Launcher barrel",
    "v300.weapon.damage_type.energy": "Damage type",
}
SOURCE_LABELS = {
    "vault": "Vault",
    "profile_inventory": "Shared inventory",
    "character_inventory": "Carried",
    "equipped": "Equipped",
    "postmaster": "Postmaster",
}


class WeaponCleanerError(RuntimeError):
    """A weapon-cleaner analysis could not be produced safely."""


class WeaponCleanerService:
    def __init__(
        self,
        database: Database,
        manifest: ManifestService,
    ) -> None:
        self.database = database
        self.manifest = manifest

    def analyze(self, bungie_membership_id: str) -> dict[str, Any]:
        source = self.database.load_active_inventory_for_analysis(
            bungie_membership_id
        )
        if source is None:
            raise WeaponCleanerError(
                "Synchronize an inventory before analyzing weapons."
            )
        manifest_status = self.manifest.status()
        if not manifest_status["available"] or not manifest_status.get("version"):
            raise WeaponCleanerError(
                "Prepare the current Destiny item definitions first."
            )

        item_definitions = self.manifest.resolve_many(
            "DestinyInventoryItemDefinition",
            (item["item_hash"] for item in source["items"]),
        )
        weapon_items = [
            item
            for item in source["items"]
            if item_definitions.get(item["item_hash"], {}).get("itemType") == 3
        ]
        plug_hashes = collect_plug_hashes(weapon_items)
        plug_definitions = self.manifest.resolve_many(
            "DestinyInventoryItemDefinition",
            plug_hashes,
        )
        loadout_instance_ids = collect_loadout_instance_ids(
            source["character_loadouts"]
        )
        result = analyze_weapons(
            weapon_items,
            item_definitions,
            plug_definitions,
            snapshot=source["snapshot"],
            loadout_instance_ids=loadout_instance_ids,
        )
        return self.database.save_cleaner_analysis(
            bungie_membership_id,
            snapshot_id=source["snapshot"]["snapshot_id"],
            analysis_kind="weapon",
            ruleset_version=WEAPON_RULESET_VERSION,
            manifest_version=manifest_status["version"],
            result=result,
        )

    def latest(self, bungie_membership_id: str) -> dict[str, Any] | None:
        return self.database.latest_cleaner_analysis(
            bungie_membership_id,
            analysis_kind="weapon",
        )


def analyze_weapons(
    items: list[dict[str, Any]],
    item_definitions: dict[int, dict[str, Any]],
    plug_definitions: dict[int, dict[str, Any]],
    *,
    snapshot: dict[str, Any],
    loadout_instance_ids: set[str],
) -> dict[str, Any]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        grouped[item["item_hash"]].append(item)

    output_groups = []
    keep_count = 0
    candidate_count = 0
    incomplete_group_count = 0
    for item_hash, copies in grouped.items():
        if len(copies) < 2:
            continue
        definition = item_definitions.get(item_hash)
        if definition is None:
            incomplete_group_count += 1
            continue
        group = analyze_weapon_group(
            copies,
            definition,
            plug_definitions,
            loadout_instance_ids=loadout_instance_ids,
        )
        if group is None:
            continue
        output_groups.append(group)
        keep_count += group["keep_count"]
        candidate_count += group["candidate_count"]
        if not group["complete"]:
            incomplete_group_count += 1

    output_groups.sort(
        key=lambda group: (
            -group["candidate_count"],
            group["name"].lower(),
            group["item_hash"],
        )
    )
    stored_instance_ids = {
        item["item_instance_id"]
        for item in items
        if item.get("item_instance_id")
    }
    unresolved_loadout_ids = sorted(
        loadout_instance_ids - stored_instance_ids
    )
    used_plug_hashes = {
        plug_hash
        for group in output_groups
        for item in group["items"]
        for column in item["columns"]
        for plug_hash in column["plugs"]
    }
    return {
        "summary": {
            "item_count": len(items),
            "weapon_count": len(items),
            "group_count": len(output_groups),
            "keep_count": keep_count,
            "candidate_count": candidate_count,
            "candidate_group_count": sum(
                group["candidate_count"] > 0 for group in output_groups
            ),
            "incomplete_group_count": incomplete_group_count,
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_fetched_at": snapshot["fetched_at"],
            "snapshot_stale_at": snapshot["stale_at"],
            "snapshot_status": snapshot["sync_status"],
            "loadout_reference_count": len(loadout_instance_ids),
            "unresolved_loadout_reference_count": len(
                unresolved_loadout_ids
            ),
        },
        "rules": {
            "coverage": (
                "Every unique selectable option in each gameplay socket column"
            ),
            "protected": [
                "Items outside the vault",
                "Locked items",
                "Crafted items",
                "Exotic items",
                "Items referenced by an in-game loadout",
                "Items with incomplete roll data",
            ],
            "excluded_columns": [
                "Shaders and ornaments",
                "Weapon mods",
                "Trackers",
                "Mementos and crafting controls",
                "Masterwork upgrade tiers and kill effects",
            ],
            "perk_display_source": "Bungie Destiny manifest",
        },
        "perk_definitions": {
            str(plug_hash): plug_display(plug_definitions, plug_hash)
            for plug_hash in sorted(used_plug_hashes)
        },
        "unresolved_loadout_instance_ids": unresolved_loadout_ids,
        "groups": output_groups,
    }


def analyze_weapon_group(
    items: list[dict[str, Any]],
    definition: dict[str, Any],
    plug_definitions: dict[int, dict[str, Any]],
    *,
    loadout_instance_ids: set[str],
) -> dict[str, Any] | None:
    item_columns = [
        extract_gameplay_columns(item, definition, plug_definitions)
        for item in items
    ]
    column_indexes = sorted(
        {index for columns in item_columns for index in columns}
    )
    if not column_indexes:
        return None

    frame_indexes = [
        index
        for index in column_indexes
        if any(
            columns.get(index, {}).get("category") == "frames"
            for columns in item_columns
        )
    ]
    column_labels = {
        index: column_label(
            index,
            item_columns,
            frame_indexes,
        )
        for index in column_indexes
    }

    analyzed_items = []
    complete = True
    for item, columns in zip(items, item_columns, strict=True):
        missing = [index for index in column_indexes if not columns.get(index)]
        if missing:
            complete = False
            coverage: frozenset[tuple[int, int]] = frozenset()
        else:
            coverage = frozenset(
                (index, plug_hash)
                for index in column_indexes
                for plug_hash in columns[index]["hashes"]
            )
        analyzed_items.append(
            {
                "source": item,
                "columns": columns,
                "coverage": coverage,
                "reasons": protection_reasons(
                    item,
                    definition,
                    loadout_instance_ids,
                ),
            }
        )

    if not complete:
        for analyzed in analyzed_items:
            if "Incomplete roll data" not in analyzed["reasons"]:
                analyzed["reasons"].append("Incomplete roll data")
        selected_ids: set[int] = set()
        candidate_ids: set[int] = set()
        universe: frozenset[tuple[int, int]] = frozenset()
    else:
        universe = frozenset().union(
            *(analyzed["coverage"] for analyzed in analyzed_items)
        )
        protected = [
            analyzed for analyzed in analyzed_items if analyzed["reasons"]
        ]
        eligible = [
            analyzed for analyzed in analyzed_items if not analyzed["reasons"]
        ]
        protected_coverage = frozenset().union(
            *(analyzed["coverage"] for analyzed in protected)
        )
        selected = choose_minimum_cover(
            eligible,
            universe - protected_coverage,
            protected_coverage,
        )
        selected_ids = {
            analyzed["source"]["id"] for analyzed in selected
        }
        candidate_ids = {
            analyzed["source"]["id"]
            for analyzed in eligible
            if analyzed["source"]["id"] not in selected_ids
        }
        retained_coverage = protected_coverage | frozenset().union(
            *(analyzed["coverage"] for analyzed in selected)
        )
        if retained_coverage != universe:
            raise WeaponCleanerError(
                "The minimum weapon coverage calculation was incomplete."
            )

    display = definition.get("displayProperties", {})
    inventory = definition.get("inventory", {})
    rendered_items = []
    for analyzed in analyzed_items:
        item = analyzed["source"]
        if item["id"] in candidate_ids:
            decision = "candidate"
        elif analyzed["reasons"]:
            decision = "protected"
        else:
            decision = "keep"
        rendered_items.append(
            {
                "item_row_id": item["id"],
                "instance_suffix": (
                    f"…{item['item_instance_id'][-6:]}"
                    if item.get("item_instance_id")
                    else "Not instanced"
                ),
                "source": SOURCE_LABELS.get(
                    item["source_kind"],
                    item["source_kind"],
                ),
                "decision": decision,
                "reasons": analyzed["reasons"],
                "power": primary_power(item),
                "locked": bool(item["state"] & 1),
                "masterworked": bool(item["state"] & 4),
                "crafted": bool(item["state"] & 8),
                "coverage_count": len(analyzed["coverage"]),
                "columns": [
                    {
                        "index": index,
                        "label": column_labels[index],
                        "plugs": sorted(
                            analyzed["columns"].get(index, {}).get(
                                "hashes",
                                set(),
                            )
                        ),
                    }
                    for index in column_indexes
                ],
            }
        )
    rendered_items.sort(
        key=lambda item: (
            {"candidate": 0, "keep": 1, "protected": 2}[item["decision"]],
            -int(item["masterworked"]),
            -(item["power"] or 0),
            item["item_row_id"],
        )
    )
    protected_count = sum(
        item["decision"] == "protected" for item in rendered_items
    )
    selected_count = sum(item["decision"] == "keep" for item in rendered_items)
    return {
        "item_hash": int(definition.get("hash") or items[0]["item_hash"]),
        "name": display.get("name") or f"Weapon {items[0]['item_hash']}",
        "type": definition.get("itemTypeDisplayName") or "Weapon",
        "tier": inventory.get("tierTypeName") or "Unknown",
        "icon": display.get("icon"),
        "owned_count": len(items),
        "keep_count": protected_count + selected_count,
        "selected_count": selected_count,
        "protected_count": protected_count,
        "candidate_count": len(candidate_ids),
        "option_count": len(universe),
        "complete": complete,
        "column_labels": [
            {"index": index, "label": column_labels[index]}
            for index in column_indexes
        ],
        "items": rendered_items,
    }


def extract_gameplay_columns(
    item: dict[str, Any],
    definition: dict[str, Any],
    plug_definitions: dict[int, dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    components = item["components"]
    hashes_by_index: dict[int, set[int]] = defaultdict(set)
    reusable = components.get("reusablePlugs", {}).get("plugs", {})
    if isinstance(reusable, dict):
        for raw_index, rows in reusable.items():
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                continue
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict):
                    plug_hash = positive_hash(row.get("plugItemHash"))
                    if plug_hash:
                        hashes_by_index[index].add(plug_hash)

    sockets = components.get("sockets", {}).get("sockets", [])
    if isinstance(sockets, list):
        for index, row in enumerate(sockets):
            if isinstance(row, dict):
                plug_hash = positive_hash(row.get("plugHash"))
                if plug_hash:
                    hashes_by_index[index].add(plug_hash)

    entries = definition.get("sockets", {}).get("socketEntries", [])
    output: dict[int, dict[str, Any]] = {}
    for index, hashes in hashes_by_index.items():
        entry = entries[index] if isinstance(entries, list) and index < len(entries) else {}
        randomized = (
            isinstance(entry, dict)
            and positive_hash(entry.get("randomizedPlugSetHash")) is not None
        )
        categories = {
            plug_category(plug_definitions.get(plug_hash))
            for plug_hash in hashes
        }
        categories.discard(None)
        include = randomized or bool(categories & GAMEPLAY_CATEGORIES)
        if not include:
            continue
        filtered = {
            plug_hash
            for plug_hash in hashes
            if not excluded_plug(plug_definitions.get(plug_hash))
        }
        if not filtered:
            continue
        gameplay_categories = sorted(categories & GAMEPLAY_CATEGORIES)
        output[index] = {
            "hashes": filtered,
            "category": (
                gameplay_categories[0]
                if gameplay_categories
                else (sorted(categories)[0] if categories else "gameplay")
            ),
        }
    return output


def choose_minimum_cover(
    candidates: list[dict[str, Any]],
    required: frozenset[tuple[int, int]],
    base_coverage: frozenset[tuple[int, int]],
) -> list[dict[str, Any]]:
    if not required:
        return []
    ordered = sorted(
        candidates,
        key=lambda candidate: keep_priority(candidate["source"]),
        reverse=True,
    )
    for size in range(1, len(ordered) + 1):
        best: tuple[tuple[int, int, int], tuple[dict[str, Any], ...]] | None = None
        for choice in itertools.combinations(ordered, size):
            coverage = base_coverage | frozenset().union(
                *(candidate["coverage"] for candidate in choice)
            )
            if not required <= coverage:
                continue
            score = (
                sum(bool(candidate["source"]["state"] & 4) for candidate in choice),
                sum(primary_power(candidate["source"]) or 0 for candidate in choice),
                -sum(candidate["source"]["id"] for candidate in choice),
            )
            if best is None or score > best[0]:
                best = (score, choice)
        if best is not None:
            return list(best[1])
    raise WeaponCleanerError("Owned weapons could not cover their own perk set.")


def protection_reasons(
    item: dict[str, Any],
    definition: dict[str, Any],
    loadout_instance_ids: set[str],
) -> list[str]:
    reasons = []
    if item["source_kind"] != "vault":
        reasons.append(SOURCE_LABELS.get(item["source_kind"], "Outside vault"))
    if item["state"] & 1:
        reasons.append("Locked")
    if item["state"] & 8:
        reasons.append("Crafted")
    if definition.get("inventory", {}).get("tierTypeName") == "Exotic":
        reasons.append("Exotic")
    if item.get("item_instance_id") in loadout_instance_ids:
        reasons.append("In-game loadout")
    if not item.get("item_instance_id"):
        reasons.append("Not instanced")
    return reasons


def collect_loadout_instance_ids(value: Any) -> set[str]:
    if not isinstance(value, dict):
        return set()
    instance_ids = set()
    for character in value.values():
        if not isinstance(character, dict):
            continue
        loadouts = character.get("loadouts")
        if not isinstance(loadouts, list):
            continue
        for loadout in loadouts:
            if not isinstance(loadout, dict):
                continue
            items = loadout.get("items")
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                instance_id = item.get("itemInstanceId")
                if isinstance(instance_id, (str, int)):
                    text = str(instance_id)
                    if text.isdecimal() and int(text) > 0:
                        instance_ids.add(text)
    return instance_ids


def collect_plug_hashes(items: Iterable[dict[str, Any]]) -> set[int]:
    hashes = set()
    for item in items:
        components = item["components"]
        for socket in components.get("sockets", {}).get("sockets", []):
            if isinstance(socket, dict):
                if plug_hash := positive_hash(socket.get("plugHash")):
                    hashes.add(plug_hash)
        reusable = components.get("reusablePlugs", {}).get("plugs", {})
        if not isinstance(reusable, dict):
            continue
        for rows in reusable.values():
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict):
                    if plug_hash := positive_hash(row.get("plugItemHash")):
                        hashes.add(plug_hash)
    return hashes


def excluded_plug(definition: dict[str, Any] | None) -> bool:
    if definition is None:
        return False
    category = plug_category(definition) or ""
    return (
        category == "crafting.recipes.empty_socket"
        or category == "shader"
        or category == "mementos"
        or category == "weapon_tiering_kill_vfx"
        or category.startswith("crafting.plugs.weapons.mods.")
        or ".weapon.mod" in category
        or ".plugs.weapons.masterworks" in category
        or ".plugs.masterworks" in category
        or category.startswith("weapon_tiering.plugs.mods.")
        or category.endswith("_skins")
        or ".masterwork" in category
    )


def column_label(
    index: int,
    item_columns: list[dict[int, dict[str, Any]]],
    frame_indexes: list[int],
) -> str:
    category = next(
        (
            columns[index]["category"]
            for columns in item_columns
            if index in columns
        ),
        "gameplay",
    )
    if category == "frames":
        return f"Trait {frame_indexes.index(index) + 1}"
    return CATEGORY_LABELS.get(category, category.replace("_", " ").title())


def plug_category(definition: dict[str, Any] | None) -> str | None:
    if not definition:
        return None
    category = definition.get("plug", {}).get("plugCategoryIdentifier")
    return category if isinstance(category, str) and category else None


def plug_display(
    definitions: dict[int, dict[str, Any]],
    plug_hash: int,
) -> dict[str, Any]:
    display = definitions.get(plug_hash, {}).get("displayProperties", {})
    name = display.get("name")
    description = display.get("description")
    icon = display.get("icon")
    return {
        "hash": plug_hash,
        "name": (
            name.strip()
            if isinstance(name, str) and name.strip()
            else f"Plug {plug_hash}"
        ),
        "description": (
            description.strip()
            if isinstance(description, str) and description.strip()
            else "No official Bungie description is available."
        ),
        "icon": icon if isinstance(icon, str) else "",
        "source": "Bungie manifest",
    }


def primary_power(item: dict[str, Any]) -> int | None:
    value = (
        item["components"]
        .get("instances", {})
        .get("primaryStat", {})
        .get("value")
    )
    return value if isinstance(value, int) else None


def keep_priority(item: dict[str, Any]) -> tuple[int, int, int]:
    return (
        int(bool(item["state"] & 4)),
        primary_power(item) or 0,
        -item["id"],
    )


def positive_hash(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if 0 < parsed < 2**32 else None
