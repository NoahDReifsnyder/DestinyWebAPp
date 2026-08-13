"""Construction and dependency container for callable loadout functions."""

from __future__ import annotations

from dataclasses import dataclass

from destiny_web_app.bungie import BungieClient
from destiny_web_app.database import Database
from destiny_web_app.inventory import InventoryService
from destiny_web_app.loadout_manager import LoadoutManagerService
from destiny_web_app.loadout_plans import ActivityPlanService
from destiny_web_app.loadout_sets import LoadoutSetService
from destiny_web_app.loadout_sync import LoadoutSyncService
from destiny_web_app.manifest import ManifestService

from .storage import LoadoutStore


@dataclass(frozen=True, slots=True)
class LoadoutFunctions:
    """All dependencies needed by the public loadout function modules.

    Create this once at application startup. Every public function accepts the
    same first two arguments: this container and a Bungie membership ID.
    """

    store: LoadoutStore
    bungie: BungieClient
    inventory: InventoryService
    library: LoadoutManagerService
    sets: LoadoutSetService
    activity_plans: ActivityPlanService
    synchronization: LoadoutSyncService


def build_loadout_functions(
    database: Database,
    bungie: BungieClient,
    inventory: InventoryService,
    manifest: ManifestService,
) -> LoadoutFunctions:
    """Build the independent loadout capabilities and initialize their store."""

    store = LoadoutStore(database)
    store.initialize()
    library = LoadoutManagerService(store, manifest)
    activity_plans = ActivityPlanService(store, library)
    sets = LoadoutSetService(store, library)
    synchronization = LoadoutSyncService(
        store,
        bungie,
        inventory,
        manifest,
        library,
        activity_plans,
    )
    return LoadoutFunctions(
        store=store,
        bungie=bungie,
        inventory=inventory,
        library=library,
        sets=sets,
        activity_plans=activity_plans,
        synchronization=synchronization,
    )
