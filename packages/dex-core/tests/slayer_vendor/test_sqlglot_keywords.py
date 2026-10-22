"""SLayer's reserved keywords stay out of dex's own generated SQL (#684).

SLayer unions its reserved-word list into every sqlglot dialect generator when
it is imported, and the sets are process-wide class attributes. Measured against
the pinned release, that changed the SQL dex regenerates itself (the query
firewall's LIMIT clamp, the mutation runner, row attribution) on Snowflake,
BigQuery, Postgres, Databricks, ClickHouse and DuckDB: ``SELECT values FROM t``
became ``SELECT "values" FROM t``, which on Snowflake names a different,
case-sensitive column. The transpiled probes were unaffected, because they quote
every identifier already.

What this pins: importing every dex module that imports SLayer leaves sqlglot's
output exactly as it was; SLayer's sets are back in place only inside
`slayer_keywords`, which a SLayer render needs; and every dex module that imports
SLayer imports the guard first.

Needs the real `[slayer]` extra, so it skips without it; the `slayer-sqlglot` CI
job is where it runs.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
import threading
from datetime import datetime
from pathlib import Path

import pytest

from exmergo_dex_core.slayer import dependency
from exmergo_dex_core.slayer.dependency import SLAYER_VERSION

if dependency.installed_version() != SLAYER_VERSION:
    pytest.skip(
        f"the [slayer] extra (SLayer {SLAYER_VERSION}) is not installed",
        allow_module_level=True,
    )

import sqlglot
import yaml
from slayer.sql.dialects import SQLGLOT_NAMES

from exmergo_dex_core.slayer import sqlglot_keywords
from exmergo_dex_core.slayer.loader import FORMAT_VERSION, validate_model_contents
from exmergo_dex_core.slayer.render import SlayerRenderer
from exmergo_dex_core.slayer.sqlglot_keywords import SLAYER_DIALECTS, slayer_keywords

SOURCE = Path(sqlglot_keywords.__file__).resolve().parents[1]

# Every dex module that imports SLayer, plus the explore backend that reaches
# them, so a fresh process imports all of them the way a SLayer command would.
DEX_SLAYER_MODULES = (
    "exmergo_dex_core.slayer.loader",
    "exmergo_dex_core.slayer.render",
    "exmergo_dex_core.slayer.schema",
    "exmergo_dex_core.slayer.translate",
    "exmergo_dex_core.slayer_dialect",
    "exmergo_dex_core.explore.semantic.slayer",
)

# Statements whose identifiers SLayer reserves in at least one dex dialect.
STATEMENTS = (
    "SELECT values FROM t",
    "SELECT user, order_id FROM t",
    "SELECT t.current, rows, start FROM t",
    "SELECT o.by, o.top FROM orders AS o",
)
DIALECTS = ("duckdb", "snowflake", "bigquery", "postgres", "redshift", "databricks")


def run_fresh(script: str) -> dict:
    """Run ``script`` in a new interpreter, where nothing has imported SLayer."""

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1])


MEASURE = f"""
    import sqlglot
    from exmergo_dex_core.guards.query_firewall import _apply_limit

    def measure():
        out = {{}}
        for dialect in {DIALECTS!r}:
            for sql in {STATEMENTS!r}:
                try:
                    root = sqlglot.parse_one(sql, read=dialect)
                except sqlglot.errors.ParseError:
                    continue  # not a statement this dialect reads
                out[f"{{dialect}} transpile {{sql}}"] = root.sql(dialect=dialect)
                limited = _apply_limit(root, 10, dialect)[0]
                out[f"{{dialect}} firewall {{sql}}"] = limited
        return out
"""


def test_importing_dex_s_slayer_modules_leaves_dex_s_sql_unchanged():
    measured = run_fresh(
        MEASURE
        + f"""
    import importlib, json
    before = measure()
    for name in {DEX_SLAYER_MODULES!r}:
        importlib.import_module(name)
    from exmergo_dex_core.slayer import sqlglot_keywords
    after = measure()
    print(json.dumps({{
        "before": before,
        "after": after,
        "contained": sqlglot_keywords.CONTAINED,
    }}))
    """
    )

    assert measured["contained"] is True
    changed = {
        key: (value, measured["after"][key])
        for key, value in measured["before"].items()
        if measured["after"][key] != value
    }
    assert not changed
    # The measurement would have caught it: these are the statements that moved.
    assert measured["after"]["snowflake firewall SELECT values FROM t"] == (
        "SELECT values FROM t LIMIT 11"
    )


def test_without_the_guard_slayer_s_import_would_change_them():
    """The control: the same measurement over a bare SLayer import does move,
    so the test above is not passing because nothing could change."""

    measured = run_fresh(
        MEASURE
        + """
    import json
    before = measure()
    import slayer.core.models
    print(json.dumps({"before": before, "after": measure()}))
    """
    )

    key = "snowflake firewall SELECT values FROM t"
    assert measured["before"][key] == "SELECT values FROM t LIMIT 11"
    assert measured["after"][key] == 'SELECT "values" FROM t LIMIT 11'


def test_slayer_imported_first_warns_instead_of_guessing():
    """A host process that imported SLayer before dex leaves nothing to restore:
    sqlglot's own sets are gone. dex warns and leaves SLayer's in place rather
    than subtracting SLayer's words, most of which some dialects reserve
    natively, and the render context still works."""

    measured = run_fresh(
        """
    import json, warnings
    import slayer.core.models
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        from exmergo_dex_core.slayer import sqlglot_keywords
    import sqlglot
    with sqlglot_keywords.slayer_keywords():
        inside = sqlglot.transpile("SELECT values FROM t", read="snowflake",
                                   write="snowflake")[0]
    print(json.dumps({
        "contained": sqlglot_keywords.CONTAINED,
        "warnings": [str(w.message) for w in caught
                     if issubclass(w.category, RuntimeWarning)],
        "inside": inside,
    }))
    """
    )

    assert measured["contained"] is False
    assert any("imported before" in message for message in measured["warnings"])
    assert measured["inside"] == 'SELECT "values" FROM t'


def snowflake(sql: str) -> str:
    return sqlglot.transpile(sql, read="snowflake", write="snowflake")[0]


def test_the_saved_dialects_are_the_ones_slayer_installs_into():
    assert SLAYER_DIALECTS == SQLGLOT_NAMES


def test_the_guard_imports_every_module_slayer_installs_from():
    """A tripwire for a SLayer bump: a module that installs the sets and is not
    imported under the guard would install them the first time something reached
    it, after the guard had restored sqlglot's own."""

    import slayer

    root = Path(slayer.__file__).resolve().parent
    installers = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            call = node.value if isinstance(node, ast.Expr) else None
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "install_reserved_keywords"
            ):
                parts = path.relative_to(root.parent).with_suffix("").parts
                installers.add(".".join(p for p in parts if p != "__init__"))

    assert installers == set(sqlglot_keywords._INSTALLING_MODULES)


def test_slayer_s_keywords_apply_only_inside_the_context():
    assert sqlglot_keywords.CONTAINED is True
    assert snowflake("SELECT values FROM t") == "SELECT values FROM t"
    with slayer_keywords():
        assert snowflake("SELECT values FROM t") == 'SELECT "values" FROM t'
    assert snowflake("SELECT values FROM t") == "SELECT values FROM t"


def test_the_context_restores_on_an_error():
    with pytest.raises(ValueError, match="boom"), slayer_keywords():
        raise ValueError("boom")

    assert snowflake("SELECT values FROM t") == "SELECT values FROM t"


def test_overlapping_blocks_on_two_threads_leave_none_behind():
    """The interleaving that leaked before the blocks were reference-counted: A
    enters, B enters (saving SLayer's sets as what to restore), A exits, B
    exits and puts SLayer's sets back for good. B must still see them after A
    has left, and neither must leave them behind."""

    a_entered, b_entered, a_exited = (threading.Event() for _ in range(3))
    seen: dict[str, str] = {}
    errors: list[BaseException] = []

    def a() -> None:
        try:
            with slayer_keywords():
                a_entered.set()
                assert b_entered.wait(5)
                seen["a inside"] = snowflake("SELECT values FROM t")
        except BaseException as exc:  # re-raised on the main thread
            errors.append(exc)
        finally:
            a_exited.set()

    def b() -> None:
        try:
            assert a_entered.wait(5)
            with slayer_keywords():
                b_entered.set()
                assert a_exited.wait(5)
                seen["b inside after a"] = snowflake("SELECT values FROM t")
        except BaseException as exc:  # re-raised on the main thread
            errors.append(exc)
            b_entered.set()

    threads = [threading.Thread(target=a), threading.Thread(target=b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert not errors, errors
    assert seen == {
        "a inside": 'SELECT "values" FROM t',
        "b inside after a": 'SELECT "values" FROM t',
    }
    assert snowflake("SELECT values FROM t") == "SELECT values FROM t"


def test_a_render_still_quotes_slayer_s_reserved_words_and_leaves_none_behind():
    """SLayer's emission reads the sets when it generates, so a render runs with
    them: a model named ``user`` with a ``values`` column renders quoted on
    Snowflake, where both are reserved. dex's own SQL is unchanged afterwards."""

    reserved = {
        "version": FORMAT_VERSION,
        "name": "user",
        "sql_table": "demo.main.user",
        "data_source": "wh",
        "columns": [
            {"name": "order_id", "type": "INT", "primary_key": True},
            {"name": "values", "type": "TEXT"},
            {"name": "amount", "type": "DOUBLE"},
        ],
        "measures": [{"name": "revenue", "formula": "amount:sum"}],
    }
    result = validate_model_contents({"models/wh/user.yaml": yaml.safe_dump(reserved)})
    assert not result.errors, result.notes()
    renderer = SlayerRenderer(
        result.models, connector="snowflake", now=datetime(2026, 1, 15, 12)
    )

    sql = renderer.render(
        {"source_model": "user", "measures": ["revenue"], "dimensions": ["values"]}
    ).sql

    assert '"user"."values"' in sql
    assert " user.values" not in sql
    assert snowflake("SELECT values FROM t") == "SELECT values FROM t"


# --- every dex module that imports SLayer imports the guard first -------------------


def _imports_slayer(node: ast.stmt) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name.split(".")[0] == "slayer" for alias in node.names)
    return (
        isinstance(node, ast.ImportFrom)
        and node.level == 0
        and (node.module or "").split(".")[0] == "slayer"
    )


def _imports_guard(node: ast.stmt) -> bool:
    if not isinstance(node, ast.ImportFrom):
        return False
    names = {alias.name for alias in node.names}
    return (node.module or "").endswith("sqlglot_keywords") or (
        "sqlglot_keywords" in names
    )


def test_every_dex_module_that_imports_slayer_imports_the_guard_first():
    unguarded = []
    for path in sorted(SOURCE.rglob("*.py")):
        if path.name == "sqlglot_keywords.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if not any(_imports_slayer(node) for node in ast.walk(tree)):
            continue
        guarded = False
        for node in tree.body:
            if _imports_guard(node):
                guarded = True
            if _imports_slayer(node) or isinstance(
                node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            ):
                break
        if not guarded:
            unguarded.append(str(path.relative_to(SOURCE)))

    assert not unguarded, (
        f"{unguarded} import slayer without first importing "
        "exmergo_dex_core.slayer.sqlglot_keywords"
    )
