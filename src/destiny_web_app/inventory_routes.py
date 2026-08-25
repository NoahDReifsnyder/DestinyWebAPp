"""Read-only, manifest-backed inventory presentation routes."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from string import Template
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

from aiohttp import web

from destiny_web_app.auth import csrf_input, require_csrf
from destiny_web_app.app_keys import (
    AUTH_SESSION_KEY,
    DATABASE_KEY,
    MANIFEST_SERVICE_KEY,
)
from destiny_web_app.manifest import (
    ManifestError,
    public_manifest_error,
)


TEMPLATE_ROOT = Path(__file__).with_name("templates")
BUNGIE_ROOT = "https://www.bungie.net"
CLASS_NAMES = {0: "Titan", 1: "Hunter", 2: "Warlock", 3: "Unknown"}
SOURCE_LABELS = {
    "equipped": "Equipped",
    "character_inventory": "Carried",
    "vault": "Vault",
    "profile_inventory": "Shared",
    "postmaster": "Postmaster",
}


async def inventory_page(request: web.Request) -> web.Response:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPSeeOther("/")

    database = request.app[DATABASE_KEY]
    inventory = await asyncio.to_thread(
        database.load_active_inventory,
        authenticated.bungie_membership_id,
    )
    manifest_service = request.app[MANIFEST_SERVICE_KEY]
    manifest_status = manifest_service.status()

    definitions: dict[int, dict[str, Any]] = {}
    buckets: dict[int, dict[str, Any]] = {}
    classes: dict[int, dict[str, Any]] = {}
    if inventory and manifest_status["available"]:
        try:
            definitions, buckets, classes = await asyncio.gather(
                asyncio.to_thread(
                    manifest_service.resolve_many,
                    "DestinyInventoryItemDefinition",
                    (item["item_hash"] for item in inventory["items"]),
                ),
                asyncio.to_thread(
                    manifest_service.resolve_many,
                    "DestinyInventoryBucketDefinition",
                    (item["bucket_hash"] for item in inventory["items"]),
                ),
                asyncio.to_thread(
                    manifest_service.resolve_many,
                    "DestinyClassDefinition",
                    (
                        character["class_hash"]
                        for character in inventory["characters"]
                        if character.get("class_hash")
                    ),
                ),
            )
        except ManifestError as error:
            manifest_status["available"] = False
            manifest_status["local_error"] = str(error)

    body = build_inventory_body(inventory, definitions, buckets, classes)
    notice = build_notice(request, inventory, manifest_status)
    html = render_template(
        "inventory.html",
        guardian_name=escape(authenticated.display_name),
        notice=notice,
        body=body,
        item_total=(
            str(inventory["snapshot"]["total_item_count"]) if inventory else "0"
        ),
        vault_total=(
            str(inventory["snapshot"]["vault_item_count"]) if inventory else "0"
        ),
        snapshot_age=snapshot_age(inventory),
        sync_tone=snapshot_tone(inventory),
        sync_label=snapshot_label(inventory),
        manifest_csrf=csrf_input(request, "/inventory/manifest/sync"),
    )
    response = web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )
    response.enable_compression()
    return response


async def synchronize_manifest(request: web.Request) -> web.StreamResponse:
    if request.get(AUTH_SESSION_KEY) is None:
        raise web.HTTPSeeOther("/")
    form = await request.post()
    require_csrf(request, form)
    force = form.get("force") == "1"
    try:
        result = await request.app[MANIFEST_SERVICE_KEY].synchronize(force=force)
        message = (
            "Item definitions downloaded."
            if result.get("downloaded")
            else "Item definitions are already current."
        )
        query = urlencode({"notice": message})
    except Exception as error:
        query = urlencode({"error": public_manifest_error(error)})
    raise web.HTTPSeeOther(f"/inventory?{query}")


async def inventory_item_detail(request: web.Request) -> web.Response:
    authenticated = request.get(AUTH_SESSION_KEY)
    if authenticated is None:
        raise web.HTTPUnauthorized(
            text="Sign in to view inventory details.",
        )
    try:
        item_id = int(request.match_info["item_id"])
    except ValueError as error:
        raise web.HTTPNotFound() from error

    database = request.app[DATABASE_KEY]
    item = await asyncio.to_thread(
        database.load_active_inventory_item,
        authenticated.bungie_membership_id,
        item_id,
    )
    if item is None:
        raise web.HTTPNotFound()

    manifest_service = request.app[MANIFEST_SERVICE_KEY]
    definitions: dict[int, dict[str, Any]] = {}
    stat_definitions: dict[int, dict[str, Any]] = {}
    plug_definitions: dict[int, dict[str, Any]] = {}
    perk_definitions: dict[int, dict[str, Any]] = {}
    if manifest_service.status()["available"]:
        component_stats = item["components"].get("stats", {}).get("stats", {})
        stat_hashes = hashes_from_mapping(component_stats, "statHash")
        sockets = item["components"].get("sockets", {}).get("sockets", [])
        plug_hashes = hashes_from_rows(sockets, "plugHash")
        perks = item["components"].get("perks", {}).get("perks", [])
        perk_hashes = hashes_from_rows(perks, "perkHash")
        try:
            (
                definitions,
                stat_definitions,
                plug_definitions,
                perk_definitions,
            ) = await asyncio.gather(
                asyncio.to_thread(
                    manifest_service.resolve_many,
                    "DestinyInventoryItemDefinition",
                    [item["item_hash"]],
                ),
                asyncio.to_thread(
                    manifest_service.resolve_many,
                    "DestinyStatDefinition",
                    stat_hashes,
                ),
                asyncio.to_thread(
                    manifest_service.resolve_many,
                    "DestinyInventoryItemDefinition",
                    plug_hashes,
                ),
                asyncio.to_thread(
                    manifest_service.resolve_many,
                    "DestinySandboxPerkDefinition",
                    perk_hashes,
                ),
            )
        except ManifestError:
            # The stored player item is still useful even when the optional
            # local definition catalog becomes unreadable.
            definitions = {}
            stat_definitions = {}
            plug_definitions = {}
            perk_definitions = {}

    payload = detail_payload(
        item,
        definitions.get(item["item_hash"]),
        stat_definitions,
        plug_definitions,
        perk_definitions,
    )
    return web.json_response(payload, headers={"Cache-Control": "no-store"})


def build_inventory_body(
    inventory: dict[str, Any] | None,
    definitions: dict[int, dict[str, Any]],
    buckets: dict[int, dict[str, Any]],
    classes: dict[int, dict[str, Any]] | None = None,
) -> str:
    if inventory is None:
        return """
<section class="empty-state">
  <div class="empty-mark">◇</div>
  <p class="eyebrow">No inventory snapshot</p>
  <h2>Bring your Guardian data into focus.</h2>
  <p>Synchronize a complete inventory first, then return here to explore it.</p>
  <a class="action primary" href="/data/status">Open synchronization</a>
</section>"""

    classes = classes or {}
    items = inventory["items"]
    characters = inventory["characters"]
    character_names = {
        character["character_id"]: (
            display_name(classes.get(character.get("class_hash")))
            or CLASS_NAMES.get(character.get("class_type"), "Guardian")
        )
        for character in characters
    }
    class_name_counts: dict[str, int] = {}
    for class_name in character_names.values():
        class_name_counts[class_name] = class_name_counts.get(class_name, 0) + 1

    character_sections: list[str] = []
    for character in characters:
        character_id = character["character_id"]
        equipped = [
            item
            for item in items
            if item["character_id"] == character_id
            and item["source_kind"] == "equipped"
        ]
        carried = [
            item
            for item in items
            if item["character_id"] == character_id
            and item["source_kind"] == "character_inventory"
        ]
        postmaster = [
            item
            for item in items
            if item["character_id"] == character_id
            and item["source_kind"] == "postmaster"
        ]
        payload = character["payload"]
        emblem_path = payload.get("emblemBackgroundPath")
        style = (
            f' style="--emblem:url(&quot;{escape(icon_url(emblem_path))}&quot;)"'
            if isinstance(emblem_path, str) and emblem_path
            else ""
        )
        class_name = character_names[character_id]
        owner_label = (
            f"{class_name} · …{character_id[-4:]}"
            if class_name_counts[class_name] > 1
            else class_name
        )
        character_sections.append(
            f"""
<article class="character-card inventory-zone"
         data-zone="{escape(class_name.lower())}">
  <header class="character-header"{style}>
    <div>
      <p class="eyebrow">Guardian</p>
      <h2>{escape(class_name)}</h2>
    </div>
    <div class="character-light"><span>✦</span>{character.get("light") or "—"}</div>
    <p>{len(equipped)} equipped · {len(carried)} carried ·
       {len(postmaster)} postmaster</p>
  </header>
  {render_grouped_items(
      "Equipped", equipped, definitions, buckets,
      owner_value=character_id, owner_label=owner_label, open_group=True
  )}
  {render_grouped_items(
      "Carried", carried, definitions, buckets,
      owner_value=character_id, owner_label=owner_label
  )}
  {render_grouped_items(
      "Postmaster", postmaster, definitions, buckets,
      owner_value=character_id, owner_label=owner_label
  )}
</article>"""
        )

    vault = [item for item in items if item["source_kind"] == "vault"]
    shared = [
        item for item in items if item["source_kind"] == "profile_inventory"
    ]
    shared_section = ""
    if shared:
        shared_section = (
            """
<section class="vault-shell inventory-zone" data-zone="shared">
  <header class="section-heading">
    <div><p class="eyebrow">Shared inventory</p><h2>Profile items</h2></div>
    <span class="section-count">"""
            + str(len(shared))
            + " items</span></header>"
            + render_grouped_items(
                "Shared profile inventory",
                shared,
                definitions,
                buckets,
                owner_value="profile:shared",
                owner_label="Shared inventory",
                open_group=True,
            )
            + "</section>"
        )
    return (
        '<section class="character-deck">'
        + "".join(character_sections)
        + "</section>"
        + """
<section class="vault-shell inventory-zone" data-zone="vault">
  <header class="section-heading">
    <div><p class="eyebrow">Shared storage</p><h2>Vault</h2></div>
    <span class="section-count">"""
        + str(len(vault))
        + " items</span></header>"
        + render_grouped_items(
            "Vault inventory",
            vault,
            definitions,
            buckets,
            owner_value="profile:vault",
            owner_label="Vault",
            open_group=True,
        )
        + "</section>"
        + shared_section
    )


def render_grouped_items(
    title: str,
    items: list[dict[str, Any]],
    definitions: dict[int, dict[str, Any]],
    buckets: dict[int, dict[str, Any]],
    *,
    owner_value: str | None = None,
    owner_label: str = "Shared storage",
    open_group: bool = False,
) -> str:
    if not items:
        return (
            f'<section class="item-section"><h3>{escape(title)}</h3>'
            '<p class="muted">No items in this location.</p></section>'
        )
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for item in items:
        bucket_hash = item["bucket_hash"]
        bucket_name = display_name(buckets.get(bucket_hash)) or "Other"
        grouped.setdefault((bucket_hash, bucket_name), []).append(item)

    groups: list[str] = []
    for (_, bucket_name), group_items in sorted(
        grouped.items(), key=lambda entry: (bucket_order(entry[0][1]), entry[0][1])
    ):
        tiles = "".join(
            render_item_tile(
                item,
                definitions.get(item["item_hash"]),
                bucket_name=bucket_name,
                owner_value=owner_value or owner_label,
                owner_label=owner_label,
            )
            for item in sorted(
                group_items,
                key=lambda value: item_sort_key(
                    definitions.get(value["item_hash"]),
                    value,
                ),
            )
        )
        groups.append(
            f"""
<details class="bucket-group"{" open" if open_group else ""}>
  <summary><span>{escape(bucket_name)}</span><b>{len(group_items)}</b></summary>
  <div class="item-grid">{tiles}</div>
</details>"""
        )
    return (
        f'<section class="item-section"><h3>{escape(title)}'
        f'<span>{len(items)}</span></h3>{"".join(groups)}</section>'
    )


def render_item_tile(
    item: dict[str, Any],
    definition: dict[str, Any] | None,
    *,
    bucket_name: str,
    owner_value: str,
    owner_label: str,
) -> str:
    name = display_name(definition) or f"Unresolved item {item['item_hash']}"
    display = definition.get("displayProperties", {}) if definition else {}
    icon_path = display.get("icon") if isinstance(display, dict) else None
    type_name = (
        definition.get("itemTypeDisplayName") if definition else None
    ) or "Unknown item"
    inventory = definition.get("inventory", {}) if definition else {}
    tier = inventory.get("tierTypeName") or "Unknown"
    power = item.get("primary_power")
    state = int(item.get("state") or 0)
    locked = bool(state & 1)
    masterworked = bool(state & 4)
    crafted = bool(state & 8)
    flags = "".join(
        [
            '<span title="Locked">◆</span>' if locked else "",
            '<span class="masterwork" title="Masterworked">✦</span>'
            if masterworked
            else "",
            '<span title="Crafted">⌁</span>' if crafted else "",
        ]
    )
    image = (
        f'<img src="{escape(icon_url(icon_path))}" alt="" loading="lazy">'
        if isinstance(icon_path, str) and icon_path
        else '<span class="missing-icon">◇</span>'
    )
    quantity = (
        f'<span class="quantity">{item["quantity"]}</span>'
        if item.get("quantity", 1) > 1
        else ""
    )
    search = " ".join(
        [
            name,
            type_name,
            tier,
            bucket_name,
            owner_label,
            SOURCE_LABELS.get(item["source_kind"], ""),
        ]
    ).lower()
    states = [
        label
        for enabled, label in (
            (locked, "locked"),
            (masterworked, "masterworked"),
            (crafted, "crafted"),
        )
        if enabled
    ]
    accessible_name = (
        f"{name}, {type_name}, {tier}, "
        f"power {power if power is not None else 'not available'}"
    )
    if states:
        accessible_name += ", " + ", ".join(states)
    return f"""
<button class="item-tile tier-{escape(tier.lower())}"
        type="button"
        data-item-id="{item["id"]}"
        data-search="{escape(search)}"
        data-location="{escape(item["source_kind"])}"
        data-owner="{escape(owner_value)}"
        data-owner-label="{escape(owner_label)}"
        data-bucket="{escape(bucket_name)}"
        data-item-type="{escape(type_name)}"
        data-rarity="{escape(tier.lower())}"
        data-rarity-label="{escape(tier)}"
        data-locked="{"yes" if locked else "no"}"
        aria-label="{escape(accessible_name)}">
  <span class="item-art">{image}{quantity}</span>
  <span class="item-copy">
    <strong>{escape(name)}</strong>
    <small>{escape(type_name)} · {escape(tier)}</small>
  </span>
  <span class="item-meta">
    <b>{power if power is not None else "—"}</b>
    <span class="item-flags">{flags}</span>
  </span>
</button>"""


def detail_payload(
    item: dict[str, Any],
    definition: dict[str, Any] | None,
    stat_definitions: dict[int, dict[str, Any]],
    plug_definitions: dict[int, dict[str, Any]],
    perk_definitions: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    display = definition.get("displayProperties", {}) if definition else {}
    name = display_name(definition) or f"Unresolved item {item['item_hash']}"
    stats_source = item["components"].get("stats", {}).get("stats", {})
    stats = []
    if isinstance(stats_source, dict):
        for key, value in stats_source.items():
            if not isinstance(value, dict):
                continue
            stat_hash = int(value.get("statHash") or key)
            stats.append(
                {
                    "name": display_name(stat_definitions.get(stat_hash))
                    or f"Stat {stat_hash}",
                    "value": value.get("value", 0),
                }
            )
    stats.sort(key=lambda value: value["name"])

    socket_rows = item["components"].get("sockets", {}).get("sockets", [])
    sockets = visible_named_hash_rows(
        socket_rows,
        "plugHash",
        plug_definitions,
        visible_field="isVisible",
        active_field="isEnabled",
        inactive_label="Disabled",
    )
    perk_rows = item["components"].get("perks", {}).get("perks", [])
    perks = visible_named_hash_rows(
        perk_rows,
        "perkHash",
        perk_definitions,
        visible_field="visible",
        active_field="isActive",
        inactive_label="Inactive",
    )
    inventory = definition.get("inventory", {}) if definition else {}
    return {
        "name": name,
        "type": (
            definition.get("itemTypeAndTierDisplayName")
            if definition
            else None
        )
        or (definition.get("itemTypeDisplayName") if definition else None)
        or "Unknown item",
        "description": (
            display.get("description") if isinstance(display, dict) else ""
        )
        or "",
        "icon": icon_url(display.get("icon")) if display.get("icon") else None,
        "watermark": (
            icon_url(definition.get("iconWatermark"))
            if definition and definition.get("iconWatermark")
            else None
        ),
        "tier": inventory.get("tierTypeName") or "Unknown",
        "power": (
            item["components"].get("instances", {}).get("primaryStat", {}).get(
                "value"
            )
        ),
        "quantity": item["quantity"],
        "location": SOURCE_LABELS.get(item["source_kind"], "Unknown"),
        "locked": bool(int(item.get("state") or 0) & 1),
        "masterworked": bool(int(item.get("state") or 0) & 4),
        "crafted": bool(int(item.get("state") or 0) & 8),
        "instance_id": item.get("item_instance_id"),
        "first_seen_at": item["first_seen_at"],
        "last_seen_at": item["last_seen_at"],
        "stats": stats,
        "sockets": sockets,
        "perks": perks,
    }


def build_notice(
    request: web.Request,
    inventory: dict[str, Any] | None,
    manifest_status: dict[str, Any],
) -> str:
    messages: list[str] = []
    if error := request.query.get("error"):
        messages.append(f'<div class="notice error">{escape(error)}</div>')
    if notice := request.query.get("notice"):
        messages.append(f'<div class="notice success">{escape(notice)}</div>')
    if inventory and inventory["snapshot"].get("sync_status") == "refresh_failed":
        last_error = inventory["snapshot"].get("last_error")
        detail = f" {escape(last_error)}" if last_error else ""
        messages.append(
            '<div class="notice error">The latest inventory refresh failed; '
            f"this page is showing the last complete snapshot.{detail} "
            '<a href="/data/status">Review synchronization</a></div>'
        )
    if (
        manifest_status.get("status") == "update_failed"
        and manifest_status["available"]
        and manifest_status.get("last_error")
    ):
        messages.append(
            '<div class="notice error">The item catalog update failed, so the '
            "previous verified catalog is still in use. "
            f'{escape(manifest_status["last_error"])}</div>'
        )
    if inventory and not manifest_status["available"]:
        manifest_error = (
            manifest_status.get("local_error")
            or manifest_status.get("last_error")
        )
        error_copy = (
            f'<p class="notice-detail">{escape(manifest_error)}</p>'
            if manifest_error
            else ""
        )
        messages.append(
            f"""
<aside class="manifest-callout">
  <div>
    <p class="eyebrow">One-time setup</p>
    <strong>Download Bungie's item definitions</strong>
    <p>Your inventory is safe. Definitions add item names, artwork, perks,
    and stats to this local view.</p>{error_copy}
  </div>
  <form method="post" action="/inventory/manifest/sync">
    {csrf_input(request, "/inventory/manifest/sync")}
    <button class="action primary" type="submit">Prepare item definitions</button>
  </form>
</aside>"""
        )
    return "".join(messages)


def snapshot_age(inventory: dict[str, Any] | None) -> str:
    if inventory is None:
        return "No saved snapshot"
    fetched = parse_iso(inventory["snapshot"]["fetched_at"])
    seconds = max(0, int((datetime.now(UTC) - fetched).total_seconds()))
    if seconds < 60:
        return "Updated just now"
    if seconds < 3600:
        return f"Updated {seconds // 60}m ago"
    if seconds < 86400:
        return f"Updated {seconds // 3600}h ago"
    return f"Updated {seconds // 86400}d ago"


def snapshot_tone(inventory: dict[str, Any] | None) -> str:
    if inventory is None:
        return "muted"
    if inventory["snapshot"].get("sync_status") == "refresh_failed":
        return "failed"
    return (
        "fresh"
        if parse_iso(inventory["snapshot"]["stale_at"]) > datetime.now(UTC)
        else "stale"
    )


def snapshot_label(inventory: dict[str, Any] | None) -> str:
    if inventory is None:
        return "Missing"
    if inventory["snapshot"].get("sync_status") == "refresh_failed":
        return "Refresh failed"
    return "Fresh" if snapshot_tone(inventory) == "fresh" else "Stale"


def display_name(definition: dict[str, Any] | None) -> str:
    if not definition:
        return ""
    display = definition.get("displayProperties")
    if not isinstance(display, dict):
        return ""
    name = display.get("name")
    return name.strip() if isinstance(name, str) else ""


def icon_url(path: Any) -> str:
    if not isinstance(path, str) or not path or any(
        character in path for character in "\"'<>\\\r\n"
    ):
        return ""
    parsed = urlsplit(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or parsed.scheme
        or parsed.netloc
    ):
        return ""
    return urljoin(BUNGIE_ROOT, path)


def item_sort_key(
    definition: dict[str, Any] | None,
    item: dict[str, Any],
) -> tuple[int, str, int]:
    tier = definition.get("inventory", {}).get("tierType", 0) if definition else 0
    return (-int(tier or 0), display_name(definition).lower(), item["id"])


def bucket_order(name: str) -> tuple[int, str]:
    preferred = (
        "Kinetic Weapons",
        "Energy Weapons",
        "Power Weapons",
        "Helmet",
        "Gauntlets",
        "Chest Armor",
        "Leg Armor",
        "Class Armor",
        "Ghost",
        "Vehicle",
        "Ships",
    )
    try:
        return (preferred.index(name), name)
    except ValueError:
        return (len(preferred), name)


def hashes_from_mapping(value: Any, field: str) -> set[int]:
    if not isinstance(value, dict):
        return set()
    hashes = set()
    for key, row in value.items():
        if isinstance(row, dict):
            raw = row.get(field, key)
            try:
                hashes.add(int(raw))
            except (TypeError, ValueError):
                pass
    return hashes


def hashes_from_rows(value: Any, field: str) -> set[int]:
    if not isinstance(value, list):
        return set()
    hashes = set()
    for row in value:
        if not isinstance(row, dict):
            continue
        try:
            raw = int(row.get(field, 0))
        except (TypeError, ValueError):
            continue
        if raw:
            hashes.add(raw)
    return hashes


def visible_named_hash_rows(
    rows: Any,
    field: str,
    definitions: dict[int, dict[str, Any]],
    *,
    visible_field: str,
    active_field: str,
    inactive_label: str,
) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    output: list[dict[str, Any]] = []
    positions: dict[int, int] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get(visible_field) is False:
            continue
        try:
            item_hash = int(row.get(field, 0))
        except (TypeError, ValueError):
            continue
        name = display_name(definitions.get(item_hash))
        if not item_hash or not name:
            continue
        inactive = row.get(active_field) is False
        if item_hash in positions:
            # The same definition can appear in more than one component row.
            # Treat it as active if any visible occurrence is active.
            if not inactive:
                output[positions[item_hash]]["status"] = None
            continue
        positions[item_hash] = len(output)
        output.append(
            {
                "name": name,
                "hash": item_hash,
                "status": inactive_label if inactive else None,
            }
        )
    return output


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def render_template(template_name: str, **values: str) -> str:
    from destiny_web_app.ui import render_header

    values.setdefault("header", render_header(template_name, values))
    template = Template(
        (TEMPLATE_ROOT / template_name).read_text(encoding="utf-8")
    )
    return template.substitute(values)
