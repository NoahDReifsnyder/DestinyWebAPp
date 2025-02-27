import json
import sqlite3
from aiohttp import web
import aiobungie
from webApp.destiny_helpers import *
from webApp.auth import router
from multiprocessing.pool import ThreadPool

loadouts = {}

class Loadout:
    def __init__(self, name, character_id, items):
        self.name = name
        self.character_id = character_id
        self.items = items


class Item:
    def __init__(self, item_hash, item_instance_id, state, plugs=[]):
        self.item_hash = item_hash
        self.item_instance_id = item_instance_id
        self.state = state
        self.plugs = plugs

class Plug:
    def __init__(self, plug_hash, plug_instance_id, state):
        self.plug_hash = plug_hash


@router.get("/loadout_landing")
async def loadout_landing(request: web.Request) -> web.Response:
    mem_id = request.query.get("mem_id")
    # get char ids, and provide user with a choice of their characters
    app = request.app
    client: aiobungie.RESTPool = request.app["client"]
    access_token = request.app["users"][mem_id]["access_token"]

    async with client.acquire() as rest:
        if not (user := app['users'][mem_id].get("user")):
            app['users'][mem_id]["user"] = user = await rest.fetch_current_user_memberships(access_token)
        if not "c_ids" in user:
            profile = await rest.fetch_profile(
                user["destinyMemberships"][0]["membershipId"],
                aiobungie.MembershipType.STEAM,
                [
                    aiobungie.ComponentType.PROFILE,
                ],
                access_token)
            user["c_ids"] = character_ids = profile['profile']['data']['characterIds']
        else:
            character_ids = user["c_ids"]
        class_names = {}
        manifest = request.app["manifest"]
        for c_id in character_ids:
            user[c_id] = {}
            character = await rest.fetch_character(
                user["destinyMemberships"][0]["membershipId"],
                user["destinyMemberships"][0]["membershipType"],
                c_id,
                [
                    aiobungie.ComponentType.CHARACTERS,
                ],
                access_token
            )
            user[c_id]['class'] = \
                class_name = \
                manifest['DestinyClassDefinition'][str(character['character']['data']['classHash'])][
                    'displayProperties'][
                    'name']
            html = "<html><body>{body}</body></html>"
            mem_id_str = "mem_id=" + mem_id
            body = ""
            a_template = "<a href='/loadout?c_id={class_id}&" + mem_id_str + "'>{class_name}</a>"
        for c_id, name in [(c_id, user[c_id]['class']) for c_id in character_ids]:
            body += a_template.format(class_name=name, class_id=c_id) + "<br>"
        return web.Response(text=html.format(body=body), content_type='text/html')


@router.post("/loadout/{mem_id}/{c_id}")
async def post_loadouts(request: web.Request) -> web.Response:
    pass

# Loadout management router
@router.get("/loadout/{mem_id}/{c_id}")
async def get_loadouts(request: web.Request) -> web.Response:
    mem_id = request.match_info["mem_id"]
    c_id = request.match_info["c_id"]
    if not mem_id:
       return web.json_response({"error": "membership_id is required"}, status=400)
    if not c_id:
       return web.json_response({"error": "character_id is required"}, status=400)
    app = request.app
    client: aiobungie.RESTPool = request.app["client"]
    access_token = request.app["users"][mem_id]["access_token"]

    # Example: Fetch and save a loadout
    membership_type = aiobungie.MembershipType.STEAM  # Replace with your membership type
    async with client.acquire() as rest:
        if not (user := app['users'][mem_id].get("user")):
            app['users'][mem_id]["user"] = user = await rest.fetch_current_user_memberships(access_token)
        loadout = await rest.fetch_profile(
            user["destinyMemberships"][0]["membershipId"],
            user["destinyMemberships"][0]["membershipType"],
            [
                aiobungie.ComponentType.CHARACTER_LOADOUTS,
                aiobungie.ComponentType.CHARACTER_EQUIPMENT,
                aiobungie.ComponentType.CHARACTER_INVENTORY,
                aiobungie.ComponentType.PROFILE_INVENTORIES,
            ],
            access_token)
    with db(db.Tables.loadouts) as conn:
        conn[c_id]=loadout['characterLoadouts']['data'][c_id]['loadouts']

    #print(loadout)
    l = loadout['characterLoadouts']['data'][c_id]['loadouts'][0]
    items = loadout['characterEquipment']['data'][c_id]['items']
    all_loadouts = loadout['characterLoadouts']['data']
    current = loadout['characterEquipment']['data'][c_id]['items']
    item_ids = [x['itemInstanceId'] for x in l['items']]
    all_items = loadout['profileInventory']['data']['items'] + [x for c_id in loadout['characterEquipment']['data'] for x in loadout['characterEquipment']['data'][c_id]['items']] + [x for c_id in loadout['characterInventories']['data'] for x in loadout['characterInventories']['data'][c_id]['items']]
    all_instance_ids = [x['itemInstanceId'] for x in all_items if 'itemInstanceId' in x]
    #find exotics
    exotic_instance_ids = [x['itemInstanceId'] for x in all_items if 'itemInstanceId' in x and x['itemInstanceId'] in item_ids and is_item_exotic(x['itemHash'])]
    r = await rest.equip_items(access_token, [x for x in item_ids if x not in exotic_instance_ids], c_id, user["destinyMemberships"][0]["membershipType"])
    r = await rest.equip_items(access_token, exotic_instance_ids, c_id, user["destinyMemberships"][0]["membershipType"])


    # Save current loadout
    # await save_in_game_loadout(client, conn, membership_type, mem_id, character_id, "In-Game PvE Loadout", access_token)
    #
    # # Load saved loadouts
    # loadouts = load_loadouts(conn, mem_id)
    # print("Saved loadouts:", loadouts)
    #
    # # Apply a saved loadout
    # if loadouts:
    #     await apply_loadout(client, loadouts[0], access_token)

    return web.json_response(loadout['characterLoadouts']['data'][c_id]['loadouts'])