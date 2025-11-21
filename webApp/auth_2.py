from aiohttp import web
import aiobungie
from dataclasses import dataclass

# ----------------------------
# Dataclass for user session
# ----------------------------
@dataclass
class User:
    membership_id: str
    access_token: str
    refresh_token: str

router = web.RouteTableDef()  # Router for auth routes

# ----------------------------
# Login Route
# ----------------------------
@router.get("/login", name="login")
async def login(request: web.Request) -> web.Response:
    """
    Starts OAuth2 flow with Bungie.
    Redirects the user to Bungie login page.
    """
    client: aiobungie.RESTPool = request.app["client"]
    async with client.acquire() as rest:
        oauth_url = rest.build_oauth2_url()

    if oauth_url is None:
        return web.json_response(
            {"error": "Couldn't generate OAuth2 URL. Check client credentials."},
            status=400
        )

    # Redirect user to Bungie OAuth login
    raise web.HTTPFound(location=oauth_url.url)


# ----------------------------
# OAuth Redirect / Callback
# ----------------------------
@router.get("/redirect")
async def oauth_redirect(request: web.Request) -> web.Response:
    """
    Handles Bungie OAuth callback.
    Exchanges code for access/refresh tokens and stores user in app state.
    """
    app = request.app
    client: aiobungie.RESTPool = app["client"]

    if code := request.query.get("code"):
        async with client.acquire() as rest:
            tokens = await rest.fetch_oauth2_tokens(code)
            mem_id = str(tokens.membership_id)

            # Store or update user in app state
            user = User(
                membership_id=tokens.membership_id,
                access_token=tokens.access_token,
                refresh_token=tokens.refresh_token
            )
            app["users"][mem_id] = user

            # Return simple HTML page with links to app tools
            var = f"?mem_id={mem_id}"
            links = [
                ("Pull Postmaster", f"/pullPM{var}"),
                ("Vault Clearing Assistant", f"/VCA{var}"),
                ("Light Level Companion", f"/LLC{var}"),
                ("Perk Manager", f"/perk_manager{var}"),
            ]
            html_links = "\n".join(f"<a href='{url}'>{text}</a>" for text, url in links)
            return web.Response(
                text=f"<html><body>{html_links}</body></html>",
                content_type="text/html",
                charset="utf-8"
            )
    else:
        return web.json_response(
            {"error": "No code found in OAuth redirect."},
            status=400
        )
