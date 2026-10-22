"""Translating a dex `SemanticQuery` into a `SlayerQuery` (#679).

What the translation promises: a saved measure named ``model.measure`` roots the
query at its model; a group-by token the catalog reports as groupable becomes a
column on that model; ``--grain`` buckets the model's default time dimension;
``--where``, ``--order-by`` and ``--limit`` carry over; and everything else, an
ad-hoc formula, metrics from two models, a token the catalog does not list, a
grain with no time dimension to bucket, is refused by name. The module renders
nothing and imports no more of SLayer than its query model.

Needs the real `[slayer]` extra, so it skips without it; the `slayer-sqlglot` CI
job is where it runs.
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from datetime import datetime
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

from slayer.core.query import ColumnRef, SlayerQuery

from exmergo_dex_core.envelope import Reason, reason_for
from exmergo_dex_core.explore.semantic import SemanticBackendError
from exmergo_dex_core.explore.semantic.model import SemanticQuery
from exmergo_dex_core.slayer.catalog import semantic_catalog
from exmergo_dex_core.slayer.loader import FORMAT_VERSION, validate_model_contents
from exmergo_dex_core.slayer.render import SlayerRenderer
from exmergo_dex_core.slayer.translate import to_slayer_query


def model(name: str, columns: dict[str, str], **fields: Any) -> dict[str, Any]:
    return {
        "version": FORMAT_VERSION,
        "name": name,
        "sql_table": f"demo.main.{name}",
        "data_source": "wh",
        "columns": [{"name": c, "type": t} for c, t in columns.items()],
        **fields,
    }


ORDERS = model(
    "orders",
    {
        "order_id": "INT",
        "customer_id": "INT",
        "status": "TEXT",
        "amount": "DOUBLE",
        "ordered_at": "TIMESTAMP",
    },
    default_time_dimension="ordered_at",
    measures=[
        {"name": "revenue", "formula": "amount:sum"},
        {"name": "order_count", "formula": "*:count"},
    ],
    joins=[{"target_model": "customers", "join_pairs": [["customer_id", "id"]]}],
)
CUSTOMERS = model(
    "customers",
    {"id": "INT", "email": "TEXT", "tier": "TEXT", "signed_up": "DATE", "left": "DATE"},
    measures=[{"name": "customer_count", "formula": "*:count"}],
)


def loaded(*models: dict[str, Any]):
    contents = {f"models/wh/{m['name']}.yaml": yaml.safe_dump(m) for m in models}
    result = validate_model_contents(contents)
    assert not result.errors, result.notes()
    return result.models


LAYER = loaded(ORDERS, CUSTOMERS)
VIEW = semantic_catalog(LAYER, connector="duckdb")


def translate(*metrics: str, view=VIEW, **fields: Any) -> SlayerQuery:
    return to_slayer_query(SemanticQuery(metrics=list(metrics), **fields), view)


def refused(match: str, *metrics: str, **fields: Any) -> None:
    with pytest.raises(SemanticBackendError, match=re.escape(match)) as caught:
        translate(*metrics, **fields)
    assert reason_for(caught.value) is not Reason.INTERNAL


def names(refs: list[ColumnRef] | None) -> list[str]:
    return [ref.full_name for ref in refs or []]


# --- metrics -------------------------------------------------------------------------


def test_a_metric_roots_the_query_at_its_model():
    q = translate("orders.revenue")

    assert q.source_model == "orders"
    assert [m.formula for m in q.measures] == ["revenue"]
    assert q.dimensions is None and q.time_dimensions is None
    assert q.filters is None and q.order is None and q.limit is None


def test_metrics_on_one_model_share_the_root_once_each():
    q = translate("orders.revenue,orders.order_count", group_by=[], order_by=[])
    assert [m.formula for m in q.measures] == ["revenue", "order_count"]

    again = translate("orders.revenue", "orders.revenue")
    assert [m.formula for m in again.measures] == ["revenue"]


def test_metrics_from_two_models_are_refused_naming_each():
    with pytest.raises(SemanticBackendError) as caught:
        translate("orders.revenue", "customers.customer_count")

    message = str(caught.value)
    assert "more than one SLayer model" in message
    assert "orders: orders.revenue" in message
    assert "customers: customers.customer_count" in message


@pytest.mark.parametrize("formula", ["sum(amount)", "orders.amount * 2", "count(*)"])
def test_an_ad_hoc_formula_is_refused(formula):
    refused(f"'{formula}' is a formula, not a metric", formula)


def test_an_unknown_metric_is_refused_by_name():
    refused("no such metric in this semantic layer: orders.margin", "orders.margin")


def test_a_bare_measure_name_is_not_a_catalog_name():
    refused("no such metric in this semantic layer: revenue", "revenue")


def test_a_query_with_no_metric_is_refused():
    refused("needs at least one --metric")


# --- group-by ------------------------------------------------------------------------


def test_a_groupable_token_becomes_a_column_of_the_source_model():
    q = translate("orders.revenue", group_by=["orders.status,orders.customer_id"])

    assert names(q.dimensions) == ["status", "customer_id"]


def test_a_repeated_group_by_is_grouped_once():
    q = translate("orders.revenue", group_by=["orders.status", "orders.status"])
    assert names(q.dimensions) == ["status"]


@pytest.mark.parametrize(
    "token",
    [
        "customers.tier",  # a joined model's column: not groupable in v1
        "orders.missing",
        "status",  # bare, not the catalog's model.column
        "orders.ordered_at__month",  # MetricFlow's grain spelling
    ],
)
def test_a_token_the_catalog_does_not_report_is_refused_by_name(token):
    refused(
        f"cannot group orders.revenue by {token}", "orders.revenue", group_by=[token]
    )


def test_groupability_is_checked_for_every_metric():
    view = semantic_catalog(LAYER, connector="duckdb")
    view.metrics[1].dimensions = ["orders.ordered_at"]  # order_count

    with pytest.raises(SemanticBackendError, match="every one of them"):
        translate(
            "orders.revenue",
            "orders.order_count",
            view=view,
            group_by=["orders.status"],
        )


# --- grain ---------------------------------------------------------------------------


def test_a_grain_buckets_the_model_s_default_time_dimension():
    q = translate("orders.revenue", grain="MONTH")

    (time,) = q.time_dimensions
    assert time.dimension.full_name == "ordered_at"
    assert str(time.granularity) == "month"
    assert q.dimensions is None


def test_grouping_by_the_bucketed_column_folds_it_into_the_time_dimension():
    """SLayer names the raw column and its bucket alike and refuses both."""

    q = translate(
        "orders.revenue",
        group_by=["orders.status,orders.ordered_at"],
        grain="week",
    )

    assert names(q.dimensions) == ["status"]
    assert [t.dimension.full_name for t in q.time_dimensions] == ["ordered_at"]


def test_a_grain_with_no_time_dimension_is_refused_naming_the_model():
    refused(
        "model 'customers' has no time dimension",
        "customers.customer_count",
        grain="month",
    )


@pytest.mark.parametrize("grain", ["fortnight", "fiscal_quarter"])
def test_a_grain_slayer_does_not_build_in_is_refused(grain):
    refused(f"unknown time grain '{grain}'", "orders.revenue", grain=grain)


def test_without_a_grain_a_time_column_groups_raw():
    q = translate("orders.revenue", group_by=["orders.ordered_at"])

    assert names(q.dimensions) == ["ordered_at"]
    assert q.time_dimensions is None


# --- filters, order, limit -----------------------------------------------------------


def test_where_clauses_carry_over_verbatim():
    clauses = ["status in ('paid', 'shipped')", "amount > 10"]
    q = translate("orders.revenue", where=list(clauses))

    assert q.filters == clauses


def test_a_filter_with_a_variable_is_refused():
    refused("carries {variable} syntax", "orders.revenue", where=["status == {s}"])


def test_order_by_and_limit_map_directly():
    q = translate(
        "orders.revenue",
        group_by=["orders.status"],
        order_by=["-orders.revenue,orders.status"],
        limit=5,
    )

    assert [(o.column.full_name, o.direction) for o in q.order] == [
        ("revenue", "desc"),
        ("status", "asc"),
    ]
    assert q.limit == 5


def test_the_bucketed_column_is_orderable():
    q = translate("orders.revenue", grain="month", order_by=["orders.ordered_at"])

    assert [(o.column.full_name, o.direction) for o in q.order] == [
        ("ordered_at", "asc")
    ]


@pytest.mark.parametrize(
    "token", ["orders.amount", "orders.order_count", "sum(amount)", "orders.ordered_at"]
)
def test_order_by_outside_the_query_is_refused(token):
    """Grouped columns are what the PII gate screens; nothing else is orderable."""

    refused(
        f"cannot order by {token}",
        "orders.revenue",
        group_by=["orders.status"],
        order_by=[token],
    )


# --- SLayer reads what dex wrote ------------------------------------------------------


def test_the_translated_query_renders_as_asked():
    q = translate(
        "orders.revenue",
        group_by=["orders.status,orders.ordered_at"],
        grain="month",
        where=["status != 'void'"],
        order_by=["-orders.revenue"],
        limit=3,
    )
    sql = (
        SlayerRenderer(LAYER, connector="duckdb", now=datetime(2026, 1, 15))
        .render(q)
        .sql
    )

    assert "DATE_TRUNC('MONTH', orders.ordered_at)" in sql
    assert "orders.status" in sql
    assert "SUM(orders.amount)" in sql
    assert '"orders.revenue" DESC' in sql
    assert "LIMIT 3" in sql


# --- imports -------------------------------------------------------------------------


def test_translating_loads_no_engine_client_or_storage(tmp_path):
    """In a fresh interpreter, since another test may have imported them."""

    script = textwrap.dedent(
        """
        import sys
        from exmergo_dex_core.slayer import translate
        print(sorted(
            name for name in sys.modules
            if name.startswith(("slayer.engine.query", "slayer.sql.client"))
            or name in ("slayer.storage.yaml_storage", "slayer.storage.sqlite_storage")
        ))
        """
    )
    run = subprocess.run(  # noqa: S603 - a fixed script, run by this interpreter
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=True,
    )
    assert run.stdout.strip().splitlines()[-1] == "[]"
