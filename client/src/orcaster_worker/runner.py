"""Subprocess runner for tasks.

It runs synchronously (await proc.communicate()) and captures stdout/stderr at
the end. Incremental output streaming lives in the dispatcher.

Process group:
- On POSIX, `preexec_fn=os.setsid` is used to create a new group so that
  `killpg` can be called on cancel without touching the worker.
- On Windows there is no setsid; `CREATE_NEW_PROCESS_GROUP` (a CreationFlags
  flag) is used, best-effort. The exact kill policy is not defined there.

Command contract: `command: str` + `payload: dict` are defined. The minimal
convention is:

    - If `payload.get("args")` is a list of strings, they are passed as argv[1:].
    - If there are no args, the command is invoked alone as argv[0].

No shell and no string interpolation is used. Any additional semantics
(`run_batch`) lives in the dispatcher.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from dataclasses import dataclass

log = logging.getLogger("orcaster.worker.runner")


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    stdout: str
    stderr: str


def _popen_kwargs() -> dict[str, object]:
    """Args for asyncio.create_subprocess_exec that isolate the process group."""
    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        import subprocess

        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["preexec_fn"] = os.setsid
    return kwargs


def kill_process_group(
    proc: asyncio.subprocess.Process | object,
    grace_seconds: float = 5.0,
) -> None:
    """Kill the whole process group of `proc`.

    POSIX:
        1. Send SIGTERM to the group via `os.killpg(getpgid(pid), SIGTERM)`.
        2. Wait up to `grace_seconds`, checking whether the process finished.
        3. If it is still alive, escalate to SIGKILL on the group.

    Windows:
        Best-effort: `proc.terminate()` on the root process. The
        `CREATE_NEW_PROCESS_GROUP` flag allows sending CTRL_BREAK_EVENT but it
        does not propagate finely to descendants; this path is documented as
        "best-effort".

    Any `ProcessLookupError` (the subprocess died between our checks) is
    swallowed silently — the desired state was already reached by others.
    """
    pid = getattr(proc, "pid", None)
    if pid is None:
        return

    if sys.platform == "win32":
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.terminate()  # type: ignore[union-attr]
        return

    # POSIX path -------------------------------------------------------
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError as e:  # pragma: no cover — defensive
        log.warning("killpg SIGTERM permission denied pid=%s: %s", pid, e)
        return

    # Poll for up to grace_seconds. Uses waitpid(WNOHANG) — works both when the
    # process was spawned by us (asyncio) and when it is adopted.
    deadline = grace_seconds
    step = 0.05 if grace_seconds > 0 else 0.0
    elapsed = 0.0
    import time as _time

    while elapsed < deadline:
        try:
            os.kill(pid, 0)  # still alive?
        except ProcessLookupError:
            return
        if step == 0.0:
            break
        _time.sleep(step)
        elapsed += step

    # Escalate to SIGKILL on the group. If the group leader already died, the
    # kernel may return EPERM on macOS for `killpg` on an "orphaned" group; in
    # that case try the pid directly.
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


async def execute(
    command: str,
    args: list[str] | None = None,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> RunResult:
    """Launch `command` with `args`, capturing the full stdout/stderr.

    Does not raise on exit_code != 0; the caller decides the final status.
    """
    argv = [command, *(args or [])]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        **_popen_kwargs(),
    )
    stdout_b, stderr_b = await proc.communicate()
    return RunResult(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout_b.decode("utf-8", errors="replace"),
        stderr=stderr_b.decode("utf-8", errors="replace"),
    )
