"""Saved-policy Armor 3.0 cleaner analysis."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from typing import Any, Iterable

from destiny_web_app.database import Database
from destiny_web_app.manifest import ManifestService


ARMOR_RULESET_VERSION = "armor-tier5-set-coverage-v2"
ARMOR_STAT_NAMES = (
    "Weapons",
    "Health",
    "Class",
    "Grenade",
    "Super",
    "Melee",
)
DEFAULT_ACCEPTED_STATS = tuple(
    name for name in ARMOR_STAT_NAMES if name != "Health"
)
CLASS_NAMES = {0: "Titan", 1: "Hunter", 2: "Warlock", 3: "Any class"}
SLOT_TRAITS = {
    "item.armor.head": "Helmet",
    "item.armor.arms": "Gauntlets",
    "item.armor.chest": "Chest",
    "item.armor.legs": "Legs",
    "item.armor.class": "Class item",
}
# Bungie's mobile manifest does not consistently label raid acquisition on the
# item or collectible. Every discovered set remains editable in the UI.
KNOWN_RAID_SET_NAMES = {"Last Discipline"}


class ArmorCleanerError(RuntimeError):
    """An armor-cleaner analysis could not be produced safely."""


class ArmorCleanerService:
    def __init__(self, database: Database, manifest: ManifestService) -> None:
        self.database = database
        self.manifest = manifest

    def policy_context(self, bungie_membership_id: str) -> dict[str, Any]:
        source, item_definitions, plug_definitions, set_catalog = (
            self._load_context(bungie_membership_id)
        )
        stored = self.database.load_armor_cleaner_policy(bungie_membership_id)
        policy = normalize_policy(
            stored["policy"] if stored else {},
            set_catalog,
        )
        return {
            "source": source,
            "item_definitions": item_definitions,
            "plug_definitions": plug_definitions,
            "set_catalog": set_catalog,
            "policy": policy,
            "revision": int(stored["revision"]) if stored else 0,
            "updated_at": stored["updated_at"] if stored else None,
        }

    def save_policy(
        self,
        bungie_membership_id: str,
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        context = self.policy_context(bungie_membership_id)
        normalized = normalize_policy(policy, context["set_catalog"])
        return self.database.save_armor_cleaner_policy(
            bungie_membership_id,
            normalized,
        )

    def analyze(self, bungie_membership_id: str) -> dict[str, Any]:
        context = self.policy_context(bungie_membership_id)
        manual_keeps = self.database.armor_manual_keeps(bungie_membership_id)
        result = analyze_armor(
            context["source"]["items"],
            context["item_definitions"],
            context["plug_definitions"],
            context["set_catalog"],
            context["policy"],
            manual_keeps=manual_keeps,
            snapshot=context["source"]["snapshot"],
        )
        policy_fingerprint = hashlib.sha256(
            json.dumps(
                context["policy"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:12]
        ruleset = (
            f"{ARMOR_RULESET_VERSION}-p{context['revision']}-"
            f"{policy_fingerprint}"
        )
        return self.database.save_cleaner_analysis(
            bungie_membership_id,
            snapshot_id=context["source"]["snapshot"]["snapshot_id"],
            analysis_kind="armor",
            ruleset_version=ruleset,
            manifest_version=self.manifest.status()["version"],
            result=result,
        )

    def latest(self, bungie_membership_id: str) -> dict[str, Any] | None:
        return self.database.latest_cleaner_analysis(
            bungie_membership_id,
            analysis_kind="armor",
        )

    def set_manual_keep(
        self,
        bungie_membership_id: str,
        item_instance_id: str,
        *,
        keep: bool,
    ) -> dict[str, Any]:
        source = self.database.load_active_inventory_for_analysis(
            bungie_membership_id
        )
        if source is None:
            raise ArmorCleanerError("Synchronize inventory before saving a keep.")
        owned = {
            item["item_instance_id"]
            for item in source["items"]
            if item.get("item_instance_id")
        }
        if item_instance_id not in owned:
            raise ArmorCleanerError("That armor instance is no longer owned.")
        self.database.set_armor_manual_keep(
            bungie_membership_id,
            item_instance_id,
            keep=keep,
        )
        return self.analyze(bungie_membership_id)

    def _load_context(
        self,
        bungie_membership_id: str,
    ) -> tuple[
        dict[str, Any],
        dict[int, dict[str, Any]],
        dict[int, dict[str, Any]],
        dict[int, dict[str, Any]],
    ]:
        source = self.database.load_active_inventory_for_analysis(
            bungie_membership_id
        )
        if source is None:
            raise ArmorCleanerError(
                "Synchronize an inventory before analyzing armor."
            )
        manifest_status = self.manifest.status()
        if not manifest_status["available"] or not manifest_status.get("version"):
            raise ArmorCleanerError(
                "Prepare the current Destiny item definitions first."
            )
        item_definitions = self.manifest.resolve_many(
            "DestinyInventoryItemDefinition",
            (item["item_hash"] for item in source["items"]),
        )
        armor_items = [
            item
            for item in source["items"]
            if item_definitions.get(item["item_hash"], {}).get("itemType") == 2
        ]
        plug_hashes = collect_armor_plug_hashes(armor_items)
        plug_definitions = self.manifest.resolve_many(
            "DestinyInventoryItemDefinition",
            plug_hashes,
        )
        stat_hashes = {
            int(raw_hash)
            for item in armor_items
            for raw_hash in item.get("components", {})
            .get("stats", {})
            .get("stats", {})
        }
        plug_definitions.update(
            self.manifest.resolve_many("DestinyStatDefinition", stat_hashes)
        )
        set_definitions = self.manifest.resolve_all(
            "DestinyEquipableItemSetDefinition"
        )
        set_catalog = build_set_catalog(
            set_definitions,
            item_definitions,
            armor_items,
            self.manifest,
        )
        return source, item_definitions, plug_definitions, set_catalog


def normalize_policy(
    value: dict[str, Any],
    set_catalog: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    source_mode = value.get("source_mode")
    if source_mode not in {"same", "separate"}:
        source_mode = "same"
    tuning_mode = value.get("tuning_mode")
    if tuning_mode not in {"required", "preferred", "ignored"}:
        tuning_mode = "preferred"
    preferred_tuning_raw = value.get("preferred_tuning")
    if isinstance(preferred_tuning_raw, str):
        preferred_tuning = (
            [preferred_tuning_raw]
            if preferred_tuning_raw in ARMOR_STAT_NAMES
            else []
        )
    elif isinstance(preferred_tuning_raw, list):
        preferred_tuning = [
            name for name in ARMOR_STAT_NAMES if name in preferred_tuning_raw
        ]
    else:
        preferred_tuning = []
    raw_sets = value.get("sets")
    if not isinstance(raw_sets, dict):
        raw_sets = {}
    policies: dict[str, dict[str, Any]] = {}
    for set_hash, catalog in sorted(set_catalog.items()):
        raw = raw_sets.get(str(set_hash), {})
        if not isinstance(raw, dict):
            raw = {}
        policies[str(set_hash)] = {
            "name": catalog["name"],
            "interested": bool(raw.get("interested", True)),
            "source": (
                raw.get("source")
                if raw.get("source") in {"raid", "nonraid"}
                else catalog["default_source"]
            ),
            "two_piece": False,
            "four_piece": False,
            "primary": accepted_stats(raw.get("primary")),
            "secondary": accepted_stats(raw.get("secondary")),
            "tertiary": accepted_stats(raw.get("tertiary")),
        }
    return {
        "source_mode": source_mode,
        "tuning_mode": tuning_mode,
        "preferred_tuning": preferred_tuning,
        "excluded_stats": [],
        "sets": policies,
    }


def accepted_stats(value: Any) -> list[str]:
    if not isinstance(value, list):
        return list(DEFAULT_ACCEPTED_STATS)
    return [name for name in ARMOR_STAT_NAMES if name in value]


def build_set_catalog(
    set_definitions: dict[int, dict[str, Any]],
    item_definitions: dict[int, dict[str, Any]],
    armor_items: list[dict[str, Any]],
    manifest: ManifestService,
) -> dict[int, dict[str, Any]]:
    owned_hashes = {item["item_hash"] for item in armor_items}
    catalog: dict[int, dict[str, Any]] = {}
    perk_hashes: set[int] = set()
    for set_hash, definition in set_definitions.items():
        members = {
            int(item_hash)
            for item_hash in definition.get("setItems", [])
            if isinstance(item_hash, int)
        }
        if not members & owned_hashes:
            continue
        for row in definition.get("setPerks", []):
            if isinstance(row, dict) and isinstance(row.get("sandboxPerkHash"), int):
                perk_hashes.add(row["sandboxPerkHash"])
        name = definition.get("displayProperties", {}).get("name")
        catalog[set_hash] = {
            "hash": set_hash,
            "name": name or f"Armor set {set_hash}",
            "members": sorted(members),
            "bonuses": {},
            "default_source": (
                "raid" if name in KNOWN_RAID_SET_NAMES else "nonraid"
            ),
        }
    perk_definitions = manifest.resolve_many(
        "DestinySandboxPerkDefinition",
        perk_hashes,
    )
    for set_hash, definition in set_definitions.items():
        if set_hash not in catalog:
            continue
        for row in definition.get("setPerks", []):
            if not isinstance(row, dict):
                continue
            required = row.get("requiredSetCount")
            perk_hash = row.get("sandboxPerkHash")
            if required not in {2, 4} or not isinstance(perk_hash, int):
                continue
            perk = perk_definitions.get(perk_hash, {})
            display = perk.get("displayProperties", {})
            catalog[set_hash]["bonuses"][str(required)] = {
                "hash": perk_hash,
                "name": display.get("name") or f"{required}-piece bonus",
                "description": display.get("description") or "No description.",
                "icon": display.get("icon"),
            }
    assigned = {
        item_hash
        for catalog_set in catalog.values()
        for item_hash in catalog_set["members"]
    }
    if owned_hashes - assigned:
        catalog[0] = {
            "hash": 0,
            "name": "No recognized armor set",
            "members": sorted(owned_hashes - assigned),
            "bonuses": {},
            "default_source": "nonraid",
        }
    return catalog


def analyze_armor(
    items: list[dict[str, Any]],
    item_definitions: dict[int, dict[str, Any]],
    plug_definitions: dict[int, dict[str, Any]],
    set_catalog: dict[int, dict[str, Any]],
    policy: dict[str, Any],
    *,
    manual_keeps: set[str],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    preferred_tuning = policy.get("preferred_tuning")
    if isinstance(preferred_tuning, str):
        preferred_tuning_stats = {preferred_tuning}
    elif isinstance(preferred_tuning, list):
        preferred_tuning_stats = {
            name for name in preferred_tuning if name in ARMOR_STAT_NAMES
        }
    else:
        preferred_tuning_stats = set()
    set_by_item = {
        item_hash: set_hash
        for set_hash, catalog in set_catalog.items()
        for item_hash in catalog["members"]
    }
    armor = [
        extract_armor_item(
            item,
            item_definitions[item["item_hash"]],
            plug_definitions,
            set_hash=set_by_item.get(item["item_hash"], 0),
            set_name=set_catalog.get(set_by_item.get(item["item_hash"], 0), {}).get(
                "name", "No recognized armor set"
            ),
        )
        for item in items
        if item.get("item_hash") in item_definitions
        and item_definitions[item["item_hash"]].get("itemType") == 2
    ]
    decisions: dict[int, dict[str, Any]] = {}
    eligible: list[dict[str, Any]] = []
    incomplete_count = 0
    for item in armor:
        instance_id = item.get("item_instance_id")
        if item["exotic"]:
            decisions[item["item_row_id"]] = decision("keep", "Exotic armor")
            continue
        if instance_id and instance_id in manual_keeps:
            decisions[item["item_row_id"]] = decision("keep", "Manual keep")
            continue
        if item["gear_tier"] != 5:
            decisions[item["item_row_id"]] = decision(
                "candidate", "Armor is not Tier 5"
            )
            continue
        if len(item["intrinsic_stats"]) != 3:
            incomplete_count += 1
            decisions[item["item_row_id"]] = decision(
                "keep", "Incomplete intrinsic data; kept conservatively"
            )
            continue
        set_policy = policy["sets"].get(str(item["set_hash"]))
        if not set_policy or not set_policy["interested"]:
            decisions[item["item_row_id"]] = decision(
                "candidate", "Armor set is not selected"
            )
            continue
        mismatch = next(
            (
                position
                for position, stat in zip(
                    ("primary", "secondary", "tertiary"),
                    item["intrinsic_stats"],
                    strict=True,
                )
                if stat not in set_policy[position]
            ),
            None,
        )
        item["stat_match"] = mismatch is None
        accepted_focus = set(
            set_policy["primary"]
            + set_policy["secondary"]
            + set_policy["tertiary"]
        )
        if preferred_tuning_stats:
            item["tuning_aligned"] = item["tuned_stat"] in preferred_tuning_stats
        else:
            item["tuning_aligned"] = (
                item["tuned_stat"] in accepted_focus
                if item["tuned_stat"]
                else False
            )
        if mismatch:
            decisions[item["item_row_id"]] = decision(
                "candidate", f"{mismatch.title()} stat is not selected"
            )
            continue
        if policy["tuning_mode"] == "required" and not item["tuning_aligned"]:
            reason = "Tuning does not match the preferred slots"
            if not preferred_tuning_stats:
                reason = "Tuning is not aligned with selected stats"
            decisions[item["item_row_id"]] = decision(
                "candidate", reason
            )
            continue
        eligible.append(item)

    duplicate_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for item in eligible:
        # Armor pieces can only replace other pieces from the same set.
        duplicate_groups[
            (
                item["set_hash"],
                item["class_type"],
                item["slot"],
                tuple(item["intrinsic_stats"]),
            )
        ].append(item)
    for copies in duplicate_groups.values():
        ordered = sorted(
            copies,
            key=lambda item: duplicate_preference_key(item, policy),
            reverse=True,
        )
        decisions[ordered[0]["item_row_id"]] = decision(
            "keep", "Representative of this intrinsic roll"
        )
        for duplicate in ordered[1:]:
            alignment_note = (
                " with tuning aligned to its intrinsic roll"
                if ordered[0]["intrinsic_tuning_aligned"]
                and not duplicate["intrinsic_tuning_aligned"]
                else ""
            )
            decisions[duplicate["item_row_id"]] = decision(
                "candidate",
                "Duplicate intrinsic roll; covered by "
                f"{ordered[0]['name']} {ordered[0]['instance_suffix']}"
                f"{alignment_note}",
                alternative_item_row_id=ordered[0]["item_row_id"],
            )

    apply_set_coverage(armor, decisions, policy)
    rendered = []
    for item in armor:
        item_decision = decisions.get(
            item["item_row_id"],
            decision("keep", "Kept conservatively"),
        )
        rendered.append({**item, **item_decision})
    rendered.sort(
        key=lambda item: (
            {"candidate": 0, "keep": 1}[item["decision"]],
            item["set_name"].lower(),
            item["class_type"],
            item["slot"],
            item["name"].lower(),
            item["item_row_id"],
        )
    )
    groups = build_result_groups(rendered, set_catalog, policy)
    coverage_conflict_count = sum(
        not row["met"] for group in groups for row in group["coverage"]
    )
    candidate_count = sum(item["decision"] == "candidate" for item in rendered)
    keep_count = len(rendered) - candidate_count
    return {
        "summary": {
            "item_count": len(rendered),
            "armor_count": len(rendered),
            "group_count": len(groups),
            "keep_count": keep_count,
            "candidate_count": candidate_count,
            "manual_keep_count": sum(
                item.get("item_instance_id") in manual_keeps for item in rendered
            ),
            "exotic_count": sum(item["exotic"] for item in rendered),
            "tier5_count": sum(item["gear_tier"] == 5 for item in rendered),
            "incomplete_count": incomplete_count,
            "coverage_conflict_count": coverage_conflict_count,
            "projected_space_recovered": candidate_count,
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_fetched_at": snapshot["fetched_at"],
            "snapshot_stale_at": snapshot["stale_at"],
            "snapshot_status": snapshot["sync_status"],
        },
        "policy": policy,
        "groups": groups,
    }


def duplicate_preference_key(
    item: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[int, int, int, int]:
    """Rank copies of the same intrinsic roll for deterministic retention."""
    return (
        int(item["intrinsic_tuning_aligned"]),
        int(
            policy["tuning_mode"] == "preferred"
            and item["tuning_aligned"]
        ),
        int(item["masterworked"]),
        -item["item_row_id"],
    )


def apply_set_coverage(
    armor: list[dict[str, Any]],
    decisions: dict[int, dict[str, Any]],
    policy: dict[str, Any],
) -> None:
    by_set_class: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for item in armor:
        if item["exotic"] or item["gear_tier"] != 5:
            continue
        by_set_class[(item["set_hash"], item["class_type"])].append(item)
    for (set_hash, _class_type), items in by_set_class.items():
        set_policy = policy["sets"].get(str(set_hash))
        if not set_policy or not set_policy["interested"]:
            continue
        required = 4 if set_policy["four_piece"] else (
            2 if set_policy["two_piece"] else 0
        )
        if not required:
            continue
        kept_slots = {
            item["slot"]
            for item in items
            if decisions.get(item["item_row_id"], {}).get("decision") == "keep"
        }
        if len(kept_slots) >= required:
            continue
        candidates = sorted(
            (
                item
                for item in items
                if item["slot"] not in kept_slots
                and decisions.get(item["item_row_id"], {}).get("decision")
                == "candidate"
            ),
            key=lambda item: (
                int(item.get("stat_match", False)),
                int(item.get("tuning_aligned", False)),
                int(item["masterworked"]),
                -item["item_row_id"],
            ),
            reverse=True,
        )
        for item in candidates:
            if item["slot"] in kept_slots:
                continue
            decisions[item["item_row_id"]] = decision(
                "keep", f"Required for selected {required}-piece set coverage"
            )
            kept_slots.add(item["slot"])
            if len(kept_slots) >= required:
                break


def extract_armor_item(
    item: dict[str, Any],
    definition: dict[str, Any],
    plug_definitions: dict[int, dict[str, Any]],
    *,
    set_hash: int,
    set_name: str,
) -> dict[str, Any]:
    components = item.get("components", {})
    instances = components.get("instances", {})
    sockets = components.get("sockets", {}).get("sockets", [])
    intrinsic: list[str] = []
    archetype = None
    tuning_index = None
    current_tuning_hash = None
    for index, socket in enumerate(sockets if isinstance(sockets, list) else []):
        if not isinstance(socket, dict):
            continue
        plug_hash = socket.get("plugHash")
        plug = plug_definitions.get(plug_hash, {}) if isinstance(plug_hash, int) else {}
        category = plug.get("plug", {}).get("plugCategoryIdentifier")
        if category == "armor_archetypes":
            display = plug.get("displayProperties", {})
            archetype = {
                "hash": plug_hash,
                "name": display.get("name") or "Unknown archetype",
                "description": display.get("description") or "",
            }
        elif category == "armor_stats":
            if stat_name := investment_stat_name(plug, plug_definitions):
                intrinsic.append(stat_name)
        elif category == "core.gear_systems.armor_tiering.plugs.tuning.mods":
            tuning_index = index
            current_tuning_hash = plug_hash
    tuned_stat = tuning_stat(
        components,
        tuning_index,
        current_tuning_hash,
        plug_definitions,
    )
    display = definition.get("displayProperties", {})
    inventory = definition.get("inventory", {})
    traits = definition.get("traitIds", [])
    slot = next(
        (label for trait, label in SLOT_TRAITS.items() if trait in traits),
        definition.get("itemTypeDisplayName") or "Unknown slot",
    )
    stats = component_stats(components, plug_definitions)
    return {
        "item_row_id": item["id"],
        "item_hash": item["item_hash"],
        "item_instance_id": item.get("item_instance_id"),
        "instance_suffix": (
            f"…{item['item_instance_id'][-6:]}"
            if item.get("item_instance_id")
            else "Not instanced"
        ),
        "name": display.get("name") or f"Armor {item['item_hash']}",
        "type": definition.get("itemTypeDisplayName") or "Armor",
        "icon": display.get("icon"),
        "class_type": int(definition.get("classType", 3)),
        "class_name": CLASS_NAMES.get(definition.get("classType"), "Unknown"),
        "slot": slot,
        "set_hash": set_hash,
        "set_name": set_name,
        "rarity": inventory.get("tierTypeName") or "Unknown",
        "exotic": inventory.get("tierTypeName") == "Exotic",
        "gear_tier": instances.get("gearTier"),
        "intrinsic_stats": intrinsic,
        "archetype": archetype,
        "tuned_stat": tuned_stat,
        "intrinsic_tuning_aligned": tuned_stat in intrinsic,
        "tuning_aligned": False,
        "stats": stats,
        "source_kind": item.get("source_kind"),
        "character_id": item.get("character_id"),
        "locked": bool(int(item.get("state") or 0) & 1),
        "masterworked": bool(int(item.get("state") or 0) & 4),
        "lockable": bool(item.get("lockable")),
    }


def investment_stat_name(
    plug: dict[str, Any],
    definitions: dict[int, dict[str, Any]],
) -> str | None:
    for row in plug.get("investmentStats", []):
        if not isinstance(row, dict) or not int(row.get("value") or 0) > 0:
            continue
        stat_hash = row.get("statTypeHash")
        if not isinstance(stat_hash, int):
            continue
        stat = definitions.get(stat_hash, {})
        name = stat.get("displayProperties", {}).get("name")
        if name in ARMOR_STAT_NAMES:
            return name
    return None


def tuning_stat(
    components: dict[str, Any],
    tuning_index: int | None,
    current_hash: int | None,
    definitions: dict[int, dict[str, Any]],
) -> str | None:
    hashes = []
    if current_hash:
        hashes.append(current_hash)
    reusable = components.get("reusablePlugs", {}).get("plugs", {})
    rows = reusable.get(str(tuning_index), []) if tuning_index is not None else []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("canInsert") is not False:
            if isinstance(row.get("plugItemHash"), int):
                hashes.append(row["plugItemHash"])
    names = [
        name
        for plug_hash in hashes
        if (name := investment_stat_name(definitions.get(plug_hash, {}), definitions))
    ]
    return Counter(names).most_common(1)[0][0] if names else None


def component_stats(
    components: dict[str, Any],
    definitions: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    output = []
    rows = components.get("stats", {}).get("stats", {})
    if not isinstance(rows, dict):
        return output
    for raw_hash, row in rows.items():
        if not isinstance(row, dict):
            continue
        try:
            stat_hash = int(raw_hash)
        except (TypeError, ValueError):
            continue
        definition = definitions.get(stat_hash, {})
        name = definition.get("displayProperties", {}).get("name")
        if name in ARMOR_STAT_NAMES:
            output.append({"name": name, "value": int(row.get("value") or 0)})
    output.sort(key=lambda row: ARMOR_STAT_NAMES.index(row["name"]))
    return output


def collect_armor_plug_hashes(items: Iterable[dict[str, Any]]) -> set[int]:
    output = set()
    for item in items:
        components = item.get("components", {})
        stats = components.get("stats", {}).get("stats", {})
        if isinstance(stats, dict):
            for raw_hash in stats:
                try:
                    output.add(int(raw_hash))
                except (TypeError, ValueError):
                    pass
        for socket in components.get("sockets", {}).get("sockets", []):
            if isinstance(socket, dict) and isinstance(socket.get("plugHash"), int):
                output.add(socket["plugHash"])
        reusable = components.get("reusablePlugs", {}).get("plugs", {})
        if not isinstance(reusable, dict):
            continue
        for rows in reusable.values():
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict) and isinstance(row.get("plugItemHash"), int):
                    output.add(row["plugItemHash"])
    return output


def decision(
    value: str,
    reason: str,
    *,
    alternative_item_row_id: int | None = None,
) -> dict[str, Any]:
    return {
        "decision": value,
        "reason": reason,
        "alternative_item_row_id": alternative_item_row_id,
    }


def build_result_groups(
    items: list[dict[str, Any]],
    catalog: dict[int, dict[str, Any]],
    policy: dict[str, Any],
) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        grouped[item["set_hash"]].append(item)
    output = []
    for set_hash, group_items in grouped.items():
        set_policy = policy["sets"].get(str(set_hash), {})
        required = 4 if set_policy.get("four_piece") else (
            2 if set_policy.get("two_piece") else 0
        )
        coverage = []
        if required:
            by_class: dict[int, list[dict[str, Any]]] = defaultdict(list)
            for item in group_items:
                if not item["exotic"]:
                    by_class[item["class_type"]].append(item)
            for class_type, class_items in sorted(by_class.items()):
                retained_slots = sorted(
                    {
                        item["slot"]
                        for item in class_items
                        if item["decision"] == "keep"
                    }
                )
                coverage.append(
                    {
                        "class_type": class_type,
                        "class_name": CLASS_NAMES.get(class_type, "Unknown"),
                        "required": required,
                        "retained_slots": retained_slots,
                        "met": len(retained_slots) >= required,
                    }
                )
        output.append(
            {
                "set_hash": set_hash,
                "name": catalog.get(set_hash, {}).get("name", "Unknown set"),
                "source": set_policy.get("source", "nonraid"),
                "bonuses": catalog.get(set_hash, {}).get("bonuses", {}),
                "owned_count": len(group_items),
                "keep_count": sum(
                    item["decision"] == "keep" for item in group_items
                ),
                "candidate_count": sum(
                    item["decision"] == "candidate" for item in group_items
                ),
                "coverage": coverage,
                "items": group_items,
            }
        )
    output.sort(key=lambda group: (-group["candidate_count"], group["name"].lower()))
    return output
