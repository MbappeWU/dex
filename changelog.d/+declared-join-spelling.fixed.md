**`--verify` measures a declared join on Snowflake whatever case the project
wrote it in.** A dbt `relationships` test or a declaration's `references` names
`customer_id` where Snowflake stores `CUSTOMER_ID`, and the overlap probe quoted
the name as written, so `explore relationships --verify` and `explore map
--verify` failed with an invalid identifier. A declared join now takes the
spelling its profiled dataset records before it is probed, where exactly one
column matches.
