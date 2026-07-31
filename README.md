# Destiny Web App

This directory contains the clean rebuild of the Destiny web application.
Development follows the gated goals in [plan.md](plan.md): a goal is not
complete until the user verifies and accepts its result.

The previous application in `../DestinyWebAPp` is legacy reference material.
The rebuilt package must not import from it.

## Requirements

- Python 3.12 through 3.14

Python 3.14 is the preferred local version and is recorded in
`.python-version`.

## Project layout

```text
DestinyWebApp/
├── main.py               # Stable application entry point
├── src/destiny_web_app/  # Rebuilt application package
├── scripts/              # Project verification tools
├── .env.example          # Safe configuration template
├── pyproject.toml         # Package and tool configuration
├── requirements.txt      # Runtime dependency list
└── plan.md                # Gated implementation plan
```

## Set up

This project uses the existing `.DesintyWebAppVenv` virtual environment.
`main.py` adds the `src` directory to its import path. For direct package checks
that bypass `main.py`, use `PYTHONPATH=src`.

Install the declared runtime dependencies with:

```bash
.DesintyWebAppVenv/bin/python -m pip install -r requirements.txt
```

`requirements.txt` is the authoritative runtime dependency list and is also
used to generate installed-package metadata from `pyproject.toml`. A package is
added only when the current goal uses it.

## Configuration

Configuration will be supplied through environment variables. When local
configuration is needed, copy `.env.example` to `.env` and replace placeholders
locally:

```bash
cp .env.example .env
```

`.env` is ignored by Git. Real API keys, OAuth credentials, access tokens,
refresh tokens, databases, and session secrets must never be committed.

Goal 3 loads `.env` automatically at startup.

### Bungie OAuth configuration

1. Register or update the application in Bungie's developer portal.
2. Register this redirect URL exactly:

   ```text
   https://localhost:42697/redirect
   ```

3. Copy the safe template:

   ```bash
   cp .env.example .env
   ```

4. Add the rotated Bungie client ID, client secret, and API key to `.env`.
5. Keep `.env` local; it is ignored by Git.

`BUNGIE_CLIENT_ID` must be the numeric client ID assigned by Bungie, not the
application name and not the `<bungie-client-id>` placeholder.

The authorization request intentionally does not send a `scope` parameter,
because Bungie determines scopes from the application registration. It also
relies on the redirect URL registered in the Bungie portal rather than sending
the optional `redirect_uri` parameter.

### Development HTTPS certificates

The local HTTPS server introduced in Goal 2 will use the existing development
certificate pair:

```text
../DestinyWebAPp/webApp/util/CERT.pem
../DestinyWebAPp/webApp/util/KEY.pem
```

Their locations are configured by `DESTINY_SSL_CERT_PATH` and
`DESTINY_SSL_KEY_PATH` in `.env.example`. The private key remains outside this
project and must not be copied or committed here. These certificates are for
local development only; deployment must supply its own TLS termination or
certificate configuration.

## Run

Start the local HTTPS site through the stable project entry point:

```bash
.DesintyWebAppVenv/bin/python main.py
```

Then open:

- `https://localhost:42697/` for the home page.
- `https://localhost:42697/health` for application health data.
- `https://localhost:42697/auth/status` for non-secret sign-in and token-expiry
  status.
- `https://localhost:42697/data/status` for inventory synchronization and the
  database verification scorecard.
- `https://localhost:42697/data/status.json` for the same non-secret database
  metrics as JSON.
- `https://localhost:42697/inventory` for the read-only character and vault
  inventory.
- `https://localhost:42697/cleaner/weapons` for the read-only weapon coverage
  analyzer.

The reused development certificate expired on June 16, 2025. Until it is
replaced, a browser will display a certificate warning that must be bypassed for
local verification.

To change the host, port, or TLS files, set the corresponding variables before
starting:

```bash
DESTINY_PORT=8443 .DesintyWebAppVenv/bin/python main.py
```

## Verify

Each user-visible goal is verified manually by running and accessing the
application. Work does not advance until the user accepts the current goal.

Before a user handoff, run the standalone source credential scan:

```bash
.DesintyWebAppVenv/bin/python scripts/check_secrets.py
```

Developer sanity checks support the handoff but do not replace user acceptance.

## Authentication storage

After Bungie login:

- The Bungie identity and OAuth tokens are stored in
  `data/destiny.sqlite3`.
- The database is created with owner-only file permissions.
- The browser receives a secure, HTTP-only, same-site opaque session cookie.
- Only a SHA-256 hash of that session identifier is stored in SQLite.
- Access tokens are refreshed automatically shortly before expiration.
- Successful refreshes replace stored tokens and extend the saved session.
- Temporary Bungie outages keep the session for a later refresh retry.
- Rejected or expired refresh tokens end the session and require login again.

OAuth tokens are stored as plaintext inside the owner-readable local SQLite
database for this development phase. The Goals 1–5 foundation review keeps that
as an explicit local-development decision; encryption at rest must be decided
before deployment.

## Inventory persistence

Goal 4 stores one Bungie `GetProfile` response as one coherent inventory
snapshot. A snapshot contains:

- The selected cross-save primary Destiny membership.
- Every character returned by Bungie.
- Vault and other profile-level inventory.
- Each character's carried inventory.
- Each character's equipped items.
- Requested instance components such as stats, sockets, perks, objectives, and
  reusable plugs.
- Bungie's response-minted timestamp plus local fetch and stale timestamps.
- The unmodified profile response for future processing.

Snapshot writes are transactional. A new snapshot becomes active only after its
account, characters, items, and component data have all been saved. If a fetch
or write fails, the previous complete snapshot remains active.

Schema migration 4 adds database-level item-instance uniqueness, owner/location
constraints, Postmaster modeling, and nullable last-snapshot references. It
preserves existing authentication and inventory data during upgrade.

Schema migration 5 stores snapshot- and manifest-bound cleaner analyses.
Schema migration 6 enforces snapshot ownership and makes the same analysis
boundary ready for the later armor analyzer. Referenced snapshots are excluded
from ordinary snapshot pruning, and the ten newest analyses of each kind are
retained per user.

The default inventory freshness window is five minutes. Configure it with:

```text
DESTINY_INVENTORY_STALE_SECONDS=300
```

Only the three newest complete snapshots are retained per user. Durable item
instance records preserve first-seen and last-seen times independently of that
snapshot-retention window.

## Inventory visualization

Goal 5 renders the active saved snapshot without making a Bungie profile
request. Item names, artwork, buckets, stats, sockets, and perks are resolved
from Bungie's official SQLite manifest, stored separately at
`data/manifest.sqlite3`.

On the first visit to `/inventory`, select **Prepare item definitions**. The
application downloads and validates the current English manifest before
atomically replacing the local copy. Later checks skip the download when the
stored version is current, and an interrupted update leaves the previous usable
manifest intact. **Check definitions** remains available afterward so the
version check can be repeated without forcing a download.

The page is intentionally read-only. Search plus owner, location, bucket,
item-type, rarity, and locked-item filters operate on the saved snapshot.
Filter choices come from the current data, so uncommon tiers and unresolved
values remain selectable, and two characters of the same class remain
distinguishable. Equipped, carried, Postmaster, vault, and other profile-level
shared items remain separate. The item detail drawer excludes invisible
component rows while retaining visible inactive perks and disabled sockets with
explicit labels. None of these controls transfer, equip, lock, or dismantle
anything.

Manifest storage and language can be changed with:

```text
DESTINY_MANIFEST_PATH=data/manifest.sqlite3
DESTINY_MANIFEST_LANGUAGE=en
```

## Weapon vault cleaner

The first Goal 7 increment analyzes weapons only. For each specific weapon
definition, it finds the smallest retained set that contains every unique
selectable option in every gameplay socket column:

- Intrinsics and weapon frames.
- Barrels, magazines, batteries, blades, guards, bowstrings, arrows, tubes,
  rails, bolts, and similar weapon-specific columns.
- Main trait columns.
- Origin traits.

Coverage is column-aware: the same plug in two different columns is treated as
two requirements. The analyzer preserves each unique option, but it does not
require every cross-column combination to remain on one physical copy.

Only unlocked, non-crafted, non-Exotic vault copies that are absent from every
stored in-game loadout can be recommended for review. Equipped, carried,
Postmaster, locked, crafted, Exotic, loadout-referenced, and incomplete records
are protected and still contribute their known coverage. Unresolved loadout
references are preserved with the analysis. Masterworked copies win ties when
several equally small retained sets exist.

Each run is stored with its inventory snapshot, manifest version, ruleset
version, and explanatory item-level evidence. The application does not expose a
dismantle endpoint.

Weapon rolls use the official perk PNG, localized name, and description from
each plug's `DestinyInventoryItemDefinition` in the recorded Bungie manifest.
Hovering a perk, focusing it with the keyboard, or tapping it on a touch device
reveals that description. Community research is intentionally kept separate so
a later Clarity integration can identify its source and version without
overwriting Bungie's text.

The weapon cleaner can organize the in-game vault for manual dismantling. It
first forces a fresh inventory sync and recomputes the analysis, then uses
Bungie's `SetLockState` action sequentially: review candidates are unlocked and
every other lockable vault weapon is locked. It honors the per-user action
interval, retries temporary failures, refreshes the full inventory afterward,
and compares every exact instance with its requested state. A partial result is
reported rather than presented as complete, and running the operation again is
safe because it refreshes and recomputes before already-correct items are
skipped. This operation never dismantles, transfers, or equips an item.

Organization runs as an in-process background job. The cleaner polls an
authenticated, non-cacheable status endpoint and displays completed versus
total actions, a measured time estimate, verification/retry stages, and an
accessible progress bar. Reloading the page reconnects to an active job.

## Current security boundary

- Unsafe form submissions use session-bound CSRF tokens and same-origin checks.
- Access logs omit query strings, keeping OAuth codes and state out of request
  logs.
- Authenticated HTML and JSON are marked non-cacheable.
- Session, token, and configuration representations redact secret fields.
- The OAuth refresh path is serialized per user and reloads tokens before
  rotation.
- Manifest and profile requests use separate timeout and size limits.

This is still a local-development deployment. The copied Bungie credentials
must be rotated, the expired legacy TLS certificate/private key must be
replaced, and token encryption at rest must be decided before hosting outside a
trusted local machine.

### Goal 4 manual verification

1. Restart the application so database migrations run.
2. Sign in if the saved browser session is no longer active.
3. Open `https://localhost:42697/data/status`.
4. Select **Use cache or synchronize if stale**.
5. Confirm the vault and every character appear with carried and equipped
   counts.
6. Confirm the calculated total passes and duplicate instances, broken owners,
   and database integrity are all healthy.
7. Select the same synchronization action while the data is fresh and confirm
   that the cache-hit count increases without increasing Bungie fetches.
8. Use **Force Bungie refresh** to verify another complete snapshot.
9. Restart the application and confirm the saved inventory remains.
10. Use the development verification controls to mark the data stale and
    simulate a failed refresh. Confirm the previous snapshot remains visible.

## Legacy-code policy

The old project may be read to recover Bungie-specific behavior, sample data, and
domain knowledge. Legacy modules are not dependencies and must not be imported
by the rebuilt package. Any logic brought forward must be simplified and verified
inside this project first.

The development TLS certificate paths documented above are the sole initial
legacy-asset exception. They are configuration inputs, not Python dependencies.
