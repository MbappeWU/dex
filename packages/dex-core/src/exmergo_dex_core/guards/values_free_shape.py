"""The statement shape `explore query` accepts in values-free mode (#591).

The query firewall decides which values a statement may carry out. In values-free
mode the answer is none, so the question becomes whether the statement can carry
any value at all, and the rule that settles it is about shape rather than about
columns: every output column has to be an aggregate over the rows the statement
reads, and the statement has to return exactly one row of them. A statement of
that shape returns numbers computed from rows, never a row's own value.

Concretely, the root must be one plain SELECT with no GROUP BY keyed by a column
(group keys come back as cells), no DISTINCT, no LIMIT, OFFSET or FETCH, no ORDER
BY, no QUALIFY, and no window function anywhere in the select list. Each output
column must contain at least one allowlisted aggregate, and outside its aggregates
may contain only numeric literals, arithmetic, comparisons and boolean logic, a
CASE whose branches obey the same rule, a cast to a numeric type, and a short list
of numeric scalar functions. A scalar subquery in the select list must itself pass
the whole rule. FROM, WHERE and HAVING are unrestricted, because values flow into
them and not out of them; that is also where the mode's residual risk lives, which
the module docstring of :mod:`..values_free` names.

**An allowlist, not a denylist.** :data:`VALUES_FREE_AGGREGATES` is the firewall's
own list of measuring aggregates (every one of which reduces a column to a
statistic) plus the boolean aggregates sqlglot canonicalizes under other names. An
aggregate not on it is refused, whether it returns a value from a row (MIN, MAX,
ANY_VALUE, ARG_MAX, MODE, FIRST, a median or quantile), a collection of them
(ARRAY_AGG, STRING_AGG, LIST, APPROX_TOP_K), or is simply one nobody has reviewed
yet. A function dex does not recognize is refused the same way.

The rule reads only the parsed statement, so it runs before any connection is
opened or any profile is scanned, and a refused statement costs nothing.
"""

from __future__ import annotations

from sqlglot import expressions as exp

from ..values_free import ValuesFreeCode, ValuesFreeError
from .query_firewall import _MEASURING, _func_name, _measures

# The firewall's measuring aggregates, plus BOOL_AND/BOOL_OR under the names
# sqlglot gives them (LogicalAnd/LogicalOr): both reduce a column to one boolean,
# which is a statistic about the rows rather than a value from one of them.
VALUES_FREE_AGGREGATES = _MEASURING | frozenset({"logical_and", "logical_or"})

# Scalar functions that compute a number from numbers. Allowed only outside an
# aggregate, over operands that obey the rule themselves, so none of them can
# reach a column.
_NUMERIC_SCALARS = frozenset(
    {
        "abs",
        "ceil",
        "ceiling",
        "coalesce",
        "exp",
        "floor",
        "greatest",
        "if",
        "least",
        "ln",
        "log",
        "nullif",
        "pow",
        "power",
        "round",
        "safe_divide",
        "sign",
        "sqrt",
    }
)

# Aggregates that return a value from a row or a collection of them, named so a
# refusal says "aggregate" rather than "function" for a name sqlglot leaves
# anonymous in some dialect. Not the allowlist: anything off the allowlist is
# refused whether or not it is named here.
_VALUE_AGGREGATE_NAMES = frozenset(
    {
        "any",
        "any_value",
        "approx_percentile",
        "approx_quantile",
        "approx_top_k",
        "arbitrary",
        "arg_max",
        "arg_min",
        "argmax",
        "argmin",
        "array_agg",
        "first",
        "first_value",
        "group_concat",
        "grouparray",
        "groupuniqarray",
        "histogram",
        "last",
        "last_value",
        "list",
        "listagg",
        "max",
        "max_by",
        "median",
        "min",
        "min_by",
        "mode",
        "percentile_cont",
        "percentile_disc",
        "quantile",
        "quantile_cont",
        "quantile_disc",
        "string_agg",
        "topk",
    }
)

_ARITHMETIC = (
    exp.Add,
    exp.Sub,
    exp.Mul,
    exp.Div,
    exp.Mod,
    exp.IntDiv,
    exp.Neg,
    exp.Paren,
)
_LOGIC = (
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.And,
    exp.Or,
    exp.Not,
    exp.Is,
)


def assert_values_free_shape(root: exp.Expression) -> None:
    """Refuse ``root`` unless it returns one row of aggregates and nothing else.

    ``root`` is the parsed statement :func:`~.query_firewall.assert_query_shape`
    returned, so it is already proven to be one read-only query. Raises a
    :class:`~..values_free.ValuesFreeError` naming the rule it broke and, where
    there is one, the column or function that broke it.
    """

    if not isinstance(root, exp.Select):
        raise ValuesFreeError(ValuesFreeCode.SET_OPERATION)
    _check_select(root)


def _check_select(select: exp.Select) -> None:
    group = select.args.get("group")
    if group is not None:
        _check_group(group)
    if select.args.get("distinct") is not None:
        raise ValuesFreeError(ValuesFreeCode.DISTINCT_PROJECTION)
    if any(select.args.get(key) is not None for key in ("limit", "offset", "fetch")):
        raise ValuesFreeError(ValuesFreeCode.LIMIT_OFFSET)
    if select.args.get("order") is not None:
        raise ValuesFreeError(ValuesFreeCode.ORDER_BY)
    if select.args.get("qualify") is not None:
        raise ValuesFreeError(ValuesFreeCode.QUALIFY)
    for projection in select.expressions:
        if not _projection(projection.unalias()):
            raise ValuesFreeError(
                ValuesFreeCode.NO_AGGREGATE, column=projection.alias or None
            )


def _check_group(group: exp.Group) -> None:
    """Only a grouping that yields no key may stand: ``GROUP BY ()`` or a string
    constant. A number is a position (``GROUP BY 1`` names the first output
    column), and ``GROUP BY ALL``, ROLLUP, CUBE and GROUPING SETS all key on
    columns."""

    if group.args.get("all"):
        raise ValuesFreeError(ValuesFreeCode.GROUP_BY_COLUMN, column="ALL")
    for key in group.expressions:
        if isinstance(key, exp.Tuple) and not key.expressions:
            continue
        if isinstance(key, exp.Literal) and key.is_string:
            continue
        column = next(iter(key.find_all(exp.Column)), None)
        raise ValuesFreeError(
            ValuesFreeCode.GROUP_BY_COLUMN,
            column=column.sql() if column is not None else key.sql(),
        )
    for kind in ("rollup", "cube", "grouping_sets"):
        if group.args.get(kind):
            raise ValuesFreeError(ValuesFreeCode.GROUP_BY_COLUMN, column=kind.upper())


def _projection(node: exp.Expression) -> bool:
    """Check one output column; return whether it contains an aggregate.

    Raises at the first part that could carry a value out. The return value is
    what turns "contains nothing illegal" into "is computed over the rows": an
    output column of only literals passes the walk and is refused by the caller,
    since it is not a statistic about anything.
    """

    if isinstance(node, exp.Window):
        raise ValuesFreeError(
            ValuesFreeCode.WINDOW_FUNCTION, function=_name_of(node.this)
        )
    if isinstance(node, (exp.Filter, exp.IgnoreNulls, exp.RespectNulls)):
        # `agg FILTER (WHERE ...)`: the condition is a filter, as it is to the
        # firewall, so only the aggregate it wraps is the output.
        return _projection(node.this)
    if isinstance(node, exp.WithinGroup):
        # PERCENTILE_CONT(...) WITHIN GROUP (ORDER BY x) and its family: every
        # one is an order statistic, which returns a value from a row.
        raise ValuesFreeError(
            ValuesFreeCode.AGGREGATE_NOT_ALLOWED, function=_name_of(node.this)
        )
    if isinstance(node, exp.Star):
        raise ValuesFreeError(ValuesFreeCode.BARE_COLUMN, column="*")
    if isinstance(node, exp.Column):
        raise ValuesFreeError(ValuesFreeCode.BARE_COLUMN, column=node.sql())
    if isinstance(node, exp.Subquery):
        inner = node.this
        try:
            if not isinstance(inner, exp.Select):
                raise ValuesFreeError(ValuesFreeCode.SET_OPERATION)
            _check_select(inner)
        except ValuesFreeError as exc:
            raise ValuesFreeError(
                ValuesFreeCode.UNAGGREGATED_SUBQUERY,
                column=exc.column,
                function=exc.function,
            ) from None
        return True
    if isinstance(node, exp.Literal):
        if node.is_string:
            raise ValuesFreeError(ValuesFreeCode.LITERAL_PROJECTION)
        return False
    if isinstance(node, exp.Null):
        return False
    if isinstance(node, exp.Boolean):
        raise ValuesFreeError(ValuesFreeCode.LITERAL_PROJECTION)
    if isinstance(node, exp.AggFunc) or (
        isinstance(node, exp.Anonymous) and _func_name(node) in VALUES_FREE_AGGREGATES
    ):
        if _func_name(node) in VALUES_FREE_AGGREGATES or _measures(node):
            # Arguments are unrestricted: whatever the aggregate reads, what
            # comes out is one statistic. A window over it was caught above.
            if any(True for _ in node.find_all(exp.Window)):
                raise ValuesFreeError(
                    ValuesFreeCode.WINDOW_FUNCTION, function=_name_of(node)
                )
            return True
        raise ValuesFreeError(
            ValuesFreeCode.AGGREGATE_NOT_ALLOWED, function=_name_of(node)
        )
    if isinstance(node, (exp.Cast, exp.TryCast)):
        target = node.args.get("to")
        if not isinstance(target, exp.DataType) or not target.is_type(
            *exp.DataType.NUMERIC_TYPES
        ):
            raise ValuesFreeError(ValuesFreeCode.FUNCTION_NOT_ALLOWED, function="cast")
        return _projection(node.this)
    if isinstance(node, (*_ARITHMETIC, *_LOGIC)):
        return _children(node)
    if isinstance(node, exp.Case):
        return _children(node)
    if isinstance(node, exp.If):
        return _children(node)
    if isinstance(node, exp.Func):
        name = _func_name(node)
        if name in _NUMERIC_SCALARS:
            return _children(node)
        code = (
            ValuesFreeCode.AGGREGATE_NOT_ALLOWED
            if name in _VALUE_AGGREGATE_NAMES
            else ValuesFreeCode.FUNCTION_NOT_ALLOWED
        )
        raise ValuesFreeError(code, function=name)
    raise ValuesFreeError(
        ValuesFreeCode.FUNCTION_NOT_ALLOWED, function=type(node).__name__.lower()
    )


def _children(node: exp.Expression) -> bool:
    found = False
    for child in node.iter_expressions():
        found = _projection(child) or found
    return found


def _name_of(node: exp.Expression | None) -> str | None:
    if isinstance(node, exp.ParameterizedAgg):
        # ClickHouse's `topK(3)(x)`: the written name rides in `this`.
        return str(node.this).lower()
    if isinstance(node, exp.Func):
        return _func_name(node)
    return None if node is None else type(node).__name__.lower()
