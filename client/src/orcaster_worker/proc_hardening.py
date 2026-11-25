"""Hardening of the worker process against its own tasks.

The worker and each task's subprocess run with the same uid inside the
container. Strict mode cleans the environment the task receives, but on Linux a
process with the same uid can read another's initial environment block in
`/proc/<pid>/environ` (and attach with ptrace) as long as the target is
"dumpable". The env scrub decides what the task inherits, but it did not stop
the task from reading the worker's environment through `/proc`, and with it the
names `task_env` cannot declare: the `ORCH_*` ones (in particular
`ORCH_ENROLLMENT_TOKEN`) and any variable the task did not declare. Those are
what this module protects. A name the task does declare in `task_env` (e.g. a
variable of `ORCH_ISOLATION_REQUIRED_ENV`) is still received from the worker:
its protection is the restricted workspace and the scope of that credential,
not this module.

`prctl(PR_SET_DUMPABLE, 0)` marks the process as non-dumpable: the kernel then
requires CAP_SYS_PTRACE to read its `environ`, its `mem` or to attach, and the
task (without capabilities) gets `EACCES`. Side effects verified on Linux
(rootless podman, `--user 1000:1000`):

- The flag is reset on `execve`: the tasks' subprocesses become dumpable again,
  so the telemetry that reads their `/proc` (psutil) does not change.
- A `fork()` without `exec` inherits the 0 (the worker does not use it for
  tasks).
- The worker itself loses the ability to read its mode-0400 `/proc/self` files
  (`environ`, `io`); `status`, `statm`, `stat`, `fd` and `smaps` stay readable.
  No worker code reads those two.
- The kernel does not write core dumps of the worker (they would contain the
  same secrets).

It does not close the underlying problem (same uid: `state.json`, the worker's
files, and the environment of any `podman exec` in the container while it
lasts); that requires a uid or a sandbox per task.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from collections.abc import Callable
from typing import NoReturn

log = logging.getLogger(__name__)

# Constants from <linux/prctl.h>.
PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4
SUID_DUMP_DISABLE = 0

PrctlFn = Callable[[int, int, int, int, int], int]


def _load_prctl() -> PrctlFn:
    """`prctl` from the process libc, with errno captured by ctypes."""
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.restype = ctypes.c_int
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    return prctl


def _fail_closed(reason: str) -> NoReturn:
    log.error(
        "refusing to start: cannot make the worker process non-dumpable (%s). "
        "Without it a task running under the same uid can read the worker's "
        "environment through /proc, including ORCH_* values such as "
        "ORCH_ENROLLMENT_TOKEN that task_env never passes to a task.",
        reason,
    )
    sys.exit(1)


def make_process_non_dumpable(
    *,
    platform: str | None = None,
    load_prctl: Callable[[], PrctlFn] = _load_prctl,
    get_errno: Callable[[], int] = ctypes.get_errno,
) -> None:
    """Mark the process as non-dumpable on Linux; fail closed if it cannot.

    Outside Linux there is no `/proc/<pid>/environ` nor `prctl`: it is skipped
    with a DEBUG log. On Linux, any failure (libc without `prctl`, a return
    other than 0, or a `PR_GET_DUMPABLE` that does not confirm the 0) logs an
    ERROR with the cause and ends the process with code 1: it never continues
    silently.

    The parameters with defaults exist for the tests (they inject a fake
    `prctl`); in production it is called without arguments.
    """
    platform = sys.platform if platform is None else platform
    if not platform.startswith("linux"):
        log.debug("non-dumpable hardening skipped: platform %s has no /proc environ", platform)
        return

    try:
        prctl = load_prctl()
    except (OSError, AttributeError) as exc:
        _fail_closed(f"prctl unavailable in libc: {exc}")

    if prctl(PR_SET_DUMPABLE, SUID_DUMP_DISABLE, 0, 0, 0) != 0:
        err = get_errno()
        _fail_closed(f"prctl(PR_SET_DUMPABLE, 0) failed: errno {err} ({os.strerror(err)})")

    # Confirm with a read: a 0 return that does not leave the flag at 0
    # (seccomp emulating the syscall, an odd libc) is also a failure.
    state = prctl(PR_GET_DUMPABLE, 0, 0, 0, 0)
    if state != SUID_DUMP_DISABLE:
        _fail_closed(f"PR_GET_DUMPABLE returned {state} after setting it to 0")

    log.info("worker process is non-dumpable: tasks cannot read its environment through /proc")
