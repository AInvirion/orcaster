"""Ephemeral per-execution venv using uv.

Every task runs in an isolated venv created on the fly with ``uv venv`` and,
optionally, ``uv pip install``. There is no shortcut: with requirements=[] the
venv is still created, so every task follows the same path.

Contract
--------
``ephemeral_venv`` is an async context manager. On entry:

    1. Creates ``<tmp>/orcaster-venv-<execution_id>-<rand>`` via ``uv venv``.
    2. If requirements: ``uv pip install --python <venv>/bin/python <reqs...>``.
    3. Yields a ``VenvHandle`` with path, python, env (VIRTUAL_ENV + PATH).

On exit (normal or by exception):

    1. ``shutil.rmtree(path, ignore_errors=True)``.

If ``abandon_event`` is set while ``uv`` runs, the subprocess is killed
(SIGTERM to the process group on POSIX, terminate on Windows) and
``VenvAbandonedError`` is raised so the caller propagates the cancel state.

``UV_CACHE_DIR``
----------------
If ``uv_cache_dir`` is None, ``uv`` uses its default (``~/.cache/uv``).
For multi-worker deployments with a shared cache, the caller passes an
explicit path (settings.uv_cache_dir).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
import tempfile
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from orcaster_worker.runner import kill_process_group
from orcaster_worker.task_env import build_scrubbed_base_env, warn_missing_declared

log = logging.getLogger("orcaster.worker.venv_manager")


class VenvManagerError(RuntimeError):
    pass


class VenvCreationError(VenvManagerError):
    pass


class VenvInstallError(VenvManagerError):
    pass


class VenvAbandonedError(VenvManagerError):
    """abandon_event was set during uv venv or uv pip install."""


@dataclass(frozen=True)
class VenvHandle:
    path: Path
    python: Path
    env: dict[str, str]


def _venv_python(venv_path: Path) -> Path:
    if sys.platform == "win32":
        return venv_path / "Scripts" / "python.exe"
    return venv_path / "bin" / "python"


def _venv_bin_dir(venv_path: Path) -> Path:
    if sys.platform == "win32":
        return venv_path / "Scripts"
    return venv_path / "bin"


async def _spawn_uv(
    argv: list[str],
    *,
    abandon_event: asyncio.Event | None,
    extra_env: dict[str, str] | None = None,
    task_env: list[str] | None = None,
    secrets: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    popen_kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        import subprocess as _sp

        popen_kwargs["creationflags"] = _sp.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        popen_kwargs["preexec_fn"] = os.setsid

    env = build_scrubbed_base_env(task_env, secrets=secrets)
    if extra_env:
        env.update(extra_env)

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        **popen_kwargs,
    )

    communicate_task = asyncio.create_task(proc.communicate())

    if abandon_event is None:
        stdout_b, stderr_b = await communicate_task
        return (
            proc.returncode if proc.returncode is not None else -1,
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
        )

    abandon_task = asyncio.create_task(abandon_event.wait())
    done, pending = await asyncio.wait(
        {communicate_task, abandon_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    if abandon_task in done and communicate_task not in done:
        log.info("abandon during uv invocation; killing pid=%s", proc.pid)
        await asyncio.to_thread(kill_process_group, proc, 1.0)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(communicate_task, timeout=2.0)
        for p in pending:
            p.cancel()
            with contextlib.suppress(asyncio.CancelledError, BaseException):
                await p
        raise VenvAbandonedError("uv process killed by abandon signal")

    for p in pending:
        p.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await p
    stdout_b, stderr_b = communicate_task.result()
    return (
        proc.returncode if proc.returncode is not None else -1,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


def _build_env(
    venv_path: Path,
    task_env: list[str] | None,
    *,
    secrets: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Environment for the task commands. See task_env.build_scrubbed_base_env.

    ``task_env is None`` -> legacy (full copy); a list -> strict, with the
    server ``secrets`` on top of the worker environment.
    """
    env = build_scrubbed_base_env(task_env, secrets=secrets)
    bin_dir = _venv_bin_dir(venv_path)
    env["VIRTUAL_ENV"] = str(venv_path)
    existing_path = env.get("PATH", "")
    env["PATH"] = f"{bin_dir}{os.pathsep}{existing_path}" if existing_path else str(bin_dir)
    env.pop("PYTHONHOME", None)
    return env


@asynccontextmanager
async def ephemeral_venv(
    execution_id: str,
    requirements: list[str],
    *,
    uv_bin: str,
    uv_cache_dir: Path | None,
    task_env: list[str] | None = None,
    secrets: Mapping[str, str] | None = None,
    abandon_event: asyncio.Event | None = None,
) -> AsyncIterator[VenvHandle]:
    """Create an ephemeral venv, install requirements, yield the handle, then clean up."""
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in execution_id)[:32]
    venv_path = Path(tempfile.gettempdir()) / f"orcaster-venv-{safe_id}-{uuid.uuid4().hex[:8]}"

    extra_env: dict[str, str] = {}
    if uv_cache_dir is not None:
        extra_env["UV_CACHE_DIR"] = str(uv_cache_dir)

    # Once per execution and before launching uv: build_scrubbed_base_env is
    # called several times (uv venv, uv pip install, _build_env) and must not warn.
    # `execution_id` is actually the task id, as main.py passes it
    # (`execution_id=str(task.task_id)`); the warning labels it `task_id`. It gets the
    # same `secrets` as build_scrubbed_base_env: a name the server supplies is
    # injected and does not count as missing.
    warn_missing_declared(task_env, task_id=execution_id, secrets=secrets)

    try:
        rc, out, err = await _spawn_uv(
            [uv_bin, "venv", str(venv_path)],
            abandon_event=abandon_event,
            extra_env=extra_env,
            task_env=task_env,
            secrets=secrets,
        )
        if rc != 0:
            raise VenvCreationError(f"uv venv failed (exit={rc}): {err.strip() or out.strip()}")

        python = _venv_python(venv_path)
        if not python.exists():
            raise VenvCreationError(f"uv venv reported success but python not found at {python}")

        if requirements:
            install_argv = [
                uv_bin,
                "pip",
                "install",
                "--python",
                str(python),
                *requirements,
            ]
            rc, out, err = await _spawn_uv(
                install_argv,
                abandon_event=abandon_event,
                extra_env=extra_env,
                task_env=task_env,
                secrets=secrets,
            )
            if rc != 0:
                raise VenvInstallError(
                    f"uv pip install failed (exit={rc}): {err.strip() or out.strip()}"
                )

        env = _build_env(venv_path, task_env, secrets=secrets)
        yield VenvHandle(path=venv_path, python=python, env=env)
    finally:
        await asyncio.to_thread(shutil.rmtree, venv_path, ignore_errors=True)
