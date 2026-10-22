"""Live `connect test` against Trino: connection discovery, the capabilities
envelope and the sanitizer end to end, plus what the server itself allows the
read-only identity, which is the half of read-only no dex guard can vouch for.

Free: capabilities and every assertion here are coordinator metadata reads or
statements the server refuses. The target is the seeded pair from
scripts/setup_trino_dev.sh (which CI runs too), connected as dex_ro.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cli import main

from .conftest import integration_budget, trino_client

pytestmark = [pytest.mark.integration, pytest.mark.trino]


def seed_repo(
    root: Path,
    *,
    catalogs: list[str] | None = None,
    budget: float | None = None,
    dev_catalog: str = "memory",
    max_full_profile_bytes: int | None = None,
) -> None:
    trino: dict = {"dev_catalog": dev_catalog, "dev_schema": "dbt_dev"}
    if catalogs is not None:
        trino["catalogs"] = catalogs
    if max_full_profile_bytes is not None:
        trino["max_full_profile_bytes"] = max_full_profile_bytes
    config: dict = {
        "connector": "trino",
        "trino": trino,
        "budget": integration_budget(budget),
    }
    (root / ".dex").mkdir(parents=True, exist_ok=True)
    (root / ".dex" / "config.yml").write_text(yaml.safe_dump(config), encoding="utf-8")


def run_cli(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one envelope line on stdout"
    return rc, json.loads(out)


def test_connect_test_reports_db_load_and_an_honest_session(
    tmp_path: Path, capsys, trino_url, monkeypatch
):
    monkeypatch.setenv("TRINO_URL", trino_url)
    seed_repo(tmp_path)
    rc, envelope = run_cli(["--repo-root", str(tmp_path), "connect", "test"], capsys)
    assert rc == 0, envelope
    data = envelope["data"]
    assert data["connector"] == "trino"
    assert data["dialect"] == "trino"
    assert data["read_only"] is True
    # Trino has no read-only session mode a client can hold; saying so is the
    # honest answer, and the access-control rule is what makes dex_ro read-only.
    assert data["session_read_only"] is False
    assert data["paradigm"] == "db_load"
    assert data["deployment"] == "self_hosted"
    assert data["server_version"]
    assert data["catalog_count"] >= 3
    assert envelope["cost"]["paradigm"] == "db_load"
    assert data["auth_method"] == "environment:dsn"
    assert "dex_ro@" not in json.dumps(envelope)


def test_a_url_naming_a_catalog_is_warned_and_ignored(
    tmp_path: Path, capsys, trino_url, monkeypatch
):
    """The client's own URL form carries a session catalog and schema; dex never
    sets either, so it says it ignored them instead of silently honoring one."""

    monkeypatch.setenv("TRINO_URL", trino_url.rstrip("/") + "/memory/app")
    seed_repo(tmp_path)
    rc, envelope = run_cli(["--repo-root", str(tmp_path), "connect", "test"], capsys)
    assert rc == 0, envelope
    # Capability warnings ride the probe's own payload, as on every connector.
    assert any(
        "never sets a session catalog" in w for w in envelope["data"]["warnings"]
    )


def test_a_bogus_scope_is_refused_for_free_and_names_what_exists(
    tmp_path: Path, capsys, trino_url, monkeypatch
):
    monkeypatch.setenv("TRINO_URL", trino_url)
    seed_repo(tmp_path, catalogs=["memory.nope"])
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "inventory"], capsys
    )
    assert rc == 1
    message = envelope["errors"][0]
    assert "catalog memory has no schema nope" in message
    assert "app" in message
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE memory.app.dex_write_probe AS SELECT 1 AS a",
        "INSERT INTO memory.app.signups VALUES (1, 'x', NULL)",
        "CREATE SCHEMA memory.dex_write_probe",
        "CREATE TABLE iceberg.app.dex_write_probe AS SELECT 1 AS a",
    ],
)
def test_the_server_refuses_writes_from_the_read_only_identity(trino_url, statement):
    """dex never issues these; this proves the cluster refuses them anyway, which
    is the access-control rule references/trino.md tells operators to set."""

    import trino.exceptions

    cursor = trino_client(trino_url).cursor()
    with pytest.raises(trino.exceptions.TrinoUserError) as exc:
        cursor.execute(statement)
        cursor.fetchall()
    assert exc.value.error_name == "PERMISSION_DENIED"


def test_the_read_only_identity_may_explain(trino_url):
    """The free estimate rests on EXPLAIN (TYPE IO) being allowed for an
    identity that may only read, and on its not executing anything."""

    cursor = trino_client(trino_url).cursor()
    cursor.execute(
        "EXPLAIN (TYPE IO, FORMAT JSON) SELECT orderkey FROM iceberg.app.orders"
    )
    plan = json.loads(cursor.fetchall()[0][0])
    (info,) = plan["inputTableColumnInfos"]
    assert info["table"]["catalog"] == "iceberg"
    assert float(info["estimate"]["outputSizeInBytes"]) > 0


def test_a_partial_name_fails_without_a_session_catalog(trino_url):
    """Why dex never sets one: with no session catalog the server itself refuses
    a partial name, so the relation the firewall approved can only be the one
    Trino reads."""

    import trino.exceptions

    cursor = trino_client(trino_url).cursor()
    with pytest.raises(trino.exceptions.TrinoUserError) as exc:
        cursor.execute("SELECT count(*) FROM app.orders")
        cursor.fetchall()
    assert exc.value.error_name == "MISSING_CATALOG_NAME"
