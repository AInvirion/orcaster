"""Push command dispatcher.

The only external command is ``run_batch`` (executed inside an ephemeral venv
managed by ``_run_external``). The former ``install_dep`` and ``self_update``
were removed from the contract.

Trust model
-----------
``run_batch`` commands run as the **current worker user**, with no sandbox or
privilege separation. The attack surface is mitigated by the mandatory Ed25519
signature of the server operator (see ``_process_push_command`` in
``orcaster_worker.main``). If isolation (containers, a dedicated user,
namespaces) is required in the future, it will be done separately, without
changing the dispatcher contract.

Payload shape
-------------
``{"commands": ["echo hello", "ls /tmp"], "timeout_seconds": 60}``

* ``commands: list[str]`` — sequential shell invocations. Each string runs via
  ``/bin/sh -c <cmd>`` (or ``cmd.exe /c`` on Windows). If a command fails
  (exit_code != 0), the sequence STOPS and ``failed_at`` reports the index of
  the one that failed.
* ``timeout_seconds: int`` optional — window **per command**. Each command gets
  this window independently; a timeout in cmd N does not reduce the window of
  cmd N+1. Default: ``settings.run_batch_per_command_timeout_seconds``.

Result shape (ACK body field ``result``)
----------------------------------------
``{"exit_codes": [0, 2], "failed_at": 1}``

``status`` (top-level of the ACK) in {"ok", "error", "timeout", "abandoned"}.

* ``timeout``: at least one command exceeded its per-command window. The batch
  keeps running the following commands with a fresh window (unless the
  operator configures ``stop_on_timeout``; it does not exist yet).
* ``abandoned``: the ``abandon_event`` was set mid-batch. The caller must
  report ``cancelled`` to the server. It is exposed through the return value
  (no need to re-read the event) — defense in depth: the caller can still
  check the event as a fallback.

Output streaming
----------------
stdout and stderr are read incrementally (two concurrent readers so the pipes
do not deadlock) and each chunk is forwarded to the server through the
``on_output`` callback → ``OutputStreamer`` (POST /v1/tasks/{id}/output). The
output travels exclusively as events persisted in ``output_events``; the result
dict does NOT include ``stdout``/``stderr`` (the server ignores them and
accumulating them in RAM was dead weight). Payload validation errors are also
emitted through ``on_output`` so they end up as output_events.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import logging
import os
import sys
from typing import Any, Protocol

from orcaster_worker.config import Settings
from orcaster_worker.runner import kill_process_group

log = logging.getLogger("orcaster.worker.dispatcher")


# Streaming callback: receives stdout/stderr chunks (either may be None) and the
# command echo (`command`). Provided by `OutputStreamer.feed`.
class OnOutput(Protocol):
    async def __call__(
        self,
        stdout: str | None = None,
        stderr: str | None = None,
        command: str | None = None,
    ) -> None: ...


def _shell_argv(cmd: str) -> tuple[str, list[str]]:
    """Return (program, args) to invoke ``cmd`` through the OS shell."""
    if sys.platform == "win32":
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return comspec, ["/c", cmd]
    return "/bin/sh", ["-c", cmd]


async def _emit(
    chunk: str,
    is_stdout: bool,
    on_output: OnOutput | None,
) -> None:
    if on_output is not None:
        await on_output(chunk if is_stdout else None, None if is_stdout else chunk)


async def _pump_stream(
    stream: asyncio.StreamReader,
    *,
    is_stdout: bool,
    on_output: OnOutput | None,
) -> None:
    """Read ``stream`` incrementally, decode UTF-8 without breaking multibyte
    characters and forward each chunk to ``on_output``. The hard persistence cap
    (10 MB per stream) is applied by the server; nothing is accumulated locally."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        data = await stream.read(65536)
        if not data:
            tail = decoder.decode(b"", final=True)
            if tail:
                await _emit(tail, is_stdout, on_output)
            return
        chunk = decoder.decode(data)
        if chunk:
            await _emit(chunk, is_stdout, on_output)


async def _run_single(
    cmd: str,
    *,
    timeout_seconds: float,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    abandon_event: asyncio.Event | None = None,
    on_output: OnOutput | None = None,
    kill_grace_seconds: float = 0.5,
) -> tuple[int, bool, bool]:
    """Run a command with a timeout window of ``timeout_seconds`` seconds.

    Returns ``(exit_code, timed_out, abandoned)``. Reads stdout and stderr with
    two concurrent readers (does not use ``proc.communicate()``) so it can
    stream through ``on_output`` while the command runs, without deadlocking the
    pipes. On timeout or abandon, the whole process group is killed.

    ``cwd``: working directory for the subprocess. If ``None``, it inherits the
    cwd of the worker process.
    """
    program, argv = _shell_argv(cmd)

    popen_kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        import subprocess as _sp

        popen_kwargs["creationflags"] = _sp.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        popen_kwargs["preexec_fn"] = os.setsid

    if env is not None:
        popen_kwargs["env"] = env

    if cwd is not None:
        popen_kwargs["cwd"] = cwd

    proc = await asyncio.create_subprocess_exec(
        program,
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **popen_kwargs,
    )

    timed_out = False
    abandoned = False

    assert proc.stdout is not None and proc.stderr is not None

    async def _read_both() -> None:
        await asyncio.gather(
            _pump_stream(
                proc.stdout,  # type: ignore[arg-type]
                is_stdout=True,
                on_output=on_output,
            ),
            _pump_stream(
                proc.stderr,  # type: ignore[arg-type]
                is_stdout=False,
                on_output=on_output,
            ),
        )

    readers_task: asyncio.Task[Any] = asyncio.create_task(_read_both())

    abandon_task: asyncio.Task[Any] | None = None
    waitset: set[asyncio.Task[Any]] = {readers_task}
    if abandon_event is not None:
        abandon_task = asyncio.create_task(abandon_event.wait())
        waitset.add(abandon_task)

    timeout = max(timeout_seconds, 0.0)
    # asyncio.wait does not raise on timeout: it returns (done, pending) with
    # done possibly empty if nothing finished within the window.
    # A timeout <= 0 here means "no per-call limit" (path unreachable from
    # dispatch_run_batch, which guards timeout <= 0 and falls back to the default).
    done, _pending = await asyncio.wait(
        waitset,
        timeout=timeout if timeout > 0 else None,
        return_when=asyncio.FIRST_COMPLETED,
    )

    if abandon_task is not None and abandon_task in done and readers_task not in done:
        abandoned = True
    elif readers_task not in done:
        timed_out = True

    if timed_out or abandoned:
        await asyncio.to_thread(kill_process_group, proc, kill_grace_seconds)

    # Cancel the abandon waiter if it is still alive.
    if abandon_task is not None and not abandon_task.done():
        abandon_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await abandon_task

    # Make sure the readers finish (EOF after kill or normal termination).
    if not readers_task.done():
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(readers_task), timeout=2.0)
    if not readers_task.done():
        readers_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await readers_task

    # Reap the process to obtain the returncode.
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=2.0)

    exit_code = proc.returncode if proc.returncode is not None else -1
    return exit_code, timed_out, abandoned


async def dispatch_run_batch(
    payload: dict[str, Any] | None,
    settings: Settings,
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    abandon_event: asyncio.Event | None = None,
    on_output: OnOutput | None = None,
) -> tuple[str, dict[str, Any]]:
    """Run a ``run_batch`` batch.

    Returns ``(status, result_dict)`` ready for ``ack_command``.

    The output travels exclusively through ``on_output`` → ``OutputStreamer`` and
    is persisted in ``output_events``; the ``result_dict`` does NOT include
    ``stdout``/``stderr``. Validation error messages are also emitted through
    ``on_output`` so they end up as output_events.

    ``cwd``: working directory passed to each subprocess of the batch. If
    ``None``, each subprocess inherits the cwd of the worker process.

    Rules (per-command timeout):
        * ``commands`` must be ``list[str]``. If missing or of an invalid type
          -> status ``error`` with a descriptive message (via on_output).
        * ``commands=[]`` -> status ``ok``, ``exit_codes=[]``.
        * Sequential execution. If a command fails (exit_code != 0), the
          sequence stops and ``failed_at`` points to the index.
        * ``timeout_seconds`` is a window **per command**. Each command gets the
          same full window; a timeout in cmd N does not reduce the window of
          cmd N+1. If a command times out, its process group is killed, a
          marker is emitted via on_output and execution continues with the next
          one. The final status is ``"timeout"`` if at least one command timed
          out.
    """
    if not isinstance(payload, dict):
        msg = "run_batch payload must be a JSON object"
        if on_output is not None:
            await on_output(None, msg + "\n")
        return "error", {"exit_codes": [], "failed_at": None}

    raw_commands = payload.get("commands")
    if not isinstance(raw_commands, list) or not all(isinstance(c, str) for c in raw_commands):
        msg = "run_batch payload missing or invalid 'commands' (expected list[str])"
        if on_output is not None:
            await on_output(None, msg + "\n")
        return "error", {"exit_codes": [], "failed_at": None}
    commands: list[str] = list(raw_commands)

    timeout_raw = payload.get("timeout_seconds")
    if not isinstance(timeout_raw, int | float) or timeout_raw <= 0:
        per_cmd_timeout = float(settings.run_batch_per_command_timeout_seconds)
    else:
        per_cmd_timeout = float(timeout_raw)

    exit_codes: list[int] = []
    failed_at: int | None = None
    any_timed_out = False
    was_abandoned = False

    for idx, cmd in enumerate(commands):
        if on_output is not None:
            await on_output(command=f"$ {cmd}\n")
        exit_code, this_timed_out, abandoned = await _run_single(
            cmd,
            timeout_seconds=per_cmd_timeout,
            env=env,
            cwd=cwd,
            abandon_event=abandon_event,
            on_output=on_output,
            kill_grace_seconds=float(settings.cancel_grace_seconds),
        )
        if abandoned:
            was_abandoned = True
            marker = f"[batch] abandoned during command #{idx}: {cmd!r}\n"
            if on_output is not None:
                await on_output(None, marker)
            exit_codes.append(exit_code)
            break
        if this_timed_out:
            any_timed_out = True
            marker = f"[batch] timeout during command #{idx}: {cmd!r}\n"
            if on_output is not None:
                await on_output(None, marker)
            exit_codes.append(exit_code)
            continue

        exit_codes.append(exit_code)
        if exit_code != 0:
            failed_at = idx
            break

    result: dict[str, Any] = {"exit_codes": exit_codes, "failed_at": failed_at}

    if was_abandoned:
        return "abandoned", result
    if any_timed_out:
        return "timeout", result
    if failed_at is not None:
        return "error", result
    return "ok", result


SUPPORTED_KINDS: frozenset[str] = frozenset({"run_batch"})
