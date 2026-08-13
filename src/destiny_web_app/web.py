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
    ARMOR_CLEANER_SERVICE_KEY,
    ARMOR_ORGANIZER_SERVICE_KEY,
    AUTH_SESSION_KEY,
    AUTH_WARNING_KEY,
    BUNGIE_CLIENT_KEY,
    DATABASE_KEY,
    INVENTORY_SERVICE_KEY,
    LOADOUT_FUNCTIONS_KEY,
    LOADOUT_MANAGER_SERVICE_KEY,
    ACTIVITY_PLAN_SERVICE_KEY,
    LOADOUT_SYNC_SERVICE_KEY,
    MANIFEST_SERVICE_KEY,
    SETTINGS_KEY,
    TOKEN_REFRESH_LOCKS_KEY,
    WEAPON_CLEANER_SERVICE_KEY,
    WEAPON_ORGANIZER_SERVICE_KEY,
)
from destiny_web_app.armor_cleaner import ArmorCleanerService
from destiny_web_app.armor_cleaner_routes import (
    analyze_armor,
    armor_cleaner_page,
    armor_organization_status,
    organize_armor,
    save_armor_policy,
    set_armor_manual_keep,
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
from destiny_web_app.organizer import ArmorOrganizerService, WeaponOrganizerService
from destiny_web_app.loadouts.runtime import build_loadout_functions
from destiny_web_app.loadout_builder_routes import (
    loadout_builder_items,
    loadout_builder_page,
    save_builder_loadout,
)
from destiny_web_app.loadout_routes import (
    capture_current_loadout,
    clone_saved_loadout,
    delete_saved_loadout,
    export_saved_loadout,
    favorite_saved_loadout,
    import_in_game_loadout,
    import_saved_loadout_bundle,
    loadout_manager_page,
    loadout_slot_page,
    restore_saved_loadout_revision,
    revise_saved_loadout,
    saved_loadout_page,
    update_saved_loadout,
)
from destiny_web_app.loadout_sync_routes import (
    confirm_loadout_preview,
    create_loadout_set_preview,
    create_single_loadout_preview,
    loadout_operation_page,
    loadout_operation_status,
    loadout_preview_page,
    resume_loadout_operation,
)
from destiny_web_app.loadout_set_routes import (
    create_loadout_page,
    create_set,
    create_set_from_character,
    delete_set,
    loadout_set_page,
    rename_set,
    save_set,
)


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
    app[ARMOR_CLEANER_SERVICE_KEY] = ArmorCleanerService(
        database,
        manifest_service,
    )
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
    app[ARMOR_ORGANIZER_SERVICE_KEY] = ArmorOrganizerService(
        database,
        bungie,
        app[INVENTORY_SERVICE_KEY],
        manifest_service,
        app[ARMOR_CLEANER_SERVICE_KEY],
    )
    loadout_functions = build_loadout_functions(
        database,
        bungie,
        app[INVENTORY_SERVICE_KEY],
        manifest_service,
    )
    app[LOADOUT_FUNCTIONS_KEY] = loadout_functions
    app[LOADOUT_MANAGER_SERVICE_KEY] = loadout_functions.library
    app[ACTIVITY_PLAN_SERVICE_KEY] = loadout_functions.activity_plans
    app[LOADOUT_SYNC_SERVICE_KEY] = loadout_functions.synchronization
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
            web.get("/cleaner/armor", armor_cleaner_page),
            web.post("/cleaner/armor/policy", save_armor_policy),
            web.post("/cleaner/armor/analyze", analyze_armor),
            web.post("/cleaner/armor/keep", set_armor_manual_keep),
            web.post("/cleaner/armor/organize", organize_armor),
            web.get(
                "/cleaner/armor/organize/status",
                armor_organization_status,
            ),
            web.get("/loadouts", loadout_manager_page),
            web.get("/loadouts/create", create_loadout_page),
            web.get("/loadouts/builder", loadout_builder_page),
            web.get("/loadouts/builder/items", loadout_builder_items),
            web.post("/loadouts/builder/save", save_builder_loadout),
            web.post("/loadouts/capture-current", capture_current_loadout),
            web.post("/loadouts/import-slot", import_in_game_loadout),
            web.post("/loadouts/import-bundle", import_saved_loadout_bundle),
            web.get("/loadouts/saved/{loadout_id}", saved_loadout_page),
            web.get(
                "/loadouts/saved/{loadout_id}/export", export_saved_loadout
            ),
            web.post("/loadouts/saved/update", update_saved_loadout),
            web.post("/loadouts/saved/revise", revise_saved_loadout),
            web.post("/loadouts/saved/clone", clone_saved_loadout),
            web.post(
                "/loadouts/saved/restore-revision",
                restore_saved_loadout_revision,
            ),
            web.post("/loadouts/saved/favorite", favorite_saved_loadout),
            web.post("/loadouts/saved/delete", delete_saved_loadout),
            web.get(
                "/loadouts/{character_id}/{slot_index}", loadout_slot_page
            ),
            web.post("/loadout-sets/create", create_set),
            web.post(
                "/loadout-sets/create-from-character",
                create_set_from_character,
            ),
            web.get("/loadout-sets/{set_id}", loadout_set_page),
            web.post("/loadout-sets/save", save_set),
            web.post("/loadout-sets/rename", rename_set),
            web.post("/loadout-sets/delete", delete_set),
            web.post("/loadouts/preview", create_single_loadout_preview),
            web.post("/loadout-sets/preview", create_loadout_set_preview),
            web.get("/loadout-previews/{preview_id}", loadout_preview_page),
            web.post(
                "/loadout-previews/{preview_id}/confirm",
                confirm_loadout_preview,
            ),
            web.get(
                "/loadout-operations/{operation_id}", loadout_operation_page
            ),
            web.get(
                "/loadout-operations/{operation_id}/status",
                loadout_operation_status,
            ),
            web.post(
                "/loadout-operations/{operation_id}/resume",
                resume_loadout_operation,
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
  <p><a class="button" href="/loadouts">Open loadout manager</a></p>
  <p><a href="/inventory">Open inventory</a></p>
  <p><a href="/cleaner/weapons">Open weapon cleaner</a> ·
  <a href="/cleaner/armor">Open armor cleaner</a></p>
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
        log = (
            self.logger.debug
            if request.rel_url.raw_path.endswith("/status")
            and request.rel_url.raw_path.startswith("/loadout-operations/")
            else self.logger.info
        )
        log(
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
