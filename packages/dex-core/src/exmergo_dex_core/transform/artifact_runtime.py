"""Artifact-producer provenance and cross-runtime isolation (#862).

Dex's dbt runtime and a separately pinned project or CI runtime can share a
``target/`` directory. Incompatible dbt versions have left
``target/run/<project>/snapshots/<name>.sql`` as a directory that a later
version then opens as a file (``IsADirectoryError``). This module records who
produced the artifacts in sanitized evidence, and gives callers a synthetic
isolation helper for the collision case.

Centralizing every reader/writer onto one target-path policy needs maintainer
pairing; this first contribution only adds producer metadata and the
isolation/collision fixture.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Relative path under ``target/`` where the file-vs-directory collision has been
#: observed across dbt versions (snapshot compiled SQL).
_SNAPSHOT_RUN_GLOB = "run/*/snapshots"


@dataclass(frozen=True)
class ArtifactProducer:
    """Who wrote the artifacts this evidence is about.

    Paths are relative project labels only. Absolute home directories and
    credential-bearing strings are refused rather than redacted in place, so a
    caller cannot accidentally publish them by formatting this object.
    """

    dbt_version: str | None
    adapter_version: str | None
    project: str | None
    target: str | None
    artifact_directory: str
    invocation_id: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "dbt_version": self.dbt_version,
            "adapter_version": self.adapter_version,
            "project": self.project,
            "target": self.target,
            "artifact_directory": self.artifact_directory,
            "invocation_id": self.invocation_id,
        }


_UNSAFE_PATH = re.compile(r"(^|[/\\])(\.env|credentials|secrets)([/\\]|$)", re.I)


def sanitize_artifact_directory(path: Path | str, *, project_root: Path | str) -> str:
    """Return a path label safe for public evidence.

    Prefer a path relative to ``project_root``. Fall back to the final path
    component. Refuse credential-like segments.
    """

    root = Path(project_root).resolve()
    raw = Path(path)
    try:
        rel = raw.resolve().relative_to(root)
        label = rel.as_posix() or "target"
    except ValueError:
        label = raw.name or "target"
    if _UNSAFE_PATH.search(label) or label.startswith(".."):
        raise ValueError("artifact directory label is not publishable")
    return label


def artifact_producer(
    *,
    project_root: Path | str,
    target: str,
    artifact_directory: Path | str,
    metadata: dict[str, Any] | None = None,
    project_name: str | None = None,
    adapter_version: str | None = None,
) -> ArtifactProducer:
    """Build sanitized producer metadata from dbt run_results metadata + context."""

    meta = metadata or {}
    return ArtifactProducer(
        dbt_version=(str(meta["dbt_version"]) if meta.get("dbt_version") else None),
        adapter_version=(
            str(adapter_version)
            if adapter_version
            else (str(meta["adapter_version"]) if meta.get("adapter_version") else None)
        ),
        project=project_name,
        target=target,
        artifact_directory=sanitize_artifact_directory(
            artifact_directory, project_root=project_root
        ),
        invocation_id=(
            str(meta["invocation_id"]) if meta.get("invocation_id") else None
        ),
    )


@dataclass(frozen=True)
class CollisionFinding:
    """A path that one runtime left as a directory and another expects as a file."""

    path: str
    kind: str  # "directory_where_file_expected"


def find_snapshot_path_collisions(artifact_dir: Path | str) -> list[CollisionFinding]:
    """Detect snapshot compiled-SQL paths that are directories instead of files.

    Models the dbt 1.12.6 (directory) then 1.10.11 (file) collision without
    running either runtime.
    """

    root = Path(artifact_dir)
    if not root.is_dir():
        return []
    found: list[CollisionFinding] = []
    for snapshots_dir in root.glob(f"{_SNAPSHOT_RUN_GLOB}"):
        if not snapshots_dir.is_dir():
            continue
        # A compiled snapshot named ``foo.sql`` that is actually a directory.
        found.extend(
            CollisionFinding(
                path=child.as_posix(),
                kind="directory_where_file_expected",
            )
            for child in snapshots_dir.iterdir()
            if child.name.endswith(".sql") and child.is_dir()
        )
    return found


def runtime_artifact_dir(base: Path | str, runtime_key: str) -> Path:
    """Per-runtime artifact directory under ``base`` (no deletion of siblings)."""

    key = re.sub(r"[^A-Za-z0-9._-]+", "_", runtime_key.strip()) or "runtime"
    return Path(base) / f"dex-runtime-{key}"


class ArtifactCollisionError(ValueError):
    """Shared artifact dir has a cross-runtime file/directory collision."""


def resolve_artifact_dir(
    shared: Path | str,
    runtime_key: str,
    *,
    isolate_on_collision: bool = True,
) -> Path:
    """Choose a writable artifact directory for this runtime.

    Same-runtime reuse of ``shared`` is fine when no collision is present.
    On collision, either return an isolated sibling directory (default) or
    refuse. Never recursively deletes caller-owned artifacts.
    """

    shared_path = Path(shared)
    collisions = find_snapshot_path_collisions(shared_path)
    if not collisions:
        shared_path.mkdir(parents=True, exist_ok=True)
        return shared_path
    if not isolate_on_collision:
        names = ", ".join(c.path for c in collisions[:5])
        raise ArtifactCollisionError(
            "incompatible artifacts in shared directory "
            f"(directory where file expected: {names}); "
            "use an isolated artifact directory per runtime"
        )
    isolated = runtime_artifact_dir(shared_path.parent, runtime_key)
    isolated.mkdir(parents=True, exist_ok=True)
    return isolated
