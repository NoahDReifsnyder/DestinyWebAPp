"""aiohttp application factory and development server."""

from __future__ import annotations

import logging
import ssl
from html import escape
from pathlib import Path
from string import Template

from aiohttp import web
from aiohttp.abc import AbstractAccessLogger

from destiny_web_app import __version__
from destiny_web_app.app_keys import (
    AUTH_SESSION_KEY,
    AUTH_WARNING_KEY,
    BUNGIE_CLIENT_KEY,
    DATABASE_KEY,
    INVENTORY_SERVICE_KEY,
    MANIFEST_SERVICE_KEY,
    SETTINGS_KEY,
    TOKEN_REFRESH_LOCKS_KEY,
    WEAPON_CLEANER_SERVICE_KEY,
    WEAPON_ORGANIZER_SERVICE_KEY,
)
from destiny_web_app.auth import (
    auth_status,
    authentication_middleware,
    begin_login,
    csrf_input,
    logout,
    oauth_callback,
)
from destiny_web_app.bungie import BungieClient
from destiny_web_app.cleaner import WeaponCleanerService
from destiny_web_app.cleaner_routes import (
    analyze_weapons,
    organize_weapons,
    weapon_organization_status,
    weapon_cleaner_page,
)
from destiny_web_app.config import Settings
from destiny_web_app.data_routes import (
    data_status,
    data_status_json,
    mark_inventory_stale,
    simulate_inventory_failure,
    synchronize_inventory,
)
from destiny_web_app.database import Database
from destiny_web_app.inventory import InventoryService
from destiny_web_app.inventory_routes import (
    inventory_item_detail,
    inventory_page,
    synchronize_manifest,
)
from destiny_web_app.manifest import ManifestService
from destiny_web_app.organizer import WeaponOrganizerService


LOGGER = logging.getLogger(__name__)
TEMPLATE_ROOT = Path(__file__).with_name("templates")
STATIC_ROOT = Path(__file__).with_name("static")


def create_app(settings: Settings | None = None) -> web.Application:
    """Build the web application without starting a network listener."""
    settings = settings or Settings.from_environment()
    database = Database(settings.database_path)
    database.initialize()
    database.remove_expired_sessions()

    app = web.Application(
        middlewares=[
            security_headers_middleware,
            authentication_middleware,
            error_page_middleware,
        ]
    )
    app[SETTINGS_KEY] = settings
    app[DATABASE_KEY] = database
    bungie = BungieClient(settings)
    app[BUNGIE_CLIENT_KEY] = bungie
    app[TOKEN_REFRESH_LOCKS_KEY] = {}
    app[INVENTORY_SERVICE_KEY] = InventoryService(
        database,
        bungie,
        stale_seconds=settings.inventory_stale_seconds,
    )
    manifest_service = ManifestService(
        database,
        bungie,
        path=settings.manifest_path,
        language=settings.manifest_language,
    )
    app[MANIFEST_SERVICE_KEY] = manifest_service
    app[WEAPON_CLEANER_SERVICE_KEY] = WeaponCleanerService(
        database,
        manifest_service,
    )
    app[WEAPON_ORGANIZER_SERVICE_KEY] = WeaponOrganizerService(
        database,
        bungie,
        app[INVENTORY_SERVICE_KEY],
        manifest_service,
        app[WEAPON_CLEANER_SERVICE_KEY],
    )
    app.add_routes(
        [
            web.get("/", home),
            web.get("/health", health),
            web.get("/auth/bungie/login", begin_login),
            web.get("/redirect", oauth_callback),
            web.get("/auth/bungie/callback", oauth_callback),
            web.get("/auth/status", auth_status),
            web.post("/auth/logout", logout),
            web.get("/data/status", data_status),
            web.get("/data/status.json", data_status_json),
            web.post("/data/inventory/sync", synchronize_inventory),
            web.post("/data/inventory/mark-stale", mark_inventory_stale),
            web.post(
                "/data/inventory/simulate-failure",
                simulate_inventory_failure,
            ),
            web.get("/inventory", inventory_page),
            web.post("/inventory/manifest/sync", synchronize_manifest),
            web.get("/inventory/item/{item_id}", inventory_item_detail),
            web.get("/cleaner/weapons", weapon_cleaner_page),
            web.post("/cleaner/weapons/analyze", analyze_weapons),
            web.post("/cleaner/weapons/organize", organize_weapons),
            web.get(
                "/cleaner/weapons/organize/status",
                weapon_organization_status,
            ),
        ]
    )
    app.router.add_static("/static/", STATIC_ROOT)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


async def home(request: web.Request) -> web.Response:
    settings = request.app[SETTINGS_KEY]
    authenticated = request.get(AUTH_SESSION_KEY)
    warning = request.get(AUTH_WARNING_KEY)

    if authenticated is None:
        if settings.bungie_is_configured:
            auth_panel = """
<section class="auth-card">
  <h2>Bungie account</h2>
  <p>Sign in to connect your Bungie account.</p>
  <a class="button" href="/auth/bungie/login">Sign in with Bungie</a>
</section>"""
        else:
            auth_panel = """
<section class="auth-card warning">
  <h2>Bungie OAuth not configured</h2>
  <p>Copy <code>.env.example</code> to <code>.env</code> and add your
  Bungie application credentials.</p>
</section>"""
    else:
        warning_html = (
            f'<p class="warning">{escape(warning)}</p>' if warning else ""
        )
        auth_panel = f"""
<section class="auth-card">
  <h2>Signed in</h2>
  <p class="username">{escape(authenticated.display_name)}</p>
  <p>Your browser session and Bungie tokens are stored locally. Access tokens
  refresh automatically before they expire.</p>
  <p><a href="/auth/status">View non-secret session status</a></p>
  <p><a class="button" href="/inventory">Open inventory</a></p>
  <p><a href="/cleaner/weapons">Open weapon cleaner</a></p>
  <p><a href="/data/status">Inventory and database status</a></p>
  {warning_html}
  <form method="post" action="/auth/logout">
    {csrf_input(request, "/auth/logout")}
    <button type="submit">Sign out</button>
  </form>
</section>"""

    html = render_template(
        "home.html",
        app_name="Destiny Web App",
        message="Hello, Guardian!",
        version=__version__,
        environment=settings.environment,
        status="online",
        auth_panel=auth_panel,
    )
    return web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


async def health(request: web.Request) -> web.Response:
    settings = request.app[SETTINGS_KEY]
    return web.json_response(
        {
            "status": "ok",
            "application": "Destiny Web App",
            "version": __version__,
            "environment": settings.environment,
        }
    )


@web.middleware
async def security_headers_middleware(
    request: web.Request,
    handler,
) -> web.StreamResponse:
    raised_response: web.HTTPException | None = None
    try:
        response = await handler(request)
    except web.HTTPException as error:
        response = error
        raised_response = error

    security_headers = {
        "Content-Security-Policy": (
            "default-src 'self'; "
            "img-src 'self' https://www.bungie.net data:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": (
            "camera=(), microphone=(), geolocation=(), payment=()"
        ),
    }
    for name, value in security_headers.items():
        response.headers.setdefault(name, value)

    if raised_response is not None:
        raise raised_response
    return response


@web.middleware
async def error_page_middleware(
    request: web.Request,
    handler,
) -> web.StreamResponse:
    try:
        return await handler(request)
    except web.HTTPNotFound:
        if request_prefers_json(request):
            return web.json_response(
                {"error": "The requested resource was not found."},
                status=404,
                headers={"Cache-Control": "no-store"},
            )
        html = render_template(
            "not_found.html",
            requested_path=escape(request.path),
        )
        return web.Response(text=html, content_type="text/html", status=404)
    except web.HTTPException:
        raise
    except Exception:
        LOGGER.exception("Unhandled request failure for %s", request.path)
        if request_prefers_json(request):
            return web.json_response(
                {"error": "The request could not be completed."},
                status=500,
                headers={"Cache-Control": "no-store"},
            )
        html = render_template("server_error.html")
        return web.Response(
            text=html,
            content_type="text/html",
            status=500,
            headers={"Cache-Control": "no-store"},
        )


def request_prefers_json(request: web.Request) -> bool:
    return (
        request.path.endswith(".json")
        or request.path.startswith("/inventory/item/")
        or "application/json" in request.headers.get("Accept", "")
    )


async def on_startup(app: web.Application) -> None:
    settings = app[SETTINGS_KEY]
    await app[BUNGIE_CLIENT_KEY].start()
    LOGGER.info(
        "Destiny Web App starting at https://%s:%s",
        settings.host,
        settings.port,
    )


async def on_cleanup(app: web.Application) -> None:
    await app[BUNGIE_CLIENT_KEY].close()
    LOGGER.info("Destiny Web App stopped")


def render_template(template_name: str, **values: str) -> str:
    template_path = TEMPLATE_ROOT / template_name
    template = Template(template_path.read_text(encoding="utf-8"))
    return template.substitute(values)


def create_ssl_context(settings: Settings) -> ssl.SSLContext:
    for label, path in (
        ("TLS certificate", settings.ssl_certificate),
        ("TLS private key", settings.ssl_key),
    ):
        if not path.is_file():
            raise FileNotFoundError(f"{label} was not found: {path}")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(settings.ssl_certificate, settings.ssl_key)
    return context


class QuerySafeAccessLogger(AbstractAccessLogger):
    """Log request paths without OAuth codes, state, or referrer queries."""

    def log(
        self,
        request: web.BaseRequest,
        response: web.StreamResponse,
        time: float,
    ) -> None:
        self.logger.info(
            "%s %s -> %s in %.1f ms",
            request.method,
            request.rel_url.raw_path,
            response.status,
            time * 1000,
        )


def run() -> None:
    settings = Settings.from_environment()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ssl_context = create_ssl_context(settings)

    print(f"Destiny Web App: https://{settings.host}:{settings.port}")
    web.run_app(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        ssl_context=ssl_context,
        access_log=LOGGER,
        access_log_class=QuerySafeAccessLogger,
    )
