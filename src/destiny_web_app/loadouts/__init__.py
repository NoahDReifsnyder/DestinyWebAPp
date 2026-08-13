"""Composable loadout capabilities.

Import the operation you need from its capability module instead of routing a
request through the web application::

    from destiny_web_app.loadouts.library import save_equipped_loadout
    from destiny_web_app.loadouts.runtime import build_loadout_functions

The modules deliberately do not prescribe an application flow. Callers can
    save, arrange sets, preview, confirm, start, and resume in whatever order their own UI
allows, subject to the safety checks inside each operation.
"""

__all__ = [
    "execution",
    "errors",
    "game_actions",
    "inspection",
    "library",
    "previews",
    "runtime",
    "sets",
    "state",
    "storage",
]
