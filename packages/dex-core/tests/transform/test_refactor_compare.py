"""Tests for schema-aware refactor comparison of two compiled SELECTs (#856)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from exmergo_dex_core.transform import compare_refactors
from exmergo_dex_core.transform.multiset_compare import (
    KeyedCounts,
    MultisetDiff,
    SchemaColumn,
    SchemaDiff,
    TypeMismatch,
    compare_schemas,
)


@pytest.fixture
def conn():
    connection = duckdb.connect(":memory:")
    yield connection
    connection.close()


def _empty_schema() -> SchemaDiff:
    return SchemaDiff((), (), ())


def test_duplicate_removal_is_not_equivalent(conn):
    baseline = """
    select id, amount from (
        select 1 as id, 10 as amount
        union all
        select 1 as id, 10 as amount
    ) s
    """
    proposed = """
    select distinct id, amount from (
        select 1 as id, 10 as amount
        union all
        select 1 as id, 10 as amount
    ) s
    """
    result = compare_refactors(baseline, proposed, conn)
    assert result.status == "different"
    assert result.reason == "row multisets differ"
    assert result.schema == _empty_schema()
    assert result.multiset == MultisetDiff(
        left_rows=2, right_rows=1, only_in_left=1, only_in_right=0
    )
    assert result.keyed is None
    assert result.baseline_id is None
    assert result.proposed_id is None
    assert result.input_scope is None


def test_changed_measure_keeps_schema_and_row_count(conn):
    baseline = "select 1 as id, 10 as amount"
    proposed = "select 1 as id, 11 as amount"
    result = compare_refactors(
        baseline,
        proposed,
        conn,
        baseline_id="orders.baseline",
        proposed_id="orders.proposed",
        input_scope="dev.orders",
    )
    assert result.status == "different"
    assert result.reason == "row multisets differ"
    assert result.schema == _empty_schema()
    assert result.multiset == MultisetDiff(
        left_rows=1, right_rows=1, only_in_left=1, only_in_right=1
    )
    assert result.keyed is None
    assert result.baseline_id == "orders.baseline"
    assert result.proposed_id == "orders.proposed"
    assert result.input_scope == "dev.orders"


def test_reordering_columns_and_rows_is_equivalent(conn):
    baseline = "select 1 as id, 10 as amount union all select 2 as id, 20 as amount"
    proposed = "select 20 as amount, 2 as id union all select 10 as amount, 1 as id"
    result = compare_refactors(baseline, proposed, conn)
    assert result.status == "equivalent"
    assert result.reason == "equivalent on the compared inputs only"
    assert result.schema == _empty_schema()
    assert result.multiset == MultisetDiff(
        left_rows=2, right_rows=2, only_in_left=0, only_in_right=0
    )
    assert result.keyed is None


def test_renamed_output_matches_only_with_a_mapping(conn):
    baseline = "select 1 as team_id, 10 as event_count"
    proposed = "select 1 as team, 10 as events"
    mapping = {"team_id": "team", "event_count": "events"}
    mapped = compare_refactors(baseline, proposed, conn, column_mapping=mapping)
    assert compare_schemas(baseline, proposed, conn, column_mapping=mapping) == (
        _empty_schema()
    )
    assert mapped.status == "equivalent"
    assert mapped.schema == _empty_schema()
    assert mapped.multiset == MultisetDiff(
        left_rows=1, right_rows=1, only_in_left=0, only_in_right=0
    )

    unmapped = compare_refactors(baseline, proposed, conn)
    assert unmapped.status == "different"
    assert unmapped.reason == "output schemas differ"
    assert unmapped.multiset is None
    assert unmapped.schema == SchemaDiff(
        only_in_left=(
            SchemaColumn("team_id", "INTEGER"),
            SchemaColumn("event_count", "INTEGER"),
        ),
        only_in_right=(
            SchemaColumn("team", "INTEGER"),
            SchemaColumn("events", "INTEGER"),
        ),
        type_mismatches=(),
    )


def test_non_executable_baseline_is_not_checked(conn):
    result = compare_refactors(
        "select * from missing_table",
        "select 1 as id",
        conn,
        baseline_id="baseline-build",
        proposed_id="proposed-build",
        input_scope="dev",
    )
    assert result.status == "not_checked"
    assert result.multiset is None
    assert result.keyed is None
    assert result.schema is None
    assert "missing_table" in result.reason
    assert result.baseline_id == "baseline-build"
    assert result.proposed_id == "proposed-build"
    assert result.input_scope == "dev"
    seen = datetime.fromisoformat(result.compared_at)
    assert seen.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - seen) < timedelta(minutes=5)


def test_equivalent_refactor_is_limited_to_compared_inputs(conn):
    baseline = """
    select t.team_id, coalesce(c.event_count, 0) as event_count
    from (select 10 as team_id union all select 11) t
    left join (select 10 as team_id, 4 as event_count) c
        on c.team_id = t.team_id
    """
    proposed = """
    with teams as (
        select 11 as team_id
        union all
        select 10 as team_id
    ),
    counts as (
        select 10 as team_id, 4 as event_count
    )
    select coalesce(counts.event_count, 0) as event_count, teams.team_id
    from teams
    left join counts on teams.team_id = counts.team_id
    """
    result = compare_refactors(
        baseline,
        proposed,
        conn,
        baseline_id="team_events",
        proposed_id="team_events_cte",
        input_scope="in-memory fixture",
        key_columns=["team_id"],
    )
    assert result.status == "equivalent"
    assert result.reason == "equivalent on the compared inputs only"
    assert result.baseline_id == "team_events"
    assert result.proposed_id == "team_events_cte"
    assert result.input_scope == "in-memory fixture"
    assert result.schema == _empty_schema()
    assert result.multiset == MultisetDiff(
        left_rows=2, right_rows=2, only_in_left=0, only_in_right=0
    )
    assert result.keyed == KeyedCounts(only_in_left=0, only_in_right=0, changed=0)
    seen = datetime.fromisoformat(result.compared_at)
    assert seen.utcoffset() == timedelta(0)


def test_numeric_type_change_is_not_a_value_comparison(conn):
    baseline = "select cast(1 as integer) as amount"
    proposed = "select cast(1 as bigint) as amount"
    assert compare_schemas(baseline, proposed, conn) == SchemaDiff(
        only_in_left=(),
        only_in_right=(),
        type_mismatches=(
            TypeMismatch(
                SchemaColumn("amount", "INTEGER"),
                SchemaColumn("amount", "BIGINT"),
            ),
        ),
    )
    result = compare_refactors(baseline, proposed, conn)
    assert result.status == "different"
    assert result.reason == "output schemas differ"
    assert result.multiset is None
    assert result.keyed is None


def test_invalid_declared_key_is_not_checked(conn):
    sql = "select 1 as id, 10 as amount"
    result = compare_refactors(sql, sql, conn, key_columns=["missing"])
    assert result.status == "not_checked"
    assert result.schema == _empty_schema()
    assert result.multiset is None
    assert result.keyed is None
    assert result.reason == "declared key is not an output column: 'missing'"


def test_declared_key_counts_missing_added_and_changed(conn):
    baseline = """
    select 1 as id, 10 as amount
    union all select 1, 11
    union all select 2, 5
    """
    proposed = """
    select 1 as id, 10 as amount
    union all select 3, 5
    """
    result = compare_refactors(baseline, proposed, conn, key_columns=["id"])
    assert result.status == "different"
    assert result.reason == "row multisets differ"
    assert result.schema == _empty_schema()
    assert result.multiset == MultisetDiff(
        left_rows=3, right_rows=2, only_in_left=2, only_in_right=1
    )
    assert result.keyed == KeyedCounts(only_in_left=1, only_in_right=1, changed=1)


def test_null_key_matches_null_key(conn):
    baseline = "select cast(null as integer) as id, 10 as amount"
    same = "select cast(null as integer) as id, 10 as amount"
    changed = "select cast(null as integer) as id, 11 as amount"
    matched = compare_refactors(baseline, same, conn, key_columns=["id"])
    assert matched.status == "equivalent"
    assert matched.keyed == KeyedCounts(0, 0, 0)
    differ = compare_refactors(baseline, changed, conn, key_columns=["id"])
    assert differ.status == "different"
    assert differ.keyed == KeyedCounts(only_in_left=0, only_in_right=0, changed=1)


def test_key_multiplicity_without_payload_is_a_change(conn):
    baseline = "select 1 as id union all select 1 as id"
    proposed = "select 1 as id"
    result = compare_refactors(baseline, proposed, conn, key_columns=["id"])
    assert result.status == "different"
    assert result.multiset == MultisetDiff(
        left_rows=2, right_rows=1, only_in_left=1, only_in_right=0
    )
    assert result.keyed == KeyedCounts(only_in_left=0, only_in_right=0, changed=1)


def test_duplicate_output_names_are_not_checked(conn):
    sql = "select 1 as id, 2 as id"
    result = compare_refactors(sql, sql, conn)
    assert result.status == "not_checked"
    assert result.schema is None
    assert result.multiset is None
    assert result.reason == "duplicate output column name 'id' on baseline"
    with pytest.raises(ValueError, match="duplicate output column name"):
        compare_schemas(sql, sql, conn)


def test_empty_sql_is_refused(conn):
    with pytest.raises(ValueError, match="required"):
        compare_refactors("   ", "select 1 as id", conn)
    with pytest.raises(ValueError, match="required"):
        compare_schemas("select 1 as id", "  ;  ", conn)


def test_mapping_must_be_one_to_one(conn):
    with pytest.raises(ValueError, match="one-to-one"):
        compare_refactors(
            "select 1 as a, 2 as b",
            "select 1 as x, 2 as y",
            conn,
            column_mapping={"a": "x", "b": "x"},
        )
    with pytest.raises(ValueError, match="resolves two baseline"):
        compare_refactors(
            "select 1 as a, 2 as b",
            "select 1 as a, 2 as b",
            conn,
            column_mapping={"b": "a"},
        )


def test_declared_key_follows_the_column_mapping(conn):
    baseline = "select 1 as team_id, 10 as amount"
    proposed = "select 1 as team, 11 as amount"
    result = compare_refactors(
        baseline,
        proposed,
        conn,
        column_mapping={"team_id": "team"},
        key_columns=["team_id"],
    )
    assert result.status == "different"
    assert result.reason == "row multisets differ"
    assert result.keyed == KeyedCounts(only_in_left=0, only_in_right=0, changed=1)
    assert result.multiset == MultisetDiff(
        left_rows=1, right_rows=1, only_in_left=1, only_in_right=1
    )


def test_mapping_key_must_name_a_baseline_column(conn):
    sql = "select 1 as id, 10 as amount"
    result = compare_refactors(sql, sql, conn, column_mapping={"typo": "id"})
    assert result.status == "not_checked"
    assert result.multiset is None
    assert result.schema is None
    assert result.reason == (
        "column mapping names a baseline column that is not an output: 'typo'"
    )
    with pytest.raises(ValueError, match="not an output"):
        compare_schemas(sql, sql, conn, column_mapping={"typo": "id"})
