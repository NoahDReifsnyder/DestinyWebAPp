from aiohttp import web

# ----------------------------
# Landing/Home Page
# ----------------------------
router = web.RouteTableDef()  # Each file can define its own router

@router.get("/")
async def home(request: web.Request) -> web.Response:
    """
    Landing page.
    Only shows a welcome message and a login link.
    """
    # Generate login link using the named route from auth.py
    login_url = request.app.router["login"].url_for()
    html = f"""
    <html>
        <body>
            <h1>Welcome to Destiny 2 Tool</h1>
            <a href='{login_url}'>Login with Bungie</a>
        </body>
    </html>
    """
    return web.Response(text=html, content_type='text/html', charset='utf-8')
