"""Aggregate exact comparison of two compiled SELECTs (#856).

``compare_selects`` compares two supplied statements as multisets of whole
rows and returns only aggregate counts, so no source value leaves the
warehouse. ``compare_refactors`` adds the schema check, caller-supplied
identities, and optional key counts around that same fingerprint.

DuckDB only. The row fingerprint matches ``mutation._ROW_FINGERPRINT["duckdb"]``
so mutation equivalence and this comparator agree on what "the same row"
means. Compiling a dbt graph into the two statements stays with the caller.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol


class _DuckConn(Protocol):
    def execute(self, query: str) -> Any: ...


#: Whole-row fingerprint used for DuckDB multiset equality (same as mutation).
_DUCKDB_ROW_FINGERPRINT = "hash({alias})"

_EQUIVALENT_ON_INPUTS = "equivalent on the compared inputs only"

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

_DESCRIBE_SQL = "select column_name, column_type from (describe {sql})"
_PROJECT_SQL = "select {select_list} from ({sql}) as dex_proj"

_KEYED_SQL = """\
with dex_left as (
    select {keys}, {fingerprint} as dex_fp from ({left_sql}) as dex_l
),
dex_right as (
    select {keys}, {fingerprint} as dex_fp from ({right_sql}) as dex_r
),
dex_left_keys as (
    select {keys}, hash(list_sort(list(dex_fp))) as dex_bag
    from dex_left group by {keys}
),
dex_right_keys as (
    select {keys}, hash(list_sort(list(dex_fp))) as dex_bag
    from dex_right group by {keys}
)
select
    coalesce(sum(case when r.dex_bag is null then 1 else 0 end), 0)
        as only_in_left,
    coalesce(sum(case when l.dex_bag is null then 1 else 0 end), 0)
        as only_in_right,
    coalesce(sum(case when l.dex_bag is not null and r.dex_bag is not null
        and l.dex_bag <> r.dex_bag then 1 else 0 end), 0) as changed
from dex_left_keys as l
full outer join dex_right_keys as r on {join_pairs}
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


@dataclass(frozen=True)
class SchemaColumn:
    """One output column, as DuckDB ``DESCRIBE`` reports it."""

    name: str
    type_name: str


@dataclass(frozen=True)
class TypeMismatch:
    """A mapped pair whose canonical DuckDB types differ.

    Types are the ``DESCRIBE`` strings. ``INTEGER`` and ``BIGINT`` mismatch.
    There is no numeric widening and no value tolerance.
    """

    left: SchemaColumn
    right: SchemaColumn


@dataclass(frozen=True)
class SchemaDiff:
    """Name and type differences after the optional left-to-right mapping.

    Columns are matched by mapped name, not by position. Tuples keep
    ``DESCRIBE`` order. A compatible diff has all three empty.
    """

    only_in_left: tuple[SchemaColumn, ...]
    only_in_right: tuple[SchemaColumn, ...]
    type_mismatches: tuple[TypeMismatch, ...]

    @property
    def compatible(self) -> bool:
        return not (self.only_in_left or self.only_in_right or self.type_mismatches)


@dataclass(frozen=True)
class KeyedCounts:
    """Key-level differences. Each count is a number of keys, not rows.

    ``changed`` is a key present on both sides whose bag of payloads differs,
    including a difference in how many times the same payload appears.
    """

    only_in_left: int
    only_in_right: int
    changed: int


@dataclass(frozen=True)
class RefactorCompareResult:
    """Verdict for two compiled SELECTs on one input scope.

    ``equivalent`` means the schema, the row multiset, and the declared key
    (when one was given) matched for these statements. ``reason`` is then
    ``equivalent on the compared inputs only``. That is not a claim about a
    build, and not a claim about any input that was not compared.

    ``different`` carries the schema diff and, when the schemas matched, the
    multiset and key counts. ``not_checked`` carries ``reason`` and no row
    verdict. A query DuckDB cannot run is ``not_checked``.
    """

    baseline_id: str | None
    proposed_id: str | None
    input_scope: str | None
    compared_at: str
    schema: SchemaDiff | None
    multiset: MultisetDiff | None
    status: Literal["equivalent", "different", "not_checked"]
    reason: str | None
    keyed: KeyedCounts | None = None

    def __post_init__(self) -> None:
        schema_ok = self.schema is not None and self.schema.compatible
        rows_ok = self.multiset is not None and self.multiset.equivalent
        keys_ok = self.keyed is None or (
            self.keyed.only_in_left == 0
            and self.keyed.only_in_right == 0
            and self.keyed.changed == 0
        )
        if self.status == "not_checked":
            if not self.reason or self.multiset is not None or self.keyed is not None:
                raise ValueError("not_checked requires a reason and no verdict")
            return
        if self.status == "equivalent":
            if not (schema_ok and rows_ok and keys_ok):
                raise ValueError("equivalent requires a match on schema, rows, and key")
            if self.reason != _EQUIVALENT_ON_INPUTS:
                raise ValueError("equivalent must say it holds for the compared inputs")
            return
        if self.status != "different":
            raise ValueError(f"unknown comparison status {self.status!r}")
        if self.schema is None or not self.reason:
            raise ValueError("different requires a schema diff and a reason")
        if self.schema.compatible:
            if self.multiset is None or (rows_ok and keys_ok):
                raise ValueError("different requires a row or key difference")
            return
        if self.multiset is not None or self.keyed is not None:
            raise ValueError("a schema difference does not carry a row verdict")


@dataclass(frozen=True)
class _MatchedColumn:
    left_name: str
    right_name: str
    type_name: str


@dataclass(frozen=True)
class _Aligned:
    schema: SchemaDiff
    matched: tuple[_MatchedColumn, ...]


class _NotCheckedError(Exception):
    """The statements ran far enough to know they cannot be verdicted."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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

    left, right = _require_sql(left_sql, right_sql)
    sql = _COMPARE_SQL.format(
        left_row=_DUCKDB_ROW_FINGERPRINT.format(alias="dex_l"),
        right_row=_DUCKDB_ROW_FINGERPRINT.format(alias="dex_r"),
        left_sql=left,
        right_sql=right,
    )
    row = _fetch_one(connection, sql)
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


def compare_schemas(
    left_sql: str,
    right_sql: str,
    connection: _DuckConn,
    *,
    column_mapping: Mapping[str, str] | None = None,
) -> SchemaDiff:
    """Compare output names and canonical DuckDB types.

    ``column_mapping`` maps a baseline output name to a proposed output name.
    Unmapped baseline columns match a proposed column of the same name.
    Column order is not a difference. An empty statement, a mapping that is
    not one-to-one, a mapping key that is not a baseline output, or a
    duplicate output name raises ``ValueError``. DuckDB errors propagate.
    """

    left, right = _require_sql(left_sql, right_sql)
    mapping = _column_mapping(column_mapping)
    try:
        return _align(left, right, connection, mapping).schema
    except _NotCheckedError as exc:
        raise ValueError(exc.reason) from exc


def compare_refactors(
    left_sql: str,
    right_sql: str,
    connection: _DuckConn,
    *,
    baseline_id: str | None = None,
    proposed_id: str | None = None,
    input_scope: str | None = None,
    column_mapping: Mapping[str, str] | None = None,
    key_columns: Sequence[str] | None = None,
) -> RefactorCompareResult:
    """Compare two compiled SELECTs on schema, multiset, and an optional key.

    ``key_columns`` are baseline output names. Each is mapped with
    ``column_mapping`` when the proposed side renamed it. Counts are keys.
    Omit the key, or pass an empty sequence, for a multiset comparison only.

    ``equivalent`` means these statements matched on the compared inputs.
    It is not evidence that a build passed. A statement DuckDB cannot run,
    a duplicate output name, a mapping key that is not a baseline output,
    or a declared key that is not an output column is ``not_checked`` with
    ``reason`` set. An empty statement raises ``ValueError``, as
    ``compare_selects`` does.
    """

    left, right = _require_sql(left_sql, right_sql)
    mapping = _column_mapping(column_mapping)
    keys = _key_columns(key_columns)
    compared_at = datetime.now(UTC).isoformat()

    def finish(
        *,
        status: Literal["equivalent", "different", "not_checked"],
        reason: str | None,
        schema: SchemaDiff | None,
        multiset: MultisetDiff | None,
        keyed: KeyedCounts | None,
    ) -> RefactorCompareResult:
        return RefactorCompareResult(
            baseline_id=baseline_id,
            proposed_id=proposed_id,
            input_scope=input_scope,
            compared_at=compared_at,
            schema=schema,
            multiset=multiset,
            status=status,
            reason=reason,
            keyed=keyed,
        )

    try:
        aligned = _align(left, right, connection, mapping)
    except _NotCheckedError as exc:
        return finish(
            status="not_checked",
            reason=exc.reason,
            schema=None,
            multiset=None,
            keyed=None,
        )
    except (ValueError, RuntimeError):
        raise
    except Exception as exc:
        return finish(
            status="not_checked",
            reason=_exc_reason(exc),
            schema=None,
            multiset=None,
            keyed=None,
        )

    schema = aligned.schema
    if not schema.compatible:
        return finish(
            status="different",
            reason="output schemas differ",
            schema=schema,
            multiset=None,
            keyed=None,
        )

    if keys is not None:
        problem = _key_problem(keys, [column.left_name for column in aligned.matched])
        if problem is not None:
            return finish(
                status="not_checked",
                reason=problem,
                schema=schema,
                multiset=None,
                keyed=None,
            )

    left_names = [column.left_name for column in aligned.matched]
    right_names = [column.right_name for column in aligned.matched]
    left_proj = _project(left, left_names, left_names)
    right_proj = _project(right, right_names, left_names)
    try:
        multiset = compare_selects(left_proj, right_proj, connection)
    except (ValueError, RuntimeError):
        raise
    except Exception as exc:
        return finish(
            status="not_checked",
            reason=_exc_reason(exc),
            schema=schema,
            multiset=None,
            keyed=None,
        )

    keyed: KeyedCounts | None = None
    if keys is not None:
        payload = [name for name in left_names if name not in set(keys)]
        try:
            keyed = _keyed_counts(left_proj, right_proj, connection, keys, payload)
        except (ValueError, RuntimeError):
            raise
        except Exception as exc:
            return finish(
                status="not_checked",
                reason=_exc_reason(exc),
                schema=schema,
                multiset=None,
                keyed=None,
            )

    rows_differ = not multiset.equivalent
    keys_differ = keyed is not None and (
        keyed.only_in_left != 0 or keyed.only_in_right != 0 or keyed.changed != 0
    )
    if rows_differ or keys_differ:
        reason = "row multisets differ" if rows_differ else "keyed rows differ"
        return finish(
            status="different",
            reason=reason,
            schema=schema,
            multiset=multiset,
            keyed=keyed,
        )
    return finish(
        status="equivalent",
        reason=_EQUIVALENT_ON_INPUTS,
        schema=schema,
        multiset=multiset,
        keyed=keyed,
    )


def _require_sql(left_sql: str, right_sql: str) -> tuple[str, str]:
    left = left_sql.strip().rstrip(";")
    right = right_sql.strip().rstrip(";")
    if not left or not right:
        raise ValueError("both SELECT statements are required")
    return left, right


def _column_mapping(column_mapping: Mapping[str, str] | None) -> dict[str, str]:
    if not column_mapping:
        return {}
    if len(set(column_mapping.values())) != len(column_mapping):
        raise ValueError("column mapping is not one-to-one")
    normalized: dict[str, str] = {}
    for left_name, right_name in column_mapping.items():
        if (
            not isinstance(left_name, str)
            or not isinstance(right_name, str)
            or not left_name
            or not right_name
        ):
            raise ValueError("column mapping names must be non-empty strings")
        normalized[left_name] = right_name
    return normalized


def _key_columns(key_columns: Sequence[str] | None) -> tuple[str, ...] | None:
    if key_columns is None:
        return None
    if isinstance(key_columns, str):
        raise ValueError("key_columns must be a sequence of column names")
    keys = tuple(key_columns)
    if not keys:
        return None
    for key in keys:
        if not isinstance(key, str) or not key:
            raise ValueError("key_columns must be a sequence of column names")
    return keys


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _fetch_one(connection: _DuckConn, sql: str) -> Any:
    result = connection.execute(sql)
    fetch = getattr(result, "fetchone", None)
    if callable(fetch):
        return fetch()
    return next(iter(result))


def _fetch_all(connection: _DuckConn, sql: str) -> list[Any]:
    result = connection.execute(sql)
    fetch = getattr(result, "fetchall", None)
    if callable(fetch):
        return list(fetch())
    return list(result)


def _exc_reason(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    if not text or not text[0].strip():
        return exc.__class__.__name__
    return text[0].strip()


def _describe(connection: _DuckConn, sql: str) -> tuple[SchemaColumn, ...]:
    query = _DESCRIBE_SQL.format(sql=sql)
    columns: list[SchemaColumn] = []
    for row in _fetch_all(connection, query):
        if row is None or len(row) != 2:
            raise RuntimeError("schema description returned no columns")
        name, type_name = row
        if not isinstance(name, str) or not isinstance(type_name, str):
            raise RuntimeError("schema description returned a non-text column")
        columns.append(SchemaColumn(name, type_name))
    return tuple(columns)


def _reject_duplicate_names(columns: Sequence[SchemaColumn], side: str) -> None:
    seen: set[str] = set()
    for column in columns:
        if column.name in seen:
            raise _NotCheckedError(
                f"duplicate output column name {column.name!r} on {side}"
            )
        seen.add(column.name)


def _align(
    left_sql: str,
    right_sql: str,
    connection: _DuckConn,
    mapping: Mapping[str, str],
) -> _Aligned:
    left_cols = _describe(connection, left_sql)
    right_cols = _describe(connection, right_sql)
    _reject_duplicate_names(left_cols, "baseline")
    _reject_duplicate_names(right_cols, "proposed")
    if not left_cols or not right_cols:
        raise _NotCheckedError("query returns no columns")
    left_names = {column.name for column in left_cols}
    unknown = [name for name in mapping if name not in left_names]
    if unknown:
        shown = ", ".join(repr(name) for name in unknown)
        raise _NotCheckedError(
            f"column mapping names a baseline column that is not an output: {shown}"
        )

    targets = [mapping.get(column.name, column.name) for column in left_cols]
    if len(targets) != len(set(targets)):
        raise ValueError(
            "column mapping resolves two baseline columns to one proposed column"
        )

    right_by_name = {column.name: column for column in right_cols}
    unmatched_right = {column.name for column in right_cols}
    only_left: list[SchemaColumn] = []
    mismatches: list[TypeMismatch] = []
    matched: list[_MatchedColumn] = []
    for column, target in zip(left_cols, targets, strict=True):
        right = right_by_name.get(target)
        if right is None:
            only_left.append(column)
            continue
        unmatched_right.discard(target)
        if column.type_name != right.type_name:
            mismatches.append(TypeMismatch(left=column, right=right))
            continue
        matched.append(_MatchedColumn(column.name, right.name, column.type_name))
    only_right = tuple(
        column for column in right_cols if column.name in unmatched_right
    )
    return _Aligned(
        schema=SchemaDiff(
            only_in_left=tuple(only_left),
            only_in_right=only_right,
            type_mismatches=tuple(mismatches),
        ),
        matched=tuple(matched),
    )


def _project(sql: str, source_names: Sequence[str], output_names: Sequence[str]) -> str:
    """Select ``source_names`` under ``output_names``, in that order.

    ``hash(row)`` follows column position and ignores names. Both sides are
    projected into one order and one set of names before the fingerprint, so
    a rename or a reordering is not reported as a changed row.
    """

    pairs = zip(source_names, output_names, strict=True)
    select_list = ", ".join(
        f"{_quote(source)} AS {_quote(output)}" for source, output in pairs
    )
    return _PROJECT_SQL.format(select_list=select_list, sql=sql)


def _key_problem(keys: Sequence[str], names: Sequence[str]) -> str | None:
    repeated = tuple(dict.fromkeys(key for key in keys if keys.count(key) > 1))
    if repeated:
        shown = ", ".join(repr(key) for key in repeated)
        return f"declared key repeats a column: {shown}"
    present = set(names)
    missing = tuple(key for key in keys if key not in present)
    if missing:
        shown = ", ".join(repr(key) for key in missing)
        return f"declared key is not an output column: {shown}"
    return None


def _payload_fingerprint(payload: Sequence[str]) -> str:
    # DuckDB hash(list_sort(list(0))) is 0 at every list length, so a constant
    # 0 would hide a change in how many rows a key has. 1 does not.
    if not payload:
        return "1"
    fields = ", ".join(f"{_quote(name)} := {_quote(name)}" for name in payload)
    return f"hash(struct_pack({fields}))"


def _keyed_counts(
    left_sql: str,
    right_sql: str,
    connection: _DuckConn,
    keys: Sequence[str],
    payload: Sequence[str],
) -> KeyedCounts:
    key_list = ", ".join(_quote(name) for name in keys)
    join_pairs = " and ".join(
        f"l.{_quote(name)} is not distinct from r.{_quote(name)}" for name in keys
    )
    sql = _KEYED_SQL.format(
        keys=key_list,
        fingerprint=_payload_fingerprint(payload),
        left_sql=left_sql,
        right_sql=right_sql,
        join_pairs=join_pairs,
    )
    row = _fetch_one(connection, sql)
    if row is None or len(row) != 3:
        raise RuntimeError("keyed comparison returned no counts")
    only_in_left, only_in_right, changed = (int(value) for value in row)
    if only_in_left < 0 or only_in_right < 0 or changed < 0:
        raise RuntimeError("keyed comparison returned inconsistent counts")
    return KeyedCounts(
        only_in_left=only_in_left,
        only_in_right=only_in_right,
        changed=changed,
    )
