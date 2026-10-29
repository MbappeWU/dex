**Every `maintain` axis runs on a Cube layer.** Under `semantic.vendor: cube`,
beside a dbt project or with none, and without a Cube server or Node:

- **Compiled-only definitions are recorded by their content.** A JS model, a
  Jinja-templated file or an `extends` cube lands in the baseline under
  `opaque_definitions`. A change is `compiled_only_changed`, and every
  `maintain semantic` and `maintain check` warns that these were never compared
  structurally, so a layer holding one never reads clean.
- **The schema axis holds cubes and views to Dex's warehouse metadata.**
  - `declared_relation_missing`: a cube's table is gone since the baseline. A
    table the connection was never scoped to is a note, not a finding.
  - `declared_column_missing`: a member's `sql`, a measure filter or a segment
    reads a dropped column.
  - `declared_type_conflict`: a `time` dimension on a non-temporal column, or a
    `number` dimension or summing measure on text.
  - `declared_dependency_dropped`: a member, join or view include that reads
    something that goes.

  An expression Dex cannot read is named as unchecked.
- **The grain axis measures each join's `relationship`.** A join that fans out
  is `join_cardinality_exceeded`, beside the `primary_key` checks already there.
- **`maintain reconcile` plans the removals through `semantic cube update`.** It
  cuts only the entries that go, keeps every other byte, and stores the edit
  under `data.semantic_plan_id`, even in a repository with no dbt project. A
  cube whose table is gone, and a view source that walks a removed join, stay
  advisory.
