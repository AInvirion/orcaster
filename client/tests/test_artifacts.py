"""Worker artifact collection: spec parsing, globbing, batching."""

import hashlib
from pathlib import Path

from orcaster_worker.artifacts import ArtifactFile, ArtifactsSpec, collect_artifacts, plan_batches


def test_spec_absent_defaults() -> None:
    spec = ArtifactsSpec.from_payload({"commands": ["echo hi"]})
    assert spec.include == ["**/*"]
    assert spec.exclude == []
    assert spec.required is False
    assert spec.enabled is False  # absent -> nothing is collected


def test_spec_present_minimal() -> None:
    spec = ArtifactsSpec.from_payload({"artifacts": {}})
    assert spec.enabled is True
    assert spec.include == ["**/*"]
    assert spec.exclude == []
    assert spec.required is False


def test_spec_full() -> None:
    spec = ArtifactsSpec.from_payload(
        {"artifacts": {"include": ["*.json"], "exclude": ["tmp/*"], "required": True}}
    )
    assert spec.enabled is True
    assert spec.include == ["*.json"]
    assert spec.exclude == ["tmp/*"]
    assert spec.required is True


def test_spec_non_dict_payload() -> None:
    spec = ArtifactsSpec.from_payload("not-a-dict")  # type: ignore[arg-type]
    assert spec.enabled is False


# ---------------------------------------------------------------------------
# Task 5.2 — collect_artifacts
# ---------------------------------------------------------------------------


def _write(base: Path, rel: str, data: bytes) -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_collect_all_default(tmp_path: Path) -> None:
    _write(tmp_path, "a.json", b"AA")
    _write(tmp_path, "sub/b.json", b"BBB")
    _write(tmp_path, "sub/deep/c.txt", b"CCCC")
    files = collect_artifacts(tmp_path, include=["**/*"], exclude=[])
    by_rel = {f.rel_path: f for f in files}
    assert set(by_rel) == {"a.json", "sub/b.json", "sub/deep/c.txt"}
    assert by_rel["a.json"].size_bytes == 2
    assert by_rel["a.json"].sha256 == hashlib.sha256(b"AA").hexdigest()
    assert "/" in by_rel["sub/b.json"].rel_path


def test_collect_include_filters(tmp_path: Path) -> None:
    _write(tmp_path, "keep.json", b"x")
    _write(tmp_path, "drop.txt", b"y")
    _write(tmp_path, "nested/keep2.json", b"z")
    files = collect_artifacts(tmp_path, include=["**/*.json"], exclude=[])
    assert {f.rel_path for f in files} == {"keep.json", "nested/keep2.json"}


def test_collect_exclude_wins(tmp_path: Path) -> None:
    _write(tmp_path, "a.json", b"x")
    _write(tmp_path, "tmp/b.json", b"y")
    files = collect_artifacts(tmp_path, include=["**/*.json"], exclude=["tmp/*"])
    assert {f.rel_path for f in files} == {"a.json"}


def test_collect_missing_dir_returns_empty(tmp_path: Path) -> None:
    files = collect_artifacts(tmp_path / "nope", include=["**/*"], exclude=[])
    assert files == []


def test_collect_ignores_directories(tmp_path: Path) -> None:
    (tmp_path / "emptydir").mkdir()
    _write(tmp_path, "f.json", b"x")
    files = collect_artifacts(tmp_path, include=["**/*"], exclude=[])
    assert {f.rel_path for f in files} == {"f.json"}


# ---------------------------------------------------------------------------
# Task 5.3 — plan_batches
# ---------------------------------------------------------------------------


def _af(rel: str, size: int) -> ArtifactFile:
    return ArtifactFile(rel_path=rel, abs_path=Path(rel), size_bytes=size, sha256="x")


def test_plan_batches_groups_small_files() -> None:
    files = [_af("a", 30), _af("b", 30), _af("c", 30)]
    batches = plan_batches(files, max_request_bytes=100)
    assert len(batches) == 1
    assert [f.rel_path for f in batches[0]] == ["a", "b", "c"]


def test_plan_batches_splits_when_exceeding() -> None:
    files = [_af("a", 60), _af("b", 60)]
    batches = plan_batches(files, max_request_bytes=100)
    assert len(batches) == 2
    assert [f.rel_path for f in batches[0]] == ["a"]
    assert [f.rel_path for f in batches[1]] == ["b"]


def test_plan_batches_large_file_alone() -> None:
    files = [_af("small", 10), _af("huge", 500), _af("small2", 10)]
    batches = plan_batches(files, max_request_bytes=100)
    assert [[f.rel_path for f in b] for b in batches] == [["small"], ["huge"], ["small2"]]


def test_plan_batches_empty() -> None:
    assert plan_batches([], max_request_bytes=100) == []


def test_collect_bare_star_is_root_only(tmp_path: Path) -> None:
    # POSIX semantics (full_match): `*.json` matches only files at the root,
    # NOT those in subdirectories (unlike fnmatch, where `*` crosses `/`).
    _write(tmp_path, "root.json", b"x")
    _write(tmp_path, "sub/nested.json", b"y")
    files = collect_artifacts(tmp_path, include=["*.json"], exclude=[])
    assert {f.rel_path for f in files} == {"root.json"}


def test_collect_doublestar_matches_root_and_nested(tmp_path: Path) -> None:
    _write(tmp_path, "root.json", b"x")
    _write(tmp_path, "sub/nested.json", b"y")
    files = collect_artifacts(tmp_path, include=["**/*.json"], exclude=[])
    assert {f.rel_path for f in files} == {"root.json", "sub/nested.json"}


def test_collect_single_segment_glob_does_not_cross_slash(tmp_path: Path) -> None:
    # `sub/*` matches direct children of sub, not grandchildren.
    _write(tmp_path, "sub/a.json", b"x")
    _write(tmp_path, "sub/deep/b.json", b"y")
    files = collect_artifacts(tmp_path, include=["sub/*"], exclude=[])
    assert {f.rel_path for f in files} == {"sub/a.json"}


def test_collect_skips_symlinks(tmp_path: Path) -> None:
    # A symlink to a file (even one outside the dir) is not collected.
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    art = tmp_path / "art"
    art.mkdir()
    _write(art, "real.json", b"ok")
    (art / "link.txt").symlink_to(outside)
    files = collect_artifacts(art, include=["**/*"], exclude=[])
    assert {f.rel_path for f in files} == {"real.json"}


def test_plan_batches_file_exactly_at_limit() -> None:
    # A file whose size == the limit fits (it is not alone); the `>` is strict.
    files = [_af("exact", 100), _af("tiny", 1)]
    batches = plan_batches(files, max_request_bytes=100)
    # exact fills the batch to 100; tiny would push it to 101 -> new batch.
    assert [[f.rel_path for f in b] for b in batches] == [["exact"], ["tiny"]]
