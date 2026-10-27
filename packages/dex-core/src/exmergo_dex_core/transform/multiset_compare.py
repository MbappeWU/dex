"""Aggregate exact multiset comparison of two compiled SELECTs (#856).

A refactor can preserve row counts and still change the result: extracting a
join into an intermediate can turn a left-joined measure into a zero. This
module compares two supplied compiled SELECT statements as multisets of whole
rows and returns only aggregate counts, so no source value leaves the warehouse.

DuckDB only for this first contribution. The row fingerprint matches
``mutation._ROW_FINGERPRINT["duckdb"]`` so mutation equivalence and this
comparator agree on what "the same row" means. Graph compilation and plan
integration stay with a maintainer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class _DuckConn(Protocol):
    def execute(self, query: str) -> Any: ...


#: Whole-row fingerprint used for DuckDB multiset equality (same as mutation).
_DUCKDB_ROW_FINGERPRINT = "hash({alias})"

_COMPARE_SQL = """\
with dex_left as (
    select {left_row} as dex_row from ({left_sql}) as dex_l
),
dex_right as (
    select {right_row} as dex_row from ({right_sql}) as dex_r
),
dex_left_rows as (
    select dex_row, count(*) as dex_n from dex_left group by dex_row
),
dex_right_rows as (
    select dex_row, count(*) as dex_n from dex_right group by dex_row
)
select
    coalesce(sum(coalesce(l.dex_n, 0)), 0) as left_rows,
    coalesce(sum(coalesce(r.dex_n, 0)), 0) as right_rows,
    coalesce(sum(case when coalesce(l.dex_n, 0) > coalesce(r.dex_n, 0)
        then coalesce(l.dex_n, 0) - coalesce(r.dex_n, 0) else 0 end), 0)
        as only_in_left,
    coalesce(sum(case when coalesce(r.dex_n, 0) > coalesce(l.dex_n, 0)
        then coalesce(r.dex_n, 0) - coalesce(l.dex_n, 0) else 0 end), 0)
        as only_in_right
from dex_left_rows as l
full outer join dex_right_rows as r on l.dex_row = r.dex_row
"""


@dataclass(frozen=True)
class MultisetDiff:
    """Aggregate multiset difference between two SELECT results.

    ``only_in_left`` / ``only_in_right`` count excess multiplicity, so a row
    that appears twice on the left and once on the right contributes 1 to
    ``only_in_left``. Equal multisets have both excesses at 0.
    """

    left_rows: int
    right_rows: int
    only_in_left: int
    only_in_right: int

    @property
    def equivalent(self) -> bool:
        return self.only_in_left == 0 and self.only_in_right == 0


def compare_selects(
    left_sql: str,
    right_sql: str,
    connection: _DuckConn,
) -> MultisetDiff:
    """Compare two compiled SELECTs as DuckDB multisets; return aggregate counts.

    ``left_sql`` and ``right_sql`` must be single SELECT statements (or WITH ...
    SELECT) that DuckDB can nest as subqueries. The connection is whatever the
    existing DuckDB test and adapter paths already open; this function does not
    open warehouses or touch the filesystem.
    """

    left = left_sql.strip().rstrip(";")
    right = right_sql.strip().rstrip(";")
    if not left or not right:
        raise ValueError("both SELECT statements are required")
    sql = _COMPARE_SQL.format(
        left_row=_DUCKDB_ROW_FINGERPRINT.format(alias="dex_l"),
        right_row=_DUCKDB_ROW_FINGERPRINT.format(alias="dex_r"),
        left_sql=left,
        right_sql=right,
    )
    result = connection.execute(sql)
    fetch = getattr(result, "fetchone", None)
    row = fetch() if callable(fetch) else next(iter(result))
    if row is None or len(row) != 4:
        raise RuntimeError("multiset comparison returned no counts")
    left_rows, right_rows, only_in_left, only_in_right = (int(v) for v in row)
    if (
        left_rows - right_rows != only_in_left - only_in_right
        or left_rows < 0
        or right_rows < 0
        or only_in_left < 0
        or only_in_right < 0
    ):
        raise RuntimeError("multiset comparison returned inconsistent counts")
    return MultisetDiff(
        left_rows=left_rows,
        right_rows=right_rows,
        only_in_left=only_in_left,
        only_in_right=only_in_right,
    )
