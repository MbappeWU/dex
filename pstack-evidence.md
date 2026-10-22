# pstack evidence — dex#657

## Intent
Follow-up to #635 / issue #657: rename and remove must honor `python_ref`,
`python_source`, and `yaml_snapshot_relation` the same way `references` already
indexes them. Without that, a rename leaves stale Python/`relation:` readers and
a remove can orphan them.

## Approach
1. Extend `_NODE_READING_FORMS` / `_BREAKS_ON_DELETE` with the three forms.
2. Teach rewrite walk to emit `yaml_snapshot_relation` for snapshot YAML `relation:`.
3. Prefer `view.files` overlays for `.py` in `ReferenceIndex` (same as SQL/YAML),
   and seed disk `.py` into projected overlays so rename/remove guards see them
   even when the caller only overlaid SQL/YAML.

## Verification
- Focused: `TestPythonAndYamlSnapshotReaders` (6) + 2 plan orphan guards → 8 passed
  (`/workspace/ops/oss/2026-10-23/evidence/dex-657-focused-final.txt`)
- Broader transform suite: 107 passed; 3 failures are baseline on this box
  (dbt/mashumaro UnserializableField) — same 3 fail on clean upstream tip
  (`dex-657-baseline-3fail.txt`, `dex-657-broader.txt`)
- ruff on touched files: clean after docstring wrap

## Non-goals / skipped
- No HW/device work; no force-push; did not open a 3rd dex PR (only #766 open
  after #655 merged).
