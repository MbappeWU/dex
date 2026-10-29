**Snowflake commands run back to back no longer each pay a full idle tail.**
Each command now records its claim on the warehouse's resume cycle in
`.dex/spend.jsonl` (a zero-amount settlement row carrying `warehouse_claim`),
and a command that finds the warehouse still running in the same cycle is
quoted and charged only the gap since the previous command plus its own span.
Ten commands half a minute apart settle about one tail, as Snowflake bills
them, instead of ten. The charge never exceeds statement time plus one tail, so
a warehouse other users keep running cannot make Dex claim their uptime.
