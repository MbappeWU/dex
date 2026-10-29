**`semantic.vendor: cube` reads Cube model files as the semantic layer.** Name
the files in `semantic.cube.files` and `explore semantic list` (with `--metric`,
`--for-dimension` and `--search`) answers from plain-YAML cubes and views, read
statically with no Node and no Cube server, and validated by dex's own port of
Cube's model rules. Every refusal names the file and the element. Cubes and
views are semantic models, dimensions are keyed `cube.member`, and aggregating
measures are measures and metrics. `explore relationships|map|profile
--use-project` carry Cube's primary keys and joins at confidence 1.0, and
`maintain snapshot` fingerprints the layer. A JS model, a templated (Jinja) file
and an `extends` are named as compiled-only under a new `compiled_only` catalog
field, never translated. Governed `query` and `values` are not available yet
and refuse by name. Install with `exmergo-dex-core[cube]`.
