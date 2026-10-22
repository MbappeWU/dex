-- Seed for the dex Trino dogfood/integration cluster: the memory.app schema.
--
-- Applied by scripts/setup_trino_dev.sh as the admin user through the trino CLI
-- inside the container, which stops at the first failing statement. That script
-- is also what the trino job in .github/workflows/integration.yml runs, so the
-- local dogfood target and the CI target are seeded by one path and cannot
-- drift apart. The Iceberg tables are seeded separately by
-- scripts/trino_iceberg_seed.sql, because that file also seeds the Glue catalog.
--
-- Every name is fully qualified: dex connects with no session catalog or schema,
-- and the seed is written the way dex has to read it.
--
-- The shape mirrors scripts/clickhouse_seed.sql, so the known answers (40
-- orphans, a 3-day gap) are the same on every live connector, plus the Trino
-- hazards only a live server can prove:
--   - customers: PII columns (email, phone, names, address) for flag-not-surface,
--     and a char(10) zip, whose padding regexp_like must not trip over
--   - products: decimal, array(varchar), map(varchar, integer), json, and JSON
--     stored as varchar. The nested and json types pass the substring type
--     predicates, and a regexp_like on one kills the whole profiling batch, so
--     this table is what proves the adapter degrades them first
--   - orders -> customers, order_items -> orders, payments -> orders: inference
--     targets (the memory connector declares no keys)
--   - order_items.product_id: 40 of 5000 rows reference products 900-939, which
--     do not exist, so the shared overlap probe has real orphans to find
--   - events.occurred_at: a deliberate 3-day hole, so temporal continuity has a
--     largest_gap of 3 to report; recorded_at is timestamp(3) for hour grain
--   - customers.customer_ref is a numeric-only string on every third row (166
--     of 500): a profiled fraction of 0.332 that comes back as 0.3 if the
--     profiling AVG runs over a decimal(2,1) literal
--   - signups: an empty table (volume drift, zero-row profiling)
--   - v_order_totals: a view
-- The dbt dev schemas are dropped and left absent: dbt creates its own schema,
-- and the dev-target preflight is meant to accept a missing one.

DROP SCHEMA IF EXISTS memory.app CASCADE;
DROP SCHEMA IF EXISTS memory.dbt_dev CASCADE;
CREATE SCHEMA memory.app;

CREATE TABLE memory.app.customers AS
SELECT
    n AS id,
    format('user%d@example.com', n) AS email,
    format('First%d', n) AS first_name,
    format('Last%d', n) AS last_name,
    IF(n % 7 = 0, NULL, format('+1-555-%d', 1000 + n)) AS phone,
    format('%d Main Street', n) AS address,
    element_at(ARRAY['Amsterdam', 'Berlin', 'Lisbon', 'Milan', 'Porto'], CAST(1 + n % 5 AS integer)) AS city,
    element_at(ARRAY['NL', 'DE', 'PT', 'IT', 'PT'], CAST(1 + n % 5 AS integer)) AS country,
    CAST(format('%05d', 10000 + n) AS char(10)) AS zip,
    IF(n % 3 = 0, format('%d', n), format('C%d', n)) AS customer_ref,
    date_add('day', -CAST(n % 400 AS integer), localtimestamp) AS created_at
FROM UNNEST(sequence(1, 500)) AS t (n);

CREATE TABLE memory.app.products AS
SELECT
    n AS id,
    format('Product %d', n) AS name,
    element_at(ARRAY['electronics', 'grocery', 'apparel', 'home'], CAST(1 + n % 4 AS integer)) AS category,
    CAST(1 + n % 200 + 0.99 AS decimal(10, 2)) AS price,
    ARRAY[format('tag%d', n % 10), format('tag%d', n % 3)] AS tags,
    MAP(ARRAY['width', 'height'], ARRAY[CAST(n AS integer), CAST(n * 2 AS integer)]) AS dims,
    json_parse(format('{"weight_g":%d}', n * 10)) AS spec,
    format('{"weight_g":%d,"in_stock":%s}', n * 10, IF(n % 2 = 0, 'true', 'false')) AS attrs
FROM UNNEST(sequence(1, 80)) AS t (n);

CREATE TABLE memory.app.orders AS
SELECT
    n AS id,
    1 + ((n - 1) * 7) % 500 AS customer_id,
    element_at(ARRAY['pending', 'paid', 'shipped', 'cancelled'], CAST(1 + (n - 1) % 4 AS integer)) AS status,
    CAST(0 AS decimal(12, 2)) AS total,
    date_add('day', -CAST((n - 1) % 365 AS integer), localtimestamp) AS ordered_at
FROM UNNEST(sequence(1, 2000)) AS t (n);

-- 40 of the 5000 rows point at products 900-939, which do not exist.
CREATE TABLE memory.app.order_items AS
SELECT
    n AS id,
    1 + ((n - 1) * 3) % 2000 AS order_id,
    IF(n <= 40, 899 + n, 1 + ((n - 1) * 11) % 80) AS product_id,
    CAST(1 + (n - 1) % 5 AS integer) AS quantity,
    CAST(1 + (n - 1) % 150 + 0.49 AS decimal(10, 2)) AS unit_price
FROM UNNEST(sequence(1, 5000)) AS t (n);

CREATE TABLE memory.app.payments AS
SELECT
    row_number() OVER (ORDER BY o.id) AS id,
    o.id AS order_id,
    element_at(ARRAY['card', 'transfer', 'wallet'], CAST(1 + o.id % 3 AS integer)) AS method,
    lower(to_hex(md5(to_utf8(CAST(o.id AS varchar))))) AS checksum,
    date_add('hour', 1, o.ordered_at) AS paid_at
FROM memory.app.orders AS o
WHERE o.status IN ('paid', 'shipped');

CREATE OR REPLACE VIEW memory.app.v_order_totals AS
SELECT
    o.id AS order_id,
    o.customer_id AS customer_id,
    o.status AS status,
    sum(i.quantity * i.unit_price) AS computed_total
FROM memory.app.orders AS o
INNER JOIN memory.app.order_items AS i ON i.order_id = o.id
GROUP BY o.id, o.customer_id, o.status;

-- Empty on purpose: volume drift's emptied-table case, and a zero-row profile.
CREATE TABLE memory.app.signups (
    id bigint,
    email varchar,
    signed_up timestamp(3)
);

-- occurred_at spans 90 days with days 30-32 removed, so span, distinct_periods,
-- missing_periods and largest_gap all have something to report. sequence() is
-- capped at 10,000 elements, so the 100,000 rows come from a cross join.
CREATE TABLE memory.app.events AS
SELECT
    m + 1 AS id,
    1 + m % 500 AS customer_id,
    element_at(ARRAY['page_view', 'add_to_cart', 'checkout', 'search', 'login'], CAST(1 + m % 5 AS integer)) AS event_type,
    lower(to_hex(md5(to_utf8(CAST(m % 10000 AS varchar))))) AS session_id,
    format('{"session":%d,"step":%d}', m % 10000, m % 7) AS payload,
    date_add('day', -CAST(m % 90 AS integer), current_date) AS occurred_at,
    date_add('hour', -CAST(m % 2160 AS integer), localtimestamp) AS recorded_at
FROM (
    SELECT i * 1000 + j AS m
    FROM UNNEST(sequence(0, 99)) AS a (i)
    CROSS JOIN UNNEST(sequence(0, 999)) AS b (j)
)
WHERE m % 90 NOT IN (30, 31, 32);
