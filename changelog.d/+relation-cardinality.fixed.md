**`maintain` no longer passes checks it could not run.**

- **Categorical dimensions are re-counted on SLayer and Cube models.** The
  dimension-cardinality check found a model's table only through a dbt model,
  so a layer whose models name their table directly (`sql_table`) was never
  checked. A `definition_changed` on such a model now names its table too.
- **A declared join is checked where the warehouse keeps no free row count.**
  On Postgres before `ANALYZE`, Trino's memory tables and any view, the join
  arity check was dropped without a word. It now runs, and the scan supplies
  the count.
- **An unjudged check is named.** Where nothing counts a table's rows (no free
  count, and a scan that does not count), the key and join checks it needed
  appear in `warnings` instead of reading as passed.
- **A semantic-only repository can reconcile.** `maintain reconcile` no longer
  refuses outright when there is no dbt project. A semantic layer's own schema
  findings are planned through their own route, and the rest stay advisory.
