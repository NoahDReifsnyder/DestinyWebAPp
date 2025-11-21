## This file is the Light Level Companion. **name is WIP**. It will keep highest light level items in inventory.


from aiohttp import web
import aiobungie
import enum
import asyncio
from webApp.destiny_helpers import *
from webApp.auth import router
import traceback

def filter_removable_items(char_inv, char_equipped, item_hash_dict):
    removal_queue = []

    for inv_slot in item_hash_dict:
        slot_inv = [x for x in char_inv if x['itemHash'] == item_hash_dict[inv_slot]]
        iter = -1
        while len(slot_inv) >= 8:
            item = slot_inv[iter]
            if (
                not is_item_locked(item['state']) and
                not any(x for x in char_equipped if x['itemHash'] == item['itemHash'])
            ):
                removal_queue.append(item)
                continue
            iter -= 1
            if -1 * iter == len(slot_inv):
                print(f"No more items to remove, please fix {inv_slot}")
                break

    return removal_queue



async def clean_inventory_loop(app, user, access_token, original_inventories, interval_seconds=20):
    print("Entered clean_inventory_loop")
    client: aiobungie.RESTPool = app["client"]
    print(app["users"].keys())
    print(user)

    membership_id = user["destinyMemberships"][0]["membershipId"]
    print(membership_id)
    membership_type = user["destinyMemberships"][0]["membershipType"]
    character_ids = user["destinyMemberships"][0]["characterIds"]

    # 🔁 Loop
    while True:
        try:
            print("Before Sleep")
            await asyncio.sleep(interval_seconds)

            print("Running inventory cleanup...")
            for c_id in character_ids:
                async with client.acquire() as rest:
                    character = await rest.fetch_character(
                        membership_id,
                        membership_type,
                        c_id,
                        [
                            aiobungie.ComponentType.CHARACTER_INVENTORY,
                            aiobungie.ComponentType.CHARACTER_EQUIPMENT,
                        ],
                        access_token
                    )

                char_inv = character["inventory"]["data"]["items"]

                # Filter out items not in original snapshot
                new_items = [
                    item for item in char_inv
                    if item not in original_inventories[c_id]
                ]
                print(new_items)
                new_items = [x for x in new_items if x['bucketHash'] in getInventoryHashDict().values()]
                print(new_items)
                for item in new_items:
                    print(f"Transferring {item['itemHash']} from {c_id}")
                    await rest.transfer_item(access_token, item['itemInstanceId'], item['itemHash'], c_id,
                                             user["destinyMemberships"][0]["membershipType"], vault=True)
                await refresh_tokens(app, membership_id)

        except aiobungie.error.HTTPError as e:
            await refresh_tokens(app, user["mem_id"])
        except Exception as e:
            traceback.print_exc()

            # Here you'd call your remove/send-to-vault function
            # for item in removal_candidates:
            #     await rest.transfer_item(...)

@router.get("/stop_cleaner")
async def stop_cleaner(request: web.Request) -> web.Response:
    mem_id = request.query.get("mem_id")
    app = request.app

    task = app["cleaners"].pop(mem_id, None)
    if task:
        task.cancel()
        return web.Response(text="Cleaner stopped.")
    return web.Response(text="No cleaner was running.")



@router.get("/LLC")
async def LLC(request: web.Request) -> web.Response:
    print("HERE")
    app = request.app
    print("Fetching my user.")
    client: aiobungie.RESTPool = request.app["client"]
    mem_id = request.query.get("mem_id")
    if not mem_id:
        print("No mem_id found")
        return await oauth(client)

    if access_token := app['users'][mem_id].get("access_token"):
        # Fetch our current Bungie.net user.
        async with client.acquire() as rest:
            if not (user :=  app['users'][mem_id].get("user")):
                app['users'][mem_id]["user"] = user = await rest.fetch_current_user_memberships(access_token)
            profile = await rest.fetch_profile(
                user["destinyMemberships"][0]["membershipId"],
                user["destinyMemberships"][0]["membershipType"],
                [
                    aiobungie.ComponentType.PROFILE_INVENTORIES,
                    aiobungie.ComponentType.PROFILE,
                ],
                access_token
            )
            vault_inv = profile['profileInventory']['data']['items']
            user["destinyMemberships"][0]["characterIds"] = character_ids = profile['profile']['data']['characterIds']

            #create a freeze of the current inventory
            c_invs = {}
            for c_id in character_ids:
                character = await rest.fetch_character(
                    user["destinyMemberships"][0]["membershipId"],
                    user["destinyMemberships"][0]["membershipType"],
                    c_id,
                    [
                        aiobungie.ComponentType.CHARACTER_INVENTORY,
                        aiobungie.ComponentType.CHARACTER_EQUIPMENT
                    ],
                    access_token
                )
                c_invs[c_id] = char_inv = [x for x in character['inventory']['data']['items']]
                char_equipped = [x for x in character['equipment']['data']['items']]
                item_hash_dict = getInventoryHashDict()
                removal_queue = []
                for inv_slot in item_hash_dict:
                    slot_inv = [x for x in char_inv if x['bucketHash'] == item_hash_dict[inv_slot]]
                    iter = -1
                    print(inv_slot, len(slot_inv))
                    count = len(slot_inv)
                    while count >= 7:
                        #pick random item that isn't equipped, preferably not locked
                        item = slot_inv[iter]
                        if not is_item_locked(item['state']) and not [x for x in char_equipped if x['itemHash'] == item['itemHash']]:
                            removal_queue.append(item)
                            iter -=1
                            count -= 1
                            continue
                        iter -= 1
                        print(iter, -1*iter, count)
                        if -1*iter == count:
                            print("No more items to remove, please fix "+str(inv_slot))
                            count -= 1
                            continue
                print(removal_queue)
                for item in removal_queue:
                    print(item)
                    await rest.transfer_item(access_token, item['itemInstanceId'], item['itemHash'], c_id,
                                             user["destinyMemberships"][0]["membershipType"], vault=True)
                    char_inv.remove(item)
        if mem_id in app["cleaners"]:
            app["cleaners"][mem_id].cancel()

        # Start new cleaner loop
        task = asyncio.create_task(
            clean_inventory_loop(app, user, access_token, c_invs)
        )
        app["cleaners"][mem_id] = task
        return web.json_response(user)
    else:
        # Otherwise return unauthorized if no access token found.
        return web.json_response({"No access token found, Unauthorized."}, status=401)


@router.get("/LLD")
async def LLC(request: web.Request) -> web.Response:

    print("HERE")
    app = request.app
    client: aiobungie.RESTPool = app["client"]
    mem_id = request.query.get("mem_id")

    if not mem_id:
        print("No mem_id found")
        return await oauth(client)

    if access_token := app['users'][mem_id].get("access_token"):
        # Fetch our current Bungie.net user.
        async with client.acquire() as rest:
            if not (user := app['users'][mem_id].get("user")):
                app['users'][mem_id]["user"] = user = await rest.fetch_current_user_memberships(access_token)
            membership_id = user["destinyMemberships"][0]["membershipId"]
            membership_type = user["destinyMemberships"][0]["membershipType"]
            profile = await rest.fetch_profile(
                membership_id,
                membership_type,
                [
                    aiobungie.ComponentType.PROFILE_INVENTORIES,
                    aiobungie.ComponentType.PROFILE,
                ],
                access_token
            )
            vault_inv = profile['profileInventory']['data']['items']
            character_ids = profile['profile']['data']['characterIds']

            # Initial snapshot
            original_inventories = {}

            for c_id in character_ids:
                character = await rest.fetch_character(
                    membership_id, membership_type, c_id,
                    [
                        aiobungie.ComponentType.CHARACTER_INVENTORY,
                        aiobungie.ComponentType.CHARACTER_EQUIPMENT,
                    ],
                    access_token
                )
                original_inventories[c_id] = [
                    item["itemInstanceId"] for item in character["inventory"]["data"]["items"]
                ]

            print("Inventory snapshot saved.")

            # Cancel old cleaner if it exists
            if mem_id in app["cleaners"]:
                app["cleaners"][mem_id].cancel()

            # Start new cleaner loop
            task = asyncio.create_task(
                clean_inventory_loop(rest, user, access_token, original_inventories)
            )
            app["cleaners"][mem_id] = task

            return web.Response(text="LLC cleaner started.")





