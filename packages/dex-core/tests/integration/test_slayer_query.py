"""Live smoke runs of a governed SLayer metric query (#683).

SLayer renders and dex executes, so the offline goldens
(``tests/slayer_vendor/test_goldens.py``) say what each dialect is handed and
these say the warehouse agrees with it: one query per connector, over six rows
this suite writes, answered with the numbers those rows hold.

BigQuery and Snowflake run the whole command, ``explore semantic query``, through
the CLI: the handshake stops before any spend, and the confirmed run comes back
with the ``model.column`` names a caller reads on every connector (BigQuery's
statement has to emit ``orders___status``). Each writes one tiny table into its
scratch scope and drops it afterwards.

Postgres runs the whole command too, over ``slayer.orders``, which
``scripts/postgres_seed.sql`` seeds with the same six rows because the read-only
role the command connects as can create nothing. Its numbers are held to the
expected answer and to the same question asked in hand-written SQL.

Redshift cannot run the command here: the test identity has no read on the
seeded schema and cannot create one, so a table the command could see does not
exist. A session temp table can, so the SQL SLayer renders for Redshift is run
in the session that holds one. That proves the statement, not the spine; the
spine is the same one every other connector runs. The same route covers a
mixed-case model, whose quoted result names Redshift folds to lowercase.

Snowflake's weeks (#699) take the same route over the table the Snowflake
fixture writes, so the session's ``WEEK_START`` can be set to each value an
account might hold before the statement runs.

Needs the ``[slayer]`` extra beside each connector's own, which the integration
jobs install.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from exmergo_dex_core.slayer import dependency
from exmergo_dex_core.slayer.dependency import SLAYER_VERSION

from .conftest import MAX_BYTES, RS_MAX_SECONDS, SF_MAX_SECONDS, assert_ok

if dependency.installed_version() != SLAYER_VERSION:
    pytest.skip(
        f"the [slayer] extra (SLayer {SLAYER_VERSION}) is not installed",
        allow_module_level=True,
    )

pytestmark = [pytest.mark.integration]

BASE = "semantics/slayer"
MODEL_FILE = "models/wh/orders.yaml"

#: (order_id, status, amount, ordered_at). The `test` row is excluded by the
#: model's own filter, so a number that counts it is wrong.
ROWS = [
    (1, "paid", 10.0, "2026-01-03 10:00:00"),
    (2, "paid", 5.5, "2026-01-20 08:30:00"),
    (3, "open", 7.0, "2026-02-01 12:00:00"),
    (4, "paid", 2.5, "2026-02-14 09:15:00"),
    (5, "refunded", 4.0, "2026-03-09 17:45:00"),
    (6, "test", 1000.0, "2026-03-10 11:00:00"),
]

#: The query: paid revenue and order count per month.
ARGV = [
    "orders.revenue,orders.order_count",
    "--group-by",
    "orders.status",
    "--grain",
    "month",
    "--where",
    "status = 'paid'",
    "--order-by",
    "orders.ordered_at",
]
COLUMNS = ["orders.status", "orders.ordered_at", "orders.revenue", "orders.order_count"]
#: (status, month, revenue, order count), from the rows above.
EXPECTED = [("paid", "2026-01", 15.5, 2), ("paid", "2026-02", 2.5, 1)]


def as_written(name: str) -> str:
    return name


def title_case(name: str) -> str:
    """``ordered_at`` as ``Ordered_At``: a spelling a model author might choose,
    and one Redshift's case folding does not keep."""

    return "_".join(part.capitalize() for part in name.split("_"))


def model(table: str, spell: Callable[[str], str] = as_written) -> dict[str, Any]:
    from exmergo_dex_core.slayer.loader import FORMAT_VERSION

    return {
        "version": FORMAT_VERSION,
        "name": spell("orders"),
        "sql_table": table,
        "data_source": "wh",
        "default_time_dimension": spell("ordered_at"),
        "filters": [f"{spell('status')} <> 'test'"],
        "columns": [
            {"name": spell("order_id"), "type": "INT", "primary_key": True},
            {"name": spell("status"), "type": "TEXT"},
            {"name": spell("amount"), "type": "DOUBLE"},
            {"name": spell("ordered_at"), "type": "TIMESTAMP"},
        ],
        "measures": [
            {"name": spell("revenue"), "formula": f"{spell('amount')}:sum"},
            {"name": spell("order_count"), "formula": "*:count"},
        ],
    }


def keyed(spell: Callable[[str], str], key: str) -> str:
    """``model.column`` in ``spell``'s spelling."""

    return ".".join(spell(part) for part in key.split("."))


def render_for(
    connector: str,
    table: str,
    query,
    spell: Callable[[str], str] = as_written,
):
    """The statement the command would hand the spine: dex's translation, then
    SLayer's dry run in ``connector``'s dialect, then dex's SELECT-only check."""

    from exmergo_dex_core.guards.sql_guard import assert_select_only
    from exmergo_dex_core.slayer.catalog import semantic_catalog
    from exmergo_dex_core.slayer.loader import validate_model_contents
    from exmergo_dex_core.slayer.render import SlayerRenderer
    from exmergo_dex_core.slayer.translate import to_slayer_query

    path = f"models/wh/{spell('orders')}.yaml"
    loaded = validate_model_contents({path: yaml.safe_dump(model(table, spell))})
    assert not loaded.errors, loaded.notes()
    view = semantic_catalog(loaded.models, connector=connector)
    rendered = SlayerRenderer(loaded.models, connector=connector).render(
        to_slayer_query(query, view)
    )
    assert_select_only(rendered.sql, dialect=connector)
    return rendered


def add_slayer_layer(root: Path, table: str) -> None:
    """Commit the one model and point the repo's config at it, beside whatever
    connector block the connector's own ``seed_repo`` wrote."""

    path = root / BASE / MODEL_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(model(table), sort_keys=False), encoding="utf-8")
    config_path = root / ".dex" / "config.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["semantic"] = {
        "vendor": "slayer",
        "slayer": {"base_dir": BASE, "models": [MODEL_FILE]},
    }
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")


def answered(columns: list[str], cells: list[list[Any]]) -> list[tuple]:
    """``(status, month, revenue, count)`` per row, however the connector spells
    a timestamp and a number."""

    index = {name: columns.index(name) for name in COLUMNS}
    return sorted(
        (
            row[index["orders.status"]],
            str(row[index["orders.ordered_at"]])[:7],
            float(row[index["orders.revenue"]]),
            int(row[index["orders.order_count"]]),
        )
        for row in cells
    )


def select_rows(literal_timestamp, literal_amount) -> str:
    """The rows as a ``UNION ALL`` of literal selects, typed by the first."""

    selects = []
    for i, (order_id, status, amount, at) in enumerate(ROWS):
        if i == 0:
            selects.append(
                f"SELECT {order_id} AS order_id, '{status}' AS status, "
                f"{literal_amount(amount)} AS amount, "
                f"{literal_timestamp(at)} AS ordered_at"
            )
        else:
            selects.append(
                f"SELECT {order_id}, '{status}', {literal_amount(amount)}, "
                f"{literal_timestamp(at)}"
            )
    return " UNION ALL ".join(selects)


def query_twice(run_cli, capsys, root: Path, budget: str) -> tuple[dict, dict]:
    """The unconfirmed call, then the confirmed one."""

    base = ["--repo-root", str(root), "explore", "semantic", "query", *ARGV]
    _, asked = run_cli(base, capsys)
    rc, confirmed = run_cli([*base, "--confirm", "--budget", budget], capsys)
    return asked, assert_ok(rc, confirmed)


def assert_answered(envelope: dict) -> None:
    data = envelope["data"]
    assert (data["vendor"], data["execution"]) == ("slayer", "dex")
    assert data["columns"] == COLUMNS
    assert answered(data["columns"], data["cells"]) == EXPECTED
    assert not any("cost guard" in warning for warning in envelope["warnings"])


# --- BigQuery ----------------------------------------------------------------------

BQ_TABLE = "dex_slayer_orders"


@pytest.fixture
def bq_slayer_orders(bq_project: str, bq_scratch_dataset: str):
    """The six rows, written into the scratch dataset and dropped afterwards; the
    dataset's table TTL is the backstop for a crashed run."""

    from google.cloud import bigquery

    identifier = f"{bq_project}.{bq_scratch_dataset}.{BQ_TABLE}"
    rows = select_rows(lambda at: f"TIMESTAMP '{at}'", lambda a: f"{a}")
    client = bigquery.Client(project=bq_project)
    try:
        # DDL over literals: no table is read, so this scans nothing.
        client.query(f"CREATE OR REPLACE TABLE `{identifier}` AS {rows}").result()
        yield identifier
    finally:
        client.delete_table(identifier, not_found_ok=True)
        client.close()


@pytest.mark.bigquery
def test_bigquery_answers_a_governed_slayer_query(
    tmp_path: Path,
    capsys,
    bq_project: str,
    bq_scratch_dataset: str,
    bq_slayer_orders: str,
):
    from .test_bigquery_connect import run_cli, seed_repo

    seed_repo(tmp_path, bq_project, datasets=[f"{bq_project}.{bq_scratch_dataset}"])
    add_slayer_layer(tmp_path, bq_slayer_orders)

    asked, envelope = query_twice(run_cli, capsys, tmp_path, str(MAX_BYTES))

    assert asked["status"] == "needs_confirmation", asked["errors"]
    assert 0 < asked["cost"]["estimate"] <= MAX_BYTES
    assert_answered(envelope)
    assert envelope["data"]["spend"]["bytes_billed"] <= MAX_BYTES


# --- Snowflake ---------------------------------------------------------------------

SF_SCHEMA = "SLAYER_QUERY"


@pytest.fixture
def sf_slayer_orders(
    tmp_path: Path, sf_scratch_database, sf_warehouse, sf_connection_name
):
    """The six rows in a transient table of a schema this fixture owns in the
    scratch database, dropped afterwards; the transient database is the
    backstop for a crashed run.

    The model names it in lowercase, the way an author writes it: SLayer leaves
    a lowercase name unquoted, so Snowflake resolves it case-insensitively to
    the uppercase objects created here.
    """

    import snowflake.connector

    from exmergo_dex_core.config import SnowflakeTarget
    from exmergo_dex_core.connect import resolve_snowflake_connection

    params, _method = resolve_snowflake_connection(
        SnowflakeTarget(connection_name=sf_connection_name), os.environ, tmp_path
    )
    schema = f'"{sf_scratch_database}".{SF_SCHEMA}'
    rows = select_rows(lambda at: f"'{at}'::TIMESTAMP_NTZ", lambda a: f"{a}::FLOAT")
    conn = snowflake.connector.connect(**params)
    try:
        cursor = conn.cursor()
        cursor.execute(f'USE WAREHOUSE "{sf_warehouse}"')
        cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        cursor.execute(f"CREATE OR REPLACE TRANSIENT TABLE {schema}.ORDERS AS {rows}")
        yield f"{sf_scratch_database}.{SF_SCHEMA.lower()}.orders"
    finally:
        try:
            conn.cursor().execute(f"DROP SCHEMA IF EXISTS {schema}")
        finally:
            conn.close()


@pytest.mark.snowflake
def test_snowflake_answers_a_governed_slayer_query(
    tmp_path: Path,
    capsys,
    sf_scratch_database,
    sf_warehouse,
    sf_connection_name,
    sf_slayer_orders: str,
):
    from .test_snowflake_connect import run_cli, seed_repo

    seed_repo(
        tmp_path,
        sf_scratch_database,
        sf_warehouse,
        sf_connection_name,
        databases=[f"{sf_scratch_database}.{SF_SCHEMA}"],
    )
    add_slayer_layer(tmp_path, sf_slayer_orders)

    # The scan plus the warehouse uptime the estimate quotes: the idle tail on a
    # warm warehouse, the 60s resume minimum on a cold one.
    asked, envelope = query_twice(run_cli, capsys, tmp_path, str(SF_MAX_SECONDS + 90))

    assert asked["status"] == "needs_confirmation", asked["errors"]
    assert asked["cost"]["estimate"] > 0
    assert_answered(envelope)


#: (week start, order count) for ``week`` and ``week_sunday`` over the rows
#: the model's filter keeps: each row falls in a week of its own.
WEEKS = {
    "week": [
        (date(2025, 12, 29), 1),
        (date(2026, 1, 19), 1),
        (date(2026, 1, 26), 1),
        (date(2026, 2, 9), 1),
        (date(2026, 3, 9), 1),
    ],
    "week_sunday": [
        (date(2025, 12, 28), 1),
        (date(2026, 1, 18), 1),
        (date(2026, 2, 1), 1),
        (date(2026, 2, 8), 1),
        (date(2026, 3, 8), 1),
    ],
}


@pytest.mark.snowflake
@pytest.mark.parametrize("grain", sorted(WEEKS))
def test_a_snowflake_week_starts_on_its_day_whatever_week_start_says(
    tmp_path: Path,
    grain: str,
    sf_warehouse,
    sf_connection_name,
    sf_slayer_orders: str,
):
    """``WEEK_START`` 0 is the default (Monday), 1 names Monday, 7 Sunday. Each
    session answers the same buckets; the control shows the account setting
    would otherwise have moved them."""

    import snowflake.connector

    from exmergo_dex_core.config import SnowflakeTarget
    from exmergo_dex_core.connect import resolve_snowflake_connection
    from exmergo_dex_core.explore.semantic.model import SemanticQuery

    rendered = render_for(
        "snowflake",
        sf_slayer_orders,
        SemanticQuery(metrics=["orders.order_count"], grain=grain),
    )
    params, _method = resolve_snowflake_connection(
        SnowflakeTarget(connection_name=sf_connection_name), os.environ, tmp_path
    )
    conn = snowflake.connector.connect(**params)
    answers: dict[int, list[tuple[date, int]]] = {}
    try:
        cursor = conn.cursor()
        cursor.execute(f'USE WAREHOUSE "{sf_warehouse}"')
        cursor.execute(
            f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {int(SF_MAX_SECONDS)}"
        )
        for week_start in (0, 1, 7):
            cursor.execute(f"ALTER SESSION SET WEEK_START = {week_start}")
            cursor.execute(rendered.sql)
            names = [rendered.result_keys.get(d[0], d[0]) for d in cursor.description]
            bucket = names.index("orders.ordered_at")
            count = names.index("orders.order_count")
            answers[week_start] = sorted(
                (row[bucket].date(), int(row[count])) for row in cursor.fetchall()
            )
        # The control: the truncation SLayer emits answers from the session,
        # so a test that ran without the pin would see Sunday here.
        cursor.execute("ALTER SESSION SET WEEK_START = 7")
        cursor.execute("SELECT DATE_TRUNC('WEEK', '2026-01-20'::DATE)")
        (sunday_start,) = cursor.fetchone()
    finally:
        try:
            conn.cursor().execute("ALTER SESSION UNSET WEEK_START")
        finally:
            conn.close()

    assert sunday_start == date(2026, 1, 18)
    assert answers == {0: WEEKS[grain], 1: WEEKS[grain], 7: WEEKS[grain]}


# --- Postgres ----------------------------------------------------------------------

#: Seeded by scripts/postgres_seed.sql with the six ROWS.
PG_SCHEMA = "slayer"
PG_TABLE = f"{PG_SCHEMA}.orders"

#: The question ARGV asks, in hand-written SQL.
PG_BY_HAND = """
SELECT status, to_char(date_trunc('month', ordered_at), 'YYYY-MM'),
       sum(amount), count(*)
FROM slayer.orders
WHERE status <> 'test' AND status = 'paid'
GROUP BY 1, 2
"""


@pytest.mark.postgres
def test_postgres_answers_a_governed_slayer_query(
    tmp_path: Path, capsys, pg_dsn, monkeypatch
):
    import psycopg

    from .test_postgres_connect import run_cli, seed_repo

    monkeypatch.setenv("DATABASE_URL", pg_dsn)
    seed_repo(tmp_path, schemas=[PG_SCHEMA])
    add_slayer_layer(tmp_path, PG_TABLE)

    asked, envelope = query_twice(run_cli, capsys, tmp_path, "30")

    assert asked["status"] == "needs_confirmation", asked["errors"]
    assert asked["cost"]["estimate"] > 0
    assert_answered(envelope)

    with psycopg.connect(pg_dsn) as conn:
        by_hand = sorted(
            (status, month, float(revenue), int(count))
            for status, month, revenue, count in conn.execute(PG_BY_HAND).fetchall()
        )
    data = envelope["data"]
    assert answered(data["columns"], data["cells"]) == by_hand == EXPECTED


# --- Redshift, on a temp table -----------------------------------------------------


def run_on_redshift_temp_table(
    tmp_path: Path, rs_workgroup, rs_database, table: str, rendered
) -> tuple[list[str], list[list[Any]]]:
    """``rendered`` run in the session that holds the six ROWS as ``table``,
    every statement capped server-side; the names come back decoded."""

    import redshift_connector

    from exmergo_dex_core.config import RedshiftTarget
    from exmergo_dex_core.connect import _connect_redshift, resolve_redshift_connection

    target = (
        RedshiftTarget(workgroup=rs_workgroup, dbname=rs_database)
        if rs_workgroup
        else RedshiftTarget()
    )
    params, _method, _compute = resolve_redshift_connection(
        target, os.environ, tmp_path
    )
    values = ", ".join(
        f"({order_id}, '{status}', {amount}, '{at}')"
        for order_id, status, amount, at in ROWS
    )
    conn = _connect_redshift(redshift_connector, params)
    try:
        cursor = conn.cursor()
        cursor.execute(f"SET statement_timeout TO {int(RS_MAX_SECONDS * 1000)}")
        cursor.execute(
            f"CREATE TEMP TABLE {table} (order_id INT, status VARCHAR(16), "
            "amount DOUBLE PRECISION, ordered_at TIMESTAMP)"
        )
        cursor.execute(f"INSERT INTO {table} VALUES {values}")  # noqa: S608
        cursor.execute(rendered.sql)
        names = [rendered.result_keys.get(d[0], d[0]) for d in cursor.description]
        cells = [list(row) for row in cursor.fetchall()]
    finally:
        conn.close()
    return names, cells


def redshift_query(spell: Callable[[str], str] = as_written):
    from exmergo_dex_core.explore.semantic.model import SemanticQuery

    return SemanticQuery(
        metrics=[keyed(spell, "orders.revenue"), keyed(spell, "orders.order_count")],
        group_by=[keyed(spell, "orders.status")],
        grain="month",
        where=[f"{spell('status')} = 'paid'"],
        order_by=[keyed(spell, "orders.ordered_at")],
    )


@pytest.mark.redshift
def test_redshift_runs_the_sql_slayer_renders_for_it(
    tmp_path: Path, rs_workgroup, rs_database
):
    """Rendered as the command renders it (dex's translation, SLayer's dry run in
    the Redshift dialect, dex's SELECT-only assertion), then run in the session
    that holds the temp table."""

    table = "dex_slayer_orders"
    rendered = render_for("redshift", table, redshift_query())
    names, cells = run_on_redshift_temp_table(
        tmp_path, rs_workgroup, rs_database, table, rendered
    )

    assert names == COLUMNS
    assert answered(names, cells) == EXPECTED


@pytest.mark.redshift
def test_a_mixed_case_model_on_redshift_comes_back_as_model_column(
    tmp_path: Path, rs_workgroup, rs_database
):
    """``Orders.Status`` is emitted quoted and returned folded to
    ``orders.status`` on a cluster without case-sensitive identifiers; the
    caller still reads the names the model spells. Its quoted column names
    fold the same way, so they resolve to the temp table's lowercase columns."""

    table = "dex_slayer_mixed_orders"
    rendered = render_for("redshift", table, redshift_query(title_case), title_case)
    names, cells = run_on_redshift_temp_table(
        tmp_path, rs_workgroup, rs_database, table, rendered
    )
    columns = [keyed(title_case, column) for column in COLUMNS]

    assert names == columns
    restored = [column.lower() for column in names]
    assert answered(restored, cells) == EXPECTED
