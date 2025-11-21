import pdb
import time
import json
import os
import enum
from pathlib import Path
from aiohttp import web
import aiobungie

from .definitions import (
    manifest_file_name,
    exotic_hashes_file_name,
    bucket_hashes_file_name,
    ids_to_hash_file_name,
    data_base_file_name,
)

# Directories and file locations
current_dir = Path(__file__).parent
debug = False

manifest_file_loc = str((current_dir / manifest_file_name).resolve())
exotic_hashes_file_loc = str((current_dir / exotic_hashes_file_name).resolve())
bucket_hashes_file_loc = str((current_dir / bucket_hashes_file_name).resolve())
ids_to_hash_file_loc = str((current_dir / ids_to_hash_file_name).resolve())
data_base_file_loc = str((current_dir / data_base_file_name).resolve())
manifest_meta_file = Path(manifest_file_loc).with_suffix('.meta.json')


# Globals
itemHashValues = [
    "kinetic",
    "energy",
    "power",
    "helmet",
    "gauntlets",
    "chest",
    "leg",
    "class_item",
    "general",
]
manifest = None
exotic_item_hashes = None
bucketHashes = None
ids_to_hash = None


# ==========================
# Utility Functions
# ==========================

def getBucketHashes():
    """Return enum of bucket hashes (same structure as before)."""
    global bucketHashes
    if not bucketHashes:
        with open(bucket_hashes_file_loc, "r") as f:
            bucketHashes = enum.Enum("bucketHashes", json.load(f))
    return bucketHashes


def getArmorHashes():
    """Return list of armor bucket hashes (same as before)."""
    bh = getBucketHashes()
    return [
        bh.helmet.value,
        bh.gauntlets.value,
        bh.chest.value,
        bh.leg.value,
        bh.class_item.value,
    ]

def getWeaponHashes():
    bh = getBucketHashes()
    return [
        bh.kinetic.value,
        bh.energy.value,
        bh.power.value,
    ]

def getGearHashes():
    """Return list of all gear bucket hashes (weapons + armor)."""
    return getWeaponHashes() + getArmorHashes()

def isGear(bucket_hash):
    """Check if a bucket hash corresponds to gear (weapons or armor)."""
    return bucket_hash in getGearHashes()

def isWeapon(bucket_hash):
    """Check if a bucket hash corresponds to a weapon."""
    return bucket_hash in getWeaponHashes()

def isArmor(bucket_hash):
    """Check if a bucket hash corresponds to armor."""
    return bucket_hash in getArmorHashes()

def getInventoryHashDict():
    """Return dictionary of inventory hash mappings."""
    with open(bucket_hashes_file_loc, "r") as f:
        inventoryHashDict = json.load(f)
    return {k: inventoryHashDict[k] for k in getItemHashValues()}


def getManifest():
    """Return loaded manifest from cache."""
    global manifest
    if not manifest:
        with open(manifest_file_loc, "r") as f:
            manifest = json.load(f)
    return manifest


def getExoticItemHashes():
    """Return set of exotic item hashes."""
    global exotic_item_hashes
    if not exotic_item_hashes:
        with open(exotic_hashes_file_loc, "r") as f:
            exotic_item_hashes = set(json.load(f))
    return exotic_item_hashes


def getItemHashValues():
    """Return consistent item hash value list."""
    global itemHashValues
    if not itemHashValues:
        itemHashValues = [
            "kinetic",
            "energy",
            "power",
            "helmet",
            "gauntlets",
            "chest",
            "leg",
            "class_item",
        ]
    return itemHashValues


def instance_id_to_hash(instance_id):
    """Return mapped item hash for an instance id."""
    return ids_to_hash.get(instance_id, None)


# ==========================
# Token Management
# ==========================

async def refresh_tokens(app: web.Application, mem_id: str) -> None:
    """Refresh OAuth tokens using aiobungie."""
    client: aiobungie.RESTPool = app["client"]
    refresh_token = app["users"][mem_id]["refresh_token"]
    async with client.acquire() as rest:
        tokens = await rest.refresh_access_token(refresh_token)
        app["users"][mem_id]["access_token"] = tokens.access_token
        app["users"][mem_id]["refresh_token"] = tokens.refresh_token
        print(f"Refreshed token for {mem_id}")


# ==========================
# Manifest Initialization
# ==========================

async def initialize(client, app):
    """
    Initialize manifest, exotic list, and bucket hashes.
    Uses aiobungie for version-aware fetching but maintains JSON cache structure.
    """
    global manifest, exotic_item_hashes, bucketHashes, ids_to_hash

    manifest_dir = os.path.dirname(manifest_file_loc)
    os.makedirs(manifest_dir, exist_ok=True)

    async with client.acquire() as rest:
        print("Checking Bungie manifest...")
        manifest_meta = await rest.fetch_manifest_path()
        remote_version = manifest_meta.get("version")

        local_version = None
        if os.path.exists(manifest_meta_file):
            try:
                with open(manifest_meta_file, "r") as f:
                    local_meta = json.load(f)
                    local_version = local_meta.get("version")
            except Exception:
                print("Local manifest metadata corrupted, rebuilding...")

        if remote_version != local_version:
            print(f"Manifest update detected: {local_version} → {remote_version}")
            await rest.download_json_manifest(str(Path(manifest_file_loc).with_suffix('')))
            with open(manifest_meta_file, "w") as f:
                json.dump({"version": remote_version}, f)
        else:
            print(f"Manifest is current (v{local_version})")

    # Load manifest into memory
    with open(manifest_file_loc, "r") as f:
        print("Loading manifest from file:", manifest_file_loc)
        manifest = app["manifest"] = json.load(f)

    # ==========================
    # Exotic Item Hashes
    # ==========================
    if os.path.exists(exotic_hashes_file_loc):
        with open(exotic_hashes_file_loc, "r") as f:
            exotic_item_hashes = set(json.load(f))
    else:
        print("Building exotic item hash list...")
        item_defs = manifest["DestinyInventoryItemDefinition"]
        exotic_item_hashes = [
            int(item_hash)
            for item_hash, data in item_defs.items()
            if "inventory" in data and data["inventory"].get("tierType") == 6
        ]
        with open(exotic_hashes_file_loc, "w") as f:
            json.dump(exotic_item_hashes, f)

    # ==========================
    # Bucket Hashes
    # ==========================
    if os.path.exists(bucket_hashes_file_loc):
        with open(bucket_hashes_file_loc, "r") as f:
            bucketHashes = json.load(f)
    else:
        print("Building bucket hash map...")
        temp = {
            e["displayProperties"]["name"].split(" ", 1)[0].lower(): e["hash"]
            for e in manifest["DestinyInventoryBucketDefinition"].values()
            if "name" in e["displayProperties"]
        }
        if "class" in temp:
            temp["class_item"] = temp.pop("class")
        temp.pop("", None)
        bucketHashes = temp
        with open(bucket_hashes_file_loc, "w") as f:
            json.dump(bucketHashes, f)

    # ==========================
    # Instance ID to Hash Map
    # ==========================
    if os.path.exists(ids_to_hash_file_loc):
        with open(ids_to_hash_file_loc, "r") as f:
            ids_to_hash = json.load(f)
    else:
        ids_to_hash = {}
        with open(ids_to_hash_file_loc, "w") as f:
            json.dump(ids_to_hash, f)

    # Convert to Enum for convenience
    bucketHashes = enum.Enum("bucketHashes", bucketHashes)

    print("Manifest initialization complete.")


# ==========================
# Inventory Helpers
# ==========================

def snapShotInventory(character):
    """Return snapshot of kinetic inventory items."""
    return [
        x
        for x in character["inventory"]["data"]["items"]
        if x["bucketHash"] == getBucketHashes().kinetic.value
    ]


def getBucketHash(itemHash):
    """Return bucketTypeHash for an item if available."""
    item_def = getManifest()["DestinyInventoryItemDefinition"].get(str(itemHash))
    if not item_def or "inventory" not in item_def:
        return None
    return item_def["inventory"].get("bucketTypeHash")

def getBucketName(bucket_hash=None, item_hash=None):
    """Return bucket name from bucket hash."""
    if not bucket_hash:
        if not item_hash:
            raise ValueError("Either bucket_hash or item_hash must be provided.")
        getBucketHash(item_hash)
    bh = getBucketHashes()
    for name, member in bh.__members__.items():
        if member.value == bucket_hash:
            return name
    return "unknown"

def sleep():
    """Light sleep to avoid rate limits."""
    time.sleep(0.1)


def is_item_locked(item_state):
    """Check if item is locked using bitmask."""
    LOCKED_STATE_MASK = 0b00000001
    return (item_state & LOCKED_STATE_MASK) != 0


def is_item_crafted(item_state):
    """Check if item is crafted using bitmask."""
    CRAFTED_STATE_MASK = 0b00001000
    return (item_state & CRAFTED_STATE_MASK) != 0


def is_special(item):
    """Check if an item is exotic or crafted."""
    return is_item_crafted(item["state"]) or is_item_exotic(item["itemHash"])


def is_item_exotic(item_hash):
    """Check if item is exotic."""
    return item_hash in getExoticItemHashes()


def name_from_hash(item_hash):
    """Return display name of item from hash."""
    return getManifest()["DestinyInventoryItemDefinition"][str(item_hash)]["displayProperties"]["name"]


def class_from_id(c_id):
    """Placeholder for future use."""
    pass


async def get_weapon_perk_options(client: aiobungie.Client, item_hash: int):
    """
    Given a weapon item_hash, returns all possible perk combinations.
    Output: dict of {socket_category: [perk_names]}.
    """
    # Fetch the weapon definition from the manifest
    weapon_def = await client.manifest.fetch(aiobungie.DestinyInventoryItemDefinition, item_hash)

    # Ensure the item has sockets (not all items do)
    if "sockets" not in weapon_def or "socketEntries" not in weapon_def["sockets"]:
        return {}

    perk_data = {}

    # Loop over each socket entry
    for i, socket in enumerate(weapon_def["sockets"]["socketEntries"]):
        plug_set_hash = socket.get("randomizedPlugSetHash") or socket.get("reusablePlugSetHash")
        if not plug_set_hash:
            continue

        # Fetch plug set definition (contains all possible plugs)
        plug_set_def = await client.manifest.fetch(aiobungie.DestinyPlugSetDefinition, plug_set_hash)
        plug_items = plug_set_def.get("reusablePlugItems", [])

        # Extract perk names from each plug item
        perk_names = []
        for plug in plug_items:
            plug_hash = plug["plugItemHash"]
            plug_def = await client.manifest.fetch(aiobungie.DestinyInventoryItemDefinition, plug_hash)

            # Only include visible perks
            if "displayProperties" in plug_def and plug_def["displayProperties"].get("name"):
                name = plug_def["displayProperties"]["name"]
                if name not in perk_names:
                    perk_names.append(name)

        # Give it a readable label
        perk_data[f"Socket {i+1}"] = perk_names

    return perk_data

