"""Import dex's sqlglot keyword guard before any test module imports SLayer.

A few modules here import `slayer` directly, and whichever is collected first
would otherwise import it ahead of the guard, which then cannot restore
sqlglot's own reserved keywords (`exmergo_dex_core.slayer.sqlglot_keywords`).
dex's own modules import the guard first; this gives the tests the same order.
"""

from __future__ import annotations

from exmergo_dex_core.slayer import dependency

if dependency.installed_version() == dependency.SLAYER_VERSION:
    from exmergo_dex_core.slayer import sqlglot_keywords  # noqa: F401
