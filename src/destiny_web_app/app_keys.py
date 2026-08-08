"""Typed keys shared by aiohttp application and request state."""

import asyncio

from aiohttp import web

from destiny_web_app.armor_cleaner import ArmorCleanerService
from destiny_web_app.bungie import BungieClient
from destiny_web_app.cleaner import WeaponCleanerService
from destiny_web_app.config import Settings
from destiny_web_app.database import AuthenticatedSession, Database
from destiny_web_app.inventory import InventoryService
from destiny_web_app.manifest import ManifestService
from destiny_web_app.organizer import ArmorOrganizerService, WeaponOrganizerService
from destiny_web_app.loadout_manager import LoadoutManagerService
from destiny_web_app.loadout_plans import ActivityPlanService
from destiny_web_app.loadout_sync import LoadoutSyncService
from destiny_web_app.loadouts.runtime import LoadoutFunctions


SETTINGS_KEY = web.AppKey("settings", Settings)
DATABASE_KEY = web.AppKey("database", Database)
BUNGIE_CLIENT_KEY = web.AppKey("bungie_client", BungieClient)
AUTH_SESSION_KEY = web.AppKey("authenticated_session", AuthenticatedSession)
AUTH_WARNING_KEY = web.AppKey("authentication_warning", str)
INVENTORY_SERVICE_KEY = web.AppKey("inventory_service", InventoryService)
MANIFEST_SERVICE_KEY = web.AppKey("manifest_service", ManifestService)
ARMOR_CLEANER_SERVICE_KEY = web.AppKey(
    "armor_cleaner_service",
    ArmorCleanerService,
)
ARMOR_ORGANIZER_SERVICE_KEY = web.AppKey(
    "armor_organizer_service",
    ArmorOrganizerService,
)
WEAPON_CLEANER_SERVICE_KEY = web.AppKey(
    "weapon_cleaner_service",
    WeaponCleanerService,
)
WEAPON_ORGANIZER_SERVICE_KEY = web.AppKey(
    "weapon_organizer_service",
    WeaponOrganizerService,
)
LOADOUT_FUNCTIONS_KEY = web.AppKey("loadout_functions", LoadoutFunctions)
# Compatibility keys keep the existing server-rendered pages working while
# custom flows call the capability modules through LOADOUT_FUNCTIONS_KEY.
LOADOUT_MANAGER_SERVICE_KEY = web.AppKey(
    "loadout_manager_service", LoadoutManagerService
)
ACTIVITY_PLAN_SERVICE_KEY = web.AppKey(
    "activity_plan_service", ActivityPlanService
)
LOADOUT_SYNC_SERVICE_KEY = web.AppKey(
    "loadout_sync_service", LoadoutSyncService
)
TOKEN_REFRESH_LOCKS_KEY = web.AppKey(
    "token_refresh_locks",
    dict[str, asyncio.Lock],
)
