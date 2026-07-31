"""Typed keys shared by aiohttp application and request state."""

import asyncio

from aiohttp import web

from destiny_web_app.bungie import BungieClient
from destiny_web_app.cleaner import WeaponCleanerService
from destiny_web_app.config import Settings
from destiny_web_app.database import AuthenticatedSession, Database
from destiny_web_app.inventory import InventoryService
from destiny_web_app.manifest import ManifestService
from destiny_web_app.organizer import WeaponOrganizerService


SETTINGS_KEY = web.AppKey("settings", Settings)
DATABASE_KEY = web.AppKey("database", Database)
BUNGIE_CLIENT_KEY = web.AppKey("bungie_client", BungieClient)
AUTH_SESSION_KEY = web.AppKey("authenticated_session", AuthenticatedSession)
AUTH_WARNING_KEY = web.AppKey("authentication_warning", str)
INVENTORY_SERVICE_KEY = web.AppKey("inventory_service", InventoryService)
MANIFEST_SERVICE_KEY = web.AppKey("manifest_service", ManifestService)
WEAPON_CLEANER_SERVICE_KEY = web.AppKey(
    "weapon_cleaner_service",
    WeaponCleanerService,
)
WEAPON_ORGANIZER_SERVICE_KEY = web.AppKey(
    "weapon_organizer_service",
    WeaponOrganizerService,
)
TOKEN_REFRESH_LOCKS_KEY = web.AppKey(
    "token_refresh_locks",
    dict[str, asyncio.Lock],
)
