**A Snowflake database scope no longer inventories `INFORMATION_SCHEMA`.** With
`snowflake.databases` naming a whole database, `SHOW ... IN DATABASE` listed that
database's `INFORMATION_SCHEMA` views beside its own objects, so `explore` and
`maintain` profiled Snowflake's catalog as data (findings on
`INFORMATION_SCHEMA.COLUMNS`) and a probe could fail on a view such as
`CORTEX_SEARCH_SERVICES`. Those rows are now skipped, as the scope resolver
already skipped the schema.
