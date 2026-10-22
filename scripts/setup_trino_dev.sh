#!/usr/bin/env bash
# Stand up (or re-seed) the Trino target for the trino connector.
#
# Nothing here provisions cloud infrastructure: self-hosted Trino is a container,
# so this script is both the local dogfood stand-up and what the trino job in
# .github/workflows/integration.yml runs, which keeps one seeding path for both.
#
# Two containers on a private network, created and removed as a pair:
#   - dex-trino: Trino with the tpch, memory and iceberg catalogs, file-based
#     access control (scripts/trino/rules.json), published on loopback only.
#   - dex-trino-catalog: the Postgres database the Iceberg JDBC catalog keeps its
#     table pointers in. It is never published to the host, and its password is
#     generated when the pair is created and handed to Trino through the
#     environment (scripts/trino/catalog/iceberg.properties reads it back), so no
#     credential is committed anywhere. The Parquet files live inside dex-trino,
#     which is why the two are only ever recreated together: a catalog that
#     outlives its files points at nothing.
#
# The cluster runs without authentication, so a client is whoever it says it is.
# That is acceptable only because the port is bound to 127.0.0.1: the identities
# in rules.json (admin seeds, dex_ro reads, dbt_dev builds) are there to prove
# the access-control rules dex documents, not to keep anyone out.
#
# --aws additionally attaches `iceberg_glue`, the Glue plus S3 Iceberg catalog
# that scripts/setup_trino_iceberg_ci.sh provisions. Trino reads the standard
# AWS credential variables: when they are already set (the CI job's OIDC step
# sets them) they are passed through; otherwise the current AWS CLI session is
# exported into a private env file that is deleted as soon as the container has
# started. Either way they are short-lived, so a re-run recreates the pair.
#
# Usage:
#   scripts/setup_trino_dev.sh          # start the pair if needed, (re)seed
#   scripts/setup_trino_dev.sh --aws    # recreate with iceberg_glue, (re)seed
#   scripts/setup_trino_dev.sh --down   # remove the pair and its network
#
# Idempotent: re-running drops and recreates memory.app and iceberg.app, and
# drops the dbt dev schemas (dbt creates its own).

set -euo pipefail

TRINO=dex-trino
CATALOG_DB=dex-trino-catalog
NETWORK=dex-trino
PORT="${DEX_TRINO_PORT:-8085}"
# trinodb/trino:483, pinned by its multi-arch index digest so every machine and
# every CI run start the same bytes. Bump it deliberately, to a release at least
# two weeks old, re-reading the digest with
#   docker buildx imagetools inspect trinodb/trino:<release> \
#     --format '{{json .Manifest.Digest}}'
TRINO_DIGEST=sha256:db58cc93e593a2706553745f276bb119c9810e69918be56ecde088ba7ccb0534
TRINO_IMAGE="trinodb/trino@$TRINO_DIGEST"
# The same postgres:16 digest as scripts/setup_postgres_dev.sh.
CATALOG_DIGEST=sha256:ca0bd484cb98bf4b24eb1010e73fb3fcbd6714d240fbc1a10eea5b7dbecb641d
CATALOG_IMAGE="postgres@$CATALOG_DIGEST"
# No capabilities for either. The Trino image runs as trino and owns everything
# it writes under /data/trino; the Postgres image started as postgres owns its
# data directory. Both limits are sized from measurement: Trino starts about 440
# threads before it serves a query and peaked near 930 over the `-m trino`
# suite, so 2048 bounds a runaway with room to spare. Its jvm.config sizes the
# heap at 80% of the container's memory, and the resident set peaked near
# 3.5 GiB of 4 GiB over the same suite; 3g sat at its ceiling after a handful of
# queries.
# scripts/check_container_hardening.sh refuses either container if any of these
# is missing.
TRINO_HARDENING=(
    --user trino
    --cap-drop ALL
    --security-opt no-new-privileges:true
    --pids-limit 2048
    --memory 4g
)
CATALOG_HARDENING=(
    --user postgres
    --cap-drop ALL
    --security-opt no-new-privileges:true
    --pids-limit 256
    --memory 1g
)
SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
ETC="$SCRIPTS/trino"
SEED="$SCRIPTS/trino_seed.sql"
ICEBERG_SEED="$SCRIPTS/trino_iceberg_seed.sql"

remove_pair() {
    docker rm -f "$TRINO" "$CATALOG_DB" >/dev/null 2>&1 || true
}

case "${1:-}" in
    --down)
        remove_pair
        docker network rm "$NETWORK" >/dev/null 2>&1 || true
        echo "removed $TRINO, $CATALOG_DB and network $NETWORK"
        exit 0
        ;;
    --aws) AWS=1 ;;
    "") AWS=0 ;;
    *)
        echo "usage: $0 [--aws | --down]" >&2
        exit 2
        ;;
esac

if ! docker info >/dev/null 2>&1; then
    echo "docker daemon is not running; start Docker Desktop first" >&2
    exit 1
fi

pair_exists() {
    docker inspect "$TRINO" >/dev/null 2>&1 && docker inspect "$CATALOG_DB" >/dev/null 2>&1
}

# AWS credentials expire, so --aws always starts a fresh pair; a pair started
# with them is likewise replaced when they are not wanted.
started_with_aws="$(docker inspect -f '{{index .Config.Labels "dex.trino.aws"}}' "$TRINO" 2>/dev/null || true)"
if ! pair_exists || [[ "$AWS" == "1" || "$started_with_aws" == "1" ]]; then
    remove_pair
    docker network inspect "$NETWORK" >/dev/null 2>&1 \
        || docker network create "$NETWORK" >/dev/null

    catalog_password="$(openssl rand -hex 24)"
    docker run -d --name "$CATALOG_DB" --network "$NETWORK" \
        "${CATALOG_HARDENING[@]}" \
        -e POSTGRES_PASSWORD="$catalog_password" \
        "$CATALOG_IMAGE" >/dev/null

    # With --aws the Glue catalog joins the base ones. It cannot be mounted as a
    # file inside the read-only catalog mount (Docker has to create its mount
    # point there and the parent is read-only), so the whole directory is
    # assembled outside the repo and mounted once. It has to outlive the
    # container, because Trino reads it again whenever the container restarts.
    catalog_dir="$ETC/catalog"
    if [[ "$AWS" == "1" ]]; then
        catalog_dir="${XDG_CACHE_HOME:-$HOME/.cache}/dex-trino/catalog"
        rm -rf "$catalog_dir"
        mkdir -p "$catalog_dir"
        cp "$ETC"/catalog/*.properties "$ETC"/catalog-aws/*.properties "$catalog_dir/"
    fi
    trino_args=(
        -d --name "$TRINO" --network "$NETWORK"
        "${TRINO_HARDENING[@]}"
        --label "dex.trino.aws=$AWS"
        -e DEX_TRINO_CATALOG_PASSWORD="$catalog_password"
        -p "127.0.0.1:$PORT:8080"
        -v "$catalog_dir:/etc/trino/catalog:ro"
        -v "$ETC/access-control.properties:/etc/trino/access-control.properties:ro"
        -v "$ETC/rules.json:/etc/trino/rules.json:ro"
    )
    env_dir=""
    if [[ "$AWS" == "1" ]]; then
        region="${AWS_REGION:-$(aws configure get region 2>/dev/null || true)}"
        region="${region:-us-east-1}"
        trino_args+=(-e AWS_REGION="$region")
        if [[ -n "${AWS_ACCESS_KEY_ID:-}" ]]; then
            # Named without a value, so docker copies them from this environment
            # and they never appear on a command line.
            trino_args+=(-e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN)
        else
            env_dir="$(mktemp -d)"
            trap 'rm -rf "$env_dir"' EXIT
            (umask 077 && aws configure export-credentials --format env-no-export \
                >"$env_dir/aws.env")
            trino_args+=(--env-file "$env_dir/aws.env")
        fi
    fi
    docker run "${trino_args[@]}" "$TRINO_IMAGE" >/dev/null
    if [[ -n "$env_dir" ]]; then
        rm -rf "$env_dir"
    fi
    echo "started $TRINO_IMAGE as $TRINO on 127.0.0.1:$PORT (aws catalog: $AWS)"
fi
# Checked before a stopped pair is started again, so a container this script
# created before it hardened them is refused rather than reused.
"$SCRIPTS/check_container_hardening.sh" "$TRINO" "$TRINO_DIGEST"
"$SCRIPTS/check_container_hardening.sh" "$CATALOG_DB" "$CATALOG_DIGEST"
for container in "$CATALOG_DB" "$TRINO"; do
    if [[ "$(docker inspect -f '{{.State.Running}}' "$container")" != "true" ]]; then
        docker start "$container" >/dev/null
        echo "restarted existing container $container"
    fi
done

until docker exec "$CATALOG_DB" pg_isready -U postgres -q; do
    sleep 0.5
done
# The Iceberg JDBC catalog reads and writes these two tables but never creates
# them. The column set is Iceberg's V1 schema (iceberg_type distinguishes tables
# from views), which is what the catalog's default schema version expects.
docker exec -i "$CATALOG_DB" psql -U postgres -q -v ON_ERROR_STOP=1 <<'SQL'
SET client_min_messages = warning;
CREATE TABLE IF NOT EXISTS iceberg_tables (
    catalog_name varchar(255) NOT NULL,
    table_namespace varchar(255) NOT NULL,
    table_name varchar(255) NOT NULL,
    metadata_location varchar(1000),
    previous_metadata_location varchar(1000),
    iceberg_type varchar(5),
    PRIMARY KEY (catalog_name, table_namespace, table_name)
);
CREATE TABLE IF NOT EXISTS iceberg_namespace_properties (
    catalog_name varchar(255) NOT NULL,
    namespace varchar(255) NOT NULL,
    property_key varchar(255),
    property_value varchar(1000),
    PRIMARY KEY (catalog_name, namespace, property_key)
);
SQL

# Trino accepts connections before it plans queries, so poll a real statement
# rather than sleeping a fixed amount.
ready=0
for _ in $(seq 1 120); do
    if docker exec "$TRINO" trino --execute "SELECT 1" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
if [[ "$ready" != "1" ]]; then
    echo "trino did not become ready; last log lines:" >&2
    docker logs --tail 40 "$TRINO" >&2
    exit 1
fi

docker exec -i "$TRINO" trino --user admin <"$SEED"
docker exec "$TRINO" trino --user admin --execute "
    DROP SCHEMA IF EXISTS iceberg.app CASCADE;
    DROP SCHEMA IF EXISTS iceberg.dbt_dev CASCADE;
    CREATE SCHEMA iceberg.app"
docker exec -i "$TRINO" trino --user admin --catalog iceberg --schema app <"$ICEBERG_SEED"

echo "seeded memory.app from scripts/trino_seed.sql and iceberg.app from scripts/trino_iceberg_seed.sql"
echo
echo "connect dex as the read-only user (no password: the cluster is keyless):"
echo "  export TRINO_URL=http://dex_ro@127.0.0.1:$PORT"
echo "dbt dev builds (transform build) run as dbt_dev, the profile's user."
