"""Live Trino against the Glue and S3 Iceberg catalog: the path most production
lakehouses take, kept narrow because the keyless container suite already owns
connector behavior (including Iceberg on local storage).

What only this catalog proves: Glue metadata and S3 Parquet read with
credentials from the AWS default chain (an OIDC session in CI, nothing stored),
statistics-backed estimates on real object storage, the dropped-PII snapshot
refused on real history, the read-only identity refused writes, one incremental
dbt build into the Glue dev database, and no credential in any envelope.

The pair comes from scripts/setup_trino_dev.sh --aws; the bucket, the Glue
databases, the CI role and the seed from scripts/setup_trino_iceberg_ci.sh.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from .conftest import TRINO_MAX_SECONDS, integration_budget, trino_client
from .test_trino_connect import run_cli

pytestmark = [pytest.mark.integration, pytest.mark.trino_iceberg]

CATALOG = "iceberg_glue"
BUDGET = TRINO_MAX_SECONDS


@pytest.fixture
def schema() -> str:
    return os.environ["DEX_TEST_TRINO_GLUE_SCHEMA"]


@pytest.fixture(autouse=True)
def _url(trino_url, monkeypatch):
    monkeypatch.setenv("TRINO_URL", trino_url)


def _seed(root: Path, schema: str, *, budget: float = BUDGET) -> None:
    config = {
        "connector": "trino",
        "trino": {
            "catalogs": [f"{CATALOG}.{schema}"],
            "dev_catalog": CATALOG,
            "dev_schema": f"{schema}_dbt_dev",
        },
        "budget": integration_budget(budget),
    }
    (root / ".dex").mkdir(parents=True, exist_ok=True)
    (root / ".dex" / "config.yml").write_text(yaml.safe_dump(config), encoding="utf-8")


def _no_credential_in(envelope: dict) -> None:
    payload = json.dumps(envelope)
    for name in ("AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_ACCESS_KEY_ID"):
        value = os.environ.get(name)
        if value:
            assert value not in payload, f"{name} reached an envelope"


def test_a_verified_map_reads_glue_and_s3_with_plan_estimates(
    tmp_path: Path, capsys, schema
):
    _seed(tmp_path, schema)
    rc, priced = run_cli(["--repo-root", str(tmp_path), "explore", "map"], capsys)
    assert rc == 0, priced
    assert priced["status"] == "needs_confirmation"
    assert priced["data"]["estimate_basis"] == "explain_io"
    _no_credential_in(priced)

    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "map",
            "--verify",
            "--confirm",
            "--budget",
            str(BUDGET),
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["data"]["object_count"] >= 2
    assert envelope["data"]["spend"]["seconds_billed"] > 0
    _no_credential_in(envelope)


def test_time_travel_to_the_dropped_pii_column_is_refused_on_glue(
    tmp_path: Path, capsys, schema, trino_url
):
    cursor = trino_client(trino_url).cursor()
    cursor.execute(
        f'SELECT snapshot_id FROM {CATALOG}.{schema}."customer_profiles$snapshots" '  # noqa: S608
        "WHERE summary['total-records'] = '300'"
    )
    (snapshot,) = cursor.fetchall()[0]

    _seed(tmp_path, schema)
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "map",
            "--confirm",
            "--budget",
            str(BUDGET),
        ],
        capsys,
    )
    assert rc == 0, envelope
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "query",
            f"SELECT * FROM {CATALOG}.{schema}.customer_profiles "  # noqa: S608
            f"FOR VERSION AS OF {snapshot}",
            "--confirm",
            "--budget",
            str(BUDGET),
        ],
        capsys,
    )
    assert rc == 1
    assert "time travel is not permitted" in envelope["errors"][0]


@pytest.mark.parametrize("user", ["dex_ro", "dbt_dev"])
def test_the_sources_cannot_be_written_by_either_identity(trino_url, schema, user):
    import trino.exceptions

    cursor = trino_client(trino_url, user=user).cursor()
    with pytest.raises(trino.exceptions.TrinoUserError) as exc:
        cursor.execute(
            f"CREATE TABLE {CATALOG}.{schema}.dex_write_probe AS SELECT 1 AS a"
        )
        cursor.fetchall()
    assert exc.value.error_name == "PERMISSION_DENIED"


def test_an_incremental_build_merges_into_the_glue_dev_database(
    tmp_path: Path, capsys, schema, trino_url, trino_dev_user, monkeypatch
):
    dev_schema = f"{schema}_dbt_dev"
    admin = trino_client(trino_url, user="admin").cursor()
    admin.execute(f"DROP TABLE IF EXISTS {CATALOG}.{dev_schema}.orders_incremental")
    admin.fetchall()

    monkeypatch.setenv("TRINO_USER", trino_dev_user)
    _seed(tmp_path, schema, budget=BUDGET * 5)
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "init",
            "glueshop",
            "--connector",
            "trino",
        ],
        capsys,
    )
    assert rc == 0, envelope
    models = tmp_path / "glueshop" / "models" / "staging"
    models.mkdir(parents=True, exist_ok=True)
    (models / "orders_incremental.sql").write_text(
        "{{ config(materialized='incremental', unique_key='orderkey', "  # noqa: S608
        "incremental_strategy='merge') }}\n"
        f"select orderkey, totalprice from {CATALOG}.{schema}.orders\n"
        "{% if is_incremental() %}\n"
        "where orderkey > (select max(orderkey) - 100 from {{ this }})\n"
        "{% endif %}\n",
        encoding="utf-8",
    )
    for _ in range(2):
        rc, envelope = run_cli(
            [
                "--repo-root",
                str(tmp_path),
                "transform",
                "build",
                "--target",
                "dev",
                "--confirm",
                "--budget",
                str(BUDGET * 5),
            ],
            capsys,
        )
        assert rc == 0, envelope
        _no_credential_in(envelope)
    admin.execute(f"SELECT count(*) FROM {CATALOG}.{dev_schema}.orders_incremental")  # noqa: S608
    assert admin.fetchall()[0][0] == 1_500_000
