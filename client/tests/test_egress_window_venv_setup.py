"""Per-task egress window around the venv setup.

Contract verified here:

    1. ``_apply_task_egress`` runs BEFORE ``ephemeral_venv``, so the
       per-task allowlist already applies during ``uv venv`` / ``uv pip install``
       (a requirement by URL, e.g. ``git+https://github.com/...``, sees the
       same network permission as the task's commands).
    2. The window closes as soon as the dispatch ends (before the venv
       context manager does its cleanup).
    3. Every exit from the setup closes it just the same: ``VenvAbandonedError``,
       venv error (``VenvInstallError``), outer timeout during the setup and
       generic crash. A stale allowlist is never left in effect for the
       next task.
    4. Legacy (``egress_allowlist`` absent and ``enforce_isolation=False``):
       the file is touched neither on apply nor on clear.

All tests observe the real allowlist file (the same one the sidecar proxy reads),
not the function mocks: what matters is what the
proxy would see in each phase.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from orcaster_worker.api import OrchestratorAPI
from orcaster_worker.config import Settings
from orcaster_worker.egress import read_allowlist, write_allowlist
from orcaster_worker.main import BusyState, _run_external
from orcaster_worker.poll import PollTaskPayload
from orcaster_worker.venv_manager import VenvAbandonedError, VenvHandle, VenvInstallError

BASE = "http://server.test"
WID = "44444444-4444-4444-4444-444444444444"

pytestmark = pytest.mark.asyncio


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        server_url="http://server.test",  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        cache_root=tmp_path / "cache",
        poll_min_interval_seconds=0.1,
        poll_max_interval_seconds=2.0,
        lease_seconds=5,
        cancel_grace_seconds=0.2,
        uv_bin="uv",
        uv_cache_dir=tmp_path / "uv-cache",
        egress_allow_file=tmp_path / "egress" / "allow.txt",
        enforce_isolation=False,
    )


def _task(
    *,
    task_id: int,
    payload: dict[str, Any],
    execution_timeout_seconds: int | None = None,
) -> PollTaskPayload:
    """Minimal PollTaskPayload: ``_run_external`` does not verify the signature (that was already
    done by ``_dispatch_poll_task`` before calling it)."""
    return PollTaskPayload(
        task_id=task_id,
        command="__run_batch__",
        payload=payload,
        created_at="2026-07-30T00:00:00Z",
        delivery_mode="claim",
        signed=False,
        signature=None,
        idempotency_key=None,
        worker_id=WID,
        target_worker_id=None,
        lease_token=f"lease-{task_id}",
        lease_expires_at="2030-01-01T00:00:00Z",
        execution_timeout_seconds=execution_timeout_seconds,
    )


def _mock_routes(task_id: int) -> list[dict[str, Any]]:
    """Mocks /complete and /output; returns the list of /complete bodies."""
    complete_calls: list[dict[str, Any]] = []

    def _capture_complete(request: httpx.Request) -> httpx.Response:
        import json

        complete_calls.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    respx.post(f"{BASE}/v1/tasks/{task_id}/complete").mock(side_effect=_capture_complete)
    respx.post(f"{BASE}/v1/tasks/{task_id}/output").mock(
        return_value=httpx.Response(202, json={"ok": True})
    )
    return complete_calls


def _fake_venv(
    events: list[tuple[str, list[str]]],
    allow_file: Path,
    *,
    raise_exc: BaseException | None = None,
    wait_for_abandon: bool = False,
) -> Callable[..., Any]:
    """Fake ``ephemeral_venv`` that records which allowlist the proxy sees.

    Records ``("venv_setup", <allowlist>)`` on entry (equivalent to the moment
    of ``uv pip install``) and ``("venv_exit", <allowlist>)`` in its cleanup.
    """

    @contextlib.asynccontextmanager
    async def _fake(
        execution_id: str,
        requirements: list[str],
        *,
        uv_bin: str,
        uv_cache_dir: Path | None,
        task_env: list[str] | None = None,
        secrets: Mapping[str, str] | None = None,
        abandon_event: asyncio.Event | None = None,
    ) -> AsyncIterator[VenvHandle]:
        events.append(("venv_setup", read_allowlist(allow_file)))
        if wait_for_abandon:
            assert abandon_event is not None
            await abandon_event.wait()
        if raise_exc is not None:
            raise raise_exc
        try:
            yield VenvHandle(
                path=Path("/nonexistent/venv"),
                python=Path("/nonexistent/venv/bin/python"),
                env={"PATH": "/usr/bin:/bin"},
            )
        finally:
            events.append(("venv_exit", read_allowlist(allow_file)))

    return _fake


def _fake_dispatch(events: list[tuple[str, list[str]]], allow_file: Path) -> Callable[..., Any]:
    async def _dispatch(
        _payload: Any,
        _settings: Any,
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        abandon_event: asyncio.Event | None = None,
        on_output: Any = None,
    ) -> tuple[str, dict[str, Any]]:
        events.append(("dispatch", read_allowlist(allow_file)))
        return "ok", {"exit_codes": [0], "stdout": "", "stderr": "", "failed_at": None}

    return _dispatch


async def _run(
    settings: Settings,
    task: PollTaskPayload,
    *,
    venv: Callable[..., Any],
    dispatch: Callable[..., Any],
) -> None:
    busy = BusyState()
    stop = asyncio.Event()
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k", stop=stop) as api:
        from unittest.mock import patch

        with (
            patch("orcaster_worker.main.ephemeral_venv", new=venv),
            patch("orcaster_worker.main.dispatch_run_batch", new=dispatch),
        ):
            await _run_external(api, settings, busy, stop, task)


# --- Happy path: apply -> venv setup -> dispatch -> clear -------------------


@respx.mock
async def test_allowlist_applies_before_venv_setup(settings: Settings) -> None:
    """The per-task allowlist is already written when the venv setup starts."""
    complete_calls = _mock_routes(900)
    events: list[tuple[str, list[str]]] = []
    task = _task(
        task_id=900,
        payload={
            "requirements": ["git+https://github.com/org/repo@v1"],
            "commands": ["echo hi"],
            "egress_allowlist": ["github.com"],
        },
    )

    await _run(
        settings,
        task,
        venv=_fake_venv(events, settings.egress_allow_file),
        dispatch=_fake_dispatch(events, settings.egress_allow_file),
    )

    phases = [name for name, _ in events]
    assert phases == ["venv_setup", "dispatch", "venv_exit"], events
    seen = dict(events)
    # The point of the contract: during the install the proxy already sees github.com.
    assert seen["venv_setup"] == ["github.com"], events
    assert seen["dispatch"] == ["github.com"], events
    # Contract "close the window as soon as the task ends": already closed before
    # the venv context manager does its cleanup.
    assert seen["venv_exit"] == [], events
    assert read_allowlist(settings.egress_allow_file) == []
    assert complete_calls and complete_calls[0]["status"] == "completed"


# --- Close on the setup failure paths ---------------------------------------


@respx.mock
async def test_clear_runs_when_venv_setup_is_abandoned(settings: Settings) -> None:
    """``VenvAbandonedError`` during the setup still closes the window."""
    complete_calls = _mock_routes(901)
    events: list[tuple[str, list[str]]] = []
    task = _task(
        task_id=901,
        payload={
            "requirements": ["torch"],
            "commands": ["echo hi"],
            "egress_allowlist": ["github.com"],
        },
    )

    await _run(
        settings,
        task,
        venv=_fake_venv(
            events, settings.egress_allow_file, raise_exc=VenvAbandonedError("abandoned")
        ),
        dispatch=_fake_dispatch(events, settings.egress_allow_file),
    )

    assert events == [("venv_setup", ["github.com"])], events
    assert read_allowlist(settings.egress_allow_file) == []
    assert complete_calls and complete_calls[0]["status"] == "cancelled"


@respx.mock
async def test_clear_runs_when_venv_setup_fails(settings: Settings) -> None:
    """A venv error (install) during the setup still closes the window."""
    complete_calls = _mock_routes(902)
    events: list[tuple[str, list[str]]] = []
    task = _task(
        task_id=902,
        payload={
            "requirements": ["nope==0"],
            "commands": ["echo hi"],
            "egress_allowlist": ["github.com"],
        },
    )

    await _run(
        settings,
        task,
        venv=_fake_venv(
            events, settings.egress_allow_file, raise_exc=VenvInstallError("uv pip install failed")
        ),
        dispatch=_fake_dispatch(events, settings.egress_allow_file),
    )

    assert events == [("venv_setup", ["github.com"])], events
    assert read_allowlist(settings.egress_allow_file) == []
    assert complete_calls and complete_calls[0]["status"] == "failed"


@respx.mock
async def test_clear_runs_when_venv_setup_crashes(settings: Settings) -> None:
    """A generic crash during the setup still closes the window."""
    complete_calls = _mock_routes(903)
    events: list[tuple[str, list[str]]] = []
    task = _task(
        task_id=903,
        payload={
            "requirements": [],
            "commands": ["echo hi"],
            "egress_allowlist": ["github.com"],
        },
    )

    await _run(
        settings,
        task,
        venv=_fake_venv(events, settings.egress_allow_file, raise_exc=RuntimeError("boom")),
        dispatch=_fake_dispatch(events, settings.egress_allow_file),
    )

    assert events == [("venv_setup", ["github.com"])], events
    assert read_allowlist(settings.egress_allow_file) == []
    assert complete_calls and complete_calls[0]["status"] == "failed"


@respx.mock
async def test_clear_runs_when_outer_timeout_hits_during_setup(settings: Settings) -> None:
    """Outer timeout during the setup: cancelled and window closed."""
    complete_calls = _mock_routes(904)
    events: list[tuple[str, list[str]]] = []
    task = _task(
        task_id=904,
        payload={
            "requirements": ["torch"],
            "commands": ["echo hi"],
            "egress_allowlist": ["github.com"],
        },
        execution_timeout_seconds=1,
    )

    await _run(
        settings,
        task,
        venv=_fake_venv(
            events,
            settings.egress_allow_file,
            raise_exc=VenvAbandonedError("outer timeout"),
            wait_for_abandon=True,
        ),
        dispatch=_fake_dispatch(events, settings.egress_allow_file),
    )

    assert events == [("venv_setup", ["github.com"])], events
    assert read_allowlist(settings.egress_allow_file) == []
    assert complete_calls and complete_calls[0]["status"] == "cancelled"
    assert complete_calls[0].get("cancelled_reason") == "outer_timeout"


# --- Cancellation between the venv setup and run_batch -----------------------


@respx.mock
async def test_cancelled_before_run_batch_closes_window_before_cleanup_and_report(
    settings: Settings,
) -> None:
    """If the task is cancelled after the venv setup but before run_batch,
    the window closes BEFORE reporting to the server and before the venv
    cleanup, same as in the happy path."""
    events: list[tuple[str, list[str]]] = []
    complete_calls: list[dict[str, Any]] = []

    def _capture_complete(request: httpx.Request) -> httpx.Response:
        import json

        complete_calls.append(json.loads(request.content))
        events.append(("complete", read_allowlist(settings.egress_allow_file)))
        return httpx.Response(200, json={"ok": True})

    respx.post(f"{BASE}/v1/tasks/906/complete").mock(side_effect=_capture_complete)
    respx.post(f"{BASE}/v1/tasks/906/output").mock(
        return_value=httpx.Response(202, json={"ok": True})
    )

    task = _task(
        task_id=906,
        payload={
            "requirements": [],
            "commands": ["echo hi"],
            "egress_allowlist": ["github.com"],
        },
    )

    busy = BusyState()
    stop = asyncio.Event()

    @contextlib.asynccontextmanager
    async def _venv_that_gets_cancelled(*_args: Any, **_kwargs: Any) -> AsyncIterator[VenvHandle]:
        events.append(("venv_setup", read_allowlist(settings.egress_allow_file)))
        # The signal arrives during the setup: on leaving the setup, _run_external
        # sees stop set and takes the early-return "cancelled before run_batch".
        stop.set()
        try:
            yield VenvHandle(
                path=Path("/nonexistent/venv"),
                python=Path("/nonexistent/venv/bin/python"),
                env={"PATH": "/usr/bin:/bin"},
            )
        finally:
            events.append(("venv_exit", read_allowlist(settings.egress_allow_file)))

    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k", stop=stop) as api:
        from unittest.mock import patch

        with (
            patch("orcaster_worker.main.ephemeral_venv", new=_venv_that_gets_cancelled),
            patch(
                "orcaster_worker.main.dispatch_run_batch",
                new=_fake_dispatch(events, settings.egress_allow_file),
            ),
        ):
            await _run_external(api, settings, busy, stop, task)

    phases = [name for name, _ in events]
    assert phases == ["venv_setup", "complete", "venv_exit"], events
    seen = dict(events)
    assert seen["venv_setup"] == ["github.com"], events
    # The window is already closed when reporting to the server and when the venv
    # context manager does its cleanup.
    assert seen["complete"] == [], events
    assert seen["venv_exit"] == [], events
    assert complete_calls and complete_calls[0]["status"] == "cancelled"


# --- Boot sweep: orphaned allowlist from a hard kill --------------------------


async def test_boot_sweep_clears_orphaned_allowlist(settings: Settings) -> None:
    """A hard kill (SIGKILL/OOM/power loss) during the open window leaves the
    task's allowlist dead in the volume; the worker startup cleans it
    up before anything else."""
    from unittest.mock import patch

    from orcaster_worker.main import run

    write_allowlist(settings.egress_allow_file, ["github.com", "evil.example"])

    with (
        patch(
            "orcaster_worker.main._ensure_pubkey_loadable_or_die",
            side_effect=SystemExit(1),
        ),
        pytest.raises(SystemExit),
    ):
        await run(settings)

    assert read_allowlist(settings.egress_allow_file) == []


async def test_boot_sweep_does_not_create_missing_allowlist_file(settings: Settings) -> None:
    """Without egress topology (the file does not exist) the boot sweep creates
    nothing: dev/CI without an egress volume do not gain a spurious file."""
    from unittest.mock import patch

    from orcaster_worker.main import run

    assert not settings.egress_allow_file.exists()

    with (
        patch(
            "orcaster_worker.main._ensure_pubkey_loadable_or_die",
            side_effect=SystemExit(1),
        ),
        pytest.raises(SystemExit),
    ):
        await run(settings)

    assert not settings.egress_allow_file.exists()
    assert not settings.egress_allow_file.parent.exists()


# --- Legacy: without egress_allowlist the file is not touched ---------------


@respx.mock
async def test_legacy_payload_never_touches_the_allowlist_file(settings: Settings) -> None:
    """``egress_allowlist`` absent + ``enforce_isolation=False`` -> the file
    stays exactly as it was (neither apply nor clear overwrites it)."""
    complete_calls = _mock_routes(905)
    events: list[tuple[str, list[str]]] = []
    write_allowlist(settings.egress_allow_file, ["preexisting.example"])
    task = _task(
        task_id=905,
        payload={"requirements": [], "commands": ["echo hi"]},
    )

    await _run(
        settings,
        task,
        venv=_fake_venv(events, settings.egress_allow_file),
        dispatch=_fake_dispatch(events, settings.egress_allow_file),
    )

    assert [name for name, _ in events] == ["venv_setup", "dispatch", "venv_exit"], events
    assert all(seen == ["preexisting.example"] for _, seen in events), events
    assert read_allowlist(settings.egress_allow_file) == ["preexisting.example"]
    assert complete_calls and complete_calls[0]["status"] == "completed"
