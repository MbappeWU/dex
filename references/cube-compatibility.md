# Cube compatibility

What Dex accepts from a [Cube](https://github.com/cube-js/cube) model file, what it
checks, what it makes of what it read, and what it does not claim. Read it beside
[Cube models](semantic-layer.md#cube-models-vendor-cube), which describes how to
configure the layer.

## The pinned rules

Cube publishes no JSON Schema for its data model. The authoritative validator is
the schema compiler's Joi rules (`packages/cubejs-schema-compiler/src/compiler/
CubeValidator.ts`), applied after Cube's YAML compiler camelizes every key. Dex
ports those rules for the fields it reads, pinned to one Cube release.

| | |
|---|---|
| Upstream | <https://github.com/cube-js/cube> |
| Release the rules are ported from | `v1.8.14` |
| Rules | `CubeValidator.ts`, `YamlCompiler.ts` and `CubeSymbols.ts` |
| License | Apache License 2.0 |

The fingerprint records that release beside every hash, so a later port that
reads a file differently is reported as a change of reader rather than as every
cube changed.

## Required extras

| capability | extra | absent |
|---|---|---|
| `config validate`, `config get` on a `semantic.cube` block | none | always available |
| reading the layer: `explore semantic list`, `--use-project`, `maintain` | `[cube]` (the dialect engine, which every connector extra already carries) | refused by name, `reason: prerequisite` |

No Cube package is installed for any of it: no Node, no Cube server, no
`cube_dbt`, no `apache-ossie-cube`.

## Plain YAML is read; nothing else is guessed

Cube renders a YAML model file as a Jinja template before it compiles it,
compiles a JavaScript model with Node, and merges `extends` in its compiler. None
of that is statically readable, so each is **compiled-only**:

- a `.js` file;
- a YAML file containing `{{`, `{%` or `{#`;
- a cube or view using `extends`, element by element.

A compiled-only definition is named in the catalog's `notes` and in its
`compiled_only` list (`file`, `reason`, and `element` where only one cube or view
is affected), and nothing is translated out of it. It is never reported as
absent. A join or a reference to a cube the set does not hold is a warning rather
than a refusal while a compiled-only file is configured, because that cube may be
the one the file defines.

## What a model file is checked against

Cube publishes no JSON Schema. Its validator is the schema compiler's Joi rules
(`CubeValidator.ts`), and Dex ports them, pinned to Cube 1.8.14, for every field
it reads:

- **each element.** An identifier `name` on every cube, view, member and join.
  A cube has `sql` or `sql_table`, not both and not neither. Dimension types are
  `string`, `number`, `boolean`, `time`, `geo` and `switch`. Measure types are
  `count`, `count_distinct`, `count_distinct_approx`, `sum`, `avg`, `min`,
  `max`, `number`, `string`, `time` and `boolean` (plus `number_agg` and `rank`
  on a multi-stage measure). Checked shapes: join `relationship`, custom
  granularities, `rolling_window`, and a view's `cubes`. Keys are read in either
  spelling, `sql_table` or `sqlTable`, as Cube camelizes them, and the legacy
  relationships `belongs_to`, `has_many` and `has_one` read as `many_to_one`,
  `one_to_many` and `one_to_one`.
- **the set.** Cubes and views share one namespace, so a name declared twice
  refuses every element declaring it. Further refusals, each of which also
  refuses the element declaring it:
  - a join to a cube the set does not hold;
  - two joins to one cube without an `alias`;
  - a `{reference}` that resolves to nothing;
  - a view `join_path` hop that no declared join supports;
  - a view `includes` member the cube does not declare;
  - a view reaching one cube by two paths from one root.

  A refused cube retracts what depends on it, to a fixed point.
- **unknown keys.** Cube refuses a key its schema does not declare. Reading, Dex
  only warns (`unknown_key`): a key the port does not know may be one a newer
  Cube added, and refusing the cube would hide everything else it declares.

Every refusal names the file and the element (`cubes['orders'].joins['customers']`,
`views['sales'].cubes['orders.customers']`). One broken file leaves the rest
readable, and the catalog raises only when nothing at all could be read.

## References and lineage

A Cube `sql` is an f-string: `{CUBE}` (or `{TABLE}`) is the cube itself,
`{member}` and `{CUBE.member}` a member of it, `{cube}` and `{cube.member}`
another cube, and `{cube1.cube2.member}` a member reached through declared joins.
`\{` and `\}` are literal braces. `{FILTER_PARAMS...}`, `{SECURITY_CONTEXT...}`,
`{SQL_UTILS...}` and `{FILTER_GROUP...}` are per-query values: not errors, but an
expression reading one has no single column behind it.

A member's physical column is traced from its `sql`. `amount`,
`{CUBE}.amount`, and a reference to a member that is itself one column all
resolve. An expression, a quoted identifier, another cube's member or a
per-query value resolves to nothing. A resolved column is folded the way the
active connector folds an unquoted identifier, as the relation is: on Snowflake,
`customer_id` is the column `CUSTOMER_ID`. A cube's relation depends on how it
reads its table:

- **`sql_table`**: read with the active connector's identifier folding.
- **`sql`**: claimed only for `SELECT <columns or *> FROM <one table>` with no
  other clause, and its columns are traced through the projection
  (`customer_key AS id` makes `id`'s column `customer_key`).
- **a filtered single-table query**: keeps its table for the PII gate alone,
  with a note, because its rows are not the table's.
- **a joined query**: no relation at all.

A join's column pairs come from an equality, or a conjunction of equalities,
between a column of each side. A range or expression join is declared with no
pairs, and a note says so.

## How Cube maps onto the catalog

| Cube | catalog |
|---|---|
| cube | a semantic model, `title` as its label, on the relation its `sql_table` (or a plain single-table `sql`) names |
| view | a semantic model with no relation: the governed public surface. Each member it exposes is listed under the view (`sales.order_status`), keeps the cube member it is in `vendor_params.source`, and reaches the PII gate through that cube's column |
| dimension | a dimension keyed `cube.member`. `time` dimensions carry Cube's built-in granularities plus their own custom ones as `queryable_granularities`; every other type is `categorical`, with `[]` |
| aggregating measure (`count`, `count_distinct`, `count_distinct_approx`, `sum`, `avg`, `min`, `max`) | a measure (its type as the aggregation, `avg` as `average`) **and** a `simple` metric `cube.member` reading it. `rolling_window` makes the metric `cumulative`. The measure's `filters` are the metric's `filter` |
| calculated measure (`number`, `string`, `time`, `boolean`) | a metric only, since it aggregates nothing itself. `ratio` where its `sql` is exactly one measure divided by another, with both sides named; `derived` otherwise, with its `{member}` references as `input_metrics` and the aggregating measures beneath them as `input_measures` |
| `primary_key` | a declared key; several on one cube are one composite key in declared order, never several single ones |
| `joins[].relationship` | a declared relationship with every ordered pair, through the declaration channel |
| segments, `pre_aggregations`, `access_policy`, `hierarchies`, `folders`, `default_filters`, `refresh_key`, view groups, multi-stage settings | named in `notes`: no home in the catalog |

A metric is groupable by its own cube's dimensions and by those of every cube its
declared joins reach, in their declared direction, as Cube's join graph is
directed. A measure name is scoped to its cube in Cube (every cube may declare
`count`), so a measure is identified by its name and its `semantic_model`
together. `agg_time_dimension` and a metric's `time_axis` are the cube's time
dimension where it has exactly one; Cube declares no default, so with several,
none is named and a note says why. `public: false` and a cube on another
`data_source` are listed, with a note.

What has no home is declared in `unavailable`: Cube has **no entities**, so every
entity field is a gap and keys and joins reach `explore` through the declaration
channel instead, and a semantic model has **no `model_ref` or primary entity**.
`dimension_scope` is `declarations`.

**What is never read.** `meta` is free-form, and `mask` and access policies
govern values at query time. None of them reaches the catalog, the declarations
or the fingerprint.

## What Cube's declarations reach in `explore`

- a sole `primary_key` dimension becomes a declared unique key; several become
  one composite grain in declared order;
- a join becomes a declared relationship at confidence 1.0, written child to
  parent: the joining cube is the child, except for `one_to_many`. A
  single-pair join is also a foreign key;
- `explore map --use-project` marks each relation with the cubes on it.

## `query` and `values` are not available yet

Cube has no Python SQL renderer: its planner ships only inside a Cube server.
`explore semantic query` refuses by name and points at `explore query` over the
relations the catalog names. `explore semantic values` resolves the token first,
so a misspelling is refused by name. It then runs the values PII gate, so a
personal dimension is refused as PII (`reason: guard`), and only then refuses
for want of a renderer. The governed path, where Cube renders and Dex executes
under the cost guard, comes next.

## What `maintain` makes of the layer

`maintain snapshot` fingerprints each cube and view, each measure (as
`cube.member`), and each join with its pairs and arity, under `vendor: cube` and
the validator release. Content hashes cover each element as written, keys
normalized, `meta` excluded, so respelling `sqlTable` as `sql_table` is not a
change. The layer-neutral axes read it today: `maintain semantic` reports a
definition added, removed or changed, and a dimension, measure, key or join
column gone from its table (`dangling_reference`, `broken_relationship`);
`maintain grain` measures every `primary_key`, a composite one whole
(`declared_grain_not_unique`). Cube's declared types are not yet compared with
the warehouse's, and `maintain reconcile` plans no Cube edit. A compiled-only definition is
not fingerprinted structurally, and the fingerprint's notes say so.

