import time
import json
import os
import enum
from sqlitedict import SqliteDict
from pathlib import Path
from .definitions import *

# get location of this file
current_dir = Path(__file__).parent

manifest_file_loc = str((current_dir / manifest_file_name).resolve())
exotic_hashes_file_loc = str((current_dir / exotic_hashes_file_name).resolve())
bucket_hashes_file_loc = str((current_dir / bucket_hashes_file_name).resolve())
ids_to_hash_file_loc = str((current_dir / ids_to_hash_file_name).resolve())
data_base_file_loc = str((current_dir / data_base_file_name).resolve())



itemHashValues = ["kinetic", "energy", "power", "helmet", "gauntlets", "chest", "leg", "class_item", "general"]
manifest = None
exotic_item_hashes = None
bucketHashes = None
ids_to_hash = None




class db:
    '''
    this is the database context manager
    Database Organization:


    '''
    def __init__(self, tablename):
        if not isinstance(tablename, db.Tables):
            raise TypeError("tablename must be of type db.Tables")
        self._tablename = tablename.value

    def __enter__(self):
        self.conn = SqliteDict(data_base_file_loc,
                               tablename=self._tablename,
                               autocommit=True,
                               outer_stack=verbose_mode,
                               encode=json.dumps,
                               decode=json.loads)
        return self.conn

    def __exit__(self, exc_type, exc_value, traceback):
        self.conn.close()

    class Tables(enum.Enum):
        loadouts = "loadouts"
        items = "items"
        users = "users"


def getBucketHashes():
    global bucketHashes
    if not bucketHashes:
        with open(bucket_hashes_file_loc, 'r') as f:
            bucketHashes = enum.Enum('bucketHashes', json.load(f))
    return bucketHashes


def getManifest():
    global manifest
    if not manifest:
        with open(manifest_file_loc, 'r') as f:
            manifest = json.load(f)
    return manifest


def getExoticItemHashes():
    global exotic_item_hashes
    if not exotic_item_hashes:
        with open(exotic_hashes_file_loc, 'r') as f:
            exotic_item_hashes = set(json.load(f))
    return exotic_item_hashes

def getItemHashValues():
    global itemHashValues
    if not itemHashValues:
        itemHashValues = ["kinetic", "energy", "power", "helmet", "gauntlets", "chest", "leg", "class_item"]
    return itemHashValues

def instance_id_to_hash(instance_id):
    return ids_to_hash.get(instance_id, None)
    

async def initialize(client, app):
    global manifest, exotic_item_hashes, bucketHashes, ids_to_hash
    if os.path.exists(manifest_file_loc):
        last_updated = os.path.getmtime(manifest_file_loc)
        if time.time() - last_updated > 86400:
            async with client.acquire() as rest:
                print("Manifest out of date, updating...")
                os.remove(manifest_file_loc)
                os.remove(exotic_hashes_file_loc)
                await rest.download_json_manifest()
    else:
        if not os.path.exists(os.path.dirname(manifest_file_loc)):
            os.makedirs(os.path.dirname(manifest_file_loc))
        async with client.acquire() as rest:
            print("Manifest not found, downloading...")
            await rest.download_json_manifest(manifest_file_loc[:-5])
    with open(manifest_file_loc, 'r') as f:
        print("loading manifest from file", manifest_file_loc)
        manifest = app['manifest'] = json.load(f)
    if os.path.exists(exotic_hashes_file_loc):
        with open(exotic_hashes_file_loc, 'r') as f:
            exotic_item_hashes = set(json.load(f))
    else:
        item_definitions = manifest["DestinyInventoryItemDefinition"]
        exotic_item_hashes = []
        for item_hash, item_data in item_definitions.items():
            if 'inventory' in item_data and item_data['inventory'].get('tierType') == 6:
                exotic_item_hashes.append(int(item_hash))
        with open(exotic_hashes_file_loc, 'w') as f:
            json.dump(exotic_item_hashes, f)
    if os.path.exists(bucket_hashes_file_loc):
        with open(bucket_hashes_file_loc, 'r') as f:
            bucketHashes = json.load(f)
    else:
        temp = {e['displayProperties']['name'].split(" ", 1)[0].lower(): e['hash'] for e in
                manifest['DestinyInventoryBucketDefinition'].values() if 'name' in e['displayProperties']}
        temp["class_item"] = temp.pop("class")
        temp.pop("")
        bucketHashes = temp
        with open(bucket_hashes_file_loc, 'w') as f:
            json.dump(bucketHashes, f)
    if os.path.exists(ids_to_hash_file_loc):
        with open(ids_to_hash_file_loc, 'r') as f:
            ids_to_hash = json.load(f)
    else:
        ids_to_hash = {}
        with open(ids_to_hash_file_loc, 'w') as f:
            json.dump(ids_to_hash, f)   
    bucketHashes = enum.Enum('bucketHashes', bucketHashes)


def getBucketHash(itemHash):
    if 'bucketTypeHash' not in getManifest()['DestinyInventoryItemDefinition'][str(itemHash)]['inventory']: return None
    return getManifest()['DestinyInventoryItemDefinition'][str(itemHash)]['inventory']['bucketTypeHash']

def sleep():
    time.sleep(.1)


def is_item_locked(item_state):
    # Locked state is represented by the 1st bit (value of 1) in the item state bitmask
    LOCKED_STATE_MASK = 0b00000001  # The first bit represents locked state
    return (item_state & LOCKED_STATE_MASK) != 0


def is_item_exotic(item_hash):
    return item_hash in getExoticItemHashes()
