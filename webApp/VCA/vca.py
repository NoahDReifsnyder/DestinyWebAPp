# This file handles the Vault Clearing Assistant. Uses auth.py to get the user's access token

from aiohttp import web
import aiobungie
import enum
from webApp.destiny_helpers import *
from webApp.auth import router
import pdb
import asyncio

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
            a_template = "<a href='/pullPM?char_token={class_id}&" + mem_id_str + "'>{class_name}</a>"
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
        c_invs = {y.value: [x for x in character['inventory']['data']['items'] if x['bucketHash'] == y.value] for y in
                  bucketHashes}
        moved = []
        for item in c_invs[bucketHashes.postmaster.value]:
            if len(c_invs[getBucketHash(item['itemHash'], request.app["manifest"])]) < 9:
                await rest.pull_item(access_token, item['itemInstanceId'], item['itemHash'], char_token,
                                     user["destinyMemberships"][0]["membershipType"])
            else:
                unlocked_items = [x for x in c_invs[getBucketHash(item['itemHash'])] if
                                  not int(f'{x['state']:08b}'[-1]) and x not in moved]
                if unlocked_items:
                    unlocked_item = unlocked_items[0]
                    moved.append(unlocked_item)
                    await rest.transfer_item(access_token, unlocked_item['itemInstanceId'], unlocked_item['itemHash'],
                                             char_token, user["destinyMemberships"][0]["membershipType"], vault=True)
                    sleep()
                    await rest.pull_item(access_token, item['itemInstanceId'], item['itemHash'], char_token,
                                         user["destinyMemberships"][0]["membershipType"])
                else:
                    await rest.transfer_item(access_token, c_invs[getBucketHash(item['itemHash'])][0]['itemInstanceId'],
                                             c_invs[getBucketHash(item['itemHash'])][0]['itemHash'], char_token,
                                             user["destinyMemberships"][0]["membershipType"], vault=True)
                    sleep()
                    await rest.pull_item(access_token, item['itemInstanceId'], item['itemHash'], char_token,
                                         user["destinyMemberships"][0]["membershipType"])
                    sleep()
                    await rest.transfer_item(access_token, item['itemInstanceId'], item['itemHash'], char_token,
                                             user["destinyMemberships"][0]["membershipType"], vault=True)
                    sleep()
                    await rest.transfer_item(access_token, c_invs[getBucketHash(item['itemHash'])][0]['itemInstanceId'],
                                             c_invs[getBucketHash(item['itemHash'])][0]['itemHash'], char_token,
                                             user["destinyMemberships"][0]["membershipType"])
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
            if not (user := app['users'][mem_id].get("user")):
                app['users'][mem_id]["user"] = user = await rest.fetch_current_user_memberships(access_token)
            character_ids_dict = await rest.fetch_profile(
                user["destinyMemberships"][0]["membershipId"],
                user["destinyMemberships"][0]["membershipType"],
                [
                    aiobungie.ComponentType.PROFILE_INVENTORIES,
                    aiobungie.ComponentType.PROFILE,
                    aiobungie.ComponentType.ITEM_INSTANCES,
                    aiobungie.ComponentType.ITEM_STATS,
                    aiobungie.ComponentType.CHARACTERS
                ],
                access_token
            )
            gear_tiers = {x: (character_ids_dict['itemComponents']['instances']['data'][x]['gearTier'], None) for x in
                          character_ids_dict['itemComponents']['instances']['data']}
            inv = character_ids_dict['profileInventory']['data']['items']
            # for item in inv:
            #     inv_info = await rest.fetch_inventory_item(item['itemHash'])
            #     print(inv_info)
            character_ids = character_ids_dict['profile']['data']['characterIds']
            CLASS_BY_TYPE = {0: "Titan", 1: "Hunter", 2: "Warlock"}

            chars = character_ids_dict["characters"]["data"]
            char_classes = {cid: CLASS_BY_TYPE.get(data["classType"]) for cid, data in chars.items()}
            best_score = -1
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
                        aiobungie.ComponentType.ITEM_INSTANCES,
                        aiobungie.ComponentType.ITEM_STATS,
                    ],
                    access_token
                )
                gear_tiers.update({x: (character['itemComponents']['instances']['data'][x]['gearTier'], c_id) for x in
                                   character['itemComponents']['instances']['data']})
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
                c_inv = c_inv + [(x, c_id) for y in buckets for x in y if not is_item_locked(x['state'])]
                score = sum(1 for x in buckets for item in x if not is_item_locked(item['state']))
                if score > best_score:
                    best_score = score
                    best_cid = c_id
                # int(f'{x['state']:08b}'[-1]) == 0 means unlocked (bit 1 of state is off)


            def _bucket_capacity(bucket_hash):
                try:
                    # If you have the manifest loaded, prefer the real capacity:
                    return int(manifest["DestinyInventoryBucketDefinition"][bucket_hash]["itemCount"])
                except Exception:
                    return 10

            def _is_vault_full_err(exc: Exception) -> bool:
                msg = str(exc).lower()
                return ("destinynoroomindestination" in msg
                        or "no room" in msg
                        or "not enough space" in msg
                        or "no space" in msg)

            async def _transfer_to_vault_with_wait(rest, access_token, instance_id, item_hash,
                                                   origin_character_id, membership_type,
                                                   poll_seconds: int = 8):
                """Try transfer→vault; if vault is full, prompt once and retry every `poll_seconds`."""
                prompted = False
                while True:
                    try:
                        await rest.transfer_item(
                            access_token, instance_id, item_hash, origin_character_id,
                            membership_type, vault=True
                        )
                        return  # success
                    except Exception as e:
                        if _is_vault_full_err(e):
                            if not prompted:
                                print("Vault is full. Delete some items in Vault (sort by Newest). "
                                      f"I'll check again every {poll_seconds}s and continue automatically.")
                                prompted = True
                            await asyncio.sleep(poll_seconds)
                            continue
                        raise

            async def _ensure_free_slots_on_char(best_cid, bucket_hash, target_free, *,
                                                 c_invs, rest, access_token, membership_type):
                """Ensure `target_free` free slots exist on `best_cid` for `bucket_hash` by
                moving the lowest-priority non-special, unlocked items from that bucket to the vault."""
                cap = _bucket_capacity(bucket_hash)
                current = sum(1 for it in c_invs[best_cid] if it['bucketHash'] == bucket_hash)
                free = max(0, cap - current)
                need = max(0, target_free - free)
                if need == 0:
                    return

                # Candidates: items on best_cid in this bucket, not locked, not special
                candidates = [
                    it for it in c_invs[best_cid]
                    if it['bucketHash'] == bucket_hash
                       and not is_item_locked(it.get('state', 0))
                       and not is_special(it)
                ]
                # Prefer lowest tier first (fallback big number if missing)
                candidates.sort(key=lambda it: gear_tiers.get(it['itemInstanceId'], (999,))[0])

                # Move just enough to hit the target_free
                for it in candidates[:need]:
                    await _transfer_to_vault_with_wait(
                        rest, access_token, it['itemInstanceId'], it['itemHash'], best_cid, membership_type
                    )
                    # keep your local snapshot consistent
                    c_invs[best_cid].remove(it)

            # --- was: for item in c_invs[best_cid]:
            # avoid mutating the list while iterating
            # figure out which buckets you’ll touch for trash items
            temp = getBucketHashes()
            bucket_hash_values = [temp[e].value for e in itemHashValues]

            # ensure ~3 free slots *per relevant bucket* on the staging character
            for bh in bucket_hash_values:
                await _ensure_free_slots_on_char(
                    best_cid, bh, target_free=3,
                    c_invs=c_invs,
                    rest=rest,
                    access_token=access_token,
                    membership_type=user["destinyMemberships"][0]["membershipType"],
                )

            temp = getBucketHashes()
            temp_armor = getArmorHashes()

            # Filter relevant items from inventory
            bucket_hash_values = [temp[e].value for e in itemHashValues]
            items = [
                x for x in inv
                if not is_item_locked(x['state']) and x['bucketHash'] in bucket_hash_values
            ]

            # Crafted or Exotic = "special"
            trash_items = [(x, gear_tiers[x['itemInstanceId']][1]) for x in inv
                           if x['bucketHash'] in bucket_hash_values
                           and gear_tiers[x['itemInstanceId']][0] <= 3
                           and not is_special(x)]
            #include armor pieces of tier 4
            trash_items += [(x, gear_tiers[x['itemInstanceId']][1]) for x in inv if
                            x['bucketHash'] in temp_armor and gear_tiers[x['itemInstanceId']][0] == 4 and not is_special(x)]

            non_trash_items = [(x, gear_tiers[x['itemInstanceId']][1]) for x in inv if
                               x['bucketHash'] in bucket_hash_values and gear_tiers[x['itemInstanceId']][
                                   0] > 3 and not is_special(x)]

            len_items = len(trash_items)
            count = 0
            for item, c_id in trash_items:
                if not c_id:
                    c_id = best_cid

                print(
                    f"Transferring {helppers.name_from_hash(item['itemHash'])} to {char_classes[c_id]}: count: {count}/{len_items}")
                count += 1
                try:
                    await rest.transfer_item(
                        access_token, item['itemInstanceId'], item['itemHash'], best_cid,
                        user["destinyMemberships"][0]["membershipType"]
                    )
                except Exception as e:
                    if _is_vault_full_err(e):
                        print("Vault is full. Delete some items in Vault (sorted by Newest) and re-run.")
                        raise
                    item_name = helppers.name_from_hash(item['itemHash'])
                    print(item_name)
                    print(char_classes[c_id])
                    pdb.set_trace()
                    print(e)

                # unlock (defensive)
                await rest.set_item_lock_state(
                    access_token, False,
                    item_id=item['itemInstanceId'],
                    character_id=best_cid,
                    membership_type=user["destinyMemberships"][0]["membershipType"]
                )

                # send to vault (this is where Bungie throws when vault is full)
                try:
                    await _transfer_to_vault_with_wait(
                        rest, access_token, item['itemInstanceId'], item['itemHash'], best_cid,
                        user["destinyMemberships"][0]["membershipType"]
                    )

                except Exception as e:
                    if _is_vault_full_err(e):
                        print("Vault is full. Delete some items in Vault (sorted by Newest) and re-run.")
                        raise
                    raise

            #filter tier 4 and 5 gear, removing items with perks that are covered by other items
            # e.g. if you have a gun whose perk pool options are all covered by other items in your inventory, it can be trashed

            for item, tier in non_trash_items:
                #get name, and perk pool
                item_name = helppers.name_from_hash(item['itemHash'])



            #filter items into special and normal

            special_items = [x for x in items if is_special(x)]
            normal_items = [x for x in items if not is_special(x)]

            special_c_inv = [x for x in c_inv if is_special(x[0])]
            normal_c_inv = [x for x in c_inv if not is_special(x[0])]

            # Calculate total for progress tracking
            num_items = len(special_items + normal_items + special_c_inv + normal_c_inv)

            # Transfer normal_c_inv first
            count = 0
            pdb.set_trace()
            for item, c_id in normal_c_inv:
                await rest.transfer_item(
                    access_token, item['itemInstanceId'], item['itemHash'], c_id,
                    user["destinyMemberships"][0]["membershipType"], vault=True
                )
                count += 1
                print(f"{count}/{num_items}")

            # Then normal items
            for item in normal_items:
                await rest.transfer_item(
                    access_token, item['itemInstanceId'], item['itemHash'], best_cid,
                    user["destinyMemberships"][0]["membershipType"]
                )
                sleep()
                await rest.transfer_item(
                    access_token, item['itemInstanceId'], item['itemHash'], best_cid,
                    user["destinyMemberships"][0]["membershipType"], vault=True
                )
                count += 1
                print(f"{count}/{num_items}")

            # Then special_c_inv
            for item, c_id in special_c_inv:
                await rest.transfer_item(
                    access_token, item['itemInstanceId'], item['itemHash'], c_id,
                    user["destinyMemberships"][0]["membershipType"], vault=True
                )
                count += 1
                print(f"{count}/{num_items}")

            # Finally special items
            for item in special_items:
                await rest.transfer_item(
                    access_token, item['itemInstanceId'], item['itemHash'], best_cid,
                    user["destinyMemberships"][0]["membershipType"]
                )
                sleep()
                await rest.transfer_item(
                    access_token, item['itemInstanceId'], item['itemHash'], best_cid,
                    user["destinyMemberships"][0]["membershipType"], vault=True
                )
                count += 1
                print(f"{count}/{num_items}")

        # Return a JSON response.
        return web.json_response(user)
    else:
        # Otherwise return unauthorized if no access token found.
        return web.json_response({"No access token found, Unauthorized."}, status=401)
