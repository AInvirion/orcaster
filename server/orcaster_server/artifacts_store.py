"""Filesystem storage of artifact bytes.

The bytes live in ``ARTIFACTS_ROOT/<execution_id>/<rel_path>``; the metadata
(``task_artifacts`` table) is handled separately in the endpoint. This layer is
pure filesystem I/O, with no DB or HTTP, so it can be tested in isolation.

Security: ``rel_path`` comes from the worker (filename of the multipart part). It
is validated against path traversal BEFORE touching disk: a ``..`` or an absolute
prefix would escape the execution's dir. Only POSIX relative paths with simple
components are accepted.

``execution_id`` is used as a path component WITHOUT validation here: it must
always be a trusted identifier, either a UUID resolved in the DB (upload endpoint)
or already normalized to ``str(UUID(...))`` at the edge (admin endpoints).
Never pass an ``execution_id`` that comes directly from unvalidated input.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Chunk size when hashing/writing from a bytes Iterator.
_WRITE_CHUNK = 1024 * 1024


class InvalidRelPath(ValueError):
    """Malformed rel_path or path traversal attempt."""


class ArtifactTooLarge(Exception):
    """The file exceeds the configured per-file limit.

    Raised from ``write_artifact_bytes`` when ``max_bytes`` is not ``None`` and the
    accumulated size of the chunks exceeds that value. At that moment the write is
    aborted immediately without materializing the whole file; the ``.tmp``
    temporary was already cleaned up before raising.
    """


class NoSpaceLeft(Exception):
    """ENOSPC while writing an artifact: disk full during materialization.

    Mapped to 507 in the endpoint. The staging phase guarantees that no
    ``rename`` has been applied yet, so aborting leaves zero changes in the
    final paths."""


@dataclass
class StagedArtifact:
    """An artifact written to ``.tmp`` pending ``rename`` to the destination.

    ``needs_promote`` is ``False`` when the identical bytes were already persisted
    (idempotency by sha): there is nothing to rename.
    """

    rel_path: str
    target: Path
    tmp_path: Path
    sha256: str
    size_bytes: int
    needs_promote: bool


def validate_rel_path(rel_path: str) -> str:
    """Validate and normalize a safe relative POSIX rel_path.

    Rejects: empty, ``/`` prefix (absolute), ``..``/``.`` components, empty
    components (``a//b``), backslashes (Windows paths) and control characters
    (NUL/``\\x00`` and the rest of ``< 0x20`` + ``\\x7f``). Returns the path
    unchanged if valid.

    Control characters are unreachable from a correct worker (the name comes from
    a real file on disk), but a ``\\x00`` would blow up in ``open()`` with
    ``ValueError`` (→ 500) and a CR/LF would open the door to header injection
    downstream; rejecting them here gives a clean ``422`` at the edge.
    """
    if not rel_path or rel_path.startswith("/") or "\\" in rel_path:
        raise InvalidRelPath(f"rel_path is not a relative POSIX path: {rel_path!r}")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in rel_path):
        raise InvalidRelPath(f"rel_path contains a control character: {rel_path!r}")
    parts = rel_path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise InvalidRelPath(f"rel_path has an invalid component: {rel_path!r}")
    # Defense in depth: the normalized result must not escape.
    normalized = PurePosixPath(rel_path)
    if normalized.is_absolute() or ".." in normalized.parts:
        raise InvalidRelPath(f"rel_path escapes the directory: {rel_path!r}")
    return rel_path


def artifact_abs_path(root: Path, execution_id: str, rel_path: str) -> Path:
    """Absolute path of an artifact. ``rel_path`` must already be validated."""
    return root / execution_id / PurePosixPath(rel_path)


def write_artifact_bytes(
    root: Path,
    execution_id: str,
    rel_path: str,
    chunks: Iterable[bytes],
    *,
    max_bytes: int | None = None,
    existing_sha: str | None = None,
) -> tuple[str, int]:
    """Write the bytes atomically and return ``(sha256_hex, size_bytes)``.

    Writes to a sibling ``.tmp`` and does a ``rename`` (atomic on POSIX). Creates
    the subdirectories of ``rel_path``. ``rel_path`` must already be validated by
    :func:`validate_rel_path`.

    The temporary's name includes the PID so that two processes (e.g. several
    uvicorn workers) writing the same ``(execution_id, rel_path)`` do not share the
    same ``.tmp``: each writes its own and the last ``rename`` wins
    (last-write-wins), without exposing a half-written temporary.

    If ``max_bytes`` is not ``None`` and the accumulated size exceeds that value,
    the write aborts immediately (the temporary was already cleaned up) and
    :exc:`ArtifactTooLarge` is raised. This avoids materializing huge files on disk
    before rejecting them: the limit acts as a real guard, not as a post-write
    check.

    Idempotency: if ``existing_sha`` matches the freshly computed sha256 and the
    ``target`` already exists on disk, the ``.tmp`` is discarded WITHOUT a ``rename``
    (same sha256 ⟹ same bytes: rewriting would clobber an identical file and would
    compete with a download in progress). The stream is consumed anyway
    (single-pass: needed for the sha and the ``max_bytes`` guard), but the good file
    is not touched. If the ``target`` does NOT exist (live metadata without bytes),
    it is rewritten → self-healing of the "missing bytes on disk" case."""
    target = artifact_abs_path(root, execution_id, rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    hasher = hashlib.sha256()
    size = 0
    try:
        with tmp.open("wb") as f:
            for chunk in chunks:
                if not chunk:
                    continue
                hasher.update(chunk)
                size += len(chunk)
                # Per-file guard: abort before writing more chunks.
                if max_bytes is not None and size > max_bytes:
                    tmp.unlink(missing_ok=True)
                    raise ArtifactTooLarge(
                        f"artifact {rel_path!r} exceeds the per-file limit of {max_bytes} bytes"
                    )
                f.write(chunk)
        digest = hasher.hexdigest()
        if existing_sha is not None and digest == existing_sha and target.is_file():
            # Identical bytes already persisted: do not rewrite (see docstring).
            tmp.unlink(missing_ok=True)
        else:
            tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return digest, size


def stage_artifact_bytes(
    root: Path,
    execution_id: str,
    rel_path: str,
    chunks: Iterable[bytes],
    *,
    max_bytes: int | None = None,
    existing_sha: str | None = None,
) -> StagedArtifact:
    """Write the bytes to a ``.tmp`` sibling of the destination WITHOUT renaming.

    Returns a :class:`StagedArtifact`. It does NOT materialize the destination:
    :func:`commit_staged` does that after the WHOLE batch was written, so a failure
    (e.g. ``ENOSPC``) in the middle of the batch leaves no partial renames.

    ``rel_path`` must already be validated by :func:`validate_rel_path`. Raises
    :exc:`ArtifactTooLarge` if it exceeds ``max_bytes`` and :exc:`NoSpaceLeft` on
    ``ENOSPC``. In both cases its own ``.tmp`` was already cleaned up.

    Idempotency: if ``existing_sha`` matches the freshly computed sha and the
    destination already exists, ``needs_promote`` stays ``False`` (the ``.tmp`` is
    cleaned up, the good file is not touched).
    """
    target = artifact_abs_path(root, execution_id, rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # PID + uuid: the uuid guarantees a unique .tmp per staging, so two concurrent
    # requests on the same (execution_id, rel_path) in the SAME process (same PID)
    # do not share a temporary: neither are the bytes corrupted nor does the second
    # rename of commit_staged blow up. The PID is kept to diagnose which uvicorn
    # process a leftover came from.
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    hasher = hashlib.sha256()
    size = 0
    try:
        with tmp.open("wb") as f:
            for chunk in chunks:
                if not chunk:
                    continue
                hasher.update(chunk)
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    tmp.unlink(missing_ok=True)
                    raise ArtifactTooLarge(
                        f"artifact {rel_path!r} exceeds the per-file limit of {max_bytes} bytes"
                    )
                f.write(chunk)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        if exc.errno == errno.ENOSPC:
            raise NoSpaceLeft(f"ENOSPC while writing {rel_path!r}") from exc
        raise
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    digest = hasher.hexdigest()
    needs_promote = not (existing_sha is not None and digest == existing_sha and target.is_file())
    if not needs_promote:
        # Identical bytes already persisted: discard the .tmp, do not rewrite.
        tmp.unlink(missing_ok=True)
    return StagedArtifact(
        rel_path=rel_path,
        target=target,
        tmp_path=tmp,
        sha256=digest,
        size_bytes=size,
        needs_promote=needs_promote,
    )


def commit_staged(staged: list[StagedArtifact]) -> None:
    """Materialize (atomic ``rename``) every staged item that requires it."""
    for s in staged:
        if s.needs_promote:
            s.tmp_path.replace(s.target)


def discard_staged(staged: list[StagedArtifact]) -> None:
    """Clean up the ``.tmp`` files of a batch (idempotent). For rollback."""
    for s in staged:
        s.tmp_path.unlink(missing_ok=True)


def iter_artifact_bytes(abs_path: Path, *, chunk_size: int = _WRITE_CHUNK) -> Iterator[bytes]:
    """Read an artifact in chunks (for download streaming)."""
    with abs_path.open("rb") as f:
        while chunk := f.read(chunk_size):
            yield chunk


def delete_execution_artifacts(root: Path, execution_id: str) -> None:
    """Delete the bytes dir of an execution. Idempotent."""
    shutil.rmtree(root / execution_id, ignore_errors=True)
