"""Cancellation tests with kill_process_group."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time

import pytest

from orcaster_worker.main import BusyState, _execute_with_abandon
from orcaster_worker.runner import kill_process_group


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: process group kill")
def test_kill_process_group_terminates_child_tree() -> None:
    """Spawn a parent that creates two children; killpg must kill the whole tree."""
    # Script: the parent prints the children's PIDs and then waits. If killpg
    # works, both children die with the parent.
    script = 'sleep 60 & PID1=$!; sleep 60 & PID2=$!; echo "$PID1 $PID2"; wait'
    proc = subprocess.Popen(
        ["bash", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().decode().strip()
    child_pids = [int(x) for x in line.split()]
    assert len(child_pids) == 2
    # Sanity: the children are alive
    for pid in child_pids:
        assert _pid_alive(pid), f"child {pid} should be alive before kill"

    kill_process_group(proc, grace_seconds=2.0)
    proc.wait(timeout=5)

    # Give the kernel a moment to reap. The group's processes must die.
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if not any(_pid_alive(pid) for pid in child_pids):
            break
        time.sleep(0.05)

    for pid in child_pids:
        assert not _pid_alive(pid), f"child {pid} survived kill_process_group"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: SIGTERM trap")
def test_kill_process_group_escalates_to_sigkill() -> None:
    """A process that ignores SIGTERM must be killed with SIGKILL."""
    proc = subprocess.Popen(
        ["bash", "-c", "trap '' TERM; sleep 60"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid = proc.pid
    assert _pid_alive(pid)

    t0 = time.time()
    kill_process_group(proc, grace_seconds=0.2)
    proc.wait(timeout=3)
    elapsed = time.time() - t0

    assert not _pid_alive(pid), "process should be dead after SIGKILL escalation"
    # It must have waited ~grace before escalating, but less than an infinite wait.
    assert elapsed < 2.5, f"escalation took too long: {elapsed}s"


def test_kill_process_group_swallows_process_lookup_error() -> None:
    """If the subprocess already finished, it must not raise an exception."""
    proc = subprocess.Popen(
        ["bash", "-c", "exit 0"] if sys.platform != "win32" else ["cmd", "/c", "exit 0"],
    )
    proc.wait(timeout=5)
    # Calling kill after death must be a silent no-op.
    kill_process_group(proc, grace_seconds=0.1)
    kill_process_group(proc, grace_seconds=0.0)


def test_kill_process_group_handles_none_pid() -> None:
    """Object without a pid (None) must be a no-op."""

    class FakeProc:
        pid = None

    kill_process_group(FakeProc(), grace_seconds=0.1)


def test_execute_with_abandon_invokes_kill_process_group(monkeypatch) -> None:
    """When abandon_event is set during execution, `kill_process_group`
    (mocked) must be called with the configured grace."""
    calls: list[tuple[int | None, float]] = []

    def fake_kill(proc, grace_seconds: float = 5.0) -> None:
        calls.append((getattr(proc, "pid", None), grace_seconds))
        # Kill the real process so communicate() finishes quickly.
        with contextlib_suppress():
            if sys.platform != "win32":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.terminate()

    # Patch in the module where it is imported (orcaster_worker.main).
    monkeypatch.setattr("orcaster_worker.main.kill_process_group", fake_kill)

    async def runner() -> None:
        busy = BusyState()

        # Set abandon_event after spawn to force the cancellation path.
        async def trigger() -> None:
            await asyncio.sleep(0.2)
            busy.abandon_event.set()

        if sys.platform == "win32":
            cmd, args = "cmd", ["/c", "ping -n 30 127.0.0.1"]
        else:
            cmd, args = "bash", ["-c", "sleep 30"]

        trigger_task = asyncio.create_task(trigger())
        result = await _execute_with_abandon(cmd, args, busy, grace_seconds=0.3)
        await trigger_task
        assert result is None  # abandoned
        assert len(calls) == 1
        pid, grace = calls[0]
        assert pid is not None
        assert grace == 0.3

    asyncio.run(runner())


# Local helper to avoid top-level imports that affect other tests.
class contextlib_suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return True
