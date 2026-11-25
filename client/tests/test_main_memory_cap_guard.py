"""Startup guard for the container memory cap.

The `--memory` of `podman run` is containment, not cosmetic: with a cap, a
runaway task dies as `CONSTRAINT_MEMCG` inside its own cgroup;
without a cap it takes the host down with it. A rollout once left
the workers without a cap and nobody noticed until reading the `ceiling` that
the task itself reports in its telemetry. These tests pin that the worker
says so by itself at startup.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from orcaster_worker import sysinfo
from orcaster_worker.config import Settings
from orcaster_worker.main import _warn_if_memory_cap_missing, run


def _fake_cgroup(tmp_path: Path, monkeypatch, *, current: str | None, maximum: str | None) -> None:
    cur = tmp_path / "memory.current"
    mx = tmp_path / "memory.max"
    if current is not None:
        cur.write_text(current)
    if maximum is not None:
        mx.write_text(maximum)
    monkeypatch.setattr(sysinfo, "_CGROUP_MEMORY_CURRENT", cur)
    monkeypatch.setattr(sysinfo, "_CGROUP_MEMORY_MAX", mx)


def test_warns_when_container_starts_without_cap(tmp_path: Path, monkeypatch, caplog) -> None:
    _fake_cgroup(tmp_path, monkeypatch, current=str(512 * 1024 * 1024), maximum="max")
    with caplog.at_level("WARNING"):
        _warn_if_memory_cap_missing()
    assert "NO memory cap" in caplog.text
    # The warning must carry the name of the variable that fixes it: whoever
    # reads it is facing an already-running worker and needs the recipe, not a
    # diagnosis.
    assert "WORKER_MEMORY" in caplog.text


def test_silent_with_cap_set(tmp_path: Path, monkeypatch, caplog) -> None:
    _fake_cgroup(tmp_path, monkeypatch, current=str(512 * 1024 * 1024), maximum=str(30 * 1024**3))
    with caplog.at_level("WARNING"):
        _warn_if_memory_cap_missing()
    assert caplog.text == ""


def test_silent_without_readable_cgroup(tmp_path: Path, monkeypatch, caplog) -> None:
    """Native execution (dev, tests): there is no cgroup to read and the absence of a
    cap is not a defect. The guard must not pollute every local startup."""
    _fake_cgroup(tmp_path, monkeypatch, current=None, maximum=None)
    with caplog.at_level("WARNING"):
        _warn_if_memory_cap_missing()
    assert caplog.text == ""


async def test_worker_startup_runs_the_guard(tmp_path: Path, monkeypatch) -> None:
    """The guard must be wired into `run()`, and before the fail-fast checks:
    a host without a pubkey aborts startup, and the operator must still
    get to see the cap warning in the log."""
    settings = Settings(
        server_url="http://server.test",  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        cache_root=tmp_path / "cache",
        egress_allow_file=tmp_path / "egress" / "allow.txt",
    )
    called: list[bool] = []
    monkeypatch.setattr(
        "orcaster_worker.main._warn_if_memory_cap_missing", lambda: called.append(True)
    )
    with (
        patch("orcaster_worker.main._ensure_pubkey_loadable_or_die", side_effect=SystemExit(1)),
        pytest.raises(SystemExit),
    ):
        await run(settings)
    assert called == [True]
