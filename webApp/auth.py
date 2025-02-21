
"""An example on how to use Bungie OAuth2 purely using aiobungie and aiohttp.web only."""
import json
import ssl
import os

from aiohttp import web
import aiohttp
import aiobungie
import enum
import sqlite3
from .destiny_helpers import *
import importlib

# Web router.
router = web.RouteTableDef()
### import all the routers from the lower level modules, by looping through the modules
# ### and importing all the routers from them.
# iterate through all modules in the current directory
users = {}

bucketHashes = None
exotic_item_hashes = None


async def oauth(client):
    async with client.acquire() as rest:
        oauth_url = rest.build_oauth2_url()

    if oauth_url is None:
        return web.json_response(
            {
                "error": "Couldn't generate OAuth2 URL.",
                "hint": "Make sure the client IDs are set.",
            },
            status=400,
        )
    print("Redirecting to Bungie OAuth2.")
    print(oauth_url.url)
    raise web.HTTPFound(location=oauth_url.url)


# Home page where we will be redirected to login.
@router.get("/")
async def home(request: web.Request) -> web.Response:
    print("User Landed")
    # Build the OAuth2 url, Make sure the client id and secret are set in the client
    # constructor.
    client: aiobungie.RESTPool = request.app["client"]
    await oauth(client)
    return


# After logging in we will be redirected from our Bungie app to this location.
# This "/redirect" route is configured in your Bungie Application at the developer portal.
@router.get("/redirect")
async def redirect(request: web.Request) -> web.Response:
    app = request.app
    print("Redirected!")
    # Check if the code parameter is in the redirect URL.
    client: aiobungie.RESTPool = app["client"]
    if code := request.query.get("code"):
        async with client.acquire() as rest:
            # Make the request and fetch the OAuth2 tokens.
            tokens = await rest.fetch_oauth2_tokens(code)
            # Store the access token in the pool metadata.
            mem_id = str(tokens.membership_id)
            access_token = tokens.access_token
            # store the access_token in a cookie
            if mem_id not in app['users'] or app['users'][mem_id].get('direct') is None:
                app['users'][mem_id] = {"access_token": access_token}
                var = "?mem_id=" + mem_id
                links = [
                    ("Pull Postmaster", f"/pullPM{var}"),
                    ("Vault Clearing Assistant", f"/VCA{var}"),
                    ("Loadout Manager", f"/loadout_landing{var}")
                ]

                # Create HTML links dynamically
                html_links = "\n".join(f"<a href='{url}'>{text}</a>" for text, url in links)

                # Wrap in HTML body
                response_text = f"<html><body>{html_links}</body></html>"
                return web.Response(
                    text=response_text,
                    content_type='text/html',
                    charset='utf-8')
            else:
                app['users'][mem_id]["access_token"] = access_token
                return await app['users'][mem_id]["direct"](mem_id)

            client.metadata['token'] = access_token
            print(f"Member {tokens.membership_id} has been authenticated!")

        # present optional links to all other pages via the web interface


    else:
        # Otherwise return 404 and couldn't authenticate.
        return web.json_response(
            {"error": "code not found and couldn't authenticate."}, status=400
        )





# When the app start, We initialize our rest pool and add it to the app storage.
async def on_start_up(app: web.Application) -> None:
    client = aiobungie.RESTPool(
        "47e0eb6f5a0f4064877eb2beb63c5f17",
        client_secret="0bTKVixhnt-8AWBhS9w.uNsVhUT94eQuU-OdvuloYmw",
        client_id=41804,
    )
    await client.start()
    app["client"] = client
    app['users'] = {}
    print("Client has been initialized.")
    await initialize(client, app)



    # if os.path.exists(item_hashes_file_loc):
    #     with open(item_hashes_file_loc, 'r') as f:
    #         itemHashValues = [getattr(bucketHashes, e).value for e in json.load(f)]
    # else:
    #     itemHashValues = [getattr(bucketHashes, e).value for e in itemHashValues]
    #     with open(item_hashes_file_loc, 'w') as f:
    #         json.dump(itemHashValues, f)



# Replace these with actual membershipType and destinyMembershipId values
membership_type = 3  # e.g., 3 for Steam
membership_id = 'YOUR_MEMBERSHIP_ID'

# Fetch profile inventory

async def on_shutdown(app: web.Application) -> None:
    # Called when the app shuts down.
    # You can close servers, cleanup database, etc.
    ...


async def direct(request: web.Request) -> web.Response:
    return None

def start() -> None:
    # The application itself.
    app = web.Application()
    # Add the routes.
    app.add_routes(router)

    # Add on start and close callbacks
    app.on_startup.append(on_start_up)
    app.on_shutdown.append(on_shutdown)

    # Bungie doesn't allow redirecting to http and requires https,
    # So we need to generate ssl certifications to allow our server
    # run on https.i
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)

    # You should generate cert and private key files and place their path here.
    d = os.path.dirname(os.path.abspath(__file__)) + "/util/"

    ctx.load_cert_chain(d + "cert.pem", d + "key.pem")

    # Run the app.
    #web.run_app(app, host="172.19.0.2", port=8080, ssl_context=ctx)
    web.run_app(app, host="localhost", port=42697, ssl_context=ctx)



if __name__ == "__main__":
    start()
