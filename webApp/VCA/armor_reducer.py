from webApp.auth import router
from aiohttp import web
from webApp.destiny_helpers import *
from webApp.VCA.perk_manager import get_possible_perks
import json
import jinja2
import aiohttp_jinja2
import pdb


@router.get('/armor_reducer')
async def gear_reducer(request):
    #get tier, main perk, tiertiary stat, and tuning slot(if 5) of all armor
    user_id = request.cookies.get("user_id")
    with Database("testing") as db:
        item_hashes = db[user_id].get("gear", {}).keys()
        for item_hash in item_hashes:
            if not isArmor(getBucketHash(item_hash)):
                continue
            instances = db[user_id]["gear"][item_hash].get("instances", [])
            for item in instances:
                pdb.set_trace()
                print(item)
u