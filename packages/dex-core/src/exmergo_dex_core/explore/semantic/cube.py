"""The local Cube layer: the Cube model files in this repository, read.

The reading is the semantic source's job and reaches this layer through the
engine's semantic-source seam. What this layer adds is provenance, the declared
gaps, and the two governed entry points a later release fills in.

**The catalog needs no Cube server.** ``explore semantic list`` reads the
repository's plain-YAML cubes and views, statically, and the declaration channel
carries their primary keys and joins to ``explore relationships|map
--use-project``.

**Querying is not available yet.** Cube has no Python SQL renderer: its planner
ships only inside a Cube server, which renders a query over ``/v1/sql``. The
governed path (Cube renders, dex executes under the cost guard) is the next
release's. Until then ``query`` refuses by name, and ``values`` runs the values
PII gate first, so a PII dimension is refused as PII, before it is refused for
want of a renderer: the order the governed path will keep.
"""

from __future__ import annotations

from typing import Any

from ...config import QueryLimits
from ...errors import ProjectError
from .backend import EXECUTION_DEX, BackendDescriptor, SemanticBackendError
from .catalog import SemanticCatalog
from .policy import resolve_values_request, screen_values_request

# Fields of the neutral catalog Cube has nowhere to put, per element kind.
#
# Cube declares no entities: its primary keys and joins reach dex through the
# declaration channel instead. A cube sits on a relation, not on a dbt model,
# and names no primary entity because it has none.
CUBE_CATALOG_GAPS: dict[str, list[str]] = {
    "semantic_models": ["model_ref", "primary_entity"],
    "entities": ["name", "type", "label", "description", "roles"],
}

_NO_RENDERER = (
    "Cube has no SQL renderer outside a Cube server, so governed Cube queries "
    "(Cube renders, dex executes under the cost guard) are not available in "
    "this release"
)


class LocalCubeLayer:
    """The Cube model files committed to this repository."""

    name = "local"
    vendor = "cube"
    deployment = "local"
    execution = EXECUTION_DEX
    catalog_gaps = CUBE_CATALOG_GAPS
    descriptor = BackendDescriptor(
        name=name,
        vendor=vendor,
        deployment=deployment,
        execution=execution,
        catalog_gaps=CUBE_CATALOG_GAPS,
    )

    def __init__(self, source: Any, engine: Any = None) -> None:
        self._source = source
        # Optional: reading the catalog needs no engine. With one, the values
        # gate reads the profiled cache rather than the name alone.
        self._dex = engine
        self._view: Any = None

    @classmethod
    def from_engine(cls, engine: Any) -> LocalCubeLayer:
        """Build through the engine's semantic-source seam, never a project."""

        if engine.repo_root is None:
            raise SemanticBackendError(
                "reading Cube models needs a repository to read them from: they "
                "are git-reviewable files, so build the engine with "
                "DexEngine.from_repo(repo_root)"
            )
        return cls(engine.semantic_catalog_source(), engine)

    # ---- discovery ---------------------------------------------------------

    def list_definitions(self) -> SemanticCatalog:
        return SemanticCatalog.from_view(self._catalog_view(), self)

    def declared_relationships(self) -> list[Any]:
        """Cube's joins, with every ordered column pair."""

        return self._source.declared_definitions().declared_relationships

    def declared_keys(self) -> tuple[list[Any], list[Any]]:
        """Cube's primary keys. Cube is never the transformation project, so
        this is the only route its keys have to grain detection."""

        defs = self._source.declared_definitions()
        return defs.declared_keys, defs.declared_composite_keys

    def _catalog_view(self):
        if self._view is None:
            try:
                self._view = self._source.semantic_catalog()
            except ProjectError as exc:
                raise SemanticBackendError(str(exc)) from exc
        return self._view

    # ---- query -------------------------------------------------------------

    def filter_refs(self, _clauses: list[str]) -> list[str] | None:
        """None: Cube's JSON filter dialect is not read yet, so a filtered query
        is refused rather than screened by half."""

        return None

    def time_axis_refs(self, metrics: list[str]) -> list[str] | None:
        """The ``cube.dimension`` tokens a time grouping on these metrics buckets.

        A metric names a time axis only where its cube has exactly one time
        dimension; None otherwise, which leaves the gate on the name heuristic.
        """

        from ...semantic_catalog import time_axis_tokens

        try:
            return time_axis_tokens(self._catalog_view(), metrics)
        except SemanticBackendError:
            return None

    def query(self, _q: Any) -> Any:
        raise SemanticBackendError(
            f"{_NO_RENDERER}. Read the layer with `explore semantic list`, then "
            "query the relations it names with `explore query`, which runs under "
            "the firewall and the cost guard"
        )

    def values(self, dimension: str, metrics: list[str]) -> Any:
        """Refused, after the values gate: a PII dimension is refused as PII."""

        from ...cube.validator import PREDEFINED_GRANULARITIES

        view = self._catalog_view()
        request = resolve_values_request(
            view,
            dimension,
            metrics,
            grains=PREDEFINED_GRANULARITIES,
            spelling="named cube.member (orders.status)",
        )
        screen_values_request(request.name, meta_lookup=self._pii_lookup())
        raise SemanticBackendError(
            f"{_NO_RENDERER}, and reading a dimension's values is one of them. "
            f"{request.token} sits on a column the catalog names: profile that "
            "relation with `explore profile` instead"
        )

    def _pii_lookup(self):
        """The cache-backed PII lookup when an engine is present, else None (the
        name heuristic alone, which still refuses an obviously personal name)."""

        if self._dex is None:
            return None
        from .governed import GovernedSql

        governed = GovernedSql(
            self._dex,
            self._dex.connector or getattr(self._dex.config, "connector", "") or "",
            getattr(self._dex.config, "query", None) or QueryLimits(),
            lambda: dict(self._catalog_view().physical_columns),
            recompile_hint="correct the Cube model's `sql_table`",
            computed_columns=lambda: dict(self._catalog_view().computed_columns),
        )
        return governed.pii_lookup(governed.load_cache())
