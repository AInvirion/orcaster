"""Non-dumpable worker process.

Tasks run with the same uid as the worker. Without `PR_SET_DUMPABLE=0`, a
task with a clean env (strict mode) can still read the worker's environment
in procfs, and with it the model provider key. These tests pin
the function's contract (success, fail closed, skipped outside Linux), that
`run()` calls it before anything else and, on Linux, the real effect in the
kernel.
"""

from __future__ import annotations

import errno
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from orcaster_worker import proc_hardening
from orcaster_worker.config import Settings
from orcaster_worker.main import run
from orcaster_worker.proc_hardening import (
    PR_GET_DUMPABLE,
    PR_SET_DUMPABLE,
    make_process_non_dumpable,
)


class _FakePrctl:
    """`prctl` double: records the calls and responds according to the scenario."""

    def __init__(self, *, set_rc: int = 0, get_value: int = 0) -> None:
        self.calls: list[tuple[int, ...]] = []
        self._set_rc = set_rc
        self._get_value = get_value

    def __call__(self, option: int, *args: int) -> int:
        self.calls.append((option, *args))
        if option == PR_SET_DUMPABLE:
            return self._set_rc
        if option == PR_GET_DUMPABLE:
            return self._get_value
        raise AssertionError(f"unexpected prctl option {option}")


def test_linux_marks_the_process_non_dumpable(caplog) -> None:
    fake = _FakePrctl()
    with caplog.at_level("INFO"):
        make_process_non_dumpable(platform="linux", load_prctl=lambda: fake)
    assert fake.calls == [(PR_SET_DUMPABLE, 0, 0, 0, 0), (PR_GET_DUMPABLE, 0, 0, 0, 0)]
    assert "non-dumpable" in caplog.text


def test_linux_prctl_failure_aborts_with_the_cause(caplog) -> None:
    fake = _FakePrctl(set_rc=-1)
    with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exc:
        make_process_non_dumpable(
            platform="linux", load_prctl=lambda: fake, get_errno=lambda: errno.EPERM
        )
    assert exc.value.code == 1
    assert f"errno {errno.EPERM}" in caplog.text
    assert "refusing to start" in caplog.text
    # PR_GET_DUMPABLE is not used to confirm if the set already failed.
    assert fake.calls == [(PR_SET_DUMPABLE, 0, 0, 0, 0)]


def test_linux_unconfirmed_flag_aborts(caplog) -> None:
    """A `prctl` that returns 0 but leaves the flag at 1 (seccomp emulating the
    syscall, odd libc) must not pass as success."""
    fake = _FakePrctl(get_value=1)
    with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exc:
        make_process_non_dumpable(platform="linux", load_prctl=lambda: fake)
    assert exc.value.code == 1
    assert "PR_GET_DUMPABLE returned 1" in caplog.text


def test_linux_libc_without_prctl_aborts(caplog) -> None:
    def _missing():
        raise AttributeError("prctl")

    with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exc:
        make_process_non_dumpable(platform="linux", load_prctl=_missing)
    assert exc.value.code == 1
    assert "prctl unavailable" in caplog.text


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_outside_linux_is_skipped_with_debug(platform: str, caplog) -> None:
    def _never():
        raise AssertionError("prctl must not be loaded outside Linux")

    with caplog.at_level("DEBUG", logger=proc_hardening.__name__):
        make_process_non_dumpable(platform=platform, load_prctl=_never)
    assert [r.levelname for r in caplog.records] == ["DEBUG"]
    assert "skipped" in caplog.text


async def test_run_calls_it_before_any_other_step(tmp_path: Path, monkeypatch) -> None:
    """Wiring: if it fails, `run()` ends without reaching any other guard."""
    settings = Settings(
        server_url="http://server.test",  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        cache_root=tmp_path / "cache",
        egress_allow_file=tmp_path / "egress" / "allow.txt",
    )
    later: list[str] = []
    monkeypatch.setattr(
        "orcaster_worker.main._warn_if_memory_cap_missing", lambda: later.append("cap")
    )
    with (
        patch("orcaster_worker.main.make_process_non_dumpable", side_effect=SystemExit(1)) as nd,
        pytest.raises(SystemExit),
    ):
        await run(settings)
    nd.assert_called_once_with()
    assert later == []


# --- Real kernel effect (Linux only) -----------------------------------------

_PARENT = textwrap.dedent(
    """
    import os, subprocess, sys
    if sys.argv[1] == "1":
        from orcaster_worker.proc_hardening import make_process_non_dumpable
        make_process_non_dumpable()
    child = (
        "import os\\n"
        "try:\\n"
        "    data = open(f'/proc/{os.getppid()}/environ', 'rb').read()\\n"
        "    print('READ', b'ORCASTER_TEST_CANARY=' in data)\\n"
        "except PermissionError:\\n"
        "    print('PermissionError')\\n"
    )
    # Child like a task in strict mode: empty env and its own session.
    out = subprocess.run(
        [sys.executable, "-c", child], env={}, start_new_session=True,
        capture_output=True, text=True, check=True,
    )
    print(out.stdout.strip())
    """
)


def _child_view_of_parent_environ(*, harden: bool) -> str:
    src = Path(proc_hardening.__file__).resolve().parents[1]
    env = {"PYTHONPATH": str(src), "ORCASTER_TEST_CANARY": "not-a-real-secret"}
    proc = subprocess.run(
        [sys.executable, "-c", _PARENT, "1" if harden else "0"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return proc.stdout.strip()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="procfs environ is Linux-only")
def test_linux_without_hardening_the_task_reads_the_worker_environment() -> None:
    """Baseline: the gap exists without the call (same uid, clean env)."""
    assert _child_view_of_parent_environ(harden=False) == "READ True"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="procfs environ is Linux-only")
def test_linux_with_hardening_the_task_gets_permission_error() -> None:
    assert _child_view_of_parent_environ(harden=True) == "PermissionError"
