-- Seed for the Iceberg tables of the dex Trino dogfood/integration cluster.
--
-- One file seeds two catalogs:
--   - the keyless local `iceberg` catalog (a JDBC catalog on a Postgres sidecar,
--     Parquet on the container's file system), applied by
--     scripts/setup_trino_dev.sh, which the every-push trino CI job runs;
--   - the `iceberg_glue` catalog (Glue plus S3), applied once by
--     scripts/setup_trino_iceberg_ci.sh.
-- The caller creates the schema first and runs this file with the trino CLI's
-- --catalog and --schema, which is why the table names here are unqualified.
-- dex itself never gets a session catalog or schema; the seed is the one place
-- that does, because it is the only thing that writes the sources.
--
-- What only a lakehouse catalog can prove:
--   - orders: Parquet with real column statistics, so EXPLAIN (TYPE IO) prices
--     it from the files rather than a floor, and large enough (1.5M rows) that a
--     scan outlives the server's roughly one-second enforcement interval for
--     query_max_scan_physical_bytes, so the physical-bytes cap visibly binds.
--   - customer_profiles: snapshot history in which a PII column is dropped. The
--     first insert carries ssn; ssn is then dropped and more rows arrive. Read as
--     of that first insert, the table returns ssn with its values, a column the
--     current schema (and so dex's cache) no longer has. Time travel must be
--     refused, and this table is the live proof.

CREATE TABLE orders
WITH (format = 'PARQUET', partitioning = ARRAY['orderstatus'])
AS SELECT * FROM tpch.sf1.orders;

CREATE TABLE customer_profiles (
    id bigint,
    email varchar,
    ssn varchar,
    plan varchar,
    signup_date date
)
WITH (format = 'PARQUET', partitioning = ARRAY['month(signup_date)']);

INSERT INTO customer_profiles
SELECT
    n,
    format('user%d@example.com', n),
    format('%03d-%02d-%04d', 100 + n % 800, 10 + n % 89, 1000 + n),
    element_at(ARRAY['free', 'pro', 'team'], CAST(1 + n % 3 AS integer)),
    date_add('day', -CAST(n % 365 AS integer), DATE '2026-09-30')
FROM UNNEST(sequence(1, 300)) AS t (n);

ALTER TABLE customer_profiles DROP COLUMN ssn;

INSERT INTO customer_profiles
SELECT
    n,
    format('user%d@example.com', n),
    element_at(ARRAY['free', 'pro', 'team'], CAST(1 + n % 3 AS integer)),
    date_add('day', -CAST(n % 365 AS integer), DATE '2026-09-30')
FROM UNNEST(sequence(301, 350)) AS t (n);
