# This file handles the Vault Clearing Assistant. Uses auth.py to get the user's access token

from aiohttp import web
import aiobungie
import enum
from webApp.destiny_helpers import *
from webApp.auth import router

async def pullPMhelper(client, mem_id, users, manifest):
    async with (client.acquire() as rest):
        access_token = users[mem_id].get("access_token")
        users[mem_id]["user"] = user = await rest.fetch_current_user_memberships(access_token)
        character_ids = await rest.fetch_profile(
            user["destinyMemberships"][0]["membershipId"],
            user["destinyMemberships"][0]["membershipType"],
            [
                aiobungie.ComponentType.PROFILE_INVENTORIES,
                aiobungie.ComponentType.PROFILE,
            ],
            access_token
        )
        users[mem_id]["c_ids"] = character_ids = character_ids['profile']['data']['characterIds']
        class_names = {}
        for c_id in character_ids:
            users[mem_id][c_id] = {}
            character = await rest.fetch_character(
                user["destinyMemberships"][0]["membershipId"],
                user["destinyMemberships"][0]["membershipType"],
                c_id,
                [
                    aiobungie.ComponentType.CHARACTERS,
                ],
                access_token
            )
            users[mem_id][c_id]['class'] = \
                class_name = \
                manifest['DestinyClassDefinition'][str(character['character']['data']['classHash'])][
                    'displayProperties'][
                    'name']
            html = "<html><body>{body}</body></html>"
            mem_id_str = "mem_id=" + mem_id
            body = ""
            a_template = "<a href='/pullPM?char_token={class_id}&"+mem_id_str+"'>{class_name}</a>"
        for c_id, name in [(c_id, users[mem_id][c_id]['class']) for c_id in character_ids]:
            # build html body with links to pull postmaster for each character
            body += a_template.format(class_name=name, class_id=c_id)
            # c_inv = [x for x in character['inventory']['data']['items'] if x['bucketHash'] == bucketHashes.postmaster.value and not int(f'{x['state']:08b}'[-1])]
            # for item in c_inv:
            #     await rest.pull_item(access_token, item['itemInstanceId'], item['itemHash'], c_id, user["destinyMemberships"][0]["membershipType"])
        return web.Response(text=html.format(body=body), content_type='text/html')


async def pullPMchar(request, char_token, mem_id, users, bucketHashes):
    client: aiobungie.RESTPool = request.app["client"]
    access_token = users[mem_id].get("access_token")
    async with client.acquire() as rest:
        user = users[mem_id].get("user")
        character = await rest.fetch_character(
            user["destinyMemberships"][0]["membershipId"],
            user["destinyMemberships"][0]["membershipType"],
            char_token,
            [
                aiobungie.ComponentType.CHARACTER_INVENTORY
            ],
            access_token
        )
        c_invs = {y.value: [x for x in character['inventory']['data']['items'] if x['bucketHash'] == y.value] for y in bucketHashes}
        moved = []
        for item in c_invs[bucketHashes.postmaster.value]:
            if len(c_invs[getBucketHash(item['itemHash'], request.app["manifest"])]) < 9:
                await rest.pull_item(access_token, item['itemInstanceId'], item['itemHash'], char_token, user["destinyMemberships"][0]["membershipType"])
            else:
                unlocked_items = [x for x in c_invs[getBucketHash(item['itemHash'])] if not int(f'{x['state']:08b}'[-1]) and x not in moved]
                if unlocked_items:
                    unlocked_item = unlocked_items[0]
                    moved.append(unlocked_item)
                    await rest.transfer_item(access_token, unlocked_item['itemInstanceId'], unlocked_item['itemHash'], char_token, user["destinyMemberships"][0]["membershipType"], vault=True)
                    sleep()
                    await rest.pull_item(access_token, item['itemInstanceId'], item['itemHash'], char_token, user["destinyMemberships"][0]["membershipType"])
                else:
                    await rest.transfer_item(access_token, c_invs[getBucketHash(item['itemHash'])][0]['itemInstanceId'], c_invs[getBucketHash(item['itemHash'])][0]['itemHash'], char_token, user["destinyMemberships"][0]["membershipType"], vault=True)
                    sleep()
                    await rest.pull_item(access_token, item['itemInstanceId'], item['itemHash'], char_token, user["destinyMemberships"][0]["membershipType"])
                    sleep()
                    await rest.transfer_item(access_token, item['itemInstanceId'], item['itemHash'], char_token, user["destinyMemberships"][0]["membershipType"], vault=True)
                    sleep()
                    await rest.transfer_item(access_token, c_invs[getBucketHash(item['itemHash'])][0]['itemInstanceId'], c_invs[getBucketHash(item['itemHash'])][0]['itemHash'], char_token, user["destinyMemberships"][0]["membershipType"])
    users[mem_id]['direct'] = None
    return await home(request)
# Pull from postmaster
@router.get("/pullPM")
async def pullPM(request: web.Request, mem_id=None) -> web.Response:
    app = request.app
    client: aiobungie.RESTPool = request.app["client"]
    # Check our pool storage if it has the tokens stored.
    if not mem_id:
        mem_id = request.query.get("mem_id")
        app['users'][mem_id]["direct"] = lambda m_id: pullPM(request, mem_id=m_id)
        return await oauth(client)
    if access_token := app['users'][mem_id].get("access_token"):
        if char_token := request.query.get("char_token"):
            print("Char Token Found")
            return await pullPMchar(request, char_token, mem_id)
        else:
            print("Char Token Not Found")
            return await pullPMhelper(client, mem_id)

    else:
        # Otherwise return unauthorized if no access token found.
        return web.json_response({"No access token found, Unauthorized."}, status=401)


# Vault Clearning Assistant
@router.get("/VCA")
async def VCA(request: web.Request) -> web.Response:
    app = request.app
    print("Fetching my user.")
    client: aiobungie.RESTPool = request.app["client"]
    mem_id = request.query.get("mem_id")
    if not mem_id:
        print("No mem_id found")
        return await oauth(client)
    # Check our pool storage if it has the tokens stored.
    if access_token := app['users'][mem_id].get("access_token"):
        # Fetch our current Bungie.net user.
        async with client.acquire() as rest:
            if not (user :=  app['users'][mem_id].get("user")):
                app['users'][mem_id]["user"] = user = await rest.fetch_current_user_memberships(access_token)
            character_ids = await rest.fetch_profile(
                user["destinyMemberships"][0]["membershipId"],
                user["destinyMemberships"][0]["membershipType"],
                [
                    aiobungie.ComponentType.PROFILE_INVENTORIES,
                    aiobungie.ComponentType.PROFILE,
                ],
                access_token
            )
            inv = character_ids['profileInventory']['data']['items']
            # for item in inv:
            #     inv_info = await rest.fetch_inventory_item(item['itemHash'])
            #     print(inv_info)
            character_ids = character_ids['profile']['data']['characterIds']
            best_score = 100
            best_cid = None
            need_empty = []
            c_invs = {}
            c_inv = []
            for c_id in character_ids:
                character = await rest.fetch_character(
                    user["destinyMemberships"][0]["membershipId"],
                    user["destinyMemberships"][0]["membershipType"],
                    c_id,
                    [
                        aiobungie.ComponentType.CHARACTER_INVENTORY,
                    ],
                    access_token
                )
                c_invs[c_id] = char_inv = [x for x in character['inventory']['data']['items']]
                primaries = [x for x in char_inv if x['bucketHash'] == getBucketHashes().kinetic.value]
                energies = [x for x in char_inv if x['bucketHash'] == getBucketHashes().energy.value]
                heavies = [x for x in char_inv if x['bucketHash'] == getBucketHashes().power.value]
                helms = [x for x in char_inv if x['bucketHash'] == getBucketHashes().helmet.value]
                gauntlets = [x for x in char_inv if x['bucketHash'] == getBucketHashes().gauntlets.value]
                chests = [x for x in char_inv if x['bucketHash'] == getBucketHashes().chest.value]
                legs = [x for x in char_inv if x['bucketHash'] == getBucketHashes().leg.value]
                class_items = [x for x in char_inv if x['bucketHash'] == getBucketHashes().class_item.value]
                buckets = [primaries, energies, heavies, helms, gauntlets, chests, legs, class_items]
                buckets = [helms, gauntlets, chests, legs, class_items]
                c_inv = c_inv + [(x,c_id) for y in buckets for x in y if not is_item_locked(x['state'])]
                score = len([x for x in buckets if len(x) == 9])
                if score < best_score:
                    best_score = score
                    best_cid = c_id
                    need_empty = [x[0]['bucketHash'] for x in buckets if len(x) == 9]
                # int(f'{x['state']:08b}'[-1]) == 0 means unlocked (bit 1 of state is off)
            for h in need_empty:
                unlocked = [x for x in c_invs[best_cid] if not is_item_locked(x['state']) and x['bucketHash'] == h]
                if not unlocked:
                    unlocked = [x for x in c_invs[best_cid] if x['bucketHash'] == h]
                item = unlocked[0]
                await rest.transfer_item(access_token, item['itemInstanceId'], item['itemHash'], best_cid,
                                         user["destinyMemberships"][0]["membershipType"], vault=True)
                c_invs[best_cid].remove(item)
            temp = getBucketHashes()
            items = [x for x in inv if
                     not is_item_locked(x['state']) and x['bucketHash'] in [temp[x].value for x in itemHashValues]]
            #sort by if exotic
            bucket_hash_values = [temp[e].value for e in itemHashValues]
            items = [x for x in inv if
                     not is_item_locked(x['state']) and x['bucketHash'] in bucket_hash_values]
            num_items = len(items + c_inv)
            exotic_items = [x for x in items if is_item_exotic(x['itemHash'])]
            items = [x for x in items if not is_item_exotic(x['itemHash'])]
            exotic_c_inv = [x for x in c_inv if is_item_exotic(x[0]['itemHash'])]
            c_inv = [x for x in c_inv if not is_item_exotic(x[0]['itemHash'])]





            count = 0
            for item, c_id in exotic_c_inv:
                await rest.transfer_item(access_token, item['itemInstanceId'], item['itemHash'], c_id,
                                         user["destinyMemberships"][0]["membershipType"], vault=True)
                count += 1
                # print progress
                print(f"{count}/{num_items}")
            for item in exotic_items:
                print(item)
                await rest.transfer_item(access_token, item['itemInstanceId'], item['itemHash'], best_cid,  user["destinyMemberships"][0]["membershipType"])
                sleep()
                await rest.transfer_item(access_token, item['itemInstanceId'], item['itemHash'], best_cid,
                                         user["destinyMemberships"][0]["membershipType"], vault=True)
                count += 1
                # print progress
                print(f"{count}/{num_items}")
            for item, c_id in c_inv:
                await rest.transfer_item(access_token, item['itemInstanceId'], item['itemHash'], c_id,
                                         user["destinyMemberships"][0]["membershipType"], vault=True)
                count += 1
                # print progress
                print(f"{count}/{num_items}")
            for item in items:
                await rest.transfer_item(access_token, item['itemInstanceId'], item['itemHash'], best_cid,
                                         user["destinyMemberships"][0]["membershipType"])
                sleep()
                await rest.transfer_item(access_token, item['itemInstanceId'], item['itemHash'], best_cid,
                                         user["destinyMemberships"][0]["membershipType"], vault=True)
                count += 1
                # print progress
                print(f"{count}/{num_items}")
        # Return a JSON response.
        return web.json_response(user)
    else:
        # Otherwise return unauthorized if no access token found.
        return web.json_response({"No access token found, Unauthorized."}, status=401)