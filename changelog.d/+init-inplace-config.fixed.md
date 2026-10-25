**`transform init` keeps comments and unread keys in an existing `.dex/config.yml`.**
When a repo already has a config, init amends only the keys it sets through the
same in-place editor as `dex config set`, so comments, key order, unread keys,
and an explicit `null` (for example `cluster.sample_seed`) survive. A repo with
no config still gets a fresh file from `save_config`.
