"""The PII gate on the columns a metric query reads without naming them (#698).

The request-gate screens the tokens a caller names in `--group-by` and the ones
a `--where` reads. Three things reach the result without being one of those
tokens, or reach the gate as a token no column sits behind, so the profiled
flag was never consulted and only the name heuristic stood in the way:

- `metric_time`, which buckets each measure's agg time dimension;
- a token carrying a grain suffix, which no column is named;
- a dimension defined as an expression, which maps to no single column.

These hold the local MetricFlow backend to screening the columns behind each,
and every backend to saying when only the name screened a dimension. None of
them needs MetricFlow: the gate runs before anything renders.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fakes.semantic import FakeHostedBackend, table_json_result

from exmergo_dex_core import dbt_project as dbt_project_module
from exmergo_dex_core.adapters.project import DbtProject
from exmergo_dex_core.cache import (
    ColumnProfile,
    Dataset,
    DexCache,
    PIICategory,
    PIIFlag,
)
from exmergo_dex_core.config import DexConfig, QueryLimits
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.explore.semantic import (
    SemanticQuery,
    SemanticQueryRefusedError,
    screen_dimension_refs,
    unadjudicated_refs,
)
from exmergo_dex_core.explore.semantic.governed import expression_columns
from exmergo_dex_core.explore.semantic.local import LocalMetricFlowBackend
from exmergo_dex_core.storage import MemoryStore

RELATION = "wh.main.patients"


class _Layer(DbtProject):
    def __init__(self, root: Path, project: Path) -> None:
        super().__init__(root, project)

    def semantic_catalog(self):
        return dbt_project_module.semantic_catalog(
            self.project_dir, resolve_paths=lambda _text: None
        )


def _manifest(tmp_path: Path) -> Path:
    """One semantic model whose time axis and two computed dimensions are named
    nothing like PII and sit on columns that are."""

    project = tmp_path / "proj"
    (project / "target").mkdir(parents=True)
    manifest = {
        "semantic_models": [
            {
                "name": "patients",
                "node_relation": {"alias": "patients", "relation_name": RELATION},
                "defaults": {"agg_time_dimension": "event_day"},
                "entities": [
                    {"name": "patient", "type": "primary", "expr": "patient_id"}
                ],
                "dimensions": [
                    {
                        "name": "event_day",
                        "type": "time",
                        "expr": "dob_col",
                        "type_params": {"time_granularity": "day"},
                    },
                    {
                        "name": "contact_key",
                        "type": "categorical",
                        "expr": "upper(handle)",
                    },
                    {
                        "name": "mail_key",
                        "type": "categorical",
                        "expr": "lower(email_address)",
                    },
                    {
                        "name": "cohort",
                        "type": "categorical",
                        "expr": "case when status = 'a' then 'x' else 'y' end",
                    },
                    {"name": "macro_key", "type": "categorical", "expr": "{{ k() }}"},
                    {"name": "status", "type": "categorical"},
                ],
                "measures": [
                    {"name": "patient_count", "agg": "count", "expr": "patient_id"}
                ],
            }
        ],
        "metrics": [
            {
                "name": "patients",
                "type": "simple",
                "type_params": {"input_measures": [{"name": "patient_count"}]},
            }
        ],
    }
    (project / "target" / "semantic_manifest.json").write_text(json.dumps(manifest))
    return project


def _flag(category: PIICategory) -> PIIFlag:
    return PIIFlag(category=category, confidence=0.9)


def _cache(*, status_profiled: bool = True) -> DexCache:
    columns = [
        ColumnProfile(name="patient_id", data_type="INTEGER"),
        ColumnProfile(name="dob_col", data_type="DATE", pii=_flag(PIICategory.DOB)),
        ColumnProfile(name="handle", data_type="VARCHAR", pii=_flag(PIICategory.EMAIL)),
    ]
    if status_profiled:
        columns.append(ColumnProfile(name="status", data_type="VARCHAR"))
    return DexCache(datasets=[Dataset(identifier=RELATION, columns=columns)])


def _backend(tmp_path: Path, cache: DexCache | None) -> LocalMetricFlowBackend:
    store = MemoryStore()
    if cache is not None:
        store.save_cache(cache)
    project = _manifest(tmp_path)
    return LocalMetricFlowBackend(
        project,
        DexEngine(config=DexConfig(), store=store),
        "duckdb",
        QueryLimits(),
        _Layer(project.parent, project),
    )


def _never_rendered(*_a, **_k):
    raise AssertionError("rendered a query the gate should have refused")


# --- the time axis -----------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        SemanticQuery(metrics=["patients"], group_by=["metric_time__day"]),
        SemanticQuery(metrics=["patients"], group_by=["metric_time"], grain="day"),
        SemanticQuery(
            metrics=["patients"],
            where=["{{ TimeDimension('metric_time', 'day') }} > '2020-01-01'"],
        ),
    ],
    ids=["grain-in-token", "grain-flag", "filter"],
)
def test_metric_time_over_a_flagged_column_is_refused(tmp_path, monkeypatch, query):
    backend = _backend(tmp_path, _cache())
    monkeypatch.setattr(backend, "_render", _never_rendered)

    with pytest.raises(SemanticQueryRefusedError) as refused:
        backend.query(query)

    message = str(refused.value)
    assert "metric_time" in message
    assert "date_of_birth (profiled and flagged)" in message
    assert "as patient__event_day" in message


def test_metric_time_resolves_to_each_measures_time_dimension(tmp_path):
    backend = _backend(tmp_path, None)

    assert backend.time_axis_refs(["patients"]) == ["patient__event_day"]
    assert backend.time_axis_refs(["no_such_metric"]) is None


def test_a_grain_suffix_is_screened_as_the_dimension_it_buckets(tmp_path, monkeypatch):
    backend = _backend(tmp_path, _cache())
    monkeypatch.setattr(backend, "_render", _never_rendered)

    with pytest.raises(SemanticQueryRefusedError, match="profiled and flagged"):
        backend.query(
            SemanticQuery(metrics=["patients"], group_by=["patient__event_day__month"])
        )


# --- computed dimensions -----------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        SemanticQuery(metrics=["patients"], group_by=["patient__contact_key"]),
        SemanticQuery(
            metrics=["patients"],
            where=["{{ Dimension('patient__contact_key') }} = 'X'"],
        ),
    ],
    ids=["group-by", "where"],
)
def test_a_computed_dimension_over_a_flagged_column_is_refused(
    tmp_path, monkeypatch, query
):
    """`upper(handle)` reads a column the profile flagged; its token reads clean."""

    backend = _backend(tmp_path, _cache())
    monkeypatch.setattr(backend, "_render", _never_rendered)

    with pytest.raises(SemanticQueryRefusedError) as refused:
        backend.query(query)

    assert "email (profiled and flagged, read from handle)" in str(refused.value)


@pytest.mark.parametrize(
    "query",
    [
        SemanticQuery(metrics=["patients"], group_by=["patient__mail_key"]),
        SemanticQuery(
            metrics=["patients"],
            where=["{{ Dimension('patient__mail_key') }} = 'x'"],
        ),
    ],
    ids=["group-by", "where"],
)
def test_without_a_profile_the_columns_an_expression_reads_are_screened_by_name(
    tmp_path, monkeypatch, query
):
    backend = _backend(tmp_path, None)
    monkeypatch.setattr(backend, "_render", _never_rendered)

    with pytest.raises(SemanticQueryRefusedError, match="read from email_address"):
        backend.query(query)


def test_an_expression_dex_cannot_read_is_refused(tmp_path, monkeypatch):
    backend = _backend(tmp_path, _cache())
    monkeypatch.setattr(backend, "_render", _never_rendered)

    with pytest.raises(SemanticQueryRefusedError, match="could not read"):
        backend.query(
            SemanticQuery(metrics=["patients"], group_by=["patient__macro_key"])
        )


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("upper(handle)", ["handle"]),
        ("coalesce(t.a, b)", ["a", "b"]),
        ("date_trunc('day', dob_col)", ["dob_col"]),
        ("'constant'", []),
        ("{{ k() }}", None),
        ("{handle}", None),
        ("(select email from users limit 1)", None),
        ("case when then end", None),
    ],
)
def test_expression_columns_reads_or_declines(expression, expected):
    assert expression_columns(expression, "duckdb") == expected


# --- disclosure --------------------------------------------------------------------


def _captured_notes(backend, monkeypatch, query: SemanticQuery) -> list[str]:
    captured: dict = {}

    def run(_sql, _cache, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(backend, "_render", lambda _q: "SELECT 1")
    monkeypatch.setattr(backend._governed, "run", run)
    backend.query(query)
    return captured.get("extra_notes") or []


def test_a_query_names_each_dimension_screened_by_name_alone(tmp_path, monkeypatch):
    backend = _backend(tmp_path, _cache(status_profiled=False))

    notes = _captured_notes(
        backend,
        monkeypatch,
        SemanticQuery(metrics=["patients"], group_by=["patient__status"]),
    )

    assert any("name heuristic alone for patient__status" in note for note in notes)


def test_a_dimension_the_profile_cleared_is_not_disclosed(tmp_path, monkeypatch):
    backend = _backend(tmp_path, _cache())

    notes = _captured_notes(
        backend,
        monkeypatch,
        SemanticQuery(
            metrics=["patients"], group_by=["patient__status", "patient__cohort"]
        ),
    )

    assert not any("name heuristic alone" in note for note in notes)


def test_hosted_metric_time_is_disclosed_as_screened_by_name():
    """The hosted backend's stated position: it cannot resolve `metric_time`, so
    the token is screened by name and the result says so."""

    backend = FakeHostedBackend(
        dimensions_meta={"patients": []},
        result=table_json_result(["patients"], ["number"], [[5.0]]),
    )

    assert backend.time_axis_refs(["patients"]) is None
    result = backend.query(
        SemanticQuery(metrics=["patients"], group_by=["metric_time__day"])
    )

    assert any(
        "name heuristic alone" in note and "metric_time__day" in note
        for note in result.notes
    )


# --- the shared policy -------------------------------------------------------------


def test_an_alias_is_screened_in_place_of_the_token_and_named_by_it():
    def lookup(ref):
        return {"pii": True, "category": "email"} if ref == "u__contact" else None

    blocked = screen_dimension_refs(
        ["metric_time"], meta_lookup=lookup, aliases={"metric_time": ["u__contact"]}
    )

    assert blocked == [("metric_time", "email (profiled and flagged), as u__contact")]


def test_an_alias_is_adjudicated_only_where_every_target_is():
    verdicts = {"a": {"pii": False}, "b": None}

    unknown = unadjudicated_refs(
        ["metric_time"], meta_lookup=verdicts.get, aliases={"metric_time": ["a", "b"]}
    )

    assert unknown == ["metric_time"]
