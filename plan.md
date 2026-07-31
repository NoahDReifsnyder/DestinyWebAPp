# Destiny Web App Rebuild Plan

## High-Level Goals

1. Establish a clean rebuild foundation.
2. Host and access a minimal site.
3. Complete the Bungie OAuth cycle.
4. Establish the database foundation and store a full inventory.
5. Visualize the full inventory.
6. Validate the downstream-ready inventory model.
7. Build the vault cleaner.
8. Build the saved loadout manager.
9. Synchronize saved loadouts to in-game loadout slots.

---

## Plan Overview

Rebuild the Destiny web application through small, working increments. Each
goal must produce something usable, be verified by the user, and satisfy its exit criteria
before work begins on the next goal.

The old implementation is reference material only. Code may be copied from it
only after it has been reviewed, simplified, and verified within the current
goal.

### Working Rules

1. Work on one goal at a time.
2. Keep each goal small enough to understand and verify independently.
3. Define user acceptance criteria before implementation.
4. Make each result directly verifiable through the running application.
5. Record the manual verification steps for the user.
6. Do not advance until the user confirms the acceptance criteria are met.
7. Avoid speculative abstractions. Add structure when a demonstrated use case
   requires it.
8. Keep credentials and other secrets out of source control.
9. Preserve a runnable application at the end of every goal.
10. Update this document when a goal's scope or decisions change.

### Initial Technical Direction

These are starting assumptions, not irreversible commitments:

- Python and `aiohttp` for the web application.
- Server-rendered HTML initially; introduce browser-side complexity only when a
  feature needs it.
- SQLite for local persistence.
- Bungie's HTTP API behind a small application-owned client rather than calling
  it directly from route handlers.
- Configuration through environment variables.
- User-driven verification through the running application.

The first goals intentionally avoid designing the final database schema.
The schema should follow the real data flows discovered while implementing
authentication and reading a Destiny profile.

### Product Direction

The application has two primary item-focused features:

1. A vault cleaner that identifies items the user may want to dismantle and
   explains every recommendation.
2. A loadout manager that groups application-owned loadouts into saved sets and
   can deliberately synchronize a saved set into selected in-game loadout
   slots.

The database, item synchronization, and manifest work should be judged by how
well they support these two features.

### Definition of Done for Every Goal

A goal is complete only when:

- Its user verification steps have been completed.
- The user confirms that the result is acceptable.
- The application starts without warnings caused by our code.
- Failure behavior is understandable to a user or developer.
- Setup or configuration changes are documented.
- No credentials, tokens, generated databases, or local session secrets are
  tracked by Git.
- The goal's exit checklist is marked complete in this document.

---

## Goal 1: Establish a Clean Rebuild Foundation

### Objective

Create a clean application area without deleting the old implementation until
we have extracted everything worth preserving.

### Scope

- Decide where the rebuilt package will live.
- Treat existing modules as legacy reference code.
- Establish the supported Python version.
- Maintain the runtime dependency list in `requirements.txt`.
- Add configuration and secret-file conventions.
- Establish `main.py` as the stable application entry point.
- Document the commands used to set up, run, and verify the application.

### Verification

- A clean environment can import the new application package.
- A source scan confirms that known credentials are not present in the rebuilt
  code.
- The user reviews and accepts the new project boundary and documentation.

### Exit criteria

- [x] New and legacy code have an unambiguous boundary.
- [x] Supported Python version is documented.
- [x] Setup, run, and verification commands are documented.
- [x] Local environment and database files are ignored by Git.
- [ ] Previously exposed Bungie credentials have been rotated.

---

## Goal 2: Host and Access a Minimal Site

### Objective

Prove that the application can start, accept an HTTP request, render HTML, and
load a small piece of application data.

### User-visible result

Opening the local site displays a page containing:

- A "Destiny Web App" heading.
- A hello-world message.
- A small value supplied by the Python application, such as application status
  or version.

This value must be passed into the page by the route handler rather than being
entirely hard-coded in the HTML.

### Scope

- Application factory.
- One route: `GET /`.
- One HTML template.
- Development configuration.
- Startup and shutdown lifecycle.
- Basic request/error logging.
- Health endpoint: `GET /health`.

### Verification checklist

- `GET /` returns HTTP 200.
- The response is HTML.
- The page contains the expected heading and injected data value.
- `GET /health` returns HTTP 200 and a small JSON response.
- An unknown route returns HTTP 404.

### User verification steps

1. Start the application using the documented command.
2. Open the site in a browser.
3. Confirm that the page renders and displays the injected value.
4. Refresh the page and confirm the server remains healthy.
5. Open `/health` and confirm the JSON response is visible.

### Exit criteria

- [x] The page behavior matches the verification checklist.
- [x] The user accepts the browser result.
- [x] The application has one clear startup path.
- [x] No Bungie credentials or database are required yet.

---

## Goal 3: Complete the Bungie OAuth Cycle

### Objective

Allow a user to sign in through Bungie and prove the authenticated identity by
displaying the user's Bungie name.

### User-visible result

- The home page offers "Sign in with Bungie."
- The user is redirected to Bungie for authorization.
- Bungie redirects back to the application.
- The application exchanges the authorization code for tokens.
- The page displays the authenticated Bungie username.
- The user can sign out and return to the anonymous state.

### Scope

- Environment-based Bungie client ID, client secret, API key, and redirect URI.
- Login, callback, and logout routes.
- Cryptographically random OAuth `state`.
- State validation during the callback.
- Encrypted or server-side session handling.
- Token exchange.
- Call Bungie's current-user memberships endpoint.
- Persist the Bungie identity, access token, refresh token, and expirations in
  SQLite.
- Store only an opaque session identifier in the browser.
- Restore sign-in sessions after an application restart.
- Refresh access tokens automatically before expiration.
- Friendly handling for denial, invalid callbacks, and Bungie API failures.

The old authentication implementation may be consulted for endpoint details and
previously working behavior, but it should not be imported into the rebuilt app.

### Verification checklist

- Login creates state and redirects to the expected Bungie authorization URL.
- Callback rejects missing or incorrect state.
- Callback handles a denied authorization.
- Callback exchanges a valid code through the Bungie client.
- Authenticated user information is placed in the session.
- The home page displays the Bungie username after login.
- Non-secret session status shows token expiration and last-refresh times.
- Restarting the application preserves the browser sign-in.
- An expired access token is refreshed without another interactive login.
- Logout clears the authenticated session.
- Tokens and secrets never appear in rendered HTML or logs.

### User verification steps

1. Configure a development Bungie application and local credentials.
2. Start the site.
3. Select "Sign in with Bungie."
4. Authorize the application on Bungie.
5. Confirm that the browser returns to the site.
6. Confirm that the correct Bungie username is displayed.
7. Open `/auth/status` and confirm that stored-session and expiration data are
   shown without either token value.
8. Stop and restart the application, then confirm that the sign-in remains.
9. Sign out and confirm that the username is no longer displayed.

### Exit criteria

- [ ] The Bungie login acceptance flow passes.
- [ ] Invalid OAuth state is rejected.
- [x] No credential or token is committed or printed.
- [ ] Login survives page refresh and application restart.
- [ ] Access tokens refresh automatically from the stored refresh token.
- [ ] Logout works.

---

## Goal 4: Establish the Database Foundation and Store a Full Inventory

### Objective

Create the durable server-side data foundation for authentication, logged-in
users, and future Bungie API data, then prove it with the application's first
substantial API workflow: fetch and persist the authenticated user's complete
inventory as one coherent snapshot. Stored data must carry enough provenance
and timing information for the application to decide whether it is fresh,
stale, or due for refresh.

### User-visible result

An authenticated user can open a database-status page and see:

- Their saved Bungie identity.
- Whether their login session is active.
- Their selected Destiny membership and characters.
- Separate item counts for the vault and each character's carried and equipped
  items.
- The total number of stored items, duplicate instance count, and broken
  reference count.
- When the inventory was fetched, when it becomes stale, and whether the
  application would use the cache or request fresh data.

No credentials, access tokens, refresh tokens, session identifiers, or raw
security-sensitive values are displayed.

### Storage strategy

Use a hybrid schema:

- Dedicated relational tables for stable, security-sensitive records such as
  users, linked Bungie accounts, OAuth tokens, and web sessions.
- A general user-scoped resource table for evolving Bungie API responses.
  Resources are identified by a resource type and stable resource key, with
  the response retained as JSON.
- First-class inventory snapshots and item records for the two primary
  downstream features.
- Explicit synchronization metadata records fetch attempts, successful fetches,
  freshness windows, errors, and source/version details.
- Schema migrations allow the database to evolve without deleting existing
  sessions or cached user data.

This flexible resource layer is not a substitute for all future domain tables.
When a downstream feature demonstrates a need for indexed or relational fields,
promote those fields into purpose-built tables while retaining the raw source
response when useful.

### Scope

- One application-owned database boundary with repositories or clearly grouped
  persistence methods; route handlers must not contain SQL.
- Versioned, repeatable SQLite migrations that preserve existing data.
- Stable tables for users, Bungie accounts, OAuth tokens, and web sessions.
- A flexible `user_resources`-style store keyed by user, resource type, and
  resource key.
- JSON payload storage for raw or lightly processed Bungie responses.
- A Bungie API client and inventory synchronization service outside route
  handlers.
- Fetch current memberships and select the cross-save primary Destiny
  membership correctly.
- Fetch the profile, character, profile-inventory, character-inventory,
  character-equipment, item-instance, stat, socket, reusable-plug, perk,
  objective, and relevant crafting components needed by the known use cases.
- An inventory synchronization record that identifies one coherent fetch and
  prevents data from different refreshes from being mistaken for one snapshot.
- One inventory snapshot must cover the profile vault and every character
  returned by Bungie, including carried and equipped items. It must not assume
  that the account has exactly one Titan, Hunter, and Warlock.
- Item records keyed by Bungie's item instance ID where available, including
  item hash, owner, location, bucket, quantity, state, and first/last-seen
  timestamps.
- Item component data needed for vault-cleaner comparisons and loadout
  reconstruction, including stats, sockets, plugs, perks, objectives, and
  crafting state when Bungie provides them.
- Stable user/item-instance identities and schema-evolution rules that allow
  purpose-built loadout and cleaner tables to be added later without
  duplicating full item payloads. Actual saved-loadout records belong to Goal
  8.
- UTC timestamps for creation, update, fetch, expiration, and staleness.
- Refresh metadata including last attempt, last success, next refresh time,
  source/version, and a safe error summary.
- Explicit cache states such as missing, fresh, stale, and refresh-failed.
- Upsert behavior that avoids duplicate users, accounts, sessions, and resource
  records.
- Explicit transaction handling and foreign-key enforcement.
- Cleanup rules for expired sessions and replaceable cached data.
- Database path configured outside the package source.
- A small authenticated database-status page for user-driven verification.
- A manual "Synchronize inventory" action.

Tokens are sensitive. The accepted local-development posture stores them in an
owner-only SQLite file. Encryption at rest and key management must be decided
before any deployment beyond a trusted local machine.

### Verification checklist

- A new database initializes through the complete migration history.
- The current Goal 3 database upgrades without losing the authenticated user,
  tokens, or web session.
- Bungie response envelopes are validated and API errors are distinguished from
  HTTP or network failures.
- Cross-save primary membership selection is correct.
- One successful synchronization stores the vault, every character inventory,
  and every character's equipped items under one completed snapshot.
- The displayed total equals the sum of all displayed location counts.
- Duplicate item-instance IDs and broken item-owner references are both zero.
- Restarting the application preserves the login and saved server-side data.
- Repeatedly saving the same user or resource updates one logical record rather
  than creating duplicates.
- Five repeated synchronizations of unchanged inventory do not duplicate users,
  accounts, characters, or the current item records.
- Two resource types and multiple resource keys can be stored for one user
  without schema changes.
- A coherent inventory snapshot can be saved and its items queried by instance,
  definition, location, bucket, and owner.
- Durable item-instance identities survive snapshot pruning and can be
  referenced by future purpose-built tables without duplicating item payloads.
- The status page correctly distinguishes missing, fresh, stale, and failed
  resource states.
- Refresh decisions use stored timestamps rather than refreshing every request.
- A failed refresh retains the last usable payload and records a safe failure
  state.
- A failed multi-record write rolls back without partial data.
- An interrupted synchronization never replaces the last complete active
  snapshot with a partial snapshot.
- One user's resources cannot be returned while another user is authenticated.
- Cached inventory and status views render in approximately 250 milliseconds or
  less locally, excluding Bungie network time.
- SQLite's integrity check succeeds.
- Tokens and session secrets never appear on the status page or in logs.

### User verification steps

1. Log in through Bungie.
2. Select "Synchronize inventory."
3. Open the database-status page and confirm the saved identity, membership,
   active session, inventory timestamp, and freshness state are shown without
   secret values.
4. Confirm that the vault and every Destiny character are listed separately
   with carried and equipped item counts.
5. Confirm that the displayed total equals the sum of those counts and that
   duplicate and broken-reference counts are zero.
6. Compare representative items and location counts with Destiny or the
   official companion application.
7. Refresh the page repeatedly and confirm that fresh cached data is reused.
8. Synchronize unchanged inventory repeatedly and confirm the current counts do
   not grow from duplication.
9. Stop and restart the application, then confirm the login and full inventory
   remain available.
10. Use a development-only verification control to mark the inventory stale and
    confirm that a new refresh is required.
11. Trigger a controlled failed or interrupted refresh and confirm that the
    prior complete inventory remains available with a non-sensitive warning.

### Exit criteria

- [ ] Existing authentication data migrates without loss.
- [ ] Session and token persistence are verified through the application.
- [ ] Flexible user-resource storage is verified with multiple resource types.
- [ ] A real Bungie synchronization stores the vault and every character's
  carried and equipped items as one coherent snapshot.
- [ ] Inventory totals, uniqueness, ownership, and locations are verified
  against the live account.
- [ ] Repeated synchronization does not create duplicate current records.
- [ ] Timestamp-based freshness and refresh decisions are verified.
- [ ] Failed refreshes preserve the last usable data.
- [ ] Partial snapshots can never become the active inventory.
- [ ] Schema migrations are repeatable and preserve existing data.
- [ ] Cross-user data isolation is verified.
- [ ] Database integrity and cached-read performance targets pass.
- [ ] Route handlers do not contain SQL.

---

## Goal 5: Visualize the Full Inventory

### Objective

Turn the complete inventory snapshot from Goal 4 into a useful, responsive,
read-only inventory screen inspired by DIM's information layout. Resolve the
required manifest definitions so Bungie hashes become recognizable items.

### User-visible result

An authenticated inventory page displays:

- A header card for each Destiny character.
- Each character's equipped and carried items, grouped by inventory bucket.
- A separate shared-vault area, grouped by item category or bucket.
- Item tiles with icon, name, rarity, power, quantity, and important state
  indicators when applicable.
- A detail view with the stored instance stats, perks, sockets, and location.
- Inventory age, stale status, synchronization status, and item counts.
- Search and basic filters that work across characters and the vault.

The visual hierarchy may follow DIM's proven character-and-vault organization,
but the application will use its own styling, templates, and implementation.
This goal remains read-only: dragging, transferring, equipping, locking, or
dismantling items is not enabled.

### Scope

- Fetch Destiny manifest metadata.
- Detect manifest version changes.
- Choose and document either the official SQLite manifest or JSON manifest.
- Cache the definitions required to render the stored inventory.
- Resolve class, inventory item, bucket, stat, and plug-set definitions as
  needed.
- Keep manifest data separate from live player data.
- Render from the last complete database snapshot rather than calling Bungie
  directly from the page route.
- Character columns or cards with clearly separated equipped and carried gear.
- Shared-vault grid grouped consistently with character inventory buckets.
- Reusable item-tile and item-detail components.
- Visual indicators for equipped, locked, crafted, masterworked, exotic, and
  missing-definition states when the stored data supports them.
- Search by item name and basic filters for character, location, bucket, item
  type, rarity, and locked state.
- Responsive behavior for desktop, tablet, and narrow/mobile screens.
- Safe icon and text fallbacks for redacted or unresolved definitions.
- Empty, stale, loading, and synchronization-failure states.

### Verification checklist

- Current manifest versions do not trigger a download.
- Changed versions trigger an update.
- Interrupted updates do not destroy the usable local manifest.
- Known live hashes resolve to expected definitions.
- Unknown or redacted hashes produce a safe fallback.
- Every item from the active database snapshot appears exactly once in the
  unfiltered inventory view.
- Displayed totals match the database-status totals for the vault, each
  character's inventory, and each character's equipment.
- Equipped, carried, and vaulted items are visually unambiguous.
- Representative icons, names, power values, perks, stats, and lock states
  agree with Destiny or the official companion application.
- Search and filters never change the underlying snapshot or persisted item
  counts.
- The page remains usable at common desktop and mobile widths.
- No Bungie write-action endpoint is called.

### User verification steps

1. Synchronize the complete inventory in Goal 4.
2. Open the inventory page and confirm that every character and the vault are
   present.
3. Compare the displayed section counts with the database-status page.
4. Compare representative equipped, carried, and vaulted items with Destiny or
   the official companion application.
5. Open representative weapon and armor details and verify their visible
   instance information.
6. Search for a known item and exercise each basic filter.
7. Resize the page or open it on a narrow display and confirm the inventory
   remains navigable.
8. Restart without refreshing Bungie data and confirm the stored inventory and
   cached definitions still render.

### Exit criteria

- [ ] All active-snapshot items render exactly once in the correct owner and
  location.
- [ ] Character, equipment, inventory, and vault counts agree with Goal 4.
- [ ] Representative item details agree with live Destiny data.
- [ ] Search, filtering, responsive layout, and fallback states are accepted.
- [ ] Version-aware updates work.
- [ ] Cached definitions work offline.
- [ ] Live player snapshots do not duplicate the full manifest.
- [ ] The inventory page is read-only and invokes no Bungie item actions.

---

## Goal 6: Validate the Downstream-Ready Inventory Model

### Objective

Enrich and validate the stored full inventory so it is a reliable, queryable
input for the vault cleaner and loadout manager.

### User-visible result

An inventory page displays gear from:

- Vault.
- Character inventories.
- Character equipment.

Items can be grouped by character, location, and bucket.

### Scope

- Load the complete inventory captured in Goal 4.
- Join live item instances with manifest definitions.
- Preserve instance ID, item hash, location, bucket, state, stats, sockets, and
  relevant ownership data.
- Define deterministic handling for missing components.

### Verification checklist

- Sample items from all locations appear exactly once.
- Equipped, carried, and vaulted locations are distinguished correctly.
- Non-instanced items do not break processing.
- Locked and crafted state bits are interpreted correctly.
- Cross-character aggregation is deterministic.

### User verification steps

Compare a representative sample against the official Destiny companion app or
in-game inventory.

### Exit criteria

- [ ] Inventory aggregation is verified against live data.
- [ ] Manual sample agrees with the live inventory.
- [ ] No write-action Bungie endpoints are used.

---

## Goal 7: Build the Vault Cleaner

### Objective

Analyze the user's stored inventory and produce understandable, conservative
recommendations for items that may be safe to dismantle to reclaim vault space.

### User-visible result

The vault cleaner groups comparable items, recommends specific item instances
for review, and explains the evidence behind each recommendation. The user can
protect an item from future recommendations.

The application does not dismantle items. The user performs dismantling in
Destiny after reviewing the recommendations.

### Scope

- Implement the weapon analyzer before the armor analyzer.
- Group weapon copies by their exact Destiny inventory-item definition.
- Model every independently selectable gameplay socket column, including
  barrels, magazines, batteries, weapon-specific equivalents, main traits, and
  origin traits.
- Find the minimum retained set whose column-tagged options cover every unique
  option observed across the user's copies. Do not require every cross-column
  combination to remain on one physical copy.
- Restrict review candidates to unlocked, non-crafted, non-Exotic vault items
  that are not referenced by a stored in-game loadout; protected items still
  contribute coverage and unresolved loadout references remain preserved.
- Prefer masterworked and higher-power copies when several minimum retained
  sets are equivalent.
- Persist each analysis with its inventory snapshot, manifest version, and
  ruleset version.
- Define comparison groups by item type, archetype, class, slot, and other
  relevant manifest properties.
- Compare rolls using instance stats, perks, sockets, crafting state, and
  ownership context.
- Exclude locked items and user-protected items by default.
- Exclude items referenced by application-owned or in-game loadouts by default.
- Treat crafted, enhanced, high-investment, and otherwise unusual items
  conservatively.
- Store recommendation runs with the inventory snapshot and ruleset version
  that produced them.
- Store user decisions such as protect, ignore, keep, and reviewed.
- Make every recommendation's reasoning inspectable.
- Render each observed perk with the official manifest icon, name, and
  description, available on hover and keyboard focus.
- Never call a dismantle or destructive item endpoint.
- Offer an explicit organization action that unlocks review candidates, locks
  all other lockable vault weapons, and verifies the resulting state from a
  fresh profile before reporting completion.

### Armor cleaner increment

Armor coverage is measured across usable build combinations rather than as an
exact collection of individual rolls. The analyzer must find a retained set
that preserves the user's chosen build possibilities while respecting a hard
armor-storage budget.

#### Stage 1: Audit and normalize armor data

- Inventory every Armor 3.0 instance by class, slot, tier, archetype, source,
  armor set, set bonus, stat distribution, tuning socket, lock state, location,
  and loadout references.
- Separate Exotics, class items, and legacy armor where their comparison rules
  differ; never silently compare incompatible armor systems.
- Resolve raid, dungeon, destination, world, seasonal, event, and other source
  categories from manifest/activity data, with an explicit `unknown` category.
- Present counts and missing-data warnings before making recommendations.

#### Stage 2: Define a saved armor-cleaner policy

- Select the classes and slots to analyze.
- Set a total retained-item budget, with optional per-class and per-slot caps.
- Exclude chosen stats from useful roll positions; Health can be excluded
  without assuming that incidental points make an otherwise useful item bad.
- Select desired stats and optional minimum build-stat targets.
- Configure tuning alignment as `required`, `preferred`, or `ignored`.
  Alignment means the tuned stat belongs to the item's accepted stat focus;
  the exact accepted positions must be visible in the policy.
- Configure source-specific retention profiles. The initial defaults should
  preserve more near-best raid armor, fewer dungeon alternatives, and only the
  strongest destination/easy-to-refarm alternatives.
- Allow each source profile to set a tier floor, score tolerance, and maximum
  number of alternatives instead of embedding source behavior in code.
- Configure required armor-set coverage: four-piece bonuses, two-plus-two set
  combinations, selected sets only, or no set-bonus obligation.
- Always protect equipped, locked, user-protected, and loadout-referenced armor
  unless the user explicitly changes the applicable safety rule.

Before implementation, manually approve the meanings of stat exclusion,
tuning alignment, source categories, set-bonus obligations, and the storage
budget. Policy changes must produce a preview and must not alter live items.

#### Stage 3: Produce conservative, explainable candidates

- Remove strictly dominated items first. Dominance comparisons must remain
  within compatible class, slot, armor-set context, source policy, tuning
  policy, and protected-state rules.
- Define a coverage scenario from class, Exotic-slot assumption, legendary
  slots, required set-bonus pattern, desired stat targets, and tuning policy.
- Measure every scenario against the full eligible collection as the benchmark.
- Select the smallest retained set, or the best set within the configured
  budget, that preserves the benchmark scenarios within their allowed score
  tolerances.
- Use deterministic tie-breakers and prefer protected or expensive-to-replace
  pieces when otherwise equivalent.
- If the requested coverage cannot fit the budget, report the conflict and rank
  the scenarios that would be lost; never silently exceed the budget or discard
  a requirement.
- Explain every candidate as dominated, unused by any retained scenario, or
  excluded by a named policy rule, and identify the alternatives that preserve
  its coverage.

#### Stage 4: Visualize and validate the result

- Show kept and candidate armor by class and slot, with stat bars, tier,
  archetype, tuning alignment, armor set, source, protection state, and reason.
- Show retained count versus budget, projected space recovered, and retained
  four-piece and two-plus-two set coverage.
- Compare full-inventory and retained-set results with scenario coverage rate,
  number of infeasible scenarios, and maximum and typical build-score loss.
- Show retention counts by source so the selected raid, dungeon, and
  destination tolerances can be inspected.
- Provide a policy sensitivity preview showing how many items and scenarios
  change when a rule is toggled.
- Reuse the verified lock/unlock workflow only after the recommendations and
  policy behavior have been manually accepted as a separate implementation
  increment.

#### Armor verification gate

- No candidate violates an enabled protected-item rule.
- No retained item violates a hard excluded-stat or tuning-alignment rule.
- Retained count does not exceed the configured budget.
- The same snapshot and policy produce the same result.
- Every lost scenario and every score reduction is visible.
- Representative builds recreated from the retained set match the analyzer's
  feasibility result when checked in Destiny or DIM.
- Tightening raid, dungeon, or destination tolerance changes only the expected
  source categories and produces an understandable explanation.
- Toggling Health exclusion and tuning alignment produces the expected,
  manually reviewed change before either rule is trusted.

### Verification checklist

- Every recommendation identifies an exact item instance.
- The compared alternatives and scoring factors are visible.
- Perk icons match Destiny and every perk description is reachable with both a
  pointer and keyboard.
- Locked, protected, equipped, and loadout-referenced items are not recommended
  under the default policy.
- Stale inventory produces a warning and requires refresh before a final review.
- Re-running the same rules against the same snapshot is deterministic.
- Changed inventory creates a new analysis rather than silently rewriting the
  evidence behind an old recommendation.

### User verification steps

1. Synchronize the full inventory.
2. Run the vault cleaner.
3. Compare representative recommendations with the same items in Destiny or the
   official companion application.
4. Protect one recommended item and confirm that it is removed from the active
   recommendation list.
5. Refresh the inventory and confirm that recommendations are recalculated from
   the new snapshot.

### Exit criteria

- [ ] Recommendation rules and safety exclusions are documented.
- [ ] The user verifies representative weapon and armor comparisons.
- [ ] Recommendations are deterministic and tied to an inventory snapshot.
- [ ] No Bungie dismantle action exists in the application.

---

## Goal 8: Build the Saved Loadout Manager

### Objective

Allow users to create, edit, version, group, and inspect application-owned
loadouts and named loadout sets without changing live Destiny state.

### User-visible result

The user can save a named loadout from current equipment or build one from
stored inventory data, then organize loadouts into named sets with explicit
in-game slot mappings. Saved loadouts and sets remain available after restart
and show missing, moved, or changed items before any live action is attempted.

### Scope

- Application-owned loadouts separate from Bungie's limited in-game slots.
- Name, description, character class, timestamps, and revision history.
- Named loadout sets containing ordered loadout entries and optional in-game
  slot mappings.
- Exact item-instance selections where required.
- Selected sockets, plugs, subclass configuration, and cosmetics where the API
  exposes enough information.
- Import from current equipment.
- Import from an existing in-game loadout.
- Compare a saved loadout with current equipment and current inventory.
- Detect missing or stale item references.
- Protect all referenced item instances from vault-cleaner recommendations by
  default.
- No Bungie write endpoints in this goal.

### Verification checklist

- A loadout round-trips through the database without losing item or plug
  selections.
- A loadout set preserves its membership, order, and in-game slot mappings.
- Editing creates an understandable new revision.
- Loadouts remain available after application restart.
- Deleted or missing Destiny item instances are reported without corrupting the
  saved loadout.
- Vault-cleaner exclusions update when loadout contents change.

### User verification steps

1. Save the currently equipped items as an application loadout.
2. Create a named loadout set and assign the loadout to a slot.
3. Restart the application and confirm the loadout and set remain.
4. Edit the loadout and confirm its selected items and revision are correct.
5. Confirm its item instances are protected from vault-cleaner recommendations.
6. Compare the saved loadout with current equipment.

### Exit criteria

- [ ] Local loadout and loadout-set creation, editing, and persistence are
  verified.
- [ ] Current equipment and in-game loadout import are verified.
- [ ] Missing-item handling is verified.
- [ ] Vault-cleaner protection integration is verified.
- [ ] No live Destiny state is changed.

---

## Goal 9: Synchronize Saved Loadouts to In-Game Loadout Slots

### Objective

Deliberately replace selected in-game loadout slots with the mapped entries
from an application-owned loadout set using Bungie's supported item and loadout
actions.

### Bungie API constraint

Bungie's snapshot action records the character's currently equipped items into
the selected in-game loadout slot. It does not accept an arbitrary saved item
list as the snapshot contents. Synchronization therefore requires a controlled
workflow:

1. Validate the saved loadout and live inventory.
2. Move required items to the target character when permitted.
3. Equip and configure the saved loadout as far as the API supports.
4. Snapshot the character's equipped state into the selected in-game slot.
5. Update the slot's supported name, icon, and color identifiers.
6. Fetch the in-game loadouts again and verify the resulting slot.

The first accepted write operation targets one slot. Full-set synchronization
is enabled only after the single-slot workflow and recovery behavior have been
verified.

A full-set synchronization runs sequentially because each saved loadout must
become the character's live equipped state before Bungie can snapshot it:

1. Save the character's original equipment state.
2. Prepare, equip, snapshot, and verify one mapped loadout slot.
3. Record a durable checkpoint for that slot.
4. Continue with the next mapped loadout only after the prior slot succeeds.
5. Restore the original equipment when the set completes, or offer a clearly
   labeled recovery action if automatic restoration cannot finish.

The user can see progress throughout the operation. A page refresh or
application restart must not lose which slots succeeded, failed, or remain
pending.

### Safety requirements

- Never delete or dismantle items through the application.
- Present the target character, in-game slot, item movements, equipment
  changes, socket changes, and unsupported settings before execution.
- Require explicit confirmation immediately before the live operation.
- Require a fresh inventory and loadout read before generating the action plan.
- Respect Bungie throttling and action restrictions.
- Give each synchronization attempt an operation ID and make individual steps
  idempotent where possible.
- Persist per-loadout and per-action checkpoints so a long-running set can be
  inspected and safely resumed.
- Record action attempts, outcomes, and Bungie error codes without recording
  tokens.
- Stop safely on partial failure, fetch live state again, and report the exact
  resulting state.
- Preserve the character's original equipment state and restore it after the
  final snapshot when possible.
- Never imply that unsupported settings, such as artifact configuration, were
  synchronized.

### Verification checklist

- No action occurs without confirmation.
- Invalid, stale, missing, or newly locked item state prevents execution.
- Only the explicitly selected in-game slot is targeted.
- Bungie errors and throttling are reported clearly.
- Retrying an interrupted operation cannot unintentionally target another slot.
- Partial failures trigger fresh inventory, equipment, and loadout reads.
- The final in-game slot is fetched and compared with the saved loadout.
- Progress survives a page refresh and application restart.
- Successful slots are not repeated when a multi-slot operation resumes.
- Original equipment is restored, or the user receives an exact recovery plan.

### User verification steps

1. Select a saved loadout set and one expendable mapped in-game loadout slot.
2. Review the complete proposed action plan.
3. Confirm the operation.
4. Verify in Destiny that the intended slot was replaced.
5. Return to the application and confirm that the post-operation comparison
   reports the actual result and any unsupported differences.
6. After the single-slot flow is accepted, preview and synchronize a small
   multi-slot set and verify each resulting slot.

### Exit criteria

- [ ] The required Bungie application permission is configured and verified.
- [ ] Preview and confirmation behavior are verified.
- [ ] One controlled in-game loadout replacement succeeds.
- [ ] A controlled multi-slot loadout-set synchronization succeeds.
- [ ] Interrupted-operation resume and original-equipment restoration are
  verified.
- [ ] The resulting inventory, equipment, and in-game loadout are resynchronized
  and verified.
- [ ] Partial-failure reporting is verified without risking another slot.

---

## Verification Strategy

Verification is performed through the running application:

1. The implementation is made available locally.
2. The user follows the current goal's verification steps.
3. The user reports failures or undesired behavior.
4. The current goal is revised and presented again.
5. Work advances only after the user explicitly accepts the result.

Developer sanity checks such as syntax validation, import checks, credential
scanning, and TLS configuration checks may be run before handoff. They support
the review but do not replace user acceptance.

## Proposed Application Boundaries

```text
Browser
  |
Routes and templates
  |
Application services
  |-------------------|
Bungie API client   Repositories
  |                   |
Bungie.net           SQLite
```

- Routes translate HTTP input and output.
- Services implement application workflows.
- The Bungie client handles Bungie HTTP details and response envelopes.
- Repositories own SQL and serialization.
- Domain functions transform profile and manifest data without depending on
  aiohttp, sessions, or global application state.

## Decisions and Remaining Architecture Gates

The accepted foundation now uses:

- Relational current-item records plus three retained raw profile snapshots.
- Bungie's official SQLite manifest in a separate local database.
- Server-rendered HTML with focused vanilla JavaScript for inventory
  interaction.
- User-triggered synchronization rather than background jobs.

The following decisions remain gated until their relevant goal:

- Token encryption at rest and key management for deployment.
- Reference-aware snapshot retention before durable cleaner evidence or saved
  loadouts depend on historical snapshots.
- Whether background synchronization becomes necessary.
- Whether later interactions justify a browser-side framework.
- Deployment topology, production TLS, and database operations.

## Foundation Review Guardrails

The Goals 1–5 review established these requirements for later goals without
implementing them early:

- Normalize observed in-game loadout slots and preserve unresolved item
  references before loadout import or vault-cleaner exclusions are built.
- Make snapshot pruning reference-aware before any cleaner result or saved
  loadout can depend on historical evidence.
- Record the manifest version and materialized scoring inputs with future
  cleaner results so a manifest update cannot silently change old evidence.
- Require an explicit Destiny-account choice for non-cross-save users before
  any future write action.
- Move OAuth access-token acquisition into a dedicated service before
  background synchronization is introduced.
- Decide token encryption at rest, rotate the historically exposed Bungie
  credentials, and replace the legacy development TLS key before deployment.

## Progress Log

Record goal completions and important decisions here.

| Date | Goal | Result | Evidence |
| --- | --- | --- | --- |
| 2026-07-30 | Goal 1 | Awaiting user acceptance and credential rotation | Package import, credential scan, metadata check, legacy boundary check, and development TLS certificate load succeeded using `.DesintyWebAppVenv`. |
| 2026-07-30 | Goal 2 | Accepted | User verified the HTTPS home page and health flow. |
| 2026-07-30 | Goal 3 | Partially verified | User completed the real Bungie sign-in flow. Durable SQLite sessions, token storage, logout, non-secret status, and automatic refresh are implemented; restart, refresh, and logout acceptance remain open. |
| 2026-07-30 | Goal 4 | Accepted for progression | User accepted the database scorecard and real snapshot: 1,830 items, a 1,311-item vault, and three characters with zero duplicate instances or broken owner references. Deeper failure and isolation checks remain listed rather than being silently claimed. |
| 2026-07-30 | Goal 5 | Awaiting user verification | A responsive read-only inventory workspace, versioned official SQLite manifest cache, character and vault groups, lightweight item tiles, search and filters, and a lazy item-detail drawer are implemented. The active snapshot renders all 1,830 rows exactly once in developer checks; visual and live-definition acceptance remain with the user. |
| 2026-07-30 | Goals 1–5 review | Awaiting user verification | Foundation audit fixed atomic migrations, schema ownership/history constraints, Postmaster handling, strict component completeness, membership refresh, refresh-token races, CSRF, query-safe OAuth logging, manifest identity/validation/caching, refresh-failure visibility, owner-safe and data-driven Goal 5 filters, explicit stale state, separate vault/profile inventory, labeled inactive detail components, accessible search, and safe error rendering. Schema 4 preserved the real 1,830-item snapshot and 1,671 instance histories with clean integrity and foreign keys. The HTTPS server, repeated thread offloads, health endpoint, and clean shutdown passed outside the restricted verification sandbox. No Goal 6+ feature was implemented. |
| 2026-07-30 | Goal 7 weapon increment | Awaiting user verification | Schema 5 and `/cleaner/weapons` store and display a snapshot-/manifest-bound minimum coverage analysis across all gameplay roll columns. The current snapshot scans 732 weapons and identifies 121 unlocked, non-crafted, non-Exotic, non-loadout-referenced vault copies for review across 39 groups while retaining every observed column-tagged option. It preserves 96 currently unresolved in-game loadout references rather than treating them as items. No armor scoring, dismantle action, or Bungie item-write call was added. |
| 2026-07-31 | Goal 7 perk visualization | Awaiting user verification | Weapon copies now render Destiny perk icons and accessible hover/focus descriptions from the exact official manifest recorded with the analysis. The current result resolves 494 unique observed perks with complete icon and description coverage; the saved catalog is deduplicated, ruleset-versioned, and leaves community research as a distinct future layer. |
| 2026-07-31 | Goal 7 weapon organization | Awaiting user verification | The cleaner now offers an explicit confirmed action that force-refreshes and re-analyzes inventory, unlocks exact review candidates, locks all other lockable vault weapons through Bungie's supported item-state endpoint, paces and retries requests, refreshes again, and reports verified versus mismatched exact instances. A partial operation can be safely rerun because the plan is refreshed and recomputed before already-correct states are skipped. No dismantle, transfer, or equip endpoint is called. |
| 2026-07-31 | Goal 7 organization progress | Awaiting user verification | Weapon organization now runs as an authenticated in-process job with a non-cacheable progress endpoint. The cleaner displays completed/total actions, elapsed-rate ETA, a filling accessible bar, verification and retry stages, and reconnects to an active job after a page reload. |
