**Snowflake commands can stop paying the warehouse's idle tail.** A short
command on a warehouse at Snowflake's default 600-second `AUTO_SUSPEND` used to
cost about 600 warehouse-seconds for a second or two of work. With
`snowflake.suspend_after_command: true` and `OPERATE` granted on the pinned
warehouse, Dex suspends it at the end of a command that woke it and left it
idle, so the command bills its statement time floored at Snowflake's 60-second
resume minimum. Dex never suspends a warehouse that was already running, that
other queries are using, that has `AUTO_RESUME` off, or that is not the pinned
one, and the statement comes
from one typed method the query firewall never sees. Without the grant the
command completes, names the grant to add, and settles the tail as before.
`connect test` reports `budget.warehouse.suspend_after_command` and, when
`AUTO_SUSPEND` is longer than 60 seconds with no suspend in place, recommends a
60-second `AUTO_SUSPEND` or the setting; Dex never changes `AUTO_SUSPEND`
itself.
