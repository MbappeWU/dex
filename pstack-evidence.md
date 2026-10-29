# pstack-evidence — dex #922

## Outcome
Under native semantic vendors (ossie/slayer/cube), explore --use-project must not fold MetricFlow primary_entities / metric_models / semantic_source into exploration. Transformation declarations stay.

## Approach
Helper `_drop_metricflow_under_native_vendor` gated on `SEMANTIC_SOURCE_FACTORIES`; wired in `_project_definitions` after `definitions()`.

## Verification
- Focused: 6 passed (unit + ossie/slayer/cube map/profile)
- Explore-related: 40 passed
- Ruff: clean
- Mutation: no-op helper → 3 unit + integration asserts fail; restore → green

## Poteto notes
Small pure helper; no drive-by; changelog fragment added.
