"""Artifact bytes layer: rel_path validation + atomic writes."""

from pathlib import Path

import pytest

from orcaster_server.artifacts_store import (
    ArtifactTooLarge,
    InvalidRelPath,
    artifact_abs_path,
    delete_execution_artifacts,
    validate_rel_path,
    write_artifact_bytes,
)

EID = "11111111-1111-1111-1111-111111111111"


def test_validate_rel_path_ok() -> None:
    assert validate_rel_path("results.json") == "results.json"
    assert validate_rel_path("sub/dir/model.json") == "sub/dir/model.json"


@pytest.mark.parametrize(
    "bad",
    [
        "../escape.json",
        "sub/../../escape.json",
        "/abs/path.json",
        "a//b.json",
        "",
        "a/./b.json",
        "C:\\win.json",
        # Control characters: NUL would blow up in open() with a 500;
        # CR/LF would open the door to header injection. They must raise InvalidRelPath.
        "a\x00b.json",
        "a\nb.json",
        "a\rb.json",
        "a\x7fb.json",
    ],
)
def test_validate_rel_path_rejects_traversal(bad: str) -> None:
    with pytest.raises(InvalidRelPath):
        validate_rel_path(bad)


def test_write_and_path_atomic(tmp_path: Path) -> None:
    data = b"hello artifact" * 100
    sha, size = write_artifact_bytes(tmp_path, EID, "sub/x.json", iter([data]))
    import hashlib

    assert size == len(data)
    assert sha == hashlib.sha256(data).hexdigest()
    abs_path = artifact_abs_path(tmp_path, EID, "sub/x.json")
    assert abs_path.read_bytes() == data
    # No leftover .tmp.
    assert not any(p.suffix == ".tmp" for p in abs_path.parent.iterdir())


def test_write_streams_multiple_chunks(tmp_path: Path) -> None:
    sha, size = write_artifact_bytes(tmp_path, EID, "y.bin", iter([b"aaa", b"bbb", b"ccc"]))
    assert size == 9
    assert artifact_abs_path(tmp_path, EID, "y.bin").read_bytes() == b"aaabbbccc"


def test_delete_execution_artifacts(tmp_path: Path) -> None:
    write_artifact_bytes(tmp_path, EID, "a.json", iter([b"x"]))
    write_artifact_bytes(tmp_path, EID, "deep/b.json", iter([b"y"]))
    delete_execution_artifacts(tmp_path, EID)
    assert not (tmp_path / EID).exists()
    # Idempotent: deleting again does not raise.
    delete_execution_artifacts(tmp_path, EID)


def test_write_max_bytes_raises_artifact_too_large(tmp_path: Path) -> None:
    # write_artifact_bytes with max_bytes must raise ArtifactTooLarge as soon as
    # the accumulated chunks exceed the limit, without materializing the target or
    # leaving a leftover .tmp.
    data = b"x" * 50
    with pytest.raises(ArtifactTooLarge):
        write_artifact_bytes(tmp_path, EID, "toobig.bin", iter([data]), max_bytes=10)

    target = artifact_abs_path(tmp_path, EID, "toobig.bin")
    # The target must NOT exist.
    assert not target.exists()
    # No leftover .tmp in the parent directory (if it was created).
    exec_dir = tmp_path / EID
    if exec_dir.exists():
        assert not any(p.suffix == ".tmp" for p in exec_dir.rglob("*.tmp"))


def test_write_max_bytes_ok_when_equal(tmp_path: Path) -> None:
    # Exactly at the limit: it must write without error.
    data = b"y" * 10
    sha, size = write_artifact_bytes(tmp_path, EID, "exact.bin", iter([data]), max_bytes=10)
    assert size == 10
    assert artifact_abs_path(tmp_path, EID, "exact.bin").read_bytes() == data


def test_write_same_sha_skips_replace(tmp_path: Path) -> None:
    # Rewriting with existing_sha == computed sha and the target already on disk must NOT
    # touch the file (same sha256 ⟹ same bytes). Verified by inode:
    # a rename would change the inode; the skip preserves it.
    import hashlib

    data = b"idempotent-bytes"
    sha, _ = write_artifact_bytes(tmp_path, EID, "r.json", iter([data]))
    assert sha == hashlib.sha256(data).hexdigest()
    target = artifact_abs_path(tmp_path, EID, "r.json")
    inode_before = target.stat().st_ino

    sha2, size2 = write_artifact_bytes(tmp_path, EID, "r.json", iter([data]), existing_sha=sha)
    assert sha2 == sha
    assert size2 == len(data)
    assert target.read_bytes() == data
    assert target.stat().st_ino == inode_before  # not rewritten
    assert not any(p.suffix == ".tmp" for p in target.parent.iterdir())


def test_write_same_sha_self_heals_when_bytes_missing(tmp_path: Path) -> None:
    # existing_sha matches but the bytes are missing on disk (live metadata with no
    # bytes): it must rewrite anyway → self-healing of the "missing bytes" case.
    import hashlib

    data = b"restore-me"
    sha = hashlib.sha256(data).hexdigest()
    target = artifact_abs_path(tmp_path, EID, "missing.json")
    assert not target.exists()

    sha2, size2 = write_artifact_bytes(
        tmp_path, EID, "missing.json", iter([data]), existing_sha=sha
    )
    assert sha2 == sha
    assert size2 == len(data)
    assert target.read_bytes() == data  # materialized despite existing_sha


def test_write_different_sha_still_replaces(tmp_path: Path) -> None:
    # existing_sha differs from the new content: it is rewritten (content changed).
    write_artifact_bytes(tmp_path, EID, "c.json", iter([b"old"]))
    sha2, _ = write_artifact_bytes(
        tmp_path, EID, "c.json", iter([b"new-content"]), existing_sha="stale-sha"
    )
    assert artifact_abs_path(tmp_path, EID, "c.json").read_bytes() == b"new-content"


def test_write_cleans_tmp_on_error(tmp_path: Path) -> None:
    # If the chunk iterator blows up mid-write, the temporary file is
    # cleaned up and neither a leftover .tmp nor a half-written target remains.
    def _boom() -> object:
        yield b"partial"
        raise RuntimeError("stream broke")

    with pytest.raises(RuntimeError, match="stream broke"):
        write_artifact_bytes(tmp_path, EID, "broken.json", _boom())

    exec_dir = tmp_path / EID
    if exec_dir.exists():
        assert not any(p.suffix == ".tmp" for p in exec_dir.iterdir())
    assert not artifact_abs_path(tmp_path, EID, "broken.json").exists()


def test_stage_and_promote_roundtrip(tmp_path: Path) -> None:
    """stage writes to .tmp without touching the destination; promote materializes it."""
    from orcaster_server.artifacts_store import (
        artifact_abs_path,
        commit_staged,
        stage_artifact_bytes,
    )

    data = b"payload" * 50
    staged = stage_artifact_bytes(tmp_path, EID, "out/r.json", iter([data]))
    target = artifact_abs_path(tmp_path, EID, "out/r.json")
    # Not yet materialized: the .tmp exists, the destination does not.
    assert staged.tmp_path.exists()
    assert not target.exists()
    assert staged.needs_promote is True

    commit_staged([staged])
    assert target.read_bytes() == data
    assert not staged.tmp_path.exists()


def test_stage_enospc_raises_nospaceleft_and_cleans(tmp_path: Path, monkeypatch) -> None:
    """An ENOSPC when writing the .tmp is mapped to NoSpaceLeft and cleans up the .tmp."""
    import errno

    from orcaster_server.artifacts_store import NoSpaceLeft, stage_artifact_bytes

    # Proxy that opens the REAL file on disk (so that it physically exists) but
    # whose write() raises ENOSPC. This way the .tmp exists on disk at the moment the
    # exception is raised, and what is verified is that tmp.unlink() in the
    # except OSError block actually removes it.
    class _EnospcProxy:
        """Wraps a real file handle; write() simulates a full disk."""

        def __init__(self, real_fh):
            self._fh = real_fh

        def __enter__(self):
            return self

        def __exit__(self, *args):
            # Close the real handle; do not suppress the exception.
            self._fh.close()
            return False

        def write(self, data):
            raise OSError(errno.ENOSPC, "No space left on device")

        def __getattr__(self, name):
            return getattr(self._fh, name)

    original_open = Path.open

    def patched_open(self, mode="r", **kwargs):
        # Only intercept the write-mode open of the .tmp.
        if "w" in mode and str(self).endswith(".tmp"):
            # Open the REAL file so that it is on disk before write().
            real_fh = original_open(self, mode, **kwargs)
            return _EnospcProxy(real_fh)
        return original_open(self, mode, **kwargs)

    monkeypatch.setattr(Path, "open", patched_open)

    with pytest.raises(NoSpaceLeft):
        stage_artifact_bytes(tmp_path, EID, "big.bin", iter([b"x" * 100]))

    # The .tmp was created on disk (guaranteed by the proxy) and must have been
    # removed by the except OSError block of stage_artifact_bytes.
    exec_dir = tmp_path / EID
    assert exec_dir.exists(), "The execution directory should have been created"
    residual = list(exec_dir.rglob("*.tmp"))
    assert residual == [], f"leftover .tmp found: {residual}"


def test_discard_staged_removes_tmp(tmp_path: Path) -> None:
    from orcaster_server.artifacts_store import discard_staged, stage_artifact_bytes

    staged = stage_artifact_bytes(tmp_path, EID, "d.json", iter([b"data"]))
    assert staged.tmp_path.exists()
    discard_staged([staged])
    assert not staged.tmp_path.exists()


def test_stage_same_sha_sets_needs_promote_false(tmp_path: Path) -> None:
    """Idempotent re-upload: existing_sha == computed sha + target on disk
    → needs_promote=False, .tmp cleaned up, commit_staged does not touch the file.
    """
    import hashlib

    from orcaster_server.artifacts_store import commit_staged, stage_artifact_bytes

    data = b"idempotent-stage-bytes" * 20
    sha = hashlib.sha256(data).hexdigest()

    # Step 1: first upload → materializes the target on disk.
    staged_first = stage_artifact_bytes(tmp_path, EID, "s.json", iter([data]))
    commit_staged([staged_first])
    target = staged_first.target
    assert target.read_bytes() == data, "the target must exist after the first commit"

    # Capture the inode to verify that commit_staged does not rewrite the file.
    inode_before = target.stat().st_ino

    # Step 2: re-upload with the same bytes and the correct sha.
    staged = stage_artifact_bytes(tmp_path, EID, "s.json", iter([data]), existing_sha=sha)

    # The sha matches and the target exists → no promote needed.
    assert staged.needs_promote is False, "idempotent re-upload must set needs_promote=False"

    # The .tmp was discarded immediately inside stage_artifact_bytes.
    assert not staged.tmp_path.exists(), "the .tmp must be removed when needs_promote=False"

    # commit_staged is a no-op: the file is not touched (identical inode).
    commit_staged([staged])
    assert target.stat().st_ino == inode_before, "commit_staged must not rewrite the file"
    assert target.read_bytes() == data, "the target content must remain intact"

    # Complementary check: without existing_sha the same staged call returns needs_promote=True.
    staged_without_sha = stage_artifact_bytes(tmp_path, EID, "s.json", iter([data]))
    assert staged_without_sha.needs_promote is True, (
        "without existing_sha it must always promote (non-idempotent behavior)"
    )
    # Clean up the .tmp generated by this last call.
    staged_without_sha.tmp_path.unlink(missing_ok=True)


def test_concurrent_stage_same_target_uses_independent_tmp(tmp_path: Path) -> None:
    """Two live stagings of the same (execution_id, rel_path), e.g. two concurrent
    requests in the same process (same PID), must use DISTINCT .tmp files. If
    they shared the path (naming by PID only), the second write would clobber the
    first one's .tmp and the second commit_staged would blow up with FileNotFoundError
    (the .tmp was already renamed by the first commit)."""
    from orcaster_server.artifacts_store import commit_staged, stage_artifact_bytes

    s1 = stage_artifact_bytes(tmp_path, EID, "same.bin", iter([b"first"]))
    s2 = stage_artifact_bytes(tmp_path, EID, "same.bin", iter([b"second"]))

    # Unique .tmp files: each staging keeps its own content without overwriting.
    assert s1.tmp_path != s2.tmp_path
    assert s1.tmp_path.read_bytes() == b"first"
    assert s2.tmp_path.read_bytes() == b"second"
    assert s1.target == s2.target

    # Both commits apply without FileNotFoundError; the last one wins.
    commit_staged([s1])
    commit_staged([s2])
    assert s1.target.read_bytes() == b"second"
