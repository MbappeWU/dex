"""The statement shape values-free mode accepts (#591), across dialects.

Pure static analysis over parsed SQL, like the firewall's own matrix: no database
is touched, which is what lets a refused shape cost nothing. The end-to-end
behavior against a real warehouse is the shipped conformance suite's job; this is
the rule itself, spelled in each dialect the connectors parse.
"""

from __future__ import annotations

import pytest
import sqlglot

from exmergo_dex_core.guards.values_free_shape import (
    VALUES_FREE_AGGREGATES,
    assert_values_free_shape,
)
from exmergo_dex_core.values_free import ValuesFreeCode, ValuesFreeError

Code = ValuesFreeCode


def check(sql: str, dialect: str = "duckdb") -> None:
    assert_values_free_shape(sqlglot.parse_one(sql, dialect=dialect))


def refusal(sql: str, dialect: str = "duckdb") -> ValuesFreeError:
    with pytest.raises(ValuesFreeError) as refused:
        check(sql, dialect)
    return refused.value


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("duckdb", "select count(*) from t"),
        ("duckdb", "select count(distinct id), avg(amount), sum(x) / count(*) from t"),
        ("duckdb", "select count(*) filter (where status = 'paid') from t"),
        ("duckdb", "select round(stddev(x), 2), coalesce(sum(x), 0) from t"),
        ("duckdb", "select cast(sum(x) as double), try_cast(avg(x) as int) from t"),
        ("duckdb", "select count(*) > 0, bool_and(f), bool_or(f) from t"),
        ("duckdb", "select case when count(*) > 0 then 1 else 0 end from t"),
        ("duckdb", "select (select count(*) from u) - count(*) from t"),
        ("duckdb", "select count(*) from t group by ()"),
        ("duckdb", "select count(*) from t where email = 'a@b.c' having count(*) > 1"),
        (
            "duckdb",
            "with x as (select * from t where y > 1) select count(*) from x",
        ),
        ("duckdb", "select count(*) from t join u on t.id = u.t_id"),
        ("postgres", "select count(distinct x), variance(y), corr(a, b) from t"),
        ("bigquery", "select countif(x > 1), approx_count_distinct(y) from t"),
        ("bigquery", "select safe_divide(sum(a), count(*)) from t"),
        ("snowflake", "select count_if(x > 1), stddev_pop(y) from t"),
        ("clickhouse", "select uniqExact(x), sumIf(x, y > 1), countIf(z = 1) from t"),
        ("databricks", "select count(*), approx_count_distinct(x) from t"),
        ("redshift", "select count(*), avg(x) from t"),
    ],
)
def test_an_aggregate_only_statement_is_accepted(dialect: str, sql: str):
    check(sql, dialect)


@pytest.mark.parametrize(
    ("dialect", "sql", "code", "named"),
    [
        ("duckdb", "select x from t", Code.BARE_COLUMN, "x"),
        ("duckdb", "select * from t", Code.BARE_COLUMN, "*"),
        ("duckdb", "select count(*) + x from t", Code.BARE_COLUMN, "x"),
        ("duckdb", "select count(*) from t group by s", Code.GROUP_BY_COLUMN, "s"),
        ("duckdb", "select count(*) from t group by all", Code.GROUP_BY_COLUMN, "ALL"),
        ("duckdb", "select count(*) from t group by 1", Code.GROUP_BY_COLUMN, "1"),
        (
            "duckdb",
            "select count(*) from t group by rollup (s)",
            Code.GROUP_BY_COLUMN,
            "s",
        ),
        ("duckdb", "select distinct count(*) from t", Code.DISTINCT_PROJECTION, None),
        ("duckdb", "select count(*) from t limit 1", Code.LIMIT_OFFSET, None),
        ("duckdb", "select count(*) from t offset 1", Code.LIMIT_OFFSET, None),
        ("duckdb", "select count(*) from t order by 1", Code.ORDER_BY, None),
        ("duckdb", "select count(*) over () from t", Code.WINDOW_FUNCTION, "count"),
        ("snowflake", "select count(*) from t qualify 1 = 1", Code.QUALIFY, None),
        ("duckdb", "select max(x) from t", Code.AGGREGATE_NOT_ALLOWED, "max"),
        ("duckdb", "select min(x) from t", Code.AGGREGATE_NOT_ALLOWED, "min"),
        ("duckdb", "select any_value(x) from t", Code.AGGREGATE_NOT_ALLOWED, None),
        ("duckdb", "select arg_max(a, b) from t", Code.AGGREGATE_NOT_ALLOWED, None),
        ("duckdb", "select mode(x) from t", Code.AGGREGATE_NOT_ALLOWED, "mode"),
        ("duckdb", "select first(x) from t", Code.AGGREGATE_NOT_ALLOWED, "first"),
        ("duckdb", "select list(x) from t", Code.AGGREGATE_NOT_ALLOWED, None),
        ("duckdb", "select median(x) from t", Code.AGGREGATE_NOT_ALLOWED, "median"),
        (
            "duckdb",
            "select approx_quantile(x, 0.5) from t",
            Code.AGGREGATE_NOT_ALLOWED,
            None,
        ),
        (
            "postgres",
            "select percentile_cont(0.5) within group (order by x) from t",
            Code.AGGREGATE_NOT_ALLOWED,
            None,
        ),
        (
            "postgres",
            "select string_agg(x, ',') from t",
            Code.AGGREGATE_NOT_ALLOWED,
            None,
        ),
        ("bigquery", "select array_agg(x) from t", Code.AGGREGATE_NOT_ALLOWED, None),
        (
            "bigquery",
            "select approx_top_k(x, 3) from t",
            Code.AGGREGATE_NOT_ALLOWED,
            None,
        ),
        (
            "snowflake",
            "select listagg(x, ',') from t",
            Code.AGGREGATE_NOT_ALLOWED,
            None,
        ),
        ("snowflake", "select min_by(a, b) from t", Code.AGGREGATE_NOT_ALLOWED, None),
        ("clickhouse", "select any(x) from t", Code.AGGREGATE_NOT_ALLOWED, None),
        ("clickhouse", "select argMax(a, b) from t", Code.AGGREGATE_NOT_ALLOWED, None),
        ("clickhouse", "select topK(3)(x) from t", Code.AGGREGATE_NOT_ALLOWED, "topk"),
        ("clickhouse", "select groupArray(x) from t", Code.AGGREGATE_NOT_ALLOWED, None),
        ("duckdb", "select 'x', count(*) from t", Code.LITERAL_PROJECTION, None),
        ("duckdb", "select true, count(*) from t", Code.LITERAL_PROJECTION, None),
        ("duckdb", "select upper(x) from t", Code.FUNCTION_NOT_ALLOWED, "upper"),
        (
            "duckdb",
            "select cast(count(*) as varchar) from t",
            Code.FUNCTION_NOT_ALLOWED,
            "cast",
        ),
        (
            "duckdb",
            "select (select x from u limit 1) from t",
            Code.UNAGGREGATED_SUBQUERY,
            None,
        ),
        (
            "duckdb",
            "select (select max(x) from u) + count(*) from t",
            Code.UNAGGREGATED_SUBQUERY,
            None,
        ),
        ("duckdb", "select 1 from t", Code.NO_AGGREGATE, None),
        (
            "duckdb",
            "select count(*) from t union all select count(*) from u",
            Code.SET_OPERATION,
            None,
        ),
        (
            "duckdb",
            "select case x when 1 then count(*) end from t",
            Code.BARE_COLUMN,
            "x",
        ),
    ],
)
def test_every_other_shape_is_refused_with_its_code(
    dialect: str, sql: str, code: ValuesFreeCode, named: str | None
):
    refused = refusal(sql, dialect)

    assert refused.code is code
    if named is not None:
        assert named in {refused.column, refused.function}


def test_a_refusal_names_the_rule_and_quotes_no_literal():
    """The agent wrote the literal, so it is not a table's value; the message still
    does not echo it, because a refusal is the one text a host forwards whole."""

    refused = refusal("select 'zq7x secret', count(*) from t")

    assert "zq7x" not in str(refused)
    assert "literal_projection" in str(refused)


def test_the_allowlist_is_the_firewalls_measuring_set_and_nothing_wider():
    """The allowlist is reviewed, not inferred: every name on it reduces rows to a
    statistic. A value-returning aggregate on it would be the whole guarantee gone,
    so the obvious ones are named here as never on it."""

    for name in ("min", "max", "any_value", "arg_max", "mode", "first", "median"):
        assert name not in VALUES_FREE_AGGREGATES
    for name in ("count", "sum", "avg", "stddev", "variance", "logical_and"):
        assert name in VALUES_FREE_AGGREGATES
