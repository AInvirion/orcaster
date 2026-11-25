"""Collection of the artifacts produced by the task.

The worker exposes ``ORCASTER_ARTIFACTS_DIR`` (a dedicated folder under
``ORCASTER_CACHE_DIR``); the task writes the files it produces there. After the
subprocess, the worker collects them (include/exclude glob over relative paths,
sha256 + size) and uploads them to the server in multipart batches.

This is pure logic (filesystem + hashing), with no HTTP, so it can be tested in
isolation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any


@dataclass(frozen=True)
class ArtifactsSpec:
    """Declaration of the payload ``artifacts`` block.

    ``enabled`` distinguishes "no block" (backward-compatible, nothing is
    collected) from "block present but empty" (collected with defaults)."""

    enabled: bool
    include: list[str] = field(default_factory=lambda: ["**/*"])
    exclude: list[str] = field(default_factory=list)
    required: bool = False

    @classmethod
    def from_payload(cls, payload: Any) -> ArtifactsSpec:
        if not isinstance(payload, dict) or "artifacts" not in payload:
            return cls(enabled=False)
        block = payload.get("artifacts")
        if not isinstance(block, dict):
            return cls(enabled=False)
        include = block.get("include")
        exclude = block.get("exclude")
        return cls(
            enabled=True,
            include=list(include) if isinstance(include, list) and include else ["**/*"],
            exclude=list(exclude) if isinstance(exclude, list) else [],
            required=bool(block.get("required", False)),
        )


@dataclass(frozen=True)
class ArtifactFile:
    """A collected file, ready to upload."""

    rel_path: str  # POSIX, relative to ORCASTER_ARTIFACTS_DIR
    abs_path: Path
    size_bytes: int
    sha256: str


# Read chunk size when hashing (bounds memory with large files).
_HASH_CHUNK = 1024 * 1024


def _matches(rel_path: str, patterns: list[str]) -> bool:
    """Does rel_path match any glob? Standard POSIX semantics via
    :meth:`PurePosixPath.full_match` (Python 3.13+):

    - ``**`` matches zero or more path segments (``**/*.json`` matches both
      ``a.json`` at the root and ``sub/b.json``).
    - ``*`` matches within ONE segment only and does NOT cross ``/`` (``*.json``
      matches only files at the root; ``sub/*`` only direct children of
      ``sub``). This avoids the ``fnmatch`` footgun (where ``*`` crosses ``/``)
      and makes include/exclude behave like a shell.
    """
    p = PurePosixPath(rel_path)
    for pat in patterns:
        if pat in ("**/*", "**"):
            return True
        if p.full_match(pat):
            return True
    return False


def _sha256_and_size(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while chunk := f.read(_HASH_CHUNK):
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


def collect_artifacts(
    artifacts_dir: Path,
    *,
    include: list[str],
    exclude: list[str],
) -> list[ArtifactFile]:
    """Collect the files under ``artifacts_dir`` that match include and not
    exclude (exclude wins). Paths are POSIX-relative; subdirectories are
    preserved. Computes sha256 + size per file.

    Missing dir → empty list. Directories and symlinks are ignored (regular
    files only): following a symlink would upload bytes from outside the
    artifacts dir, which is not the ORCASTER_ARTIFACTS_DIR contract."""
    if not artifacts_dir.is_dir():
        return []
    out: list[ArtifactFile] = []
    for abs_path in sorted(artifacts_dir.rglob("*")):
        if abs_path.is_symlink() or not abs_path.is_file():
            continue
        rel = PurePosixPath(abs_path.relative_to(artifacts_dir).as_posix())
        rel_str = str(rel)
        if not _matches(rel_str, include):
            continue
        if exclude and _matches(rel_str, exclude):
            continue
        sha256, size = _sha256_and_size(abs_path)
        out.append(
            ArtifactFile(rel_path=rel_str, abs_path=abs_path, size_bytes=size, sha256=sha256)
        )
    return out


def plan_batches(files: list[ArtifactFile], *, max_request_bytes: int) -> list[list[ArtifactFile]]:
    """Group files into batches bounded by ``max_request_bytes``.

    A file that alone exceeds the limit goes in its own batch (it is not
    split: a single large POST is preferable to splitting a file). Input order
    is preserved."""
    batches: list[list[ArtifactFile]] = []
    current: list[ArtifactFile] = []
    current_bytes = 0
    for f in files:
        if current and current_bytes + f.size_bytes > max_request_bytes:
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(f)
        current_bytes += f.size_bytes
        if f.size_bytes > max_request_bytes:
            batches.append(current)
            current = []
            current_bytes = 0
    if current:
        batches.append(current)
    return batches
