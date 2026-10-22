"""Live maintain against Trino: a baseline over a dbt project scaffolded from a
real table, a warehouse change made behind dex's back, and the loop that has to
notice it. `maintain snapshot` pins the map for free, `maintain check` finds the
dropped column for free and the lost key under a confirmed budget, and
`maintain reconcile` turns the drop into a stored plan against the staging model.

The drifting table lives in its own memory schema, created and dropped here by
the admin identity, so the shared seed in memory.app never changes under the
other suites. dex reads as dex_ro through TRINO_URL; dbt is never run, so the
dbt_dev identity is only named, for `transform init`'s rendered profile.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from .conftest import TRINO_MAX_SECONDS, trino_client
from .test_trino_connect import run_cli, seed_repo

pytestmark = [pytest.mark.integration, pytest.mark.trino]

BUDGET = TRINO_MAX_SECONDS

_ROWS = 200


def _admin(trino_url: str, *statements: str) -> None:
    cursor = trino_client(trino_url, user="admin").cursor()
    for statement in statements:
        cursor.execute(statement)
        cursor.fetchall()


def _orders(relation: str, *, with_status: bool, duplicate_ids: int = 0) -> str:
    """The orders table, built from a sequence so the counts are exact. A
    duplicated id is a second row for an id that already exists."""

    status = ", CASE WHEN n % 3 = 0 THEN 'paid' ELSE 'open' END AS status"
    rows = (
        "SELECT CAST(n AS bigint) AS id, CAST(n % 17 AS bigint) AS customer_id, "  # noqa: S608
        f"CAST(n * 1.5e0 AS double) AS amount{status if with_status else ''} "
        f"FROM UNNEST(sequence(1, {_ROWS})) AS t(n)"
    )
    if duplicate_ids:
        rows += (
            "\nUNION ALL\n"
            "SELECT CAST(n AS bigint), CAST(n % 17 AS bigint), "
            f"CAST(n * 1.5e0 AS double){status if with_status else ''} "
            f"FROM UNNEST(sequence(1, {duplicate_ids})) AS t(n)"
        )
    return f"CREATE TABLE {relation} AS {rows}"


@pytest.fixture
def drift_schema(trino_url, trino_dev_user, monkeypatch):
    schema = f"dex_drift_{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("TRINO_URL", trino_url)
    monkeypatch.setenv("TRINO_USER", trino_dev_user)
    for name in ("TRINO_HOST", "TRINO_PASSWORD", "TRINO_JWT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    _admin(
        trino_url,
        f"CREATE SCHEMA memory.{schema}",
        _orders(f"memory.{schema}.orders", with_status=True),
    )
    yield schema
    _admin(
        trino_url,
        f"DROP TABLE IF EXISTS memory.{schema}.orders",
        f"DROP SCHEMA IF EXISTS memory.{schema}",
    )


def _dex(tmp_path: Path, capsys, *argv: str) -> dict:
    rc, envelope = run_cli(["--repo-root", str(tmp_path), *argv], capsys)
    assert rc == 0, envelope
    assert envelope["status"] == "ok", envelope
    return envelope


def _confirmed(*argv: str) -> tuple[str, ...]:
    return (*argv, "--confirm", "--budget", str(BUDGET))


def test_drift_behind_dex_is_detected_and_reconciled(
    tmp_path: Path, capsys, trino_url, drift_schema
):
    relation = f"memory.{drift_schema}.orders"
    seed_repo(tmp_path, catalogs=[f"memory.{drift_schema}"], budget=BUDGET)

    _dex(tmp_path, capsys, *_confirmed("explore", "map"))
    _dex(tmp_path, capsys, "transform", "init", "trshop", "--connector", "trino")
    planned = _dex(
        tmp_path,
        capsys,
        "transform",
        "plan",
        "--scaffold",
        relation,
        "scaffold stg_orders",
    )
    _dex(tmp_path, capsys, "transform", "apply", planned["data"]["plan_id"])

    # A hand edit since scaffolding, which reconcile has to leave alone (#713).
    staging = next((tmp_path / "trshop" / "models").rglob("stg_*.sql"))
    scaffolded = staging.read_text(encoding="utf-8")
    assert "        amount,\n" in scaffolded, scaffolded
    staging.write_text(
        scaffolded.replace(
            "        amount,\n", "        cast(amount as decimal(12,2)) as amount,\n"
        ),
        encoding="utf-8",
    )

    snapshot = _dex(tmp_path, capsys, "maintain", "snapshot")
    assert snapshot["connection"]["connector"] == "trino"

    # Behind dex's back: the status column is gone and five ids now repeat.
    _admin(
        trino_url,
        f"DROP TABLE {relation}",
        _orders(relation, with_status=False, duplicate_ids=5),
    )

    # Free first: the schema axis finds the drop and the scan is only offered.
    free = _dex(tmp_path, capsys, "maintain", "check")
    codes = {f["code"] for f in free["data"]["findings"]}
    assert "column_dropped" in codes, free["data"]["findings"]
    assert "grain" in free["data"]["offer"]["axes"], free["data"]

    # Confirmed: the grain axis re-measures the key the baseline proved unique.
    swept = _dex(tmp_path, capsys, *_confirmed("maintain", "check"))
    findings = swept["data"]["findings"]
    dropped = [f for f in findings if f["code"] == "column_dropped"]
    assert any(f["column"] == "status" for f in dropped), dropped
    lost = [f for f in findings if f["code"] == "key_lost_uniqueness"]
    assert any(f["column"] == "id" for f in lost), findings

    reconciled = _dex(tmp_path, capsys, "maintain", "reconcile")
    assert reconciled["data"]["proposal_count"] >= 1
    # The drop is mechanical against the scaffolded model, so a plan is stored
    # and nothing is applied: the staging model still selects the column.
    assert reconciled["data"]["plan_id"], reconciled["data"]
    assert "status" in staging.read_text(encoding="utf-8")

    # The edit is the dropped column's line and the comma it leaves behind,
    # never a regenerated model reverting the cast.
    relative = staging.relative_to(tmp_path / "trshop").as_posix()
    diff = next(d for d in reconciled["diffs"] if d["path"] == relative)
    # status is the last column, so the comma after amount goes with it: two
    # lines out, the cast back in without its comma, and nothing else.
    assert (diff["additions"], diff["deletions"]) == (1, 2), diff["unified"]
    _dex(tmp_path, capsys, "transform", "apply", reconciled["data"]["plan_id"])
    applied = staging.read_text(encoding="utf-8")
    assert "cast(amount as decimal(12,2)) as amount" in applied
    assert "status" not in applied
