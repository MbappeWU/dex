**`semantic cube define|update|plan` authors Cube model files, and a declared
mart derives a cube.** Whole plain-YAML files listed in `semantic.cube.files`
are validated as one prospective set by Dex's port of Cube's model rules, with
a key Cube does not declare refused in a file the plan writes and a `fan_out`
warning for a summing cube with no `primary_key` on the one side of a join.
JavaScript models, `globals.py`, `cube.py`/`cube.js` and compiled-only files are
refused by name, and each cube and view is reported `defined`, `updated` or
`unchanged`. `transform apply` writes the authored bytes, all files or none.
Under `semantic.vendor: cube`, a declared dbt mart now gets a cube of its own in
`data.semantic_proposal` (grain as `primary_key`, foreign keys as `many_to_one`
joins, declared aggregations as measures, the population as `meta.dex` and never
a filter), and `semantic cube` takes `declarations` that annotate one cube
without touching the rest of its file. No Node, no Cube server and no warehouse
connection is opened.
