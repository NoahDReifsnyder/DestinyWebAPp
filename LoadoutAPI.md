# Composable Loadout API

The public loadout code lives in `src/destiny_web_app/loadouts/`. It is split by
capability and does not require an aiohttp request or route.

## Capability map

1. `runtime.py` — constructs the shared `LoadoutFunctions` dependency bundle.
2. `inspection.py` — reads in-game slots and builds exact-item catalogs.
3. `library.py` — saves, reads, revises, clones, archives, imports, and exports
   individual application-owned loadouts.
4. `sets.py` — builds activity/raid sets, encounter groups, and pinned slot
   assignments.
5. `previews.py` — creates and validates read-only mutation previews.
6. `execution.py` — confirms, starts/resumes, and observes durable applications.
7. `game_actions.py` — sends one atomic Bungie action without imposing a flow.
8. `state.py` — refreshes and verifies live state without imposing a flow.
9. `storage.py` — owns loadout SQLite schema and persistence.

All high-level function signatures begin with the same pair:

```python
function(functions: LoadoutFunctions, user_id: str, *, ...)
```

All atomic Bungie write signatures begin with:

```python
async_function(functions: LoadoutFunctions, access_token: str, *, ...)
```

Slot indices are zero-based at the Python/API boundary. A displayed in-game
slot 5 is `slot_index=4`.

## Construct once

```python
from destiny_web_app.loadouts.runtime import build_loadout_functions

loadouts = build_loadout_functions(database, bungie, inventory, manifest)
```

The existing web application stores this object under `LOADOUT_FUNCTIONS_KEY`.

## Save an individual loadout

```python
from destiny_web_app.loadouts.library import save_equipped_loadout

saved = save_equipped_loadout(
    loadouts,
    user_id,
    character_id=character_id,
    name="Deep Stone Crypt — Atraks",
    tags=("raid", "dsc"),
)
```

To assemble exact instances yourself, call `build_item_catalog` from
`inspection.py`, then pass a complete `items_by_bucket` mapping to
`save_selected_loadout` in `library.py`. Partial loadouts are rejected.

## Build a loadout set

```python
from destiny_web_app.loadouts.sets import (
    add_encounter,
    assign_loadout,
    create_loadout_set,
)

loadout_set = create_loadout_set(
    loadouts,
    user_id,
    name="Deep Stone Crypt",
    activity_name="Deep Stone Crypt",
)
loadout_set = add_encounter(
    loadouts,
    user_id,
    set_id=loadout_set["plan_id"],
    order=0,
    name="Crypt Security",
)
loadout_set = assign_loadout(
    loadouts,
    user_id,
    set_id=loadout_set["plan_id"],
    encounter_id=loadout_set["encounters"][0]["encounter_id"],
    order=0,
    loadout_revision_id=saved["revision_id"],
    character_id=character_id,
    slot_index=4,
)
```

Set assignments pin immutable revision IDs. Later edits to a saved loadout do
not silently change an existing set.

## Use the durable safety workflow

```python
from destiny_web_app.loadouts.execution import (
    confirm_application,
    get_application,
    start_application,
)
from destiny_web_app.loadouts.previews import preview_loadout_application

preview = preview_loadout_application(
    loadouts,
    user_id,
    loadout_id=saved["loadout_id"],
    revision_id=saved["revision_id"],
    character_id=character_id,
    slot_index=4,
)
operation = confirm_application(
    loadouts,
    user_id,
    preview_id=preview["preview_id"],
    backup_choice="skip",
)
start_application(
    loadouts,
    user_id,
    operation_id=operation["operation_id"],
    access_token=access_token,
)
status = get_application(
    loadouts,
    user_id,
    operation_id=operation["operation_id"],
)
```

Creating/validating a preview and confirming an operation are local-only.
`start_application` is the point that can send Bungie writes.

## Build a completely custom write flow

`game_actions.py` exposes the payload-correct atomic operations:

- `transfer_item`
- `equip_items`
- `insert_free_plug`
- `save_equipped_to_slot`
- `set_slot_identifiers`
- `clear_slot`

These calls do not add safety behavior. A direct caller must decide how to
handle activity restrictions, Exotic displacement, throttling, retries,
`responseMintedTimestamp` cache lag, verification, restoration, and recovery.
`state.py` exposes `refresh_live_state`, `wait_for_post_write_state`, exact item
and slot reads, equipment/slot matching, empty-slot checks, and state
fingerprints so those policies can be composed without reaching into the old
synchronization class.

## Errors

- `LoadoutInspectionError` — incomplete, stale, unresolved, or incompatible.
- `ActivityPlanError` — ordering, ownership, class, or slot invariant failed.
- `LoadoutPreviewError` — current state cannot produce a safe preview.
- `LoadoutOperationError` — a durable application cannot continue.
- `BungieActionError` — a write was rejected with structured API evidence.

All five are re-exported from `destiny_web_app.loadouts.errors`.
