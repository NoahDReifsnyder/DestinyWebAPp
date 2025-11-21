from webApp.auth import router
from aiohttp import web
from webApp.destiny_helpers import *
from webApp.VCA.perk_manager import get_possible_perks
import json
import jinja2
import aiohttp_jinja2
import pdb

# Example of your weapon socket data
sockets_data = {
    0: {"socket_name": "Intrinsic", "plug_names": ["Area Denial Frame"]},
    1: {"socket_name": "Launcher Barrel", "plug_names": ["Volatile Launch", "Confined Launch", "Countermass", "Hard Launch", "Quick Launch"]},
    2: {"socket_name": "Magazine", "plug_names": ["High-Velocity Rounds", "Implosion Rounds", "Proximity Grenades"]},
    3: {"socket_name": "Trait", "plug_names": ["Blast Distributor", "Demolitionist", "Transcendent Moment"]},
    4: {"socket_name": "Shader", "plug_names": ["Superblack", "Iron Banner Keepsake", "Dawn of Dusk"]}
}


def get_item_info(item_hash):
    man = getManifest()
    items_def = man.get("DestinyInventoryItemDefinition", {})
    item_data = items_def.get(str(item_hash))
    if not item_data:
        return None
    return {"type": item_data["itemTypeDisplayName"], "name": item_data.get("displayProperties", {}).get("name", "Unknown"), "icon": f"https://www.bungie.net{item_data.get('displayProperties', {}).get('icon', '')}"}


# --- Routes ---
@router.get("/perk_form")
@aiohttp_jinja2.template("perk_picker.html")
async def index(request):
    app = request.app
    user_id = request.cookies.get("user_id")
    weapon_perks, armor_perks = {}, {}
    with Database("testing") as db:
        item_hashes = db[user_id].get("gear", {}).keys()

        weapon_data = {}

        for item_hash in item_hashes:
            if not isWeapon(getBucketHash(item_hash)):
                continue
            item_info = get_item_info(item_hash)
            if item_info:
                if item_info['type'] not in weapon_data:
                    weapon_data[item_info['type']] = []
                weapon_data[item_info['type']].append({
                    "hash": item_hash,
                    "name": item_info['name'],
                    "icon": item_info['icon']
                })

        # Optional: sort alphabetically by name
        return {"weapons": weapon_data}


@router.get("/perk_selector")
@aiohttp_jinja2.template("perk_selector.html")
async def perk_selector(request):
    item_hash_index = int(request.cookies.get("item_hash_idx", None))
    print(item_hash_index)
    user_id = request.cookies.get("user_id")
    with Database("testing") as db:
        user_id = request.cookies.get("user_id")
        item_hash = db[user_id]["gear"]["selections"][item_hash_index]
        load_next = item_hash_index + 1 < len(db[user_id]["gear"]["selections"])
    sockets = get_possible_perks(item_hash)
    parsed = {
        "Barrel": None,
        "Magazine": None,
        "Trait1": None,
        "Trait2": None
    }

    traits_found = 0

    for idx, socket in sockets.items():
        name = socket["socket_name"].lower()

        slot_1 = {"barrel", "frame", "launcher", "bowstring", "haft"}
        slot_2 = {"magazine", "mag", "arrow"}
        slot_3 = {"trait", "perk"}
        if any(slot_1_name in name for slot_1_name in slot_1):
            parsed["Barrel"] = {"plug_names": socket["plug_names"], "plug_hashes": socket["plug_hashes"]}
        elif any(slot_2_name in name for slot_2_name in slot_2):
            parsed["Magazine"] = {"plug_names": socket["plug_names"], "plug_hashes": socket["plug_hashes"]}
        elif any(slot_3_name in name for slot_3_name in slot_3):
            if traits_found == 0:
                parsed["Trait1"] = {"plug_names": socket["plug_names"], "plug_hashes": socket["plug_hashes"]}
                traits_found += 1
            elif traits_found == 1:
                parsed["Trait2"] = {"plug_names": socket["plug_names"], "plug_hashes": socket["plug_hashes"]}
                traits_found += 1

    selected_perks = []
    with Database("testing") as db:
        selected_perks = list(db[user_id]["selected_perks"].get(item_hash, []))
    return {"item_hash": item_hash,
            "load_next": load_next,
            "weapon_name": name_from_hash(item_hash),
            "sockets": {x: list(zip(y['plug_names'], y['plug_hashes'])) for x, y in parsed.items()},
            "selected_perks": selected_perks}


@router.post("/api/weapons/selections")
async def update_weapon_selections(request):
    """
    Expect JSON body: { "<item_hash>": "<action>", ... }
    action ∈ {"keep", "discard", "perk"}
    """
    app = request.app
    user_id = request.cookies.get("user_id")
    if not user_id:
        return web.json_response({"error": "Invalid user"}, status=401)

    try:
        selections = await request.post()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    #save only selections that are "perk"
    with Database("testing") as db:
        db[user_id]["gear"]["selections"] = [k for k, v in selections.items() if v == "perk"]
    response = web.HTTPFound(location="/perk_selector")
    response.set_cookie("item_hash_idx", "0")

    return response


@router.post("/submit_perks")
async def save_perk_selection(request):
    app = request.app
    user_id = request.cookies.get("user_id")
    if not user_id:
        return web.json_response({"error": "Invalid user"}, status=401)

    data = await request.post()
    data = {x: json.loads(y) if y.startswith('{') else y for x, y in data.items()}
    sockets = ["Barrel", "Magazine", "Trait1", "Trait2"]
    sockets = {x: {} for x in sockets}
    sockets.update(data)
    print(sockets)
    item_hash = data["item_hash"]

    with Database("testing") as db:
        if "selected_perks" not in db[user_id]:
            db[user_id]["selected_perks"] = {}
        if item_hash not in db[user_id]["selected_perks"]:
            db[user_id]["selected_perks"][item_hash] = []
        db[user_id]["selected_perks"][item_hash].append(sockets)

    response = web.HTTPFound(location="/perk_selector")
    return response

@router.post("/next_weapon")
async def next_weapon(request):
    user_id = request.cookies.get("user_id")
    item_hash_index = int(request.cookies.get("item_hash_idx", 0))+1

    with Database("testing") as db:
        # validate user + gear structure
        if (
            user_id not in db
            or "gear" not in db[user_id]
            or "selections" not in db[user_id]["gear"]
        ):
            return web.Response(text="No gear data found", status=404)

        item_hash = db[user_id]["gear"]["selections"][item_hash_index]

    # redirect to perk selector for next weapon
    response = web.HTTPFound(f"/perk_selector")
    response.set_cookie("item_hash_idx", str(item_hash_index))
    return response

@router.post("/delete_perk")
async def delete_perk(request):
    user_id = request.cookies.get("user_id")
    data = await request.post()
    item_hash = data.get("item_hash")
    instance_index = int(data.get("instance_index", -1))

    if user_id is None or item_hash is None or instance_index < 0:
        return web.Response(text="Invalid request", status=400)

    with Database("testing") as db:
        if (
            user_id not in db
            or "gear" not in db[user_id]
            or item_hash not in db[user_id]["gear"]
            or "selected_perks" not in db[user_id]["gear"][item_hash]
        ):
            return web.Response(text="No such selection", status=404)

        selected_list = db[user_id]["gear"][item_hash]["selected_perks"]

        if instance_index >= len(selected_list):
            return web.Response(text="Invalid instance index", status=400)

        # Remove the selected instance
        selected_list.pop(instance_index)

    # Redirect back to the same perk selector page for this weapon
    response = web.HTTPFound(f"/perk_selector")

    return response


