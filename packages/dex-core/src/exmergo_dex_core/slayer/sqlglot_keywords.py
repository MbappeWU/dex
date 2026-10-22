"""Keeping SLayer's sqlglot reserved keywords out of dex's own SQL.

SLayer quotes a reserved word used as an identifier by unioning its own word
list into each sqlglot dialect's ``Generator.RESERVED_KEYWORDS``, and it does
that at import (``slayer.sql.dialects`` and ``slayer.sql.scope`` each call
``install_reserved_keywords``). The sets are class attributes, so the change is
process-wide: once ``slayer.core.models`` has been imported, every statement
sqlglot generates quotes those words too, dex's included. On Snowflake a quoted
lowercase identifier is case-sensitive, so ``SELECT values FROM t`` becoming
``SELECT "values" FROM t`` names a different column, and the query firewall,
the mutation runner and row attribution all regenerate SQL dex did not write.
What dex runs must not depend on whether a SLayer model was read first.

So the containment is two halves:

- **Importing this module imports SLayer's two installing modules with the
  generators' own sets saved and put back afterwards.** Every dex module that
  imports SLayer imports this one first, so SLayer's import-time install never
  outlives the import. The sets SLayer installed are kept, not discarded.
- **:func:`slayer_keywords` puts SLayer's sets back for the length of a call
  into SLayer that emits SQL** (a render, a schema-drift computation). SLayer's
  own emission relies on them at generation time, since ``identifier_sql``
  reads the class attribute when it runs, and rendering without them would emit
  a reserved-word alias bare. Calls are reference-counted under a module-level
  lock: the first to enter installs SLayer's sets and the last to exit restores
  what was there before, error or not, so overlapping calls on different threads
  cannot leave SLayer's sets behind.

Only the generators of the dialects SLayer targets are saved
(:data:`SLAYER_DIALECTS`, held equal to SLayer's ``SQLGLOT_NAMES`` by a test).
SLayer touches no other generator, and assigns a fresh set per class rather
than mutating an inherited one, so restoring these restores every dialect,
including the ones that inherit from them. Loading all of sqlglot's dialects to
save them would cost more than the SLayer import does.
"""

from __future__ import annotations

import sys
import threading
import warnings
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from sqlglot.dialects.dialect import Dialect

__all__ = ["CONTAINED", "SLAYER_DIALECTS", "slayer_keywords"]

#: The sqlglot dialects whose generators SLayer's install touches.
SLAYER_DIALECTS: tuple[str, ...] = (
    "sqlite",
    "postgres",
    "duckdb",
    "mysql",
    "clickhouse",
    "tsql",
    "snowflake",
    "bigquery",
    "redshift",
    "trino",
    "presto",
    "databricks",
    "spark",
    "oracle",
)

# A generator class with no ``RESERVED_KEYWORDS`` of its own inherits one, and
# restoring it means deleting the attribute SLayer gave it, not assigning a copy
# of what it inherited.
_INHERITED = object()

# The modules that install SLayer's sets when they are first imported.
_INSTALLING_MODULES = ("slayer.sql.dialects", "slayer.sql.scope")


def _import_installing() -> None:
    import slayer.sql.dialects
    import slayer.sql.scope  # noqa: F401


_KeywordState = Mapping[type, object]


def _generators() -> list[type]:
    return [Dialect.get_or_raise(name).generator_class for name in SLAYER_DIALECTS]


def _capture() -> dict[type, object]:
    return {
        cls: cls.__dict__.get("RESERVED_KEYWORDS", _INHERITED) for cls in _generators()
    }


def _apply(state: _KeywordState) -> None:
    for cls, keywords in state.items():
        if keywords is _INHERITED:
            if "RESERVED_KEYWORDS" in cls.__dict__:
                delattr(cls, "RESERVED_KEYWORDS")
        else:
            cls.RESERVED_KEYWORDS = keywords  # type: ignore[attr-defined]


def _import_contained() -> tuple[dict[type, object], bool]:
    """Import SLayer's installing modules, undo what they install, and return
    the sets SLayer installed with whether the undo was possible.

    It is not when something imported SLayer before this module did (a host
    process embedding both, say): sqlglot's own sets are class-body literals
    that SLayer replaced, so there is nothing left to put back. Subtracting
    SLayer's words is no recovery, since most of them are reserved natively on
    DuckDB, BigQuery and Redshift too, and dropping them would emit reserved
    words bare. The sets are left as SLayer made them and the process is
    warned. dex's own modules always import this one first, so a dex command
    never reaches that branch.
    """

    already = [name for name in _INSTALLING_MODULES if name in sys.modules]
    if already:
        warnings.warn(
            f"{', '.join(already)} was imported before {__name__}, so sqlglot's "
            "reserved keywords carry SLayer's for the rest of this process and "
            "dex's own generated SQL quotes those words too. Import "
            f"{__name__} before anything that imports slayer.",
            RuntimeWarning,
            stacklevel=2,
        )
        _import_installing()
        return _capture(), False
    native = _capture()
    try:
        _import_installing()
        installed = _capture()
    finally:
        _apply(native)
    return installed, True


_SLAYER, CONTAINED = _import_contained()

# How many `slayer_keywords` blocks are open, across every thread, and the sets
# the first of them replaced. Both are read and written only under the lock.
_LOCK = threading.Lock()
_depth = 0
_outside: _KeywordState = {}


@contextmanager
def slayer_keywords() -> Iterator[None]:
    """SLayer's reserved keywords installed for the length of the block.

    Wrap every call into SLayer that generates SQL. The first block to enter
    installs them and the last to exit restores the sets in place before it,
    which are sqlglot's own outside any block, so blocks may nest or overlap
    across threads.
    """

    global _depth, _outside
    with _LOCK:
        if _depth == 0:
            _outside = _capture()
            _apply(_SLAYER)
        _depth += 1
    try:
        yield
    finally:
        with _LOCK:
            _depth -= 1
            if _depth == 0:
                _apply(_outside)
