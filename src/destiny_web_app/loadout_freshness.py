"""Inventory freshness guarantees shared by loadout routes."""

from __future__ import annotations

from typing import Any

from aiohttp import web

from destiny_web_app.app_keys import INVENTORY_SERVICE_KEY


async def ensure_fresh_loadout_snapshot(
    request: web.Request,
    authenticated: Any,
    *,
    force: bool = False,
) -> None:
    """Reuse a fresh snapshot or fetch a complete replacement from Bungie."""

    await request.app[INVENTORY_SERVICE_KEY].synchronize(
        bungie_membership_id=authenticated.bungie_membership_id,
        access_token=authenticated.token.access_token,
        force=force,
    )
