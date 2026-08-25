"""Shared page chrome for the web application."""

from __future__ import annotations

from contextvars import ContextVar
from collections.abc import Mapping
from html import escape
from aiohttp import web


_ACTIVE_NAV = {
    "inventory.html": "/inventory",
    "loadouts.html": "/loadouts",
    "loadout_builder.html": "/loadouts",
    "loadout_create.html": "/loadouts",
    "loadout_operation.html": "/loadouts",
    "loadout_plan.html": "/loadouts",
    "loadout_preview.html": "/loadouts",
    "loadout_set.html": "/loadouts",
    "loadout_slot.html": "/loadouts",
    "saved_loadout.html": "/loadouts",
    "weapon_cleaner.html": "/cleaner/weapons",
    "armor_cleaner.html": "/cleaner/armor",
    "data_status.html": "/data/status",
}

_NAV_ITEMS = (
    ("/inventory", "Inventory"),
    ("/loadouts", "Loadouts"),
    ("/cleaner/weapons", "Weapons"),
    ("/cleaner/armor", "Armor"),
    ("/data/status", "Data status"),
)

_render_request: ContextVar[web.Request | None] = ContextVar(
    "render_request", default=None
)


def set_render_request(request: web.Request):
    return _render_request.set(request)


def reset_render_request(token) -> None:
    _render_request.reset(token)


def render_header(template_name: str, values: Mapping[str, str]) -> str:
    """Render the shared header for a page template that opts into it."""
    active_path = _ACTIVE_NAV.get(template_name, "")
    guardian_name = values.get("guardian_name") or values.get("bungie_name", "")
    request = _render_request.get()
    refresh_csrf = ""
    return_to = "/data/status"
    if request is not None:
        from destiny_web_app.auth import csrf_input

        refresh_csrf = csrf_input(request, "/data/inventory/sync")
        return_to = escape(str(request.rel_url), quote=True)
    links = "".join(
        f'<a class="active" href="{path}">{label}</a>'
        if path == active_path
        else f'<a href="{path}">{label}</a>'
        for path, label in _NAV_ITEMS
    )
    return (
        '<header class="site-header">'
        '<a class="site-brand" href="/" aria-label="Destiny Web App home">'
        '<span class="site-brand-mark">D</span>'
        '<span>Destiny Web App</span>'
        "</a>"
        f'<nav class="site-nav">{links}</nav>'
        '<div class="site-account">'
        '<label class="site-character-select">'
        '<span class="sr-only">Default character</span>'
        '<select data-default-character title="Default character">'
        '<option value="2">Warlock</option>'
        '<option value="1">Hunter</option>'
        '<option value="0">Titan</option>'
        '</select>'
        '</label>'
        '<form class="site-refresh" method="post" action="/data/inventory/sync">'
        f'{refresh_csrf}<input type="hidden" name="force" value="true">'
        f'<input type="hidden" name="return_to" value="{return_to}">'
        '<button type="submit" title="Refresh data">Refresh data</button>'
        '</form>'
        '<span class="site-account-dot"></span>'
        f"<span>{guardian_name}</span>"
        "</div>"
        '<script src="/static/header.js" defer></script>'
        "</header>"
    )
