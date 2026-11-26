"""The worker creates an empty ORCASTER_ARTIFACTS_DIR and exports it to the subprocess."""

from pathlib import Path

from orcaster_worker.main import _prepare_cache_env


def test_prepare_cache_env_creates_artifacts_dir(tmp_path) -> None:
    env: dict[str, str] = {}
    cache_dir, artifacts_dir = _prepare_cache_env(env, cache_root=tmp_path, job_key="task-7")
    art = Path(env["ORCASTER_ARTIFACTS_DIR"])
    assert art == cache_dir / "artifacts"
    # The function is the single source of the path: the returned dir == the exported one.
    assert artifacts_dir == art
    assert art.is_dir()
    assert env["HF_HOME"] == str(cache_dir / "hf")
    assert "artifacts" not in env["HF_HOME"]


def test_prepare_cache_env_sets_legacy_aliases(tmp_path) -> None:
    """TASKORCH_* are the names from before the rename: same values as ORCASTER_*."""
    env: dict[str, str] = {}
    cache_dir, artifacts_dir = _prepare_cache_env(env, cache_root=tmp_path, job_key="task-8")
    assert env["TASKORCH_CACHE_DIR"] == env["ORCASTER_CACHE_DIR"] == str(cache_dir)
    assert env["TASKORCH_ARTIFACTS_DIR"] == env["ORCASTER_ARTIFACTS_DIR"] == str(artifacts_dir)
