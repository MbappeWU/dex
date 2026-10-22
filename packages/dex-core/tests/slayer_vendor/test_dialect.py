"""Reading the columns a SLayer filter names, fail-closed (#678).

What the dialect promises: every reference in a clause comes back as a
``model.column`` token, a bare one on the source model and a dotted one on the
model its declared joins end at, aggregation and transform arguments included;
and anything it cannot account for is None, never ``[]``, so the PII gate refuses
rather than screening a filter it did not read.

Needs the real `[slayer]` extra, so it skips without it; the `slayer-sqlglot` CI
job is where it runs.
"""

from __future__ import annotations

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

from exmergo_dex_core import slayer_dialect
from exmergo_dex_core.explore.semantic.conformance import SemanticBackendContract
from exmergo_dex_core.slayer.loader import FORMAT_VERSION, validate_model_contents


def model(name: str, columns: list[str], **fields: Any) -> dict[str, Any]:
    return {
        "version": FORMAT_VERSION,
        "name": name,
        "sql_table": f"demo.main.{name}",
        "data_source": "wh",
        "columns": [{"name": c, "type": "TEXT"} for c in columns],
        **fields,
    }


def join(target: str, source_col: str, target_col: str, **fields: Any) -> dict:
    return {"target_model": target, "join_pairs": [[source_col, target_col]], **fields}


def models_by_name(*models: dict[str, Any]) -> dict[str, Any]:
    contents = {f"models/wh/{m['name']}.yaml": yaml.safe_dump(m) for m in models}
    result = validate_model_contents(contents)
    assert not result.errors, result.notes()
    return {loaded.model.name: loaded.model for loaded in result.models}


# orders -> customers -> regions, declared on the side that holds the key, plus
# a reverse hop (shipments declares its join to orders, not the other way).
LAYER = models_by_name(
    model(
        "orders",
        ["order_id", "customer_id", "status", "amount", "created_at", "price", "qty"],
        joins=[join("customers", "customer_id", "customer_id")],
    ),
    model(
        "customers",
        ["customer_id", "region_id", "email", "name"],
        joins=[join("regions", "region_id", "region_id")],
    ),
    model("regions", ["region_id", "name"]),
    model(
        "shipments",
        ["shipment_id", "order_id", "carrier"],
        joins=[join("orders", "order_id", "order_id")],
    ),
)


def refs(*clauses: str, source: str = "orders", layer=LAYER) -> list[str] | None:
    return slayer_dialect.filter_refs(list(clauses), source, models=layer)


@pytest.mark.parametrize(
    ("clause", "expected"),
    [
        # SQL spellings, normalized by SLayer's own filter parser
        ("status = 'paid'", ["orders.status"]),
        ("status <> 'paid'", ["orders.status"]),
        ("status = 'paid' AND amount > 10", ["orders.status", "orders.amount"]),
        ("NOT (status = 'a' OR amount < 1)", ["orders.status", "orders.amount"]),
        ("status LIKE 'pa%'", ["orders.status"]),
        # the Python spellings the DSL admits natively
        ("status == 'paid' and amount >= 10", ["orders.status", "orders.amount"]),
        # IN / NOT IN
        ("status IN ('a', 'b')", ["orders.status"]),
        ("status not in ('a', 'b')", ["orders.status"]),
        # IS NULL / IS NOT NULL
        ("created_at IS NULL", ["orders.created_at"]),
        ("created_at is not null", ["orders.created_at"]),
        # references inside aggregates count
        ("sum(amount) > 100", ["orders.amount"]),
        ("amount:sum > 100", ["orders.amount"]),
        ("sum(price * qty) > 0", ["orders.price", "orders.qty"]),
        ("avg(sum(amount)) > 1", ["orders.amount"]),
        # ...and so do an aggregation's own arguments, which SLayer's walk skips
        ("weighted_avg(price, weight=qty) > 3", ["orders.price", "orders.qty"]),
        (
            "first(status, order_by=created_at) == 'x'",
            ["orders.status", "orders.created_at"],
        ),
        # ...and a transform's, where a partition can name a PII column
        (
            "rank(sum(amount), partition_by=customers.email) <= 1",
            ["orders.amount", "customers.email"],
        ),
        ("count(*) > 2", []),
        # scalar functions and CASE descend into their operands
        ("upper(status) == 'X'", ["orders.status"]),
        (
            "CASE WHEN status = 'a' THEN amount ELSE 0 END > 1",
            ["orders.status", "orders.amount"],
        ),
        ("created_at > now() - interval(7, 'day')", ["orders.created_at"]),
        # literal-only clauses name nothing, which is an answer, not a miss
        ("1 = 1", []),
        # the source model's own name as a qualifier is the source model
        ("orders.status = 'paid'", ["orders.status"]),
        # dotted refs resolve through declared joins, multi-hop and reversed
        ("customers.email like '%@x.com'", ["customers.email"]),
        ("customers.regions.name = 'EU'", ["regions.name"]),
        ("shipments.carrier = 'ups'", ["shipments.carrier"]),
        ("customers.region_id is null", ["customers.region_id"]),
    ],
)
def test_a_clause_names_its_columns(clause: str, expected: list[str]) -> None:
    assert refs(clause) == expected


def test_clauses_answer_in_order_with_duplicates() -> None:
    """De-duplication is the caller's, which merges these with the group-by."""

    assert refs("status = 'a'", "amount > 1 and status = 'b'") == [
        "orders.status",
        "orders.amount",
        "orders.status",
    ]


def test_a_bare_ref_resolves_to_the_source_model() -> None:
    assert refs("name = 'Ada'", source="customers") == ["customers.name"]
    assert refs("name = 'EU'", source="regions") == ["regions.name"]


@pytest.mark.parametrize(
    "clause",
    [
        # does not parse in Mode-B
        "not a filter in any dialect",
        "status = 'a'; drop table orders",
        "x between 1 and 2",
        "",
        # other dialects
        "{{ Dimension('user__pricing_tier') }} = 'pro'",
        '{"member": "users.pricing_tier", "operator": "equals"}',
        # {var} placeholders: filled after the gate, possibly as a column
        "status = {status}",
        "status in {statuses}",
        "status = '{status}'",
        "{column} = 1",
        # a dotted ref no declared join reaches
        "suppliers.name = 'x'",
        "customers.nope.name = 'x'",
    ],
)
def test_a_clause_it_cannot_account_for_is_none_not_empty(clause: str) -> None:
    assert refs(clause) is None


def test_one_unreadable_clause_withholds_the_whole_answer() -> None:
    """A partial list would screen the clauses it read and pass the one it did not."""

    assert refs("status = 'a'", "not a filter") is None


def test_an_ambiguous_join_path_is_none() -> None:
    """Two unnamed edges between the same models: which one a dotted ref takes is
    SLayer's to refuse, and the gate must not screen a guess."""

    layer = models_by_name(
        model(
            "orders",
            ["order_id", "buyer_id", "seller_id"],
            joins=[
                join("people", "buyer_id", "person_id"),
                join("people", "seller_id", "person_id"),
            ],
        ),
        model("people", ["person_id", "email"]),
    )

    assert refs("people.email = 'x'", layer=layer) is None


def test_a_named_edge_disambiguates() -> None:
    layer = models_by_name(
        model(
            "orders",
            ["order_id", "buyer_id", "seller_id"],
            joins=[
                join("people", "buyer_id", "person_id", name="buyer"),
                join("people", "seller_id", "person_id", name="seller"),
            ],
        ),
        model("people", ["person_id", "email"]),
    )

    assert refs("buyer.email = 'x'", layer=layer) == ["people.email"]


def test_an_unknown_source_model_is_none() -> None:
    assert refs("status = 'a'", source="nowhere") is None


def test_no_clause_names_nothing() -> None:
    assert refs() == []


class _DialectBackend:
    """Just enough backend to run the contract's filter-dialect assertion."""

    def filter_refs(self, clauses: list[str]) -> list[str] | None:
        return slayer_dialect.filter_refs(clauses, "orders", models=LAYER)


def test_the_backend_contract_on_a_foreign_dialect() -> None:
    """The contract's own body, run against this reader: list or None, never a
    raise, for MetricFlow's dialect, a JSON filter and plain prose."""

    contract = SemanticBackendContract()
    contract.make_backend = _DialectBackend  # type: ignore[method-assign]
    contract.test_reading_a_foreign_filter_dialect_answers_or_declines()


# A saved measure in a filter reads the columns of its formula, not a column of
# its own name (#698): `revenue > 100` renders `HAVING SUM(orders.amount) > 100`.
MEASURED = models_by_name(
    model(
        "orders",
        ["order_id", "customer_id", "amount", "contact_email"],
        measures=[
            {"name": "revenue", "formula": "amount:sum"},
            {"name": "contacts", "formula": "contact_email:count_distinct"},
            {"name": "order_count", "formula": "*:count"},
            # A measure over another measure, and one over a joined model's.
            {"name": "per_order", "formula": "revenue / order_count"},
            {"name": "reach", "formula": "customers.email:count_distinct"},
        ],
        joins=[join("customers", "customer_id", "customer_id")],
    ),
    model("customers", ["customer_id", "email"]),
)


@pytest.mark.parametrize(
    ("clause", "expected"),
    [
        ("revenue > 100", ["orders.amount"]),
        ("contacts > 5", ["orders.contact_email"]),
        ("order_count > 2", []),
        ("per_order > 1", ["orders.amount"]),
        ("reach > 1", ["customers.email"]),
        ("orders.revenue > 100", ["orders.amount"]),
    ],
)
def test_a_saved_measure_reads_the_columns_of_its_formula(
    clause: str, expected: list[str]
) -> None:
    assert refs(clause, layer=MEASURED) == expected


def test_a_measure_cycle_is_none() -> None:
    """A formula that reaches itself has no columns to report, so the filter is
    one the gate cannot screen."""

    layer = models_by_name(
        model(
            "orders",
            ["order_id"],
            measures=[
                {"name": "a", "formula": "b + 1"},
                {"name": "b", "formula": "a + 1"},
            ],
        )
    )

    assert refs("a > 1", layer=layer) is None
