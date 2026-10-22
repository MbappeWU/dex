"""Values-free mode's plumbing (#591): who selects it, what refuses in it, and
what an error in it may say.

The guarantee itself (no sentinel value in any output) is the shipped
conformance suite's job, run in `test_values_free_conformance.py`. These are the
parts around it: the engine pin, the CLI flag and the host's environment pin, the
command allowlist, the code-only errors, and the repository config that can
never select the mode.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core import envelope as env
from exmergo_dex_core.cli import RESULT_MODE_ENV, main
from exmergo_dex_core.config import DexConfig
from exmergo_dex_core.engine import _VALUES_FREE_METHODS, DexEngine
from exmergo_dex_core.errors import (
    ConfigurationError,
    RequestError,
    StatementStage,
    WarehouseQueryError,
)
from exmergo_dex_core.values_free import (
    ALLOWED_COMMANDS,
    ResultMode,
    ValuesFreeCode,
    ValuesFreeError,
    call_withholding,
    withhold_error,
)


def engine_for(path: Path, result_mode: str = "values_free") -> DexEngine:
    return DexEngine(
        connector="duckdb",
        path=str(path),
        config=DexConfig(connector="duckdb"),
        result_mode=result_mode,
    )


def run(argv: list[str], capsys) -> tuple[int, dict]:
    code = main(argv)
    return code, json.loads(capsys.readouterr().out)


@pytest.fixture
def repo(duckdb_file: Path, tmp_path: Path, monkeypatch) -> Path:
    """A repo whose committed config points at the shared DuckDB fixture, with
    the working directory inside it, and no host pin in the environment."""

    root = tmp_path / "repo"
    (root / ".dex").mkdir(parents=True)
    (root / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {duckdb_file}\n", encoding="utf-8"
    )
    monkeypatch.chdir(root)
    monkeypatch.delenv(RESULT_MODE_ENV, raising=False)
    return root


# --- the engine ----------------------------------------------------------------


def test_an_engine_defaults_to_full_results(duckdb_file: Path):
    assert engine_for(duckdb_file, "full").result_mode is ResultMode.FULL
    assert DexEngine(connector="duckdb").result_mode is ResultMode.FULL


def test_an_unknown_mode_is_refused_by_name():
    with pytest.raises(ConfigurationError, match="unknown result mode 'none'"):
        DexEngine(connector="duckdb", result_mode="none")


def test_a_pinned_engine_refuses_a_call_asking_for_full_results(duckdb_file: Path):
    engine = engine_for(duckdb_file)

    with pytest.raises(ValuesFreeError) as refused:
        engine.inventory(result_mode="full")

    assert refused.value.code is ValuesFreeCode.RESULT_MODE_PINNED
    assert env.reason_for(refused.value) is env.Reason.GUARD


def test_a_full_engine_lets_one_call_opt_in(duckdb_file: Path):
    engine = engine_for(duckdb_file, "full")

    opted_in = engine.inventory(result_mode="values_free")
    plain = engine.inventory()

    assert opted_in.result_mode is ResultMode.VALUES_FREE
    assert plain.result_mode is ResultMode.FULL
    assert "result_mode" not in env_data(plain)


def env_data(result) -> dict:
    from exmergo_dex_core.results import to_envelope

    return to_envelope(result).data


def test_every_unreviewed_engine_method_refuses_before_opening_anything(
    duckdb_file: Path, monkeypatch
):
    """The allowlist is the safety property: a method nobody reviewed for the
    mode refuses, and it refuses before a connection could be opened, so a
    method added later is refused until it is reviewed."""

    def no_connection(*_args, **_kwargs):
        raise AssertionError("a refused command opened a connection")

    monkeypatch.setattr(DexEngine, "_adapter", no_connection)
    engine = engine_for(duckdb_file)
    unreviewed = [
        name
        for name, member in vars(DexEngine).items()
        if not name.startswith("_")
        and callable(member)
        and not isinstance(member, (classmethod, staticmethod))
        and name not in _VALUES_FREE_METHODS
    ]

    assert {"cluster", "semantic_values", "semantic_query", "check", "build"} <= set(
        unreviewed
    )
    for name in unreviewed:
        with pytest.raises(ValuesFreeError) as refused:
            getattr(engine, name)()
        assert refused.value.code is ValuesFreeCode.COMMAND_NOT_VALUES_FREE, name


def test_an_unreviewed_method_still_works_on_a_full_engine(duckdb_file: Path):
    assert engine_for(duckdb_file, "full").plans() is not None


def test_from_repo_never_reads_the_mode_from_config(repo: Path):
    (repo / ".dex" / "config.yml").write_text(
        (repo / ".dex" / "config.yml").read_text(encoding="utf-8")
        + "result_mode: values_free\n",
        encoding="utf-8",
    )

    engine = DexEngine.from_repo(repo)

    assert engine.result_mode is ResultMode.FULL
    assert any("selected by the caller" in w for w in engine.config.load_warnings)
    assert DexEngine.from_repo(repo, result_mode="values_free").result_mode is (
        ResultMode.VALUES_FREE
    )


# --- errors --------------------------------------------------------------------


def test_a_warehouse_refusal_keeps_its_type_and_loses_the_servers_words():
    original = WarehouseQueryError(
        "Could not convert string 'zq7x' to INT32",
        reading="db.main.t",
        stage=StatementStage.RUN,
    )

    withheld = withhold_error(original)

    assert isinstance(withheld, WarehouseQueryError)
    assert "zq7x" not in str(withheld)
    assert withheld.reading == "db.main.t"
    assert withheld.stage is StatementStage.RUN
    envelope = env.error_for(withheld)
    assert envelope.reason is env.Reason.EXECUTION_FAILURE
    assert envelope.data == {"stage": "run", "code": "warehouse_refused"}


def test_an_exception_dex_did_not_raise_becomes_a_code_with_no_text():
    withheld = withhold_error(RuntimeError("Conversion Error: 'zq7x'"))

    assert isinstance(withheld, ValuesFreeError)
    assert withheld.code is ValuesFreeCode.UNEXPECTED_ERROR
    assert "zq7x" not in str(withheld)
    assert env.reason_for(withheld) is env.Reason.INTERNAL


def test_a_refusal_dex_wrote_keeps_its_words_and_gains_a_code():
    refusal = RequestError("no object named 'orders' in this connection")

    withheld = withhold_error(refusal)

    assert withheld is refusal
    assert env.error_for(withheld).data["code"] == "request"


def _fails() -> None:
    raise RuntimeError("value 'zq7x'")


def test_the_replacement_is_raised_without_the_original_attached():
    with pytest.raises(ValuesFreeError) as raised:
        call_withholding(ResultMode.VALUES_FREE, _fails)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_outside_the_mode_an_error_keeps_every_word():
    with pytest.raises(RuntimeError, match="zq7x"):
        call_withholding(ResultMode.FULL, _fails)

    assert "code" not in env.error_for(RuntimeError("x")).data


# --- the CLI -------------------------------------------------------------------


def test_the_flag_stamps_every_envelope(repo: Path, capsys):
    code, ok = run(["explore", "inventory", "--values-free"], capsys)
    assert code == 0
    assert ok["data"]["result_mode"] == "values_free"

    code, refused = run(
        ["explore", "query", "--values-free", "select email from customers"], capsys
    )
    assert code == 1
    assert refused["data"]["result_mode"] == "values_free"


def test_without_the_mode_nothing_is_stamped(repo: Path, capsys):
    _code, ok = run(["explore", "inventory"], capsys)

    assert "result_mode" not in ok["data"]


def test_the_host_pin_cannot_be_undone_from_the_command_line(
    repo: Path, capsys, monkeypatch
):
    monkeypatch.setenv(RESULT_MODE_ENV, "values_free")

    _code, profiled = run(["explore", "profile", "orders"], capsys)

    assert profiled["data"]["result_mode"] == "values_free"
    for column in profiled["data"]["datasets"][0]["columns"]:
        assert "min_value" not in column


def test_an_unknown_host_pin_refuses_rather_than_reading_as_full(
    repo: Path, capsys, monkeypatch
):
    monkeypatch.setenv(RESULT_MODE_ENV, "nope")

    code, refused = run(["explore", "inventory"], capsys)

    assert code == 1
    assert refused["reason"] == "configuration"
    assert "unknown result mode" in refused["errors"][0]


@pytest.mark.parametrize(
    "argv",
    [
        ["maintain", "check"],
        ["explore", "cluster", "orders"],
        ["explore", "semantic", "values", "tier"],
        ["transform", "plans"],
        ["config", "set", "query.max_rows", "10"],
        ["demo", "elsewhere.duckdb"],
    ],
)
def test_a_command_outside_the_allowlist_refuses(repo: Path, capsys, argv):
    code, refused = run([*argv, "--values-free"], capsys)

    assert code == 1
    assert refused["reason"] == "guard"
    assert refused["data"]["code"] == "command_not_values_free"
    assert refused["data"]["result_mode"] == "values_free"


def test_the_allowlist_names_only_commands_the_cli_has(repo: Path, capsys):
    from exmergo_dex_core.cli import _build_parser

    parser = _build_parser()
    for command in ALLOWED_COMMANDS:
        words = command.split()
        if words[:2] == ["explore", "semantic"]:
            words = words[:2]
        with pytest.raises(SystemExit) as shown:
            parser.parse_args([*words, "--help"])
        assert shown.value.code == 0
    capsys.readouterr()


def test_config_get_and_validate_still_answer(repo: Path, capsys):
    code, got = run(["config", "get", "connector", "--values-free"], capsys)
    assert code == 0
    assert got["data"]["result_mode"] == "values_free"

    code, _validated = run(["config", "validate", "--values-free"], capsys)
    assert code == 0


def test_config_validate_names_a_mode_key_as_one_the_file_cannot_hold(
    repo: Path, capsys
):
    config = repo / ".dex" / "config.yml"
    config.write_text(
        config.read_text(encoding="utf-8") + "result_mode: values_free\n",
        encoding="utf-8",
    )

    code, validated = run(["config", "validate"], capsys)

    assert code == 1
    (problem,) = validated["data"]["problems"]
    assert problem["kind"] == "caller_only_key"
    assert problem["key"] == "result_mode"
    assert problem["fatal"] is False


def test_config_set_refuses_to_write_a_mode_key(repo: Path, capsys):
    code, refused = run(["config", "set", "result_mode", "values_free"], capsys)

    assert code == 1
    assert "selected by the caller" in refused["errors"][0]
    assert "result_mode" not in (repo / ".dex" / "config.yml").read_text("utf-8")


def test_a_refused_query_reports_its_code(repo: Path, capsys):
    run(["explore", "profile", "customers", "orders", "--values-free"], capsys)

    _code, refused = run(
        [
            "explore",
            "query",
            "--values-free",
            "select status, count(*) from orders group by status",
        ],
        capsys,
    )

    assert refused["data"]["code"] == "group_by_column"
    assert refused["reason"] == "guard"


def test_a_batch_reports_a_code_per_statement_only_in_the_mode(repo: Path, capsys):
    run(["explore", "profile", "customers", "orders"], capsys)
    batch = ["select count(*) from orders", "select email from customers"]

    _code, moded = run(["explore", "query", "--values-free", *batch], capsys)
    _code, full = run(["explore", "query", *batch], capsys)

    assert [r.get("code") for r in moded["data"]["results"]] == [None, "bare_column"]
    assert all("code" not in r for r in full["data"]["results"])
