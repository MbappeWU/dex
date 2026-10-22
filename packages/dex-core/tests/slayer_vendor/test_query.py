"""A governed `explore semantic query` and `values` over a SLayer layer (#680, #681).

SLayer renders and dex executes. What this release promises: a saved measure
grouped by a column returns the numbers hand-written SQL does; the PII gate
screens every grouped column and every column a filter names, looked up in the
`.dex/` cache through the catalog's physical columns before anything renders;
a filter dex cannot read, a connector SLayer has no dialect for and a relation
the connection lacks are each refused by name; a SLayer resolution error comes
back redacted; and on a billed connector the query stops for confirmation
before any spend. `values` reads one dimension's domain through the same spine:
an unknown token and a PII dimension are refused before anything renders,
`--metric` roots the render at that metric's model and `scoped_to` says so, and
the values come back capped.

Needs the real `[slayer]` extra, so it skips without it; the `slayer-sqlglot` CI
job is where it runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from exmergo_dex_core.slayer import dependency
from exmergo_dex_core.slayer.dependency import SLAYER_VERSION

if dependency.installed_version() != SLAYER_VERSION:
    pytest.skip(
        f"the [slayer] extra (SLayer {SLAYER_VERSION}) is not installed",
        allow_module_level=True,
    )

from exmergo_dex_core.cli import main
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.explore.semantic import SemanticBackendError
from exmergo_dex_core.explore.semantic.backend import resolve_semantic_layer
from exmergo_dex_core.explore.semantic.model import SemanticQuery
from exmergo_dex_core.slayer import render
from exmergo_dex_core.slayer.loader import FORMAT_VERSION

duckdb = pytest.importorskip("duckdb")

BASE = "semantics/slayer"


def model(name: str, *, table: str | None = None, **fields: Any) -> dict[str, Any]:
    return {
        "version": FORMAT_VERSION,
        "name": name,
        "sql_table": table or f"shop.main.{name}",
        "data_source": "wh",
        **fields,
    }


ORDERS = model(
    "orders",
    default_time_dimension="ordered_at",
    filters=["status <> 'test'"],
    columns=[
        {"name": "order_id", "type": "INT", "primary_key": True},
        {"name": "customer_id", "type": "INT"},
        {"name": "status", "type": "TEXT"},
        {"name": "amount", "type": "DOUBLE"},
        {"name": "ordered_at", "type": "TIMESTAMP"},
        # Named nothing like PII, and backed by a column that is: only the
        # catalog's physical columns can tell the gate what it reads.
        {"name": "buyer", "type": "TEXT", "sql": "contact_email"},
    ],
    measures=[
        {"name": "revenue", "formula": "amount:sum"},
        {"name": "order_count", "formula": "*:count"},
    ],
    joins=[{"target_model": "customers", "join_pairs": [["customer_id", "id"]]}],
)
CUSTOMERS = model(
    "customers",
    columns=[
        {"name": "id", "type": "INT", "primary_key": True},
        {"name": "email", "type": "TEXT"},
        {"name": "tier", "type": "TEXT"},
    ],
    measures=[{"name": "customer_count", "formula": "*:count"}],
)

ROWS = [
    (1, 1, "paid", 10.0, "2026-01-03", "a@example.com"),
    (2, 1, "paid", 5.5, "2026-01-20", "a@example.com"),
    (3, 2, "open", 7.0, "2026-02-01", "b@example.com"),
    (4, 2, "paid", 2.5, "2026-02-14", "b@example.com"),
    (5, 3, "refunded", 4.0, "2026-03-09", "c@example.com"),
    # Excluded by the model's own filter, so a number that counts it is wrong.
    (6, 3, "test", 1000.0, "2026-03-10", "c@example.com"),
]


def _warehouse(root: Path) -> Path:
    path = root / "shop.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute(
        "CREATE TABLE orders (order_id INT, customer_id INT, status VARCHAR, "
        "amount DOUBLE, ordered_at TIMESTAMP, contact_email VARCHAR)"
    )
    conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", ROWS)
    conn.execute("CREATE TABLE customers (id INT, email VARCHAR, tier VARCHAR)")
    conn.executemany(
        "INSERT INTO customers VALUES (?, ?, ?)",
        [(1, "a@example.com", "gold"), (2, "b@example.com", "free")],
    )
    conn.close()
    return path


def _repo(root: Path, *models: dict[str, Any], connector: str = "duckdb") -> Path:
    names: list[str] = []
    for entry in models:
        name = f"models/wh/{entry['name']}.yaml"
        path = root / BASE / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(entry, sort_keys=False), encoding="utf-8")
        names.append(name)
    warehouse = _warehouse(root)
    (root / ".dex").mkdir(exist_ok=True)
    (root / ".dex" / "config.yml").write_text(
        f"connector: {connector}\nduckdb:\n  path: {warehouse}\n"
        f"semantic:\n  vendor: slayer\n  slayer:\n    base_dir: {BASE}\n"
        "    models:\n" + "".join(f"      - {name}\n" for name in names),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _repo(tmp_path, ORDERS, CUSTOMERS)


def _run(capsys, root: Path, *argv: str) -> tuple[int, dict]:
    rc = main(["--repo-root", str(root), *argv])
    return rc, json.loads(capsys.readouterr().out)


def _query(capsys, root: Path, *argv: str) -> tuple[int, dict]:
    return _run(capsys, root, "explore", "semantic", "query", *argv)


def _values(capsys, root: Path, *argv: str) -> tuple[int, dict]:
    return _run(capsys, root, "explore", "semantic", "values", *argv)


def _by_hand(root: Path, sql: str) -> list[list[Any]]:
    conn = duckdb.connect(str(root / "shop.duckdb"), read_only=True)
    try:
        return [list(row) for row in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def _errors(envelope: dict) -> str:
    return " ".join(envelope["errors"])


# --- the numbers -------------------------------------------------------------------


def test_a_saved_measure_grouped_by_a_column_matches_hand_written_sql(repo, capsys):
    rc, envelope = _query(capsys, repo, "orders.revenue", "--group-by", "orders.status")

    assert rc == 0, envelope["errors"]
    data = envelope["data"]
    assert data["columns"] == ["orders.status", "orders.revenue"]
    assert sorted(data["cells"]) == sorted(
        _by_hand(
            repo,
            "SELECT status, SUM(amount) FROM orders WHERE status <> 'test' "
            "GROUP BY status",
        )
    )
    assert (data["vendor"], data["execution"]) == ("slayer", "dex")
    # dex executed it, so the cost guard applied and nothing says otherwise.
    assert not any("cost guard" in warning for warning in envelope["warnings"])


def test_a_filtered_bucketed_query_matches_hand_written_sql(repo, capsys):
    rc, envelope = _query(
        capsys,
        repo,
        "orders.revenue,orders.order_count",
        "--grain",
        "month",
        "--where",
        "status = 'paid'",
        "--order-by",
        "orders.ordered_at",
    )

    assert rc == 0, envelope["errors"]
    by_hand = _by_hand(
        repo,
        "SELECT date_trunc('month', ordered_at), SUM(amount), COUNT(*) FROM orders "
        "WHERE status <> 'test' AND status = 'paid' GROUP BY 1 ORDER BY 1",
    )
    assert [cells[1:] for cells in envelope["data"]["cells"]] == [
        row[1:] for row in by_hand
    ]
    assert len(envelope["data"]["cells"]) == 2


def test_order_and_limit_reach_the_result(repo, capsys):
    rc, envelope = _query(
        capsys,
        repo,
        "orders.revenue",
        "--group-by",
        "orders.status",
        "--order-by=-orders.revenue",
        "--limit",
        "1",
    )

    assert rc == 0, envelope["errors"]
    assert envelope["data"]["cells"] == [["paid", 18.0]]


def test_a_mangled_alias_comes_back_under_its_model_column_key(
    repo, capsys, monkeypatch
):
    """BigQuery's render emits `orders___status`, because a dotted output column
    is an error there. Mangled the same way on DuckDB, the caller still reads the
    `model.column` names, because the render's result keys reach the spine."""

    real = render.SlayerRenderer.render

    def mangled(self, query):
        result = real(self, query)
        keys = {
            key.replace(".", "___"): key for key in ("orders.status", "orders.revenue")
        }
        sql = result.sql
        for emitted, key in keys.items():
            sql = sql.replace(f'"{key}"', f'"{emitted}"')
        return render.RenderedQuery(sql=sql, result_keys=keys)

    monkeypatch.setattr(render.SlayerRenderer, "render", mangled)

    rc, envelope = _query(capsys, repo, "orders.revenue", "--group-by", "orders.status")

    assert rc == 0, envelope["errors"]
    assert envelope["data"]["columns"] == ["orders.status", "orders.revenue"]


def test_an_unprofiled_relation_is_disclosed(repo, capsys):
    _, envelope = _query(capsys, repo, "orders.revenue")

    assert any(
        "name heuristic" in note and "shop.main.orders" in note
        for note in envelope["data"]["notes"]
    )


# --- the PII gate ------------------------------------------------------------------


def test_a_pii_group_by_is_refused_by_its_name(repo, capsys):
    rc, envelope = _query(
        capsys, repo, "customers.customer_count", "--group-by", "customers.email"
    )

    assert rc != 0
    assert envelope["reason"] == "guard"
    assert "customers.email" in _errors(envelope)
    assert "would surface PII" in _errors(envelope)


def test_a_pii_group_by_is_refused_from_the_profile_through_its_physical_column(
    repo, capsys
):
    """`buyer` reads `contact_email`, which the profile flags: the heuristic would pass
    the token, so the refusal is the cache speaking through the catalog."""

    rc, profiled = _run(capsys, repo, "explore", "profile", "shop.main.orders")
    assert rc == 0, profiled["errors"]

    rc, envelope = _query(capsys, repo, "orders.revenue", "--group-by", "orders.buyer")

    assert rc != 0
    assert "orders.buyer" in _errors(envelope)
    assert "profiled and flagged" in _errors(envelope)


@pytest.mark.parametrize(
    "clause",
    [
        "buyer IS NOT NULL",
        # Through the declared join, to the model the column lives on.
        "customers.email = 'a@example.com'",
        # Inside an aggregation's argument, which SLayer's own walker skips.
        "count(customers.email) > 0",
    ],
)
def test_a_pii_filter_is_refused(repo, capsys, clause):
    _run(capsys, repo, "explore", "profile", "shop.main.orders")

    rc, envelope = _query(capsys, repo, "orders.revenue", "--where", clause)

    assert rc != 0
    assert envelope["reason"] == "guard"
    assert "would surface PII" in _errors(envelope)


@pytest.mark.parametrize("table", ["main.orders", "orders"])
@pytest.mark.parametrize(
    "request_argv",
    [["--group-by", "orders.buyer"], ["--where", "buyer IS NOT NULL"]],
    ids=["group-by", "where"],
)
def test_a_flag_behind_a_partial_sql_table_is_refused(
    tmp_path, capsys, table, request_argv
):
    """A `sql_table` dex cannot qualify carries no relation in the catalog, and
    the gate still resolves it against the profile by suffix: `buyer` passes the
    heuristic, so only the flag on `contact_email` can refuse it."""

    root = _repo(tmp_path, {**ORDERS, "sql_table": table}, CUSTOMERS)
    rc, profiled = _run(capsys, root, "explore", "profile", "shop.main.orders")
    assert rc == 0, profiled["errors"]

    rc, envelope = _query(capsys, root, "orders.revenue", *request_argv)

    assert rc != 0
    assert envelope["reason"] == "guard"
    assert "orders.buyer" in _errors(envelope)
    assert "profiled and flagged" in _errors(envelope)


def test_a_column_with_no_mapped_warehouse_column_is_disclosed(tmp_path, capsys):
    recent = model(
        "recent",
        sql_table=None,
        sql="select status, amount from orders",
        columns=[
            {"name": "status", "type": "TEXT"},
            {"name": "amount", "type": "DOUBLE"},
        ],
        measures=[{"name": "revenue", "formula": "amount:sum"}],
    )
    root = _repo(tmp_path, recent)

    rc, envelope = _query(capsys, root, "recent.revenue", "--group-by", "recent.status")

    assert rc == 0, envelope["errors"]
    assert any(
        "name heuristic alone for recent.status" in note
        for note in envelope["data"]["notes"]
    )


def test_a_grain_on_a_pii_time_dimension_is_refused(tmp_path, capsys):
    """`--grain` groups by the root's time dimension, so the gate screens it."""

    patients = model(
        "patients",
        default_time_dimension="date_of_birth",
        columns=[
            {"name": "patient_id", "type": "INT", "primary_key": True},
            {"name": "date_of_birth", "type": "DATE"},
        ],
        measures=[{"name": "patient_count", "formula": "*:count"}],
    )
    root = _repo(tmp_path, patients)
    conn = duckdb.connect(str(root / "shop.duckdb"))
    conn.execute("CREATE TABLE patients (patient_id INT, date_of_birth DATE)")
    conn.execute("INSERT INTO patients VALUES (1, '1990-04-02'), (2, '1985-11-30')")
    conn.close()

    rc, envelope = _query(capsys, root, "patients.patient_count", "--grain", "day")

    assert rc != 0
    assert envelope["reason"] == "guard"
    assert "patients.date_of_birth" in _errors(envelope)
    assert "would surface PII" in _errors(envelope)


# --- what a query reads without naming it (#698) -----------------------------------

# A column computed from a flagged one, named nothing like PII, and a saved measure
# over the same column: only reading the expression or the formula reaches it.
COMPUTED = {
    **ORDERS,
    "columns": [
        *ORDERS["columns"],
        {"name": "contact_key", "type": "TEXT", "sql": "upper(contact_email)"},
    ],
    "measures": [
        *ORDERS["measures"],
        {"name": "reach", "formula": "contact_email:count_distinct"},
    ],
}


@pytest.mark.parametrize("profiled", [True, False])
@pytest.mark.parametrize(
    "request_argv",
    [
        ("--group-by", "orders.contact_key"),
        ("--where", "contact_key = 'A@EXAMPLE.COM'"),
    ],
)
def test_a_computed_column_over_a_pii_column_is_refused(
    tmp_path, capsys, monkeypatch, request_argv, profiled
):
    """Screened by the columns its `sql` reads, with the profile's flag where one
    exists and the read column's own name where none does."""

    root = _repo(tmp_path, COMPUTED, CUSTOMERS)
    if profiled:
        rc, envelope = _run(capsys, root, "explore", "profile", "shop.main.orders")
        assert rc == 0, envelope["errors"]
    monkeypatch.setattr(render.SlayerRenderer, "render", _never_rendered)

    rc, envelope = _query(capsys, root, "orders.revenue", *request_argv)

    assert rc != 0
    assert envelope["reason"] == "guard"
    assert "would surface PII" in _errors(envelope)
    assert "read from contact_email" in _errors(envelope)
    if profiled:
        assert "profiled and flagged" in _errors(envelope)


def test_a_saved_measure_over_a_pii_column_in_a_filter_is_refused(
    tmp_path, capsys, monkeypatch
):
    """`reach > 1` renders `HAVING COUNT(DISTINCT contact_email) > 1`, so the
    filter reads that column, not one called `reach`."""

    root = _repo(tmp_path, COMPUTED, CUSTOMERS)
    monkeypatch.setattr(render.SlayerRenderer, "render", _never_rendered)

    rc, envelope = _query(capsys, root, "orders.revenue", "--where", "reach > 1")

    assert rc != 0
    assert envelope["reason"] == "guard"
    assert "orders.contact_email" in _errors(envelope)


def test_a_grain_on_a_flagged_time_column_with_a_neutral_name_is_refused(
    tmp_path, capsys, monkeypatch
):
    """The time axis `--grain` buckets is looked up in the profile like a grouped
    column, so a flag decides even where the column's token reads clean."""

    patients = model(
        "patients",
        default_time_dimension="event_day",
        columns=[
            {"name": "patient_id", "type": "INT", "primary_key": True},
            {"name": "event_day", "type": "DATE", "sql": "date_of_birth"},
        ],
        measures=[{"name": "patient_count", "formula": "*:count"}],
    )
    root = _repo(tmp_path, patients)
    conn = duckdb.connect(str(root / "shop.duckdb"))
    conn.execute("CREATE TABLE patients (patient_id INT, date_of_birth DATE)")
    conn.execute("INSERT INTO patients VALUES (1, '1990-04-02'), (2, '1985-11-30')")
    conn.close()
    rc, envelope = _run(capsys, root, "explore", "profile", "shop.main.patients")
    assert rc == 0, envelope["errors"]
    monkeypatch.setattr(render.SlayerRenderer, "render", _never_rendered)

    rc, envelope = _query(capsys, root, "patients.patient_count", "--grain", "day")

    assert rc != 0
    assert envelope["reason"] == "guard"
    assert "patients.event_day" in _errors(envelope)
    assert "profiled and flagged" in _errors(envelope)


@pytest.mark.parametrize("clause", ["status =", "status == {state}"])
def test_a_filter_dex_cannot_read_is_refused(repo, capsys, clause):
    rc, envelope = _query(capsys, repo, "orders.revenue", "--where", clause)

    assert rc != 0
    assert envelope["reason"] == "guard"
    assert "cannot read the dimensions its own filter dialect names" in _errors(
        envelope
    )


def test_a_filtered_query_with_no_single_root_is_refused_by_the_metric(repo, capsys):
    """The filter is read against the query's root, so a metric that names none
    is the problem stated, rather than a filter the gate could not read."""

    rc, envelope = _query(capsys, repo, "orders.revnue", "--where", "status = 'paid'")

    assert rc != 0
    assert "no such metric in this semantic layer: orders.revnue" in _errors(envelope)


def test_the_gate_runs_before_anything_renders(repo, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("rendered a query the gate should have refused")

    monkeypatch.setattr(render.SlayerRenderer, "render", boom)
    layer = resolve_semantic_layer(DexEngine.from_repo(repo))

    with pytest.raises(SemanticBackendError, match="would surface PII"):
        layer.query(
            SemanticQuery(
                metrics=["customers.customer_count"], group_by=["customers.email"]
            )
        )


# --- the other refusals ------------------------------------------------------------


def test_a_connector_with_no_slayer_dialect_is_refused(repo, capsys):
    rc, envelope = _query(capsys, repo, "orders.revenue", "--connector", "clickhouse")

    assert rc != 0
    assert "no SLayer dialect for connector 'clickhouse'" in _errors(envelope)


def test_a_relation_the_connection_lacks_is_refused_before_it_runs(tmp_path, capsys):
    root = _repo(
        tmp_path,
        model(
            "orders",
            table="shop.main.archived_orders",
            columns=[
                {"name": "amount", "type": "DOUBLE"},
            ],
            measures=[{"name": "revenue", "formula": "amount:sum"}],
        ),
    )

    rc, envelope = _query(capsys, root, "orders.revenue")

    assert rc != 0
    assert "shop.main.archived_orders" in _errors(envelope)
    assert "does not have" in _errors(envelope)
    assert "sql_table" in _errors(envelope)


def test_a_slayer_resolution_error_is_reported_redacted(repo, capsys, monkeypatch):
    def unresolvable(*_a, **_k):
        raise ValueError("no path to model x via postgres://dex:hunter2@db/x")

    monkeypatch.setattr(render.SlayerRenderer, "render", unresolvable)

    rc, envelope = _query(capsys, repo, "orders.revenue")

    assert rc != 0
    assert "could not resolve the metric query" in _errors(envelope)
    assert "hunter2" not in json.dumps(envelope)


def test_the_filter_reader_needs_the_query_s_root(repo):
    layer = resolve_semantic_layer(DexEngine.from_repo(repo))

    assert layer.filter_refs(["status = 'paid'"]) is None
    assert layer.filter_refs(
        ["status = 'paid' and customers.tier = 'gold'"], source_model="orders"
    ) == ["orders.status", "customers.tier"]


# --- the billed path ---------------------------------------------------------------


def test_a_billed_connector_asks_before_any_spend(
    fake_bq_client, tmp_path, capsys, monkeypatch
):
    import exmergo_dex_core.connect as connect_mod
    from exmergo_dex_core.adapters.bigquery import BigQueryAdapter
    from exmergo_dex_core.config import BigQueryTarget, load_config
    from exmergo_dex_core.connect import new_cost_gate
    from exmergo_dex_core.storage import FilesystemStore

    customers = model(
        "customers",
        table="test-proj.shop.customers",
        columns=[{"name": "id", "type": "INT"}, {"name": "email", "type": "TEXT"}],
        measures=[{"name": "customer_count", "formula": "*:count"}],
    )
    name = "models/wh/customers.yaml"
    path = tmp_path / BASE / name
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(customers), encoding="utf-8")
    (tmp_path / ".dex").mkdir()
    (tmp_path / ".dex" / "config.yml").write_text(
        "connector: bigquery\nbigquery:\n  project: test-proj\n"
        "budget:\n  session_ceiling: 1073741824\n"
        f"semantic:\n  vendor: slayer\n  slayer:\n    base_dir: {BASE}\n"
        f"    models:\n      - {name}\n",
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    store = FilesystemStore(tmp_path)

    def opener(**opened):
        return BigQueryAdapter(
            project="test-proj",
            cost_gate=new_cost_gate(
                "bigquery",
                config,
                store,
                budget=opened.get("budget"),
                confirmed=opened.get("confirmed", False),
                command=opened.get("command"),
            ),
            target=BigQueryTarget(),
            client=fake_bq_client,
            principal_type="user",
        )

    monkeypatch.setattr(connect_mod, "open_adapter", opener)

    _, envelope = _query(
        capsys, tmp_path, "customers.customer_count", "--group-by", "customers.id"
    )

    assert envelope["status"] == "needs_confirmation", envelope["errors"]
    assert envelope["cost"]["estimate"] > 0
    assert fake_bq_client.query_calls, "nothing was priced, so nothing was proven"
    assert all(call.dry_run for call in fake_bq_client.query_calls)
    assert "test-proj.shop.customers" in fake_bq_client.query_calls[-1].sql.replace(
        "`", ""
    )


# --- values ------------------------------------------------------------------------


def test_a_dimension_s_values_match_hand_written_sql(repo, capsys):
    rc, envelope = _values(capsys, repo, "orders.status")

    assert rc == 0, envelope["errors"]
    data = envelope["data"]
    assert (data["dimension"], data["scoped_to"]) == ("orders.status", [])
    assert data["columns"] == ["orders.status"]
    # The model's own filter applies: SLayer renders every query on the model
    # under it, so `test` is not a value of this dimension.
    assert sorted(data["cells"]) == sorted(
        _by_hand(repo, "SELECT DISTINCT status FROM orders WHERE status <> 'test'")
    )
    assert data["truncated"] is False
    assert (data["vendor"], data["execution"]) == ("slayer", "dex")
    assert not any("cost guard" in warning for warning in envelope["warnings"])


def test_a_metric_scopes_the_render_and_scoped_to_reports_it(repo, capsys, monkeypatch):
    rendered: list[Any] = []
    real = render.SlayerRenderer.render

    def spy(self, query):
        rendered.append(query)
        return real(self, query)

    monkeypatch.setattr(render.SlayerRenderer, "render", spy)

    rc, envelope = _values(capsys, repo, "orders.status", "--metric", "orders.revenue")

    assert rc == 0, envelope["errors"]
    assert envelope["data"]["scoped_to"] == ["orders.revenue"]
    (query,) = rendered
    # Rooted at the metric's model, and still no measure: a value domain.
    assert query.source_model == "orders"
    assert not query.measures
    assert query.distinct_dimension_values is True


def test_a_dimension_the_metric_cannot_group_by_is_refused(repo, capsys):
    rc, envelope = _values(
        capsys, repo, "orders.status", "--metric", "customers.customer_count"
    )

    assert rc != 0
    assert "orders.status is not groupable for customers.customer_count" in _errors(
        envelope
    )


def test_a_time_dimension_s_values_at_a_grain(repo, capsys):
    rc, envelope = _values(capsys, repo, "orders.ordered_at__month")

    assert rc == 0, envelope["errors"]
    assert envelope["data"]["dimension"] == "orders.ordered_at__month"
    by_hand = _by_hand(
        repo,
        "SELECT DISTINCT date_trunc('month', ordered_at) FROM orders "
        "WHERE status <> 'test'",
    )
    assert sorted(row[0] for row in envelope["data"]["cells"]) == sorted(
        row[0].isoformat() for row in by_hand
    )


def test_a_grain_on_a_categorical_dimension_is_refused(repo, capsys):
    rc, envelope = _values(capsys, repo, "orders.status__month")

    assert rc != 0
    assert "orders.status is not a time dimension" in _errors(envelope)


def test_an_unknown_dimension_is_refused_by_name(repo, capsys, monkeypatch):
    monkeypatch.setattr(render.SlayerRenderer, "render", _never_rendered)

    rc, envelope = _values(capsys, repo, "orders.statsu")

    assert rc != 0
    assert "no such dimension in this semantic layer: orders.statsu" in _errors(
        envelope
    )
    assert "model.column" in _errors(envelope)


def test_an_unknown_scoping_metric_is_refused_by_name(repo, capsys):
    rc, envelope = _values(capsys, repo, "orders.status", "--metric", "orders.revnue")

    assert rc != 0
    assert "no such metric in this semantic layer: orders.revnue" in _errors(envelope)


def test_a_pii_dimension_s_values_are_refused_by_its_name(repo, capsys, monkeypatch):
    monkeypatch.setattr(render.SlayerRenderer, "render", _never_rendered)

    rc, envelope = _values(capsys, repo, "customers.email")

    assert rc != 0
    assert envelope["reason"] == "guard"
    assert "customers.email is PII" in _errors(envelope)


def test_a_pii_dimension_s_values_are_refused_from_the_profile(repo, capsys):
    """`buyer` reads `contact_email`: only the cache, reached through the catalog's
    physical column, can tell the gate what it is."""

    rc, profiled = _run(capsys, repo, "explore", "profile", "shop.main.orders")
    assert rc == 0, profiled["errors"]

    rc, envelope = _values(capsys, repo, "orders.buyer")

    assert rc != 0
    assert envelope["reason"] == "guard"
    assert "orders.buyer is PII" in _errors(envelope)
    assert "profiled and flagged" in _errors(envelope)


def test_a_flag_behind_a_partial_sql_table_refuses_values(
    tmp_path, capsys, monkeypatch
):
    """`main.orders` carries no relation in the catalog, so `contact` resolves to
    the profile by suffix: its name passes the heuristic, and only the flag on
    `contact_email` can refuse it."""

    contact = {"name": "contact", "type": "TEXT", "sql": "contact_email"}
    orders = {
        **ORDERS,
        "sql_table": "main.orders",
        "columns": [*ORDERS["columns"], contact],
    }
    root = _repo(tmp_path, orders, CUSTOMERS)
    rc, profiled = _run(capsys, root, "explore", "profile", "shop.main.orders")
    assert rc == 0, profiled["errors"]
    monkeypatch.setattr(render.SlayerRenderer, "render", _never_rendered)

    rc, envelope = _values(capsys, root, "orders.contact")

    assert rc != 0
    assert envelope["reason"] == "guard"
    assert "orders.contact is PII" in _errors(envelope)
    assert "profiled and flagged" in _errors(envelope)


def test_values_screened_by_name_alone_say_so(repo, capsys):
    rc, envelope = _values(capsys, repo, "orders.status")

    assert rc == 0, envelope["errors"]
    assert any(
        "name heuristic alone for orders.status" in note
        for note in envelope["data"]["notes"]
    )


def test_values_come_back_capped_and_truncated(repo, capsys):
    config = repo / ".dex" / "config.yml"
    config.write_text(
        config.read_text(encoding="utf-8") + "query:\n  max_rows: 2\n",
        encoding="utf-8",
    )

    rc, envelope = _values(capsys, repo, "orders.status")

    assert rc == 0, envelope["errors"]
    data = envelope["data"]
    assert data["row_count"] == 2
    assert len(data["cells"]) == 2
    assert data["truncated"] is True


def test_slayer_s_own_value_profiling_is_never_reached(repo, capsys, monkeypatch):
    """SLayer samples a column's values into its model file through
    `update_column_sampled`; under dex nothing may call it, and the model file
    is left byte-identical."""

    calls: list[Any] = []

    async def recorded(self, *args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("SLayer's value profiling was reached")

    monkeypatch.setattr(render.ValidatedModelStorage, "update_column_sampled", recorded)
    files = sorted((repo / BASE).rglob("*.yaml"))
    before = [path.read_bytes() for path in files]

    rc, envelope = _values(capsys, repo, "orders.status")

    assert rc == 0, envelope["errors"]
    assert calls == []
    assert [path.read_bytes() for path in files] == before


def _never_rendered(*_a, **_k):
    raise AssertionError("rendered a values request that should have been refused")
