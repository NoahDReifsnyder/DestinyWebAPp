# This module manages user perk preferences across mainly guns, but maybe also armor.
# use these preferences mainly for reducing vault space, but also for suggesting
# guns to use in certain activities.
from webApp.destiny_helpers import *
from webApp.auth import router
import pdb
import asyncio

data_base_file_name = "perks.db"

'''
perk database schema:
UserID
ItemHash
list(perk combos)
'''


def get_perk_columns(item):
    options = get_weapon_perk_options(item)
    print(options)


def perk_picker(user, item):
    # pick perks for this user on this item
    # return list of perk combos to keep
    # save to db
    perks = get_possible_perks(item['itemHash'])
    pdb.set_trace()
    with Database(data_base_file_name) as db:
        db[user][item['itemHash']] = []  # placeholder

    return web.Response()


def keep(user, item):
    # decide if we want to keep this item based on perk preferences
    with Database(data_base_file_name) as db:
        if user not in db:
            db[user] = {}
        user_perks = db[user].get(item['itemHash'], None)
        if not user_perks:
            user_perks = perk_picker(user, item)

    return True


from collections import defaultdict

def get_possible_perks2(item_hash):
    """
    Returns a dict mapping socket_index -> dict with socket info and possible perks.
    Each socket dict contains:
        - 'socket_name': description of socket (fallback if empty)
        - 'plug_hashes': list of possible plug hashes
        - 'plug_names': list of plug names for debugging
    """
    manifest = getManifest()
    item_def = manifest.get("DestinyInventoryItemDefinition", {}).get(str(item_hash))
    if not item_def:
        return {}

    possible_perks = {}
    sockets = item_def.get("sockets", {})
    socket_entries = sockets.get("socketEntries", [])

    for index, socket in enumerate(socket_entries):
        # Fallback socket name
        weapon_name = manifest["DestinyInventoryItemDefinition"].get(str(item_hash), {}).get("displayProperties", {}).get("name", str(item_hash))

        socket_name = None
        plug_set_hash = socket.get("randomizedPlugSetHash") or socket.get("reusablePlugSetHash")
        plug_hashes = []
        plug_names = []
        if plug_set_hash:
            plug_set = manifest.get("DestinyPlugSetDefinition", {}).get(str(plug_set_hash))
            if plug_set:
                for entry in plug_set.get("reusablePlugItems", []):
                    plug_hashes.append(entry["plugItemHash"])
                    plug_item_def = manifest.get("DestinyInventoryItemDefinition", {}).get(str(entry["plugItemHash"]))
                    #pdb.set_trace()
                    if not socket_name:
                        socket_name = plug_item_def['itemTypeDisplayName']
                    plug_name = plug_item_def.get("displayProperties", {}).get("name", f"Plug {entry['plugItemHash']}")
                    if "Enhanced" in plug_item_def.get('itemTypeAndTierDisplayName', ''):
                        plug_name += "(Enhanced)"
                    plug_names.append(plug_name)

        if "weapon mod" in (socket_name or "").lower():
            perk_types = {}  # use a set to avoid duplicates

            for plug_name, plug_hash in zip(plug_names, plug_hashes):
                if ":" in plug_name and "optics" not in plug_name.lower():
                    socket_name = "Masterwork"
                    name = plug_name.split(":", 1)[1].strip()
                    if name not in perk_types:
                        perk_types[name] = plug_hash
                else:
                    if plug_name not in perk_types:
                        perk_types[plug_name] = plug_hash
                    # convert back to sorted list if you want
            plug_names = list(perk_types.keys())
            plug_hashes = list(perk_types.values())

        possible_perks[index] = {
            "socket_name": socket_name or f"Socket {index}",
            "plug_hashes": plug_hashes,
            "plug_names": plug_names,}

    return possible_perks

def get_possible_perks(item_hash):
    """
    Returns a dict mapping socket index -> dict with socket info and possible perks.
    Each socket dict contains:
        - 'socket_name': description of socket (fallback if empty)
        - 'plug_names': list of plug names for display
        - 'plug_hashes': list of corresponding plug hashes
    Works for any item type with sockets.
    """
    manifest = getManifest()
    item_def = manifest.get("DestinyInventoryItemDefinition", {}).get(str(item_hash))
    if not item_def:
        return {}

    possible_perks = {}
    sockets = item_def.get("sockets", {})
    socket_entries = sockets.get("socketEntries", [])

    for index, socket in enumerate(socket_entries):
        socket_name = None
        plug_names = []
        plug_hashes = []

        # Determine the plug set(s) for this socket
        plug_set_hash = socket.get("reusablePlugSetHash") or socket.get("randomizedPlugSetHash")
        plugs = []

        # Add plugs from plug set if it exists
        if plug_set_hash:
            plug_set = manifest.get("DestinyPlugSetDefinition", {}).get(str(plug_set_hash), {})
            plugs.extend(plug_set.get("reusablePlugItems", []))

        # Include the default plug if present
        if "singleInitialItemHash" in socket:
            plugs.append({"plugItemHash": socket["singleInitialItemHash"]})

        # Extract plug names and hashes
        for plug in plugs:
            plug_hash = plug["plugItemHash"]
            plug_def = manifest.get("DestinyInventoryItemDefinition", {}).get(str(plug_hash), {})

            # Socket name fallback
            if not socket_name:
                socket_name = plug_def.get("itemTypeDisplayName")

            # Human-readable name
            name = plug_def.get("displayProperties", {}).get("name", f"Plug {plug_hash}")

            # Append "(Enhanced)" if applicable
            if "Enhanced" in plug_def.get("itemTypeAndTierDisplayName", "") and "(Enhanced)" not in name:
                name += " (Enhanced)"

            bad_names = ["empty", "plug"]
            if all(bad not in name.lower() for bad in bad_names):
                plug_names.append(name)
                plug_hashes.append(plug_hash)


        possible_perks[index] = {
            "socket_name": socket_name or f"Socket {index}",
            "plug_names": list(dict.fromkeys(plug_names)),
            "plug_hashes": list(dict.fromkeys(plug_hashes))
        }

    return possible_perks



def print_perks(all_items_dict, manifest):
    for item_hash in all_items_dict:
        perks = all_items_dict[item_hash]['perks']
        weapon_name = manifest["DestinyInventoryItemDefinition"].get(str(item_hash), {}).get("displayProperties",
                                                                                             {}).get("name",
                                                                                                     str(item_hash))
        print(f"Item {weapon_name} ({item_hash}) has perks:")
        for socket_index, data_dict in perks.items():

            plug_hashes = data_dict['plug_hashes']
            plug_names = data_dict['plug_names']
            socket_name = data_dict['socket_name']
            if ("shader" or "weapon mod" or "defaults") in socket_name.lower():
                continue
            print(f"Socket {socket_name} has {len(plug_hashes)} possible perks:")
            for plug_name, plug_hash in zip(plug_names, plug_hashes):
                print(f"  - {plug_name} ({plug_hash})")


def extract_all_weapon_perks(profile_data, manifest):
    """
    Iterates over all characters and returns a list of weapon items.
    """
    all_weapons = []
    all_items = []
    weapons, armor, all_items_dict = {}, {}, {}
    item_holders = ["profileInventory", "characterInventories", "characterEquipment"]
    def get_perks(items):
        for item in items:
            if "itemInstanceId" not in item:
                continue

            bucket_hash = getBucketHash(item["itemHash"])
            if not isGear(bucket_hash):
                continue

            # initialize once per itemHash
            if item["itemHash"] not in all_items_dict:
                perks = get_possible_perks(item["itemHash"])
                all_items_dict[item["itemHash"]] = {"perks": perks, "items": []}
                (weapons if isWeapon(bucket_hash) else armor)[item["itemHash"]] = {
                    "perks": perks,
                    "items": [],
                }

            # add the instance
            all_items_dict[item["itemHash"]]["items"].append(item)
            (weapons if isWeapon(bucket_hash) else armor)[item["itemHash"]]["items"].append(item)

    for holder in item_holders:
        data = profile_data.get(holder, {})['data']
        if "items" in data:
            items = data.get("items", [])
            get_perks(items)
        else:
            for key, value in data.items():
                items = value.get("items", [])
                get_perks(items)
    return weapons, armor



@router.get("/perk_manager")
async def perk_manager(request: web.Request) -> web.Response:
    # When this module is called directly, it should have a section showing the users picked perks, as well as items that haven't had their perks picked yet
    manifest = getManifest()
    print("Perk manager landing")
    app = request.app
    client: aiobungie.RESTPool = app['client']
    if (mem_id := request.cookies.get("mem_id")) and (mem_id in app['users']) and (access_token := app['users'][mem_id].get("access_token")):
        async with client.acquire() as rest:
            if not (user := app['users'][mem_id].get("user")):
                app['users'][mem_id]["user"] = user = await rest.fetch_current_user_memberships(access_token)
            # Load user items and perk preferences
            user_id = user["destinyMemberships"][0]["membershipId"]
            character_items = await rest.fetch_profile(
                user["destinyMemberships"][0]["membershipId"],
                user["destinyMemberships"][0]["membershipType"],
                [
                    aiobungie.ComponentType.PROFILE_INVENTORIES,
                    aiobungie.ComponentType.CHARACTER_INVENTORY,
                    aiobungie.ComponentType.CHARACTER_EQUIPMENT,
                    aiobungie.ComponentType.ITEM_INSTANCES,
                    aiobungie.ComponentType.ITEM_STATS,
                    aiobungie.ComponentType.ITEM_PERKS,
                    aiobungie.ComponentType.ALL,
                ],
                access_token,
            )
            item_holders = ["profileInventory", "characterInventories", "characterEquipment"]
            with Database("testing") as db:
                if user_id not in db:
                    db[user_id] = {}
                if "gear" not in db[user_id]:
                    db[user_id]["gear"] = {}
                gear_data = {}
                for holder in item_holders:
                    data = character_items.get(holder, {}).get("data", {}).get("items", [])
                    for item in data:
                        if isGear(getBucketHash(item["itemHash"])) and not is_item_exotic(item["itemHash"]):
                            if item["itemHash"] not in gear_data:
                              gear_data[item["itemHash"]] = {'instances': []}
                            print(name_from_hash(item["itemHash"]))
                            if isArmor(getBucketHash(item["itemHash"])):
                                stat_block = {}
                                pdb.set_trace()
                                for stat in character_items['itemComponents']['stats']['data'][item['itemInstanceId']]['stats'].values():
                                    stat_hash = stat['statHash']
                                    val = stat['value']
                                    stat_info = manifest['DestinyStatDefinition'].get(str(stat_hash), {})
                                    name = stat_info.get('displayProperties', {}).get('name', 'Unknown Stat')
                                    stat_block[name] = val
                                item['statBlock'] = stat_block
                            gear_data[item["itemHash"]]['instances'].append(item)
                db[user_id]["gear"] = gear_data

            #response = web.HTTPFound(location="/perk_form")
            response = web.HTTPFound(location="/armor_reducer")
            response.set_cookie("user_id", user_id)
            return response

            json.dump(weapon_perks, open("weapon_perks.json", "w"), indent=4)
    else:
        print("ERROR")
        raise web.HTTPFound(location="/")

    return web.Response(text="Perk management placeholder")


if __name__ == '__main__':
    example_item_instance = {
        "itemHash": 1364093401,  # Fatebringer
        "itemInstanceId": "6917529115210791234",
        "quantity": 1,
        "bindStatus": 0,
        "location": 1,
        "bucketHash": 1498876634,
        "transferStatus": 0,
        "lockable": True,
        "state": 0,
        "overrideStyleItemHash": 0,
        "expirationDate": "9999-12-31T23:59:59Z",
        "isWrapper": False,
        "versionNumber": 0,
        "itemLevel": 0,
        "quality": 0,
        "isEquipped": False,
        "energy": {
            "energyTypeHash": 4069572561,
            "energyType": 3,
            "energyCapacity": 10,
            "energyUsed": 8,
            "energyUnused": 2
        },
        "sockets": {
            "socketEntries": [
                # Barrel
                {"socketTypeHash": 1282012138, "randomizedPlugSetHash": 4041097739},
                # Magazine
                {"socketTypeHash": 1282012139, "randomizedPlugSetHash": 2784762653},
                # Trait 1
                {"socketTypeHash": 1282012140, "randomizedPlugSetHash": 1767713192},
                # Trait 2
                {"socketTypeHash": 1282012141, "randomizedPlugSetHash": 2645487691},
                # Masterwork
                {"socketTypeHash": 1282012142, "reusablePlugSetHash": 1234567890},
            ]
        },
        "stats": {
            "stats": {
                "4284893193": {"statHash": 4284893193, "value": 84, "maximumValue": 100},  # Impact
                "4043523819": {"statHash": 4043523819, "value": 55, "maximumValue": 100},  # Range
                "1240592695": {"statHash": 1240592695, "value": 67, "maximumValue": 100},  # Stability
            }
        }
    }

    user_id = "123456789"
    keep(user_id, example_item_instance)
