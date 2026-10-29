"""Cube's own drift fingerprint: the maintain snapshot channel.

Builds the neutral `SemanticLayerSnapshot` from the validated set, reusing the
catalog's resolution (:class:`.catalog.Resolution`) so whether a field links to
a physical column is decided once, whichever channel asks.

- Each cube is one semantic model: its dimensions and aggregating measures, each
  mapped to the column behind it or None, and its primary key.
- Each view is one semantic model with no relation, its members mapped to
  nothing: a view's columns are its cubes', and a dropped column is reported
  against the cube that reads it.
- Each measure, of a cube or of a view, is one metric named the way the catalog
  names it (``cube.member``), so a removed or rewritten one is its own finding.
- Each join is one relationship with every ordered pair and its arity, oriented
  child to parent.

**What the content hash covers.** The element as written, keys normalized to
snake_case, so respelling ``sqlTable`` as ``sql_table`` is not a change and
``meta`` is excluded: it is free-form and carries nothing a definition means.

A compiled-only file or element is not fingerprinted here: its definitions are
only Cube's compiler's to read, and the fingerprint says so in its notes rather
than leaving them to read as absent.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from ..maintain.snapshot import (
    MetricDef,
    RelationshipDef,
    SemanticLayerSnapshot,
    SemanticModelDef,
)
from . import catalog as catalog_mod
from .model import Cube, View
from .validator import canonical_key

if TYPE_CHECKING:
    from .loader import LoadResult

__all__ = ["semantic_layer"]


def semantic_layer(
    loaded: LoadResult, resolved: catalog_mod.Resolution
) -> SemanticLayerSnapshot:
    """Fingerprint the semantic layer from the validated set."""

    notes = loaded.notes()
    if loaded.compiled_only:
        notes.append(
            "compiled-only Cube definitions are not fingerprinted structurally, so "
            "a change inside them is not detected by this baseline"
        )
    if loaded.empty:
        return SemanticLayerSnapshot(**_provenance(), notes=notes)

    relations = {
        cube.name: relation
        for cube in loaded.cubes
        if (relation := resolved.relation(cube.name))
    }
    models: list[SemanticModelDef] = []
    metrics: list[MetricDef] = []
    relationships: list[RelationshipDef] = []
    for cube in loaded.cubes:
        if cube.dimensions or cube.measures or cube.joins:
            models.append(_cube_def(cube, resolved))
        metrics.extend(
            MetricDef(
                name=catalog_mod.member_token(cube.name, measure.name),
                path=cube.file,
                content_sha256=_hash(measure.raw),
            )
            for measure in cube.measures
        )
        for join in cube.joins:
            declared, note = catalog_mod.declared_relationship(
                cube, join, resolved, relations
            )
            if note:
                notes.append(note)
            if declared is not None:
                relationships.append(_relationship_def(cube, join, declared))
    for view in loaded.views:
        models.append(_view_def(view))
        metrics.extend(
            MetricDef(
                name=catalog_mod.member_token(view.name, exposed.name),
                path=view.file,
                content_sha256=_hash(
                    {"source": exposed.source, "definition": exposed.member.raw}
                ),
            )
            for exposed in view.members_of("measure")
        )
    return SemanticLayerSnapshot(
        semantic_models=models,
        metrics=metrics,
        relationships=relationships,
        relationships_and_keys_captured=True,
        relationship_cardinality_captured=True,
        **_provenance(),
        notes=notes,
    )


def _provenance() -> dict[str, Any]:
    """Which reader produced the hashes. Cube versions no model format, so the
    validator release is the pin."""

    from .dependency import CUBE_VALIDATOR_RELEASE

    return {"vendor": "cube", "vendor_release": CUBE_VALIDATOR_RELEASE}


def _cube_def(cube: Cube, resolved: catalog_mod.Resolution) -> SemanticModelDef:
    relation = resolved.relation(cube.name)
    dimensions: dict[str, str | None] = {}
    categorical: dict[str, str] = {}
    for member in cube.dimensions:
        column = resolved.column(cube.name, member.name)
        dimensions[member.name] = column
        if column and member.type != "time":
            categorical[member.name] = column
    measures = {
        member.name: resolved.column(cube.name, member.name)
        for member in cube.measures
        if catalog_mod._aggregating(member)
    }
    key = catalog_mod.declared_key_columns(cube, resolved)
    return SemanticModelDef(
        name=cube.name,
        path=cube.file,
        content_sha256=_hash(cube.raw),
        relation=relation,
        dimensions=dimensions,
        categorical_dimensions=categorical,
        measures=measures,
        keys=[key] if key else [],
    )


def _view_def(view: View) -> SemanticModelDef:
    return SemanticModelDef(
        name=view.name,
        path=view.file,
        content_sha256=_hash(view.raw),
        dimensions={m.name: None for m in view.members_of("dimension")},
    )


def _relationship_def(cube: Cube, join: Any, declared: Any) -> RelationshipDef:
    """One join, named by its endpoints and pairs, the identity drift diffs by."""

    name = declared.name or (
        f"{declared.model}->{declared.to_model}:"
        + ",".join(f"{frm}={to}" for frm, to in declared.column_pairs)
    )
    return RelationshipDef(
        name=name,
        path=cube.file,
        content_sha256=_hash({"declared_on": cube.name, **join.raw}),
        model=declared.model,
        to_model=declared.to_model,
        column_pairs=declared.column_pairs,
        # `declared_relationship` already turned a one_to_many join round so the
        # row reads child to parent; the arity turns with it.
        cardinality=(
            "many_to_one" if join.relationship == "one_to_many" else join.relationship
        ),
    )


def _hash(definition: Any) -> str:
    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                canonical_key(str(k)): strip(v)
                for k, v in value.items()
                if canonical_key(str(k)) != "meta"
            }
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    text = json.dumps(strip(definition), sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
