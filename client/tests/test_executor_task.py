"""Tests for the executor in a separate asyncio.Task -- ephemeral venv.

Validates the worker's topology:

    * poll_loop keeps emitting polls during a long task (implicit heartbeats
      -- precondition for the lease not to expire).
    * A server-side cancel signal reaches the worker while the task is
      running and sets ``abandon_event`` in <2x idle_poll_interval.
    * SIGTERM/SIGINT (modeled as ``stop.set()``) ends the worker in
      <GRACE_SECONDS and the execution is reported as ``cancelled``.
    * An executor crash (unhandled raise) is reaped with
      ``failed/exit_code=2/stderr="executor_crashed: <Type>"`` and leaves
      BusyState clean for the next poll.
    * The short happy path keeps working identically (regression).

External tasks carry ``payload={"requirements": [...], "commands": [...]}``
and run inside an ephemeral venv via ``ephemeral_venv``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import shutil
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orcaster_worker.api import OrchestratorAPI
from orcaster_worker.config import Settings
from orcaster_worker.main import (
    GRACE_SECONDS,
    BusyState,
    _reap_executor_task,
    _shutdown_executor,
    poll_loop,
)
from orcaster_worker.poll import PollTaskPayload
from orcaster_worker.signing import build_task_message, reset_key_cache
from orcaster_worker.task_cache import ResetStats, ensure_task_cache_dir

BASE = "http://server.test"
WID = "11111111-1111-1111-1111-111111111111"
POLL_URL = f"{BASE}/v1/workers/{WID}/poll"

_UV_BIN = shutil.which("uv")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        server_url="http://server.test",  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        poll_min_interval_seconds=0.1,
        poll_max_interval_seconds=2.0,
        lease_seconds=30,
        cancel_grace_seconds=0.2,
        uv_bin=_UV_BIN or "uv",
        uv_cache_dir=tmp_path / "uv-cache",
    )


def _make_task_payload(
    *,
    task_id: int = 100,
    commands: list[str] | None = None,
    requirements: list[str] | None = None,
    lease_token: str = "lease-100",
    priv: Ed25519PrivateKey | None = None,
    command: str = "__run_batch__",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "requirements": requirements or [],
        "commands": commands or ["sleep 60"],
    }
    created_at = "2026-05-19T00:00:00Z"
    signature: str | None = None
    if priv is not None:
        msg = build_task_message(
            task_id=task_id,
            worker_id=WID,
            command=command,
            payload=payload,
            idempotency_key=None,
            created_at_iso=created_at,
        )
        signature = base64.b64encode(priv.sign(msg)).decode("ascii")
    return {
        "task_id": task_id,
        "command": command,
        "payload": payload,
        "created_at": created_at,
        "delivery_mode": "claim",
        "signed": priv is not None,
        "signature": signature,
        "idempotency_key": None,
        "worker_id": WID,
        "target_worker_id": None,
        "lease_token": lease_token,
        "lease_expires_at": "2030-01-01T00:00:00Z",
    }


@pytest.fixture
def signing_priv(tmp_path: Path, settings: Settings) -> Ed25519PrivateKey:
    """Generates a keypair, loads the pub key into settings, returns the priv key.

    The poll_loop tests need the client to be able to verify the signature
    of every incoming task; this fixture provides the priv key so the test can build
    correctly signed payloads.
    """
    priv = Ed25519PrivateKey.generate()
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pub_path = tmp_path / "ed25519.pub.pem"
    pub_path.write_bytes(pub_pem)
    reset_key_cache()
    settings.ed25519_public_key_path = pub_path
    return priv


# --- BusyState helpers -------------------------------------------------------


def test_busy_state_is_busy_property_uses_executor_task() -> None:
    """is_busy is decided by executor_task (not by task_id)."""
    busy = BusyState()
    assert not busy.is_busy
    # Without executor_task, we are not busy even if there is a legacy task_id.
    busy.task_id = 5
    busy.lease_token = "x"
    assert not busy.is_busy


def test_busy_state_reset_clears_executor_task() -> None:
    busy = BusyState()
    busy.task_id = 1
    busy.lease_token = "lt"

    async def _runner() -> None:
        async def _noop() -> None:
            return None

        busy.executor_task = asyncio.create_task(_noop())
        await busy.executor_task
        assert busy.is_busy is False  # done() -> not busy
        busy.reset()
        assert busy.executor_task is None
        assert busy.task_id is None
        assert busy.task_payload is None

    asyncio.run(_runner())


# --- Reaping the executor task ----------------------------------------------


@respx.mock
async def test_reap_executor_crash_reports_failed_with_exit_code_2(
    settings: Settings,
) -> None:
    """Unhandled executor crash -> failed/exit_code=2/stderr=executor_crashed.

    The reaper must use ``busy.task_payload`` (set by the poll_loop at
    spawn) to build ``complete()``. The diagnostic text travels
    as an output_event via /output, not in the /complete body.
    """
    complete_route = respx.post(f"{BASE}/v1/tasks/200/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    output_route = respx.post(f"{BASE}/v1/tasks/200/output").mock(
        return_value=httpx.Response(202, json={"ok": True})
    )

    task = PollTaskPayload(
        task_id=200,
        command="/bin/true",
        payload={"requirements": [], "commands": ["true"]},
        created_at="2026-05-19T00:00:00Z",
        delivery_mode="claim",
        signed=False,
        signature=None,
        idempotency_key=None,
        worker_id=WID,
        target_worker_id=None,
        lease_token="lease-200",
        lease_expires_at="2030-01-01T00:00:00Z",
    )

    async def _crasher() -> None:
        raise RuntimeError("synthetic boom")

    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        busy = BusyState()
        busy.task_id = task.task_id
        busy.lease_token = task.lease_token
        busy.task_payload = task
        busy.executor_task = asyncio.create_task(_crasher())
        # Wait for it to finish with an exception before reaping.
        with pytest.raises(RuntimeError):
            await busy.executor_task

        await _reap_executor_task(api, busy, settings)

    assert complete_route.called
    body = complete_route.calls.last.request.read().decode()
    assert '"status":"failed"' in body or '"status": "failed"' in body
    assert '"exit_code":2' in body or '"exit_code": 2' in body
    # The diagnostic text travels as an output_event, not in /complete.
    assert output_route.called
    import json as _json

    output_body = _json.loads(output_route.calls.last.request.read())
    events = output_body.get("events", [])
    assert any("executor_crashed: RuntimeError" in e.get("text", "") for e in events), (
        f"diagnostic text not found in events={events}"
    )
    # BusyState must be left clean for the next poll.
    assert busy.executor_task is None
    assert busy.task_id is None
    assert busy.task_payload is None


async def test_reap_executor_normal_completion_just_clears_busy(
    settings: Settings,
) -> None:
    """Normal executor termination (no exception): the reaper must NOT
    re-send complete (the dispatch already did); it only clears BusyState."""
    task = PollTaskPayload(
        task_id=201,
        command="/bin/true",
        payload={"requirements": [], "commands": ["true"]},
        created_at="2026-05-19T00:00:00Z",
        delivery_mode="claim",
        signed=False,
        signature=None,
        idempotency_key=None,
        worker_id=WID,
        target_worker_id=None,
        lease_token="lease-201",
        lease_expires_at="2030-01-01T00:00:00Z",
    )

    async def _ok() -> None:
        return None

    with respx.mock(assert_all_called=False) as mock:
        complete_route = mock.post(f"{BASE}/v1/tasks/201/complete").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
            busy = BusyState()
            busy.task_id = task.task_id
            busy.lease_token = task.lease_token
            busy.task_payload = task
            busy.executor_task = asyncio.create_task(_ok())
            await busy.executor_task
            await _reap_executor_task(api, busy)

    assert not complete_route.called
    assert busy.executor_task is None
    assert busy.task_id is None


# --- Polls continue during a long task --------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: sleep binary")
@respx.mock
async def test_poll_loop_emits_polls_while_executor_task_runs(
    settings: Settings,
    signing_priv: Ed25519PrivateKey,
) -> None:
    """While an executor_task is running (long sleep), the poll_loop
    must keep emitting polls. We wait for >=3 polls during the execution.

    This is the structural property that enables the ephemeral venv.
    """
    poll_count = {"n": 0}
    task_payload = _make_task_payload(
        task_id=300,
        commands=["sleep 3"],
        lease_token="lease-300",
        priv=signing_priv,
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        poll_count["n"] += 1
        if poll_count["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "task": task_payload,
                    "cancel_signals": [],
                    "next_poll_after_seconds": 1,
                },
            )
        return httpx.Response(
            200,
            json={
                "task": None,
                "cancel_signals": [],
                "next_poll_after_seconds": 1,
            },
        )

    respx.post(POLL_URL).mock(side_effect=_handler)
    complete_route = respx.post(f"{BASE}/v1/tasks/300/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.post(f"{BASE}/v1/tasks/300/output").mock(
        return_value=httpx.Response(202, json={"ok": True})
    )

    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        busy = BusyState()
        stop = asyncio.Event()
        loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop))
        # Wait long enough for >=3 polls to be emitted and the complete
        # to arrive. Venv creation adds overhead (~0.5-1.5s),
        # so we take a wide window.
        for _ in range(150):
            await asyncio.sleep(0.2)
            if complete_route.called and poll_count["n"] >= 4:
                break
        stop.set()
        await asyncio.wait_for(loop_task, timeout=10.0)

    assert poll_count["n"] >= 3, f"poll_loop emitted only {poll_count['n']} polls; expected >=3."
    # The long task eventually finishes; we should have seen the complete.
    assert complete_route.called


# --- Server-side cancel during a long task ----------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: sleep + killpg")
@respx.mock
async def test_cancel_signal_during_long_task_sets_abandon_under_2x_interval(
    settings: Settings,
    signing_priv: Ed25519PrivateKey,
) -> None:
    """A server-side cancel during a long task must reach the worker on the
    next poll (<2x idle_poll_interval) and set busy.abandon_event."""
    poll_count = {"n": 0}
    cancel_emitted_at = {"t": None}
    abandon_seen_at = {"t": None}

    task_payload = _make_task_payload(
        task_id=400,
        commands=["sleep 10"],
        lease_token="lease-400",
        priv=signing_priv,
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        poll_count["n"] += 1
        if poll_count["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "task": task_payload,
                    "cancel_signals": [],
                    "next_poll_after_seconds": 1,
                },
            )
        if poll_count["n"] == 3:
            cancel_emitted_at["t"] = asyncio.get_event_loop().time()
            return httpx.Response(
                200,
                json={
                    "task": None,
                    "cancel_signals": [{"task_id": 400, "lease_token": "lease-400"}],
                    "next_poll_after_seconds": 1,
                },
            )
        return httpx.Response(
            200,
            json={
                "task": None,
                "cancel_signals": [],
                "next_poll_after_seconds": 1,
            },
        )

    respx.post(POLL_URL).mock(side_effect=_handler)
    complete_route = respx.post(f"{BASE}/v1/tasks/400/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.post(f"{BASE}/v1/tasks/400/output").mock(
        return_value=httpx.Response(202, json={"ok": True})
    )

    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        busy = BusyState()
        stop = asyncio.Event()
        loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop))

        for _ in range(100):
            await asyncio.sleep(0.1)
            if busy.abandon_event.is_set() and abandon_seen_at["t"] is None:
                abandon_seen_at["t"] = asyncio.get_event_loop().time()
                break
        # Wait for the complete() to be reported after the abandon (includes
        # kill_process_group + reap of the executor_task by the next poll).
        for _ in range(50):
            if complete_route.called:
                break
            await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=10.0)

    assert cancel_emitted_at["t"] is not None, "server never emitted cancel"
    assert abandon_seen_at["t"] is not None, "abandon_event was never set"
    elapsed = abandon_seen_at["t"] - cancel_emitted_at["t"]
    # idle_poll_interval = 1s (server_hint) clamped between [0.1, 2.0]; 2x = 2s.
    # We allow margin for event loop scheduling in CI.
    assert elapsed < 2.0, f"cancel detected after {elapsed:.2f}s; expected <2.0s (2x interval)."
    # The client must report cancelled to the server.
    assert complete_route.called
    body = complete_route.calls.last.request.read().decode()
    assert '"status":"cancelled"' in body or '"status": "cancelled"' in body


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: sleep + killpg")
@respx.mock
async def test_cancel_during_second_command_of_multi_command_batch_reports_cancelled(
    settings: Settings,
    signing_priv: Ed25519PrivateKey,
) -> None:
    """End-to-end: with a multi-command batch, if the server-side cancel arrives
    during the second command (the first already finished OK), the dispatcher
    must return ``abandoned`` and the worker report ``cancelled`` to the server.

    Without the fix the dispatcher returned ``("ok", ...)`` and _run_external
    depended on re-reading ``busy.abandon_event``; now the status flows through the
    return value.
    """
    poll_count = {"n": 0}
    task_payload = _make_task_payload(
        task_id=401,
        commands=["echo first-done", "sleep 30"],
        lease_token="lease-401",
        priv=signing_priv,
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        poll_count["n"] += 1
        if poll_count["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "task": task_payload,
                    "cancel_signals": [],
                    "next_poll_after_seconds": 1,
                },
            )
        if poll_count["n"] == 3:
            return httpx.Response(
                200,
                json={
                    "task": None,
                    "cancel_signals": [{"task_id": 401, "lease_token": "lease-401"}],
                    "next_poll_after_seconds": 1,
                },
            )
        return httpx.Response(
            200,
            json={
                "task": None,
                "cancel_signals": [],
                "next_poll_after_seconds": 1,
            },
        )

    respx.post(POLL_URL).mock(side_effect=_handler)
    complete_route = respx.post(f"{BASE}/v1/tasks/401/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.post(f"{BASE}/v1/tasks/401/output").mock(
        return_value=httpx.Response(202, json={"ok": True})
    )

    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        busy = BusyState()
        stop = asyncio.Event()
        loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop))

        for _ in range(100):
            await asyncio.sleep(0.1)
            if complete_route.called:
                break
        stop.set()
        await asyncio.wait_for(loop_task, timeout=10.0)

    assert complete_route.called, "worker never reported complete after cancel"
    body = complete_route.calls.last.request.read().decode()
    assert '"status":"cancelled"' in body or '"status": "cancelled"' in body
    # Post-dispatch: stdout was already streamed via /output; /complete does not re-send it.


# --- Cooperative shutdown ----------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: sleep + killpg")
@respx.mock
async def test_cooperative_shutdown_ends_in_less_than_grace(
    settings: Settings,
    signing_priv: Ed25519PrivateKey,
) -> None:
    """SIGTERM (modeled as stop.set + _shutdown_executor) during a long task
    ends the worker in <GRACE_SECONDS and leaves the execution as
    'cancelled'."""
    poll_count = {"n": 0}
    task_payload = _make_task_payload(
        task_id=500,
        commands=["sleep 30"],  # much longer than GRACE_SECONDS
        lease_token="lease-500",
        priv=signing_priv,
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        poll_count["n"] += 1
        if poll_count["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "task": task_payload,
                    "cancel_signals": [],
                    "next_poll_after_seconds": 1,
                },
            )
        return httpx.Response(
            200,
            json={
                "task": None,
                "cancel_signals": [],
                "next_poll_after_seconds": 1,
            },
        )

    respx.post(POLL_URL).mock(side_effect=_handler)
    complete_route = respx.post(f"{BASE}/v1/tasks/500/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.post(f"{BASE}/v1/tasks/500/output").mock(
        return_value=httpx.Response(202, json={"ok": True})
    )

    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        busy = BusyState()
        stop = asyncio.Event()
        loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop))

        # Wait for the executor_task to start.
        for _ in range(50):
            await asyncio.sleep(0.1)
            if busy.is_busy:
                break
        assert busy.is_busy, "executor_task never started"

        t0 = asyncio.get_event_loop().time()
        stop.set()
        # Wait for poll_loop to exit (it may take up to 1 iteration).
        await asyncio.wait_for(loop_task, timeout=GRACE_SECONDS + 2.0)
        # After the loop, simulate run()'s shutdown path.
        await _shutdown_executor(api, busy)
        elapsed = asyncio.get_event_loop().time() - t0

    assert elapsed < GRACE_SECONDS + 1.0, (
        f"shutdown tardo {elapsed:.2f}s; esperaba <{GRACE_SECONDS + 1.0}s."
    )
    assert busy.executor_task is None
    # It must have been reported to the server.
    assert complete_route.called
    body = complete_route.calls.last.request.read().decode()
    assert '"status":"cancelled"' in body or '"status": "cancelled"' in body


# --- Happy path (regression, short task) -------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: echo")
@respx.mock
async def test_happy_path_short_task_still_works(
    settings: Settings,
    signing_priv: Ed25519PrivateKey,
) -> None:
    """Regression: a short task keeps running and being reported
    completed identically to before."""
    poll_count = {"n": 0}
    task_payload = _make_task_payload(
        task_id=600,
        commands=["echo phase21-hello"],
        lease_token="lease-600",
        priv=signing_priv,
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        poll_count["n"] += 1
        if poll_count["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "task": task_payload,
                    "cancel_signals": [],
                    "next_poll_after_seconds": 1,
                },
            )
        return httpx.Response(
            200,
            json={
                "task": None,
                "cancel_signals": [],
                "next_poll_after_seconds": 1,
            },
        )

    respx.post(POLL_URL).mock(side_effect=_handler)
    complete_route = respx.post(f"{BASE}/v1/tasks/600/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.post(f"{BASE}/v1/tasks/600/output").mock(
        return_value=httpx.Response(202, json={"ok": True})
    )

    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        busy = BusyState()
        stop = asyncio.Event()
        loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop))
        for _ in range(50):
            await asyncio.sleep(0.1)
            if complete_route.called:
                break
        stop.set()
        await asyncio.wait_for(loop_task, timeout=5.0)

    assert complete_route.called
    body = complete_route.calls.last.request.read().decode()
    # Post-dispatch: stdout was already streamed via /output; /complete does not re-send it.
    assert '"status":"completed"' in body or '"status": "completed"' in body
    # After reap, busy must be clean.
    assert busy.executor_task is None
    assert busy.task_id is None


# --- Malformed payload -------------------------------------------


@pytest.mark.skipif(_UV_BIN is None, reason="requires uv binary in PATH")
@respx.mock
async def test_external_task_malformed_payload_fails_with_exit_code_2(
    settings: Settings,
    signing_priv: Ed25519PrivateKey,
) -> None:
    """Payload without 'commands' (legacy format {'args': [...]}) -> failed/exit_code=2."""
    from orcaster_worker.main import _dispatch_poll_task

    task_id = 700
    payload = {"args": ["legacy", "format"]}
    created_at = "2026-05-19T00:00:00Z"
    msg = build_task_message(
        task_id=task_id,
        worker_id=WID,
        command="__run_batch__",
        payload=payload,
        idempotency_key=None,
        created_at_iso=created_at,
    )
    sig = base64.b64encode(signing_priv.sign(msg)).decode("ascii")
    task = PollTaskPayload(
        task_id=task_id,
        command="__run_batch__",
        payload=payload,
        created_at=created_at,
        delivery_mode="claim",
        signed=True,
        signature=sig,
        idempotency_key=None,
        worker_id=WID,
        target_worker_id=None,
        lease_token="lease-700",
        lease_expires_at="2030-01-01T00:00:00Z",
    )

    complete_route = respx.post(f"{BASE}/v1/tasks/{task_id}/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    output_route = respx.post(f"{BASE}/v1/tasks/{task_id}/output").mock(
        return_value=httpx.Response(202, json={"ok": True})
    )
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        busy = BusyState()
        stop = asyncio.Event()
        await _dispatch_poll_task(api, settings, busy, stop, task)

    assert complete_route.called
    body = complete_route.calls.last.request.read().decode()
    assert '"status":"failed"' in body or '"status": "failed"' in body
    assert '"exit_code":2' in body or '"exit_code": 2' in body
    # The payload error text travels as an output_event, not in /complete.
    assert output_route.called
    import json as _json

    output_events = _json.loads(output_route.calls.last.request.read()).get("events", [])
    assert any("task payload must include" in e.get("text", "") for e in output_events), (
        f"payload error text not found in events={output_events}"
    )


# --- Task-scoped scratch cache ----------------------------------------------


@pytest.mark.skipif(_UV_BIN is None, reason="requires uv binary in PATH")
@respx.mock
async def test_external_task_injects_orcaster_cache_dir(
    settings: Settings,
    signing_priv: Ed25519PrivateKey,
    tmp_path: Path,
) -> None:
    """_run_external must inject ORCASTER_CACHE_DIR into the subprocess env,
    pointing to an existing dir under <cache_root>/tasks/<key>/.

    We validate via a ``dispatch_run_batch`` mock to capture the env without
    needing to run the user script.
    """
    from unittest.mock import AsyncMock, patch

    from orcaster_worker.main import _dispatch_poll_task

    settings.cache_root = tmp_path / "scratch-cache"
    # This test validates the cache dir injection, not its lifecycle: we turn off
    # deletion on success so we can assert the dir persists.
    settings.cache_delete_on_success = False

    task_id = 800
    idempotency_key = "11111111-2222-3333-4444-555555555555"
    payload = {"requirements": [], "commands": ["echo cached"]}
    created_at = "2026-05-19T00:00:00Z"
    msg = build_task_message(
        task_id=task_id,
        worker_id=WID,
        command="__run_batch__",
        payload=payload,
        idempotency_key=idempotency_key,
        created_at_iso=created_at,
    )
    sig = base64.b64encode(signing_priv.sign(msg)).decode("ascii")
    task = PollTaskPayload(
        task_id=task_id,
        command="__run_batch__",
        payload=payload,
        created_at=created_at,
        delivery_mode="claim",
        signed=True,
        signature=sig,
        idempotency_key=idempotency_key,
        worker_id=WID,
        target_worker_id=None,
        lease_token="lease-800",
        lease_expires_at="2030-01-01T00:00:00Z",
    )

    captured_env: dict[str, dict[str, str]] = {}

    async def _fake_dispatch(
        _payload, _settings, *, env=None, cwd=None, abandon_event=None, on_output=None
    ):
        captured_env["env"] = dict(env or {})
        return "ok", {
            "exit_codes": [0],
            "stdout": "",
            "stderr": "",
            "failed_at": None,
        }

    complete_route = respx.post(f"{BASE}/v1/tasks/{task_id}/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        busy = BusyState()
        stop = asyncio.Event()
        with patch(
            "orcaster_worker.main.dispatch_run_batch",
            new=AsyncMock(side_effect=_fake_dispatch),
        ):
            await _dispatch_poll_task(api, settings, busy, stop, task)

    assert complete_route.called
    env = captured_env["env"]
    assert "ORCASTER_CACHE_DIR" in env, f"env keys: {sorted(env.keys())}"
    cache_dir = Path(env["ORCASTER_CACHE_DIR"])
    assert cache_dir.exists() and cache_dir.is_dir()
    # Layout: <cache_root>/tasks/<idempotency_key>/
    assert cache_dir.parent == settings.cache_root / "tasks"
    assert cache_dir.name == idempotency_key
    # Pre-set ecosystem cache variables under ORCASTER_CACHE_DIR.
    assert env.get("HF_HOME") == str(cache_dir / "hf")
    assert env.get("PIP_CACHE_DIR") == str(cache_dir / "pip")
    assert env.get("XDG_CACHE_HOME") == str(cache_dir / "xdg")


# --- Cache deletion after success -------------------------------------------


def _signed_run_batch_task(
    signing_priv: Ed25519PrivateKey,
    *,
    task_id: int,
    idempotency_key: str,
    payload: dict,
    lease: str = "lease",
    execution_timeout_seconds: int | None = None,
) -> PollTaskPayload:
    """Builds a signed run_batch PollTaskPayload for the cache tests."""
    created_at = "2026-05-19T00:00:00Z"
    msg = build_task_message(
        task_id=task_id,
        worker_id=WID,
        command="__run_batch__",
        payload=payload,
        idempotency_key=idempotency_key,
        created_at_iso=created_at,
    )
    sig = base64.b64encode(signing_priv.sign(msg)).decode("ascii")
    return PollTaskPayload(
        task_id=task_id,
        command="__run_batch__",
        payload=payload,
        created_at=created_at,
        delivery_mode="claim",
        signed=True,
        signature=sig,
        idempotency_key=idempotency_key,
        worker_id=WID,
        target_worker_id=None,
        lease_token=lease,
        lease_expires_at="2030-01-01T00:00:00Z",
        execution_timeout_seconds=execution_timeout_seconds,
    )


async def _run_with_dispatch(
    settings: Settings, task: PollTaskPayload, *, status: str, exit_code: int
) -> Path:
    """Drives _run_external with a mocked dispatch that returns ``status`` and
    creates a file inside ORCASTER_CACHE_DIR. Returns the captured cache_dir."""
    from unittest.mock import AsyncMock, patch

    from orcaster_worker.main import _dispatch_poll_task

    captured: dict[str, Path] = {}

    async def _fake_dispatch(
        _payload, _settings, *, env=None, cwd=None, abandon_event=None, on_output=None
    ):
        cd = Path((env or {})["ORCASTER_CACHE_DIR"])
        (cd / "hf").mkdir(parents=True, exist_ok=True)
        (cd / "hf" / "model.bin").write_text("weights")
        captured["cache_dir"] = cd
        return status, {
            "exit_codes": [exit_code],
            "stdout": "",
            "stderr": "",
            "failed_at": None,
        }

    respx.post(f"{BASE}/v1/tasks/{task.task_id}/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        busy = BusyState()
        stop = asyncio.Event()
        with patch(
            "orcaster_worker.main.dispatch_run_batch",
            new=AsyncMock(side_effect=_fake_dispatch),
        ):
            await _dispatch_poll_task(api, settings, busy, stop, task)
    return captured["cache_dir"]


@pytest.mark.skipif(_UV_BIN is None, reason="requires uv binary in PATH")
@respx.mock
async def test_cache_deleted_on_success(
    settings: Settings, signing_priv: Ed25519PrivateKey, tmp_path: Path
) -> None:
    """With cache_delete_on_success=True (default) and status `ok`, the
    task-scoped dir is deleted after reporting `completed`."""
    settings.cache_root = tmp_path / "scratch-cache"
    assert settings.cache_delete_on_success is True
    task = _signed_run_batch_task(
        signing_priv,
        task_id=900,
        idempotency_key="aaaaaaaa-0000-0000-0000-000000000001",
        payload={"requirements": [], "commands": ["echo ok"]},
    )
    cache_dir = await _run_with_dispatch(settings, task, status="ok", exit_code=0)
    assert not cache_dir.exists(), "the cache should have been deleted after success"


@pytest.mark.skipif(_UV_BIN is None, reason="requires uv binary in PATH")
@respx.mock
async def test_cache_preserved_on_failure(
    settings: Settings, signing_priv: Ed25519PrivateKey, tmp_path: Path
) -> None:
    """On failure (status != ok) the cache is preserved so a retry can reuse the
    download."""
    settings.cache_root = tmp_path / "scratch-cache"
    task = _signed_run_batch_task(
        signing_priv,
        task_id=901,
        idempotency_key="aaaaaaaa-0000-0000-0000-000000000002",
        payload={"requirements": [], "commands": ["false"]},
    )
    cache_dir = await _run_with_dispatch(settings, task, status="error", exit_code=1)
    assert cache_dir.exists(), "the cache must NOT be deleted on failure"


@pytest.mark.skipif(_UV_BIN is None, reason="requires uv binary in PATH")
@respx.mock
async def test_cache_preserved_when_flag_disabled(
    settings: Settings, signing_priv: Ed25519PrivateKey, tmp_path: Path
) -> None:
    """With cache_delete_on_success=False the cache is preserved even on success."""
    settings.cache_root = tmp_path / "scratch-cache"
    settings.cache_delete_on_success = False
    task = _signed_run_batch_task(
        signing_priv,
        task_id=902,
        idempotency_key="aaaaaaaa-0000-0000-0000-000000000003",
        payload={"requirements": [], "commands": ["echo ok"]},
    )
    cache_dir = await _run_with_dispatch(settings, task, status="ok", exit_code=0)
    assert cache_dir.exists(), "with the flag off the cache must not be deleted"


# --- Payload extractor ------------------------------------------


def test_extract_task_payload_with_timeout() -> None:
    """payload with a valid timeout_seconds -> third position = the value."""
    from orcaster_worker.main import _extract_task_payload

    parsed = _extract_task_payload(
        {"requirements": ["pkg"], "commands": ["echo hi"], "timeout_seconds": 3}
    )
    assert parsed is not None
    reqs, cmds, timeout = parsed
    assert reqs == ["pkg"]
    assert cmds == ["echo hi"]
    assert timeout == 3


def test_extract_task_payload_without_timeout() -> None:
    """payload without timeout_seconds -> third position = None."""
    from orcaster_worker.main import _extract_task_payload

    parsed = _extract_task_payload({"requirements": [], "commands": ["echo hi"]})
    assert parsed is not None
    reqs, cmds, timeout = parsed
    assert reqs == []
    assert cmds == ["echo hi"]
    assert timeout is None


@pytest.mark.skipif(_UV_BIN is None, reason="requires uv binary in PATH")
@respx.mock
async def test_external_task_propagates_timeout_seconds_to_dispatcher(
    settings: Settings,
    signing_priv: Ed25519PrivateKey,
    tmp_path: Path,
) -> None:
    """timeout_seconds from the payload must reach the dispatcher."""
    from unittest.mock import AsyncMock, patch

    from orcaster_worker.main import _dispatch_poll_task

    settings.cache_root = tmp_path / "scratch-cache"

    task_id = 801
    idempotency_key = "22222222-2222-3333-4444-555555555555"
    payload = {
        "requirements": [],
        "commands": ["sleep 30"],
        "timeout_seconds": 3,
    }
    created_at = "2026-05-19T00:00:00Z"
    msg = build_task_message(
        task_id=task_id,
        worker_id=WID,
        command="__run_batch__",
        payload=payload,
        idempotency_key=idempotency_key,
        created_at_iso=created_at,
    )
    sig = base64.b64encode(signing_priv.sign(msg)).decode("ascii")
    task = PollTaskPayload(
        task_id=task_id,
        command="__run_batch__",
        payload=payload,
        created_at=created_at,
        delivery_mode="claim",
        signed=True,
        signature=sig,
        idempotency_key=idempotency_key,
        worker_id=WID,
        target_worker_id=None,
        lease_token="lease-801",
        lease_expires_at="2030-01-01T00:00:00Z",
    )

    captured: dict[str, Any] = {}

    async def _fake_dispatch(
        _payload, _settings, *, env=None, cwd=None, abandon_event=None, on_output=None
    ):
        captured["payload"] = dict(_payload)
        return "ok", {
            "exit_codes": [0],
            "stdout": "",
            "stderr": "",
            "failed_at": None,
        }

    respx.post(f"{BASE}/v1/tasks/{task_id}/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        busy = BusyState()
        stop = asyncio.Event()
        with patch(
            "orcaster_worker.main.dispatch_run_batch",
            new=AsyncMock(side_effect=_fake_dispatch),
        ):
            await _dispatch_poll_task(api, settings, busy, stop, task)

    assert captured["payload"]["commands"] == ["sleep 30"]
    assert captured["payload"]["timeout_seconds"] == 3


# --- Streaming: /complete does not duplicate output ----------------------------------


@pytest.mark.asyncio
async def test_streaming_completes_with_none_output(
    settings: Settings,
    signing_priv: Ed25519PrivateKey,
    tmp_path: Path,
) -> None:
    """In streaming mode, /complete travels with stdout=None/stderr=None
    (the output was already sent via /output) and the output was streamed."""
    from unittest.mock import patch

    from orcaster_worker import main as main_mod

    output_events: list[dict] = []
    complete_calls: list[dict] = []

    class _API:
        async def output(self, task_id, *, lease_token, events):
            output_events.extend(events)

        async def complete(
            self,
            task_id,
            *,
            lease_token,
            exit_code,
            status,
            cancelled_reason=None,
        ):
            complete_calls.append(
                {
                    "status": status,
                    "exit_code": exit_code,
                }
            )

    async def fake_dispatch(
        payload, settings, *, env=None, cwd=None, abandon_event=None, on_output=None
    ):
        if on_output is not None:
            await on_output("hello\n", None)
        return "ok", {
            "exit_codes": [0],
            "stdout": "hello\n",
            "stderr": "",
            "failed_at": None,
        }

    task_id = 900
    payload = {"requirements": [], "commands": ["echo hello"]}
    created_at = "2026-05-19T00:00:00Z"
    import base64 as _b64

    from orcaster_worker.signing import build_task_message

    msg = build_task_message(
        task_id=task_id,
        worker_id=WID,
        command="__run_batch__",
        payload=payload,
        idempotency_key=None,
        created_at_iso=created_at,
    )
    sig = _b64.b64encode(signing_priv.sign(msg)).decode("ascii")
    task = PollTaskPayload(
        task_id=task_id,
        command="__run_batch__",
        payload=payload,
        created_at=created_at,
        delivery_mode="claim",
        signed=True,
        signature=sig,
        idempotency_key=None,
        worker_id=WID,
        target_worker_id=None,
        lease_token="lease-900",
        lease_expires_at="2030-01-01T00:00:00Z",
    )

    busy = BusyState()
    busy.task_id = task_id
    busy.lease_token = "lease-900"
    stop = asyncio.Event()

    # Mock ephemeral_venv so it doesn't need uv installed.
    import contextlib as _cl

    class _FakeVenv:
        env: dict = {}

    @_cl.asynccontextmanager
    async def _fake_ephemeral_venv(**kwargs):
        yield _FakeVenv()

    api = _API()
    with (
        patch("orcaster_worker.main.dispatch_run_batch", new=fake_dispatch),
        patch("orcaster_worker.main.ephemeral_venv", new=_fake_ephemeral_venv),
    ):
        await main_mod._run_external(api, settings, busy, stop, task)

    assert any(e["stream"] == "stdout" and e["text"] == "hello\n" for e in output_events), (
        f"output_events={output_events}"
    )
    assert complete_calls, "complete was never called"
    last = complete_calls[-1]
    assert last["status"] == "completed", f"status={last['status']}"
    # In streaming mode, /complete carries no stdout/stderr (they go via /output).
    assert "stdout" not in last, f"complete must not include stdout, includes: {last}"
    assert "stderr" not in last, f"complete must not include stderr, includes: {last}"


# --- Hardening: drain with deadline and seq continuity -----------------------


def _make_unsigned_task(task_id: int, lease_token: str) -> PollTaskPayload:
    return PollTaskPayload(
        task_id=task_id,
        command="__run_batch__",
        payload={"requirements": [], "commands": ["echo hi"]},
        created_at="2026-05-19T00:00:00Z",
        delivery_mode="claim",
        signed=False,
        signature=None,
        idempotency_key=None,
        worker_id=WID,
        target_worker_id=None,
        lease_token=lease_token,
        lease_expires_at="2030-01-01T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_run_external_happy_path_drains_with_deadline(tmp_path: Path) -> None:
    """Drain hardening: the dispatch finally's aclose() must carry a
    deadline in the happy path too (no stop). If the server goes down
    right as the execution ends, the worker cannot block in the drain
    indefinitely -- the real reliability window is the lease timeout,
    so the deadline is derived from lease_seconds."""
    from contextlib import asynccontextmanager
    from unittest.mock import patch

    from orcaster_worker import main as main_mod

    settings = Settings(
        server_url="http://server.test",  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        lease_seconds=1,  # short deadline so the test is fast
        uv_bin=_UV_BIN or "uv",
        uv_cache_dir=tmp_path / "uv-cache",
    )

    complete_calls: list[dict] = []

    class _DownAPI:
        async def output(self, task_id, *, lease_token, events):
            raise httpx.ConnectError("server down")

        async def complete(self, task_id, *, lease_token, exit_code, status, **kw):
            complete_calls.append({"status": status, "exit_code": exit_code})

    async def fake_dispatch(
        payload, settings, *, env=None, cwd=None, abandon_event=None, on_output=None
    ):
        if on_output is not None:
            await on_output("hello\n", None)
        return "ok", {"exit_codes": [0], "stdout": "hello\n", "stderr": "", "failed_at": None}

    class _FakeVenv:
        env: dict = {}

    @asynccontextmanager
    async def _fake_ephemeral_venv(**kwargs):
        yield _FakeVenv()

    busy = BusyState()
    busy.task_id = 950
    busy.lease_token = "lease-950"
    stop = asyncio.Event()  # NOT set: happy path

    with (
        patch("orcaster_worker.main.dispatch_run_batch", new=fake_dispatch),
        patch("orcaster_worker.main.ephemeral_venv", new=_fake_ephemeral_venv),
    ):
        # Without a deadline in the happy path this would hang (indefinite retry of the
        # sender against the downed server); with deadline=lease_seconds it returns.
        await asyncio.wait_for(
            main_mod._run_external(
                _DownAPI(), settings, busy, stop, _make_unsigned_task(950, "lease-950")
            ),
            timeout=5.0,
        )

    assert complete_calls, "complete was never called after the drain with deadline"
    assert complete_calls[-1]["status"] == "completed"


@pytest.mark.asyncio
async def test_run_external_publishes_streamer_in_busy_and_reset_clears(
    tmp_path: Path, settings: Settings
) -> None:
    """_run_external must publish its streamer in
    BusyState so the reaper can continue the seq sequence, and
    busy.reset() must clear it."""
    from contextlib import asynccontextmanager
    from unittest.mock import patch

    from orcaster_worker import main as main_mod

    class _API:
        async def output(self, task_id, *, lease_token, events):
            return None

        async def complete(self, task_id, *, lease_token, exit_code, status, **kw):
            return None

    async def fake_dispatch(
        payload, settings, *, env=None, cwd=None, abandon_event=None, on_output=None
    ):
        if on_output is not None:
            await on_output("hello\n", None)
        return "ok", {"exit_codes": [0], "stdout": "hello\n", "stderr": "", "failed_at": None}

    class _FakeVenv:
        env: dict = {}

    @asynccontextmanager
    async def _fake_ephemeral_venv(**kwargs):
        yield _FakeVenv()

    busy = BusyState()
    busy.task_id = 951
    busy.lease_token = "lease-951"
    stop = asyncio.Event()

    with (
        patch("orcaster_worker.main.dispatch_run_batch", new=fake_dispatch),
        patch("orcaster_worker.main.ephemeral_venv", new=_fake_ephemeral_venv),
    ):
        await main_mod._run_external(
            _API(), settings, busy, stop, _make_unsigned_task(951, "lease-951")
        )

    assert busy.streamer is not None, "busy.streamer was not published by _run_external"
    assert busy.streamer.last_seq >= 1, "the published streamer did not record the output"
    busy.reset()
    assert busy.streamer is None, "busy.reset() did not clear the streamer"


@pytest.mark.asyncio
async def test_reap_executor_crash_continues_streamer_seq(
    tmp_path: Path, settings: Settings
) -> None:
    """If the execution already streamed events, the
    reaper's synthetic diagnostic must continue the streamer's sequence
    (an ephemeral streamer would start at seq=1 and the server's ON CONFLICT would
    silently discard it)."""
    from orcaster_worker.output_streamer import OutputStreamer

    sent_events: list[dict] = []
    complete_calls: list[dict] = []

    class _API:
        async def output(self, task_id, *, lease_token, events):
            sent_events.extend(events)

        async def complete(self, task_id, *, lease_token, exit_code, status, **kw):
            complete_calls.append({"status": status, "exit_code": exit_code})

    api = _API()
    task = _make_unsigned_task(952, "lease-952")

    # The execution's streamer: it already streamed 2 events (seq=1, seq=2).
    streamer = OutputStreamer(
        api,  # type: ignore[arg-type]
        task.task_id,
        task.lease_token,
        max_bytes=8192,
        interval=0.05,
        buffer_max_bytes=65536,
        spill_dir=tmp_path / "spill",
    )
    streamer.start()
    await streamer.feed(stdout="line 1\n")
    await streamer.feed(stdout="line 2\n")
    await streamer.aclose(drain_deadline=5.0)
    assert streamer.last_seq == 2

    async def _crasher() -> None:
        raise RuntimeError("synthetic boom")

    busy = BusyState()
    busy.task_id = task.task_id
    busy.lease_token = task.lease_token
    busy.task_payload = task
    busy.streamer = streamer
    busy.executor_task = asyncio.create_task(_crasher())
    with pytest.raises(RuntimeError):
        await busy.executor_task

    await _reap_executor_task(api, busy, settings)  # type: ignore[arg-type]

    diag = [e for e in sent_events if "executor_crashed: RuntimeError" in e.get("text", "")]
    assert diag, f"synthetic diagnostic not found in events={sent_events}"
    assert diag[0]["seq"] == 3, (
        f"the diagnostic must continue the sequence (seq=3), got seq={diag[0]['seq']}"
    )
    assert complete_calls and complete_calls[-1]["status"] == "failed"
    assert busy.streamer is None, "the reaper must leave BusyState clean (streamer included)"


# --- ephemeral cwd per execution (created under run/, cleaned after dispatch) ---


@pytest.mark.asyncio
async def test_run_external_creates_and_cleans_ephemeral_run_dir(
    tmp_path: Path, settings: Settings
) -> None:
    """_run_external creates the ephemeral cwd under ``<cache_root>/run/`` and
    removes it after the dispatch.

    We verify the real filesystem invariant (without mocking
    ``create_execution_run_dir`` or ``shutil.rmtree``): during the call to
    ``dispatch_run_batch`` the ``cwd`` (a) is not None, (b) hangs from
    ``<cache_root>/run/`` and (c) exists on disk; after _run_external returns the
    dir has already been cleaned by the dispatch's ``finally``.
    """
    from contextlib import asynccontextmanager
    from unittest.mock import patch

    from orcaster_worker import main as main_mod

    settings.cache_root = tmp_path / "scratch-cache"

    # We capture the cwd and its state DURING the call (after returning the
    # dir no longer exists, so the existence assert must be deferred here).
    captured: dict[str, Any] = {}

    class _API:
        async def output(self, task_id, *, lease_token, events):
            return None

        async def complete(self, task_id, *, lease_token, exit_code, status, **kw):
            return None

    async def fake_dispatch(
        payload, settings, *, env=None, cwd=None, abandon_event=None, on_output=None
    ):
        captured["cwd"] = cwd
        captured["is_dir_during_call"] = cwd is not None and Path(cwd).is_dir()
        return "ok", {"exit_codes": [0], "stdout": "", "stderr": "", "failed_at": None}

    class _FakeVenv:
        env: dict = {}

    @asynccontextmanager
    async def _fake_ephemeral_venv(**kwargs):
        yield _FakeVenv()

    busy = BusyState()
    busy.task_id = 960
    busy.lease_token = "lease-960"
    stop = asyncio.Event()

    with (
        patch("orcaster_worker.main.dispatch_run_batch", new=fake_dispatch),
        patch("orcaster_worker.main.ephemeral_venv", new=_fake_ephemeral_venv),
    ):
        await main_mod._run_external(
            _API(), settings, busy, stop, _make_unsigned_task(960, "lease-960")
        )

    cwd = captured.get("cwd")
    assert cwd is not None, "dispatch_run_batch received cwd=None; the run_dir was not created"
    run_dir = Path(cwd)
    # (b) The run_dir hangs from <cache_root>/run/.
    assert run_dir.parent == settings.resolved_cache_root() / "run", (
        f"run_dir.parent={run_dir.parent}, esperaba {settings.resolved_cache_root() / 'run'}"
    )
    # (c) It existed on disk DURING the dispatch (while the subprocess ran).
    assert captured["is_dir_during_call"], "the run_dir did not exist during the dispatch call"
    # After _run_external returns the dir was already cleaned by the finally.
    assert not run_dir.exists(), f"the run_dir {run_dir} was not cleaned after the dispatch"


@pytest.mark.asyncio
async def test_run_external_cleans_run_dir_even_when_dispatch_raises(
    tmp_path: Path, settings: Settings
) -> None:
    """Exception path: even if ``dispatch_run_batch`` raises, the dispatch's
    ``finally`` removes the ephemeral run_dir.

    _run_external does NOT propagate the exception: the outer ``except Exception``
    catches it, emits the diagnostic and reports ``failed``/exit_code=1. That is why the
    test does NOT use pytest.raises; it verifies that _run_external returns normally,
    that it reported failed, and that the captured run_dir no longer exists.
    """
    from contextlib import asynccontextmanager
    from unittest.mock import patch

    from orcaster_worker import main as main_mod

    settings.cache_root = tmp_path / "scratch-cache"

    captured: dict[str, Any] = {}
    complete_calls: list[dict] = []

    class _API:
        async def output(self, task_id, *, lease_token, events):
            return None

        async def complete(self, task_id, *, lease_token, exit_code, status, **kw):
            complete_calls.append({"status": status, "exit_code": exit_code})

    async def fake_dispatch(
        payload, settings, *, env=None, cwd=None, abandon_event=None, on_output=None
    ):
        # We capture the cwd and verify it exists BEFORE raising, to later
        # check that the finally cleaned it up despite the exception.
        captured["cwd"] = cwd
        captured["is_dir_during_call"] = cwd is not None and Path(cwd).is_dir()
        raise RuntimeError("boom")

    class _FakeVenv:
        env: dict = {}

    @asynccontextmanager
    async def _fake_ephemeral_venv(**kwargs):
        yield _FakeVenv()

    busy = BusyState()
    busy.task_id = 961
    busy.lease_token = "lease-961"
    stop = asyncio.Event()

    with (
        patch("orcaster_worker.main.dispatch_run_batch", new=fake_dispatch),
        patch("orcaster_worker.main.ephemeral_venv", new=_fake_ephemeral_venv),
    ):
        # _run_external catches the dispatch exception in its outer except Exception;
        # it must not propagate.
        await main_mod._run_external(
            _API(), settings, busy, stop, _make_unsigned_task(961, "lease-961")
        )

    cwd = captured.get("cwd")
    assert cwd is not None, "dispatch_run_batch received cwd=None; the run_dir was not created"
    run_dir = Path(cwd)
    assert run_dir.parent == settings.resolved_cache_root() / "run", (
        f"run_dir.parent={run_dir.parent}, esperaba {settings.resolved_cache_root() / 'run'}"
    )
    assert captured["is_dir_during_call"], "the run_dir did not exist during the dispatch call"
    # The dispatch's finally ran despite the exception: the dir was cleaned up.
    assert not run_dir.exists(), f"the run_dir {run_dir} was not cleaned after the exception"
    # The exception was caught and reported as failed (not propagated).
    assert complete_calls and complete_calls[-1]["status"] == "failed", (
        f"expected a complete with status=failed, got: {complete_calls}"
    )


@pytest.mark.skipif(_UV_BIN is None, reason="requires uv binary in PATH")
@respx.mock
async def test_reset_clears_previous_job_on_new_job(
    settings: Settings, signing_priv: Ed25519PrivateKey, tmp_path: Path
) -> None:
    """When starting a different job, the cache of a previous job is deleted."""
    settings.cache_root = tmp_path / "scratch-cache"
    cache_root = settings.resolved_cache_root()
    old = ensure_task_cache_dir(cache_root, "task-700")
    (old / "hf").mkdir()
    (old / "hf" / "old.bin").write_text("stale")

    task = _signed_run_batch_task(
        signing_priv,
        task_id=910,
        idempotency_key="bbbbbbbb-0000-0000-0000-000000000001",
        payload={"requirements": [], "commands": ["false"]},
    )
    # status="error" -> the new job's cache is preserved; the old one was deleted by the reset.
    new_cache = await _run_with_dispatch(settings, task, status="error", exit_code=1)

    assert not old.exists(), "the reset should have deleted the previous job's cache"
    assert new_cache.exists(), "the in-progress (failed) job's cache is preserved"


@pytest.mark.skipif(_UV_BIN is None, reason="requires uv binary in PATH")
@respx.mock
async def test_reset_preserves_same_job_retry(
    settings: Settings, signing_priv: Ed25519PrivateKey, tmp_path: Path
) -> None:
    """A retry of the same job_key keeps its cache (it is not deleted)."""
    settings.cache_root = tmp_path / "scratch-cache"
    cache_root = settings.resolved_cache_root()
    idem = "bbbbbbbb-0000-0000-0000-000000000002"
    same = ensure_task_cache_dir(cache_root, idem)
    (same / "marker.txt").write_text("reuse-me")

    task = _signed_run_batch_task(
        signing_priv,
        task_id=911,
        idempotency_key=idem,
        payload={"requirements": [], "commands": ["false"]},
    )
    await _run_with_dispatch(settings, task, status="error", exit_code=1)

    assert same.exists()
    assert (same / "marker.txt").read_text() == "reuse-me"


@respx.mock
async def test_reset_failure_aborts_without_dispatch(
    settings: Settings, signing_priv: Ed25519PrivateKey, tmp_path: Path
) -> None:
    """If the reset fails (failed_dirs), the task is marked failed and is not executed."""
    from pathlib import Path as _P
    from unittest.mock import AsyncMock, patch

    from orcaster_worker.main import _dispatch_poll_task

    settings.cache_root = tmp_path / "scratch-cache"
    task = _signed_run_batch_task(
        signing_priv,
        task_id=912,
        idempotency_key="bbbbbbbb-0000-0000-0000-000000000003",
        payload={"requirements": [], "commands": ["echo hi"]},
    )

    captured: dict[str, str] = {}

    def _capture_complete(request) -> httpx.Response:
        import json

        captured["status"] = json.loads(request.content)["status"]
        return httpx.Response(200, json={"ok": True})

    respx.post(f"{BASE}/v1/tasks/{task.task_id}/complete").mock(side_effect=_capture_complete)
    # /output may be called by the synthetic diagnostic of the abort.
    respx.post(f"{BASE}/v1/tasks/{task.task_id}/output").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    dispatch_mock = AsyncMock()
    with (
        patch(
            "orcaster_worker.main.reset_other_job_caches",
            return_value=ResetStats(
                dirs_deleted=0, bytes_freed_est=0, failed_dirs=(_P("/blocked"),)
            ),
        ),
        patch("orcaster_worker.main.dispatch_run_batch", new=dispatch_mock),
    ):
        async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
            busy = BusyState()
            stop = asyncio.Event()
            await _dispatch_poll_task(api, settings, busy, stop, task)

    assert captured["status"] == "failed"
    dispatch_mock.assert_not_called()


@respx.mock
async def test_reset_failure_cancels_outer_timeout_timer(
    settings: Settings, signing_priv: Ed25519PrivateKey, tmp_path: Path
) -> None:
    """If the reset fails on a task WITH execution_timeout_seconds, the
    outer-timeout timer must be cancelled on the abort path (not left orphaned
    and spuriously abort the next task)."""
    from pathlib import Path as _P
    from unittest.mock import AsyncMock, patch

    from orcaster_worker.main import _dispatch_poll_task

    settings.cache_root = tmp_path / "scratch-cache"
    # Large timeout: it never fires on its own within the test; if it is still alive
    # after the abort it is because it was left orphaned.
    task = _signed_run_batch_task(
        signing_priv,
        task_id=914,
        idempotency_key="cccccccc-0000-0000-0000-000000000001",
        payload={"requirements": [], "commands": ["echo hi"]},
        execution_timeout_seconds=3600,
    )

    captured: dict[str, str] = {}

    def _capture_complete(request) -> httpx.Response:
        import json

        captured["status"] = json.loads(request.content)["status"]
        return httpx.Response(200, json={"ok": True})

    respx.post(f"{BASE}/v1/tasks/{task.task_id}/complete").mock(side_effect=_capture_complete)
    respx.post(f"{BASE}/v1/tasks/{task.task_id}/output").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    dispatch_mock = AsyncMock()
    with (
        patch(
            "orcaster_worker.main.reset_other_job_caches",
            return_value=ResetStats(
                dirs_deleted=0, bytes_freed_est=0, failed_dirs=(_P("/blocked"),)
            ),
        ),
        patch("orcaster_worker.main.dispatch_run_batch", new=dispatch_mock),
    ):
        async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
            busy = BusyState()
            stop = asyncio.Event()
            await _dispatch_poll_task(api, settings, busy, stop, task)

    assert captured["status"] == "failed"
    dispatch_mock.assert_not_called()

    # Give the loop the chance to run any pending callback.
    await asyncio.sleep(0)
    lingering = [
        t for t in asyncio.all_tasks() if t.get_name().startswith("outer-timeout-") and not t.done()
    ]
    assert not lingering, f"orphaned timer: {lingering}"


@respx.mock
async def test_prepare_cache_env_oserror_degrades_gracefully(
    settings: Settings, signing_priv: Ed25519PrivateKey, tmp_path: Path
) -> None:
    """An OSError from _prepare_cache_env must not fail the task: it degrades
    gracefully (without ORCASTER_CACHE_DIR) and the execution continues."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from orcaster_worker.main import _dispatch_poll_task

    settings.cache_root = tmp_path / "scratch-cache"
    settings.uv_cache_dir = None
    task = _signed_run_batch_task(
        signing_priv,
        task_id=915,
        idempotency_key="cccccccc-0000-0000-0000-000000000002",
        payload={"requirements": [], "commands": ["echo hi"]},
    )

    @asynccontextmanager
    async def _fake_venv(**kwargs):
        yield SimpleNamespace(env={"PATH": "/usr/bin"})

    dispatched: dict[str, bool] = {"called": False}

    async def _fake_dispatch(
        _payload, _settings, *, env=None, cwd=None, abandon_event=None, on_output=None
    ):
        dispatched["called"] = True
        return "ok", {"exit_codes": [0], "stdout": "", "stderr": "", "failed_at": None}

    captured: dict[str, str] = {}

    def _capture_complete(request) -> httpx.Response:
        import json

        captured["status"] = json.loads(request.content)["status"]
        return httpx.Response(200, json={"ok": True})

    respx.post(f"{BASE}/v1/tasks/{task.task_id}/complete").mock(side_effect=_capture_complete)
    respx.post(f"{BASE}/v1/tasks/{task.task_id}/output").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    with (
        patch("orcaster_worker.main.ephemeral_venv", _fake_venv),
        patch(
            "orcaster_worker.main._prepare_cache_env",
            side_effect=OSError("disk full"),
        ),
        patch(
            "orcaster_worker.main.dispatch_run_batch",
            new=AsyncMock(side_effect=_fake_dispatch),
        ),
    ):
        async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
            busy = BusyState()
            stop = asyncio.Event()
            await _dispatch_poll_task(api, settings, busy, stop, task)

    assert dispatched["called"], "the task must run despite the cache env OSError"
    assert captured.get("status") == "completed", (
        f"expected completed (graceful degradation), got: {captured.get('status')}"
    )


@respx.mock
async def test_prepare_cache_env_oserror_does_not_leak_partial_env(
    settings: Settings, signing_priv: Ed25519PrivateKey, tmp_path: Path
) -> None:
    """If _prepare_cache_env mutates `env` and THEN raises OSError (e.g. the
    artifacts mkdir fails after setting ORCASTER_CACHE_DIR/HF_HOME/...), the
    degradation must be clean: NONE of the cache env vars (legacy aliases
    included) must
    reach the subprocess (the env that dispatch_run_batch receives)."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from orcaster_worker.main import _dispatch_poll_task

    settings.cache_root = tmp_path / "scratch-cache"
    settings.uv_cache_dir = None
    task = _signed_run_batch_task(
        signing_priv,
        task_id=916,
        idempotency_key="cccccccc-0000-0000-0000-000000000003",
        payload={"requirements": [], "commands": ["echo hi"]},
    )

    @asynccontextmanager
    async def _fake_venv(**kwargs):
        yield SimpleNamespace(env={"PATH": "/usr/bin"})

    def _mutate_then_raise(env, *, cache_root, job_key):
        env["ORCASTER_CACHE_DIR"] = "/x"
        env["TASKORCH_CACHE_DIR"] = "/x"
        env["HF_HOME"] = "/x/hf"
        env["PIP_CACHE_DIR"] = "/x/pip"
        env["XDG_CACHE_HOME"] = "/x/xdg"
        env["ORCASTER_ARTIFACTS_DIR"] = "/x/artifacts"
        env["TASKORCH_ARTIFACTS_DIR"] = "/x/artifacts"
        raise OSError("disk full")

    captured: dict[str, object] = {}

    async def _fake_dispatch(
        _payload, _settings, *, env=None, cwd=None, abandon_event=None, on_output=None
    ):
        captured["env"] = dict(env or {})
        return "ok", {"exit_codes": [0], "stdout": "", "stderr": "", "failed_at": None}

    respx.post(f"{BASE}/v1/tasks/{task.task_id}/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.post(f"{BASE}/v1/tasks/{task.task_id}/output").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    with (
        patch("orcaster_worker.main.ephemeral_venv", _fake_venv),
        patch(
            "orcaster_worker.main._prepare_cache_env",
            side_effect=_mutate_then_raise,
        ),
        patch(
            "orcaster_worker.main.dispatch_run_batch",
            new=AsyncMock(side_effect=_fake_dispatch),
        ),
    ):
        async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
            busy = BusyState()
            stop = asyncio.Event()
            await _dispatch_poll_task(api, settings, busy, stop, task)

    env = captured.get("env")
    assert env is not None, "dispatch_run_batch did not receive env"
    leaked = [
        k
        for k in (
            "ORCASTER_CACHE_DIR",
            "TASKORCH_CACHE_DIR",
            "HF_HOME",
            "PIP_CACHE_DIR",
            "XDG_CACHE_HOME",
            "ORCASTER_ARTIFACTS_DIR",
            "TASKORCH_ARTIFACTS_DIR",
        )
        if k in env
    ]
    assert not leaked, f"partial cache env vars leaked to the subprocess: {leaked}"


@respx.mock
async def test_uv_cache_dir_is_per_job_when_no_override(
    settings: Settings, signing_priv: Ed25519PrivateKey, tmp_path: Path
) -> None:
    """Without an override (uv_cache_dir=None), the UV_CACHE_DIR passed to the venv is
    `<cache_dir>/uv` (per-job)."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from orcaster_worker.main import _dispatch_poll_task

    settings.cache_root = tmp_path / "scratch-cache"
    settings.uv_cache_dir = None  # forces per-job mode
    idem = "bbbbbbbb-0000-0000-0000-000000000004"
    task = _signed_run_batch_task(
        signing_priv,
        task_id=913,
        idempotency_key=idem,
        payload={"requirements": [], "commands": ["echo hi"]},
    )

    captured: dict[str, object] = {}

    @asynccontextmanager
    async def _fake_venv(**kwargs):
        captured["uv_cache_dir"] = kwargs.get("uv_cache_dir")
        yield SimpleNamespace(env={"PATH": "/usr/bin"})

    async def _fake_dispatch(
        _payload, _settings, *, env=None, cwd=None, abandon_event=None, on_output=None
    ):
        return "ok", {"exit_codes": [0], "stdout": "", "stderr": "", "failed_at": None}

    respx.post(f"{BASE}/v1/tasks/{task.task_id}/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    with (
        patch("orcaster_worker.main.ephemeral_venv", _fake_venv),
        patch(
            "orcaster_worker.main.dispatch_run_batch",
            new=AsyncMock(side_effect=_fake_dispatch),
        ),
    ):
        async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
            busy = BusyState()
            stop = asyncio.Event()
            await _dispatch_poll_task(api, settings, busy, stop, task)

    expected = settings.resolved_cache_root() / "tasks" / idem / "uv"
    assert captured["uv_cache_dir"] == expected


@respx.mock
async def test_reset_runs_in_worker_thread_not_event_loop(
    settings: Settings, signing_priv: Ed25519PrivateKey, tmp_path: Path
) -> None:
    """The cache reset (multi-GB rmtree) must be offloaded to a thread via
    asyncio.to_thread, not run inline on the event loop's thread. If it
    ran inline, it would freeze the loop for several seconds. We capture the thread in
    which reset_other_job_caches runs and compare it against the main
    thread: they must differ."""
    import threading
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from orcaster_worker.main import _dispatch_poll_task

    settings.cache_root = tmp_path / "scratch-cache"
    settings.uv_cache_dir = None
    task = _signed_run_batch_task(
        signing_priv,
        task_id=915,
        idempotency_key="bbbbbbbb-0000-0000-0000-000000000005",
        payload={"requirements": [], "commands": ["echo hi"]},
    )

    main_thread_id = threading.get_ident()
    captured: dict[str, object] = {}

    def _fake_reset(cache_root, *, keep_job_key):
        captured["reset_thread_id"] = threading.get_ident()
        return ResetStats(dirs_deleted=0, bytes_freed_est=0, failed_dirs=())

    @asynccontextmanager
    async def _fake_venv(**kwargs):
        yield SimpleNamespace(env={"PATH": "/usr/bin"})

    async def _fake_dispatch(
        _payload, _settings, *, env=None, cwd=None, abandon_event=None, on_output=None
    ):
        return "ok", {"exit_codes": [0], "stdout": "", "stderr": "", "failed_at": None}

    respx.post(f"{BASE}/v1/tasks/{task.task_id}/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    with (
        patch("orcaster_worker.main.reset_other_job_caches", new=_fake_reset),
        patch("orcaster_worker.main.ephemeral_venv", _fake_venv),
        patch(
            "orcaster_worker.main.dispatch_run_batch",
            new=AsyncMock(side_effect=_fake_dispatch),
        ),
    ):
        async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
            busy = BusyState()
            stop = asyncio.Event()
            await _dispatch_poll_task(api, settings, busy, stop, task)

    assert "reset_thread_id" in captured, "reset_other_job_caches was never invoked"
    assert captured["reset_thread_id"] != main_thread_id, (
        "the reset ran on the event loop's thread; it must be offloaded via asyncio.to_thread"
    )


# ---------------------------------------------------------------------------
# The active job is published for the GC
# ---------------------------------------------------------------------------


@respx.mock
async def test_active_job_key_published_before_cache_reset(
    settings: Settings, signing_priv: Ed25519PrivateKey, tmp_path: Path
) -> None:
    """``busy.active_job_key`` must be published BEFORE the cache is touched,
    so that a GC tick that lands during the execution knows which dir is
    untouchable. When the dispatch ends, the reap clears it.

    This test asserts the RAW field on purpose: it invokes ``_dispatch_poll_task``
    directly, with no ``executor_task`` assigned, because what it measures is the ORDER
    of two statements inside that coroutine (publishing the key before touching the
    cache) and for that the spawn is not needed. What the GC really reads is
    ``busy.live_job_key``, which also requires a live ``executor_task``: that
    invariant is covered by ``test_live_job_key_published_by_the_poll_loop_spawn``,
    which goes through the production path. Both are necessary; neither
    replaces the other.
    """
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from orcaster_worker.main import _dispatch_poll_task

    settings.cache_root = tmp_path / "scratch-cache"
    settings.uv_cache_dir = None
    idem = "cccccccc-0000-0000-0000-000000000006"
    task = _signed_run_batch_task(
        signing_priv,
        task_id=916,
        idempotency_key=idem,
        payload={"requirements": [], "commands": ["echo hi"]},
    )

    busy = BusyState()
    captured: dict[str, object] = {}

    def _fake_reset(cache_root, *, keep_job_key):
        captured["active_at_reset"] = busy.active_job_key
        return ResetStats(dirs_deleted=0, bytes_freed_est=0, failed_dirs=())

    @asynccontextmanager
    async def _fake_venv(**kwargs):
        yield SimpleNamespace(env={"PATH": "/usr/bin"})

    async def _fake_dispatch(
        _payload, _settings, *, env=None, cwd=None, abandon_event=None, on_output=None
    ):
        captured["active_at_dispatch"] = busy.active_job_key
        return "ok", {"exit_codes": [0], "stdout": "", "stderr": "", "failed_at": None}

    respx.post(f"{BASE}/v1/tasks/{task.task_id}/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    with (
        patch("orcaster_worker.main.reset_other_job_caches", new=_fake_reset),
        patch("orcaster_worker.main.ephemeral_venv", _fake_venv),
        patch(
            "orcaster_worker.main.dispatch_run_batch",
            new=AsyncMock(side_effect=_fake_dispatch),
        ),
    ):
        async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
            stop = asyncio.Event()
            await _dispatch_poll_task(api, settings, busy, stop, task)

    assert captured.get("active_at_reset") == idem, (
        "the GC did not know which dir to protect when the cache was already in play"
    )
    assert captured.get("active_at_dispatch") == idem
    busy.reset()
    assert busy.active_job_key is None


@respx.mock
async def test_live_job_key_published_by_the_poll_loop_spawn(
    settings: Settings, signing_priv: Ed25519PrivateKey, tmp_path: Path
) -> None:
    """The GC sees a LIVE job while the dispatch touches the cache.

    The GC exclusion stopped reading ``active_job_key`` and now reads
    ``live_job_key``, which returns ``None`` if the ``executor_task`` is not
    assigned or has already finished. That rests on an invariant of the
    production path: the key is published from INSIDE the ``executor_task``, so
    when it is published there is already a live task assigned to ``busy``. If the invariant
    breaks -- the ``poll_loop`` spawn split into two statements with an
    ``await`` in between, or a second call site that does
    ``await _dispatch_poll_task(...)`` without ``asyncio.create_task`` --
    ``live_job_key`` returns ``None`` with the job running, the GC deletes the
    cache of the task in progress and kills it: the deletion of a live job's cache comes back.

    That is why this test goes through ``poll_loop``, which is the only real spawn, and reads
    ``live_job_key`` and not the raw field. A test that assigns the
    ``executor_task`` by hand reimplements the invariant in the test instead of
    verifying it, and would stay green with the production spawn broken.

    The two checkpoints are those of the test above (the cache reset and the
    batch dispatch) and are asserted against the exact key, so this test
    also covers the publish-before-reset order through the real path. The
    reset checkpoint is read in a thread (``asyncio.to_thread``),
    which is exactly where ``cache_gc_loop`` invokes the getter.
    """
    from contextlib import asynccontextmanager
    from dataclasses import asdict
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    settings.cache_root = tmp_path / "scratch-cache"
    settings.uv_cache_dir = None
    idem = "cccccccc-0000-0000-0000-000000000007"
    task = _signed_run_batch_task(
        signing_priv,
        task_id=917,
        idempotency_key=idem,
        payload={"requirements": [], "commands": ["echo hi"]},
    )

    busy = BusyState()
    captured: dict[str, object] = {}

    # Capture, never assert, inside the fakes: an AssertionError here goes up
    # through `asyncio.to_thread` to the `except Exception` of `_dispatch_poll_task` and
    # becomes an `abort_reason`, which aborts the execution and masks the real
    # cause of the failure. The assertions go at the end, with the loop stopped.
    def _fake_reset(cache_root, *, keep_job_key):
        captured["live_at_reset"] = busy.live_job_key
        return ResetStats(dirs_deleted=0, bytes_freed_est=0, failed_dirs=())

    @asynccontextmanager
    async def _fake_venv(**kwargs):
        yield SimpleNamespace(env={"PATH": "/usr/bin"})

    async def _fake_dispatch(
        _payload, _settings, *, env=None, cwd=None, abandon_event=None, on_output=None
    ):
        captured["live_at_dispatch"] = busy.live_job_key
        return "ok", {"exit_codes": [0], "stdout": "", "stderr": "", "failed_at": None}

    polls = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        polls["n"] += 1
        body: dict[str, Any] = {
            "task": asdict(task) if polls["n"] == 1 else None,
            "cancel_signals": [],
            "next_poll_after_seconds": 1,
        }
        return httpx.Response(200, json=body)

    respx.post(POLL_URL).mock(side_effect=_handler)
    complete_route = respx.post(f"{BASE}/v1/tasks/{task.task_id}/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    with (
        patch("orcaster_worker.main.reset_other_job_caches", new=_fake_reset),
        patch("orcaster_worker.main.ephemeral_venv", _fake_venv),
        patch(
            "orcaster_worker.main.dispatch_run_batch",
            new=AsyncMock(side_effect=_fake_dispatch),
        ),
    ):
        async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
            stop = asyncio.Event()
            loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop))
            for _ in range(100):
                await asyncio.sleep(0.1)
                if complete_route.called:
                    break
            stop.set()
            await asyncio.wait_for(loop_task, timeout=10.0)

    assert complete_route.called, "the task was never dispatched; the test measured nothing"
    assert captured.get("live_at_reset") == idem, (
        "the GC did not see a live job when the cache was already in play: the live-job exclusion "
        "is a no-op and the dir of the task in progress is deletable"
    )
    assert captured.get("live_at_dispatch") == idem, (
        "the GC did not see a live job while the batch was running"
    )


async def test_cache_gc_loop_protects_the_active_job(settings: Settings, tmp_path: Path) -> None:
    """The GC loop passes ``run_gc`` a live-job getter, not a snapshot.

    A frozen value leaves unprotected the job that starts while the pass
    runs, which is the long window for deleting a live job (each deletion is a multi-GB ``rmtree``).

    The ``executor_task`` parked on an ``Event`` is not decorated: the protection
    holds while the job is alive, and the getter derives it from that task.
    """
    from unittest.mock import patch

    from orcaster_worker.main import cache_gc_loop
    from orcaster_worker.task_cache import GcStats

    settings.cache_root = tmp_path / "scratch-cache"
    settings.cache_gc_interval_seconds = 1
    busy = BusyState()
    running: asyncio.Task[bool] = asyncio.create_task(asyncio.Event().wait())
    busy.executor_task = running
    busy.active_job_key = "live-job"
    stop = asyncio.Event()
    seen: list[str | None] = []

    def _fake_gc(cache_root, **kwargs) -> GcStats:
        getter = kwargs.get("keep_job_key")
        assert callable(getter), "the loop must pass a getter, not the tick's value"
        seen.append(getter())
        # The worker switches job while the pass is still running.
        busy.active_job_key = "new-job"
        seen.append(getter())
        stop.set()
        return GcStats(
            scanned=0,
            deleted_age=0,
            deleted_pressure=0,
            bytes_freed_estimate=0,
            free_before=0,
            free_after=0,
        )

    with patch("orcaster_worker.main.run_gc", new=_fake_gc):
        await asyncio.wait_for(cache_gc_loop(settings, stop, busy), timeout=5)

    running.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await running

    assert seen == ["live-job", "new-job"]


async def test_live_job_key_only_holds_while_the_executor_task_is_alive() -> None:
    """The key the GC honors comes from ``live_job_key``, not from the raw field.

    ``busy.reset()`` runs in a single place, ``_reap_executor_task``, i.e. at the
    start of the poll_loop's next iteration: between the job's asyncio task ending
    and that reap, ``active_job_key`` still names a dead job.
    ``live_job_key`` derives the protection from the task's state -- like
    ``is_busy`` -- so it turns off the instant the job stops running,
    without opening the inverse window: while the task is alive it returns the key.
    """
    busy = BusyState()
    gate = asyncio.Event()
    running: asyncio.Task[bool] = asyncio.create_task(gate.wait())
    busy.executor_task = running
    busy.active_job_key = "live-job"

    assert busy.live_job_key == "live-job", "the GC would delete the cache of the job in progress"

    gate.set()
    await running

    assert busy.live_job_key is None, "the GC kept protecting a dir that nobody uses anymore"
    assert busy.active_job_key == "live-job", (
        "the reap has not run yet: that is exactly the window the test covers"
    )


async def test_cache_gc_loop_stops_protecting_a_finished_job(
    settings: Settings, tmp_path: Path
) -> None:
    """The getter the loop passes to the GC does not protect a dead job without reaping.

    The window is bounded by ``poll_max_interval_seconds``, but network backoff
    pushes the interval to that ceiling: during an incident the operator read the
    GC declaring the pressure unrecoverable when the only multi-GB candidate
    was the dir of an already finished job.
    """
    from unittest.mock import patch

    from orcaster_worker.main import cache_gc_loop
    from orcaster_worker.task_cache import GcStats

    settings.cache_root = tmp_path / "scratch-cache"
    settings.cache_gc_interval_seconds = 1
    busy = BusyState()
    finished: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(0))
    await finished
    busy.executor_task = finished
    busy.active_job_key = "finished-job"
    stop = asyncio.Event()
    seen: list[str | None] = []

    def _fake_gc(cache_root, **kwargs) -> GcStats:
        getter = kwargs.get("keep_job_key")
        assert callable(getter), "the loop must pass a getter, not the tick's value"
        seen.append(getter())
        stop.set()
        return GcStats(
            scanned=0,
            deleted_age=0,
            deleted_pressure=0,
            bytes_freed_estimate=0,
            free_before=0,
            free_after=0,
        )

    with patch("orcaster_worker.main.run_gc", new=_fake_gc):
        await asyncio.wait_for(cache_gc_loop(settings, stop, busy), timeout=5)

    assert seen == [None], "the GC protected the dir of a job that had already finished"
    assert busy.active_job_key == "finished-job", (
        "the reap has not run yet: the window the test covers is exactly that"
    )


async def test_cache_gc_loop_runs_a_pass_when_woken(settings: Settings, tmp_path: Path) -> None:
    """The disk gate asks for an immediate pass instead of waiting for the interval.

    The gate closes right when the GC's pressure pass is the remedy, and with the
    default cadence (300 s) the worker stayed idle for up to five minutes before that
    remedy ran. This test's interval is 300 s on purpose: if the loop did not honor
    the event, the `wait_for` would die by timeout.
    """
    from unittest.mock import patch

    from orcaster_worker.main import cache_gc_loop
    from orcaster_worker.task_cache import GcStats

    settings.cache_root = tmp_path / "scratch-cache"
    settings.cache_gc_interval_seconds = 300
    busy = BusyState()
    stop = asyncio.Event()
    gc_wakeup = asyncio.Event()
    passes = 0

    def _fake_gc(cache_root, **kwargs) -> GcStats:
        nonlocal passes
        passes += 1
        if passes == 1:
            # The gate closes while the GC sleeps through its long interval.
            gc_wakeup.set()
        else:
            stop.set()
        return GcStats(
            scanned=0,
            deleted_age=0,
            deleted_pressure=0,
            bytes_freed_estimate=0,
            free_before=0,
            free_after=0,
        )

    with patch("orcaster_worker.main.run_gc", new=_fake_gc):
        await asyncio.wait_for(cache_gc_loop(settings, stop, busy, gc_wakeup=gc_wakeup), timeout=5)

    assert passes == 2, "the event must cut the wait for the interval short"
    assert not gc_wakeup.is_set(), "the event is consumed: otherwise the next pass is a no-op"


@respx.mock
async def test_task_received_on_busy_poll_is_ignored_even_if_executor_finished(
    settings: Settings,
) -> None:
    """A poll that reported `busy` discards the task it receives, even if the executor
    finished while the poll was in flight.

    The server does not attach secrets to a push delivered to a `busy` poll; if the
    worker ran it anyway, it would run without its secrets. It discards it and the lease
    returns it to the queue, where an idle poll receives it with its secrets.
    """
    import json
    from unittest.mock import AsyncMock, patch

    busy = BusyState()
    parked = asyncio.Event()
    busy.executor_task = asyncio.create_task(parked.wait())
    busy.task_id = 1
    busy.lease_token = "lease-1"
    polls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        polls.append(json.loads(request.content)["status"])
        task = None
        if len(polls) == 1:
            # The executor terminates while the `busy` poll is in flight.
            busy.executor_task = None
            busy.task_id = None
            busy.lease_token = None
            task = _make_task_payload(task_id=2, lease_token="lease-2")
        return httpx.Response(
            200, json={"task": task, "cancel_signals": [], "next_poll_after_seconds": 1}
        )

    respx.post(POLL_URL).mock(side_effect=_handler)
    dispatch = AsyncMock()
    with patch("orcaster_worker.main._dispatch_poll_task", new=dispatch):
        async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
            stop = asyncio.Event()
            loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop))
            for _ in range(50):
                await asyncio.sleep(0.05)
                if len(polls) >= 2:
                    break
            stop.set()
            await asyncio.wait_for(loop_task, timeout=10.0)
    parked.set()

    assert polls[0] == "busy"
    assert len(polls) >= 2, "the loop did not resume polling; the test did not measure anything"
    dispatch.assert_not_called()
    assert busy.task_id is None


# --- Legacy TASKORCH_* aliases of the cache and artifacts dirs ----------------

_DIR_ALIASES = (
    ("ORCASTER_CACHE_DIR", "TASKORCH_CACHE_DIR"),
    ("ORCASTER_ARTIFACTS_DIR", "TASKORCH_ARTIFACTS_DIR"),
)


async def _dispatched_env(
    settings: Settings,
    task: PollTaskPayload,
    *,
    source_env: dict[str, str] | None = None,
    extra_patches: tuple = (),
) -> dict[str, str]:
    """Runs ``task`` with a fake venv and a mocked dispatch; returns the env the
    subprocess would receive. The fake venv builds its env with the real
    ``build_scrubbed_base_env`` from the ``task_env`` and ``secrets`` the worker
    passes, so declared names and server secrets land in it as in production."""
    from contextlib import ExitStack, asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from orcaster_worker.main import _dispatch_poll_task
    from orcaster_worker.task_env import build_scrubbed_base_env

    source = {"PATH": "/usr/bin"} if source_env is None else source_env

    @asynccontextmanager
    async def _fake_venv(**kwargs):
        yield SimpleNamespace(
            env=build_scrubbed_base_env(
                kwargs.get("task_env"), source=source, secrets=kwargs.get("secrets")
            )
        )

    captured: dict[str, dict[str, str]] = {}

    async def _fake_dispatch(
        _payload, _settings, *, env=None, cwd=None, abandon_event=None, on_output=None
    ):
        captured["env"] = dict(env or {})
        return "ok", {"exit_codes": [0], "stdout": "", "stderr": "", "failed_at": None}

    respx.post(f"{BASE}/v1/tasks/{task.task_id}/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.post(f"{BASE}/v1/tasks/{task.task_id}/output").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    with ExitStack() as stack:
        stack.enter_context(patch("orcaster_worker.main.ephemeral_venv", _fake_venv))
        stack.enter_context(
            patch(
                "orcaster_worker.main.dispatch_run_batch",
                new=AsyncMock(side_effect=_fake_dispatch),
            )
        )
        for p in extra_patches:
            stack.enter_context(p)
        async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
            await _dispatch_poll_task(api, settings, BusyState(), asyncio.Event(), task)
    assert "env" in captured, "dispatch_run_batch did not receive env"
    return captured["env"]


@respx.mock
async def test_subprocess_env_has_legacy_dir_aliases_with_identical_values(
    settings: Settings, signing_priv: Ed25519PrivateKey, tmp_path: Path
) -> None:
    """Tasks written before the rename read TASKORCH_CACHE_DIR and
    TASKORCH_ARTIFACTS_DIR: the subprocess gets them next to the ORCASTER_*
    names, with the same values."""
    settings.cache_root = tmp_path / "scratch-cache"
    settings.uv_cache_dir = None
    settings.cache_delete_on_success = False
    task = _signed_run_batch_task(
        signing_priv,
        task_id=930,
        idempotency_key="dddddddd-0000-0000-0000-000000000001",
        payload={"requirements": [], "commands": ["echo hi"]},
    )

    env = await _dispatched_env(settings, task)

    for current, legacy in _DIR_ALIASES:
        assert current in env, f"env keys: {sorted(env)}"
        assert legacy in env, f"env keys: {sorted(env)}"
        assert env[legacy] == env[current]
    assert Path(env["TASKORCH_CACHE_DIR"]).is_dir()
    assert Path(env["TASKORCH_ARTIFACTS_DIR"]).is_dir()


@respx.mock
async def test_legacy_dir_aliases_absent_when_cache_dir_cannot_be_prepared(
    settings: Settings, signing_priv: Ed25519PrivateKey, tmp_path: Path
) -> None:
    """If the cache dir cannot be created, neither the ORCASTER_* names nor
    their legacy aliases reach the subprocess."""
    from unittest.mock import patch

    settings.cache_root = tmp_path / "scratch-cache"
    settings.uv_cache_dir = None
    task = _signed_run_batch_task(
        signing_priv,
        task_id=931,
        idempotency_key="dddddddd-0000-0000-0000-000000000002",
        payload={"requirements": [], "commands": ["echo hi"]},
    )

    env = await _dispatched_env(
        settings,
        task,
        extra_patches=(
            patch(
                "orcaster_worker.main.ensure_task_cache_dir",
                side_effect=OSError("disk full"),
            ),
        ),
    )

    for current, legacy in _DIR_ALIASES:
        assert current not in env
        assert legacy not in env


@respx.mock
async def test_legacy_dir_aliases_not_overridable_by_task_env_or_secret(
    settings: Settings, signing_priv: Ed25519PrivateKey, tmp_path: Path
) -> None:
    """A task that declares the names in ``task_env`` gets the worker's
    directories, whether the competing value comes from the worker environment
    (TASKORCH_CACHE_DIR) or from a server secret (TASKORCH_ARTIFACTS_DIR)."""
    settings.cache_root = tmp_path / "scratch-cache"
    settings.uv_cache_dir = None
    settings.cache_delete_on_success = False
    idem = "dddddddd-0000-0000-0000-000000000003"
    task = _signed_run_batch_task(
        signing_priv,
        task_id=932,
        idempotency_key=idem,
        payload={
            "requirements": [],
            "commands": ["echo hi"],
            "task_env": [name for pair in _DIR_ALIASES for name in pair],
        },
    )
    task = dataclasses.replace(
        task,
        secrets={
            "ORCASTER_ARTIFACTS_DIR": "/from-secret/current",
            "TASKORCH_ARTIFACTS_DIR": "/from-secret/legacy",
        },
    )

    env = await _dispatched_env(
        settings,
        task,
        source_env={
            "PATH": "/usr/bin",
            "ORCASTER_CACHE_DIR": "/from-worker-env/current",
            "TASKORCH_CACHE_DIR": "/from-worker-env/legacy",
        },
    )

    cache_dir = settings.resolved_cache_root() / "tasks" / idem
    assert env["ORCASTER_CACHE_DIR"] == str(cache_dir)
    assert env["TASKORCH_CACHE_DIR"] == str(cache_dir)
    assert env["ORCASTER_ARTIFACTS_DIR"] == str(cache_dir / "artifacts")
    assert env["TASKORCH_ARTIFACTS_DIR"] == str(cache_dir / "artifacts")


@respx.mock
async def test_dir_names_absent_when_cache_dir_fails_even_if_declared_or_supplied(
    settings: Settings, signing_priv: Ed25519PrivateKey, tmp_path: Path
) -> None:
    """The four directory names belong to the worker. When it cannot create the
    cache dir it sets none of them, and a value from the worker environment or
    from a server secret does not take their place, even if ``task_env`` declares
    them."""
    from unittest.mock import patch

    settings.cache_root = tmp_path / "scratch-cache"
    settings.uv_cache_dir = None
    names = [name for pair in _DIR_ALIASES for name in pair]
    task = _signed_run_batch_task(
        signing_priv,
        task_id=933,
        idempotency_key="dddddddd-0000-0000-0000-000000000004",
        payload={"requirements": [], "commands": ["echo hi"], "task_env": names},
    )
    task = dataclasses.replace(task, secrets={name: f"/from-secret/{name}" for name in names})

    env = await _dispatched_env(
        settings,
        task,
        source_env={"PATH": "/usr/bin", **{name: f"/from-worker-env/{name}" for name in names}},
        extra_patches=(
            patch(
                "orcaster_worker.main.ensure_task_cache_dir",
                side_effect=OSError("disk full"),
            ),
        ),
    )

    assert [name for name in names if name in env] == []


@respx.mock
async def test_dir_names_absent_in_legacy_mode_when_cache_dir_fails(
    settings: Settings, signing_priv: Ed25519PrivateKey, tmp_path: Path
) -> None:
    """Legacy mode (no ``task_env``) copies the worker environment; the four
    directory names are still never taken from it."""
    from unittest.mock import patch

    settings.cache_root = tmp_path / "scratch-cache"
    settings.uv_cache_dir = None
    # Without this the worker promotes an absent ``task_env`` to strict mode.
    settings.enforce_isolation = False
    names = [name for pair in _DIR_ALIASES for name in pair]
    task = _signed_run_batch_task(
        signing_priv,
        task_id=934,
        idempotency_key="dddddddd-0000-0000-0000-000000000005",
        payload={"requirements": [], "commands": ["echo hi"]},
    )

    env = await _dispatched_env(
        settings,
        task,
        source_env={"PATH": "/usr/bin", **{name: f"/from-worker-env/{name}" for name in names}},
        extra_patches=(
            patch(
                "orcaster_worker.main.ensure_task_cache_dir",
                side_effect=OSError("disk full"),
            ),
        ),
    )

    assert [name for name in names if name in env] == []


@respx.mock
async def test_stored_secret_with_dir_name_is_not_delivered_to_the_task(
    settings: Settings, signing_priv: Ed25519PrivateKey, tmp_path: Path
) -> None:
    """A secret stored under one of the four names before they were reserved may
    still arrive from the server. The task never sees its value: the worker
    directory applies, also for a lowercase name that is not a directory name."""
    settings.cache_root = tmp_path / "scratch-cache"
    settings.uv_cache_dir = None
    settings.cache_delete_on_success = False
    idem = "dddddddd-0000-0000-0000-000000000006"
    names = [name for pair in _DIR_ALIASES for name in pair]
    task = _signed_run_batch_task(
        signing_priv,
        task_id=935,
        idempotency_key=idem,
        payload={
            "requirements": [],
            "commands": ["echo hi"],
            "task_env": [*names, "orcaster_cache_dir"],
        },
    )
    secrets = {name: "/from-stored-secret" for name in names}
    secrets["orcaster_cache_dir"] = "/from-stored-secret"
    task = dataclasses.replace(task, secrets=secrets)

    env = await _dispatched_env(settings, task)

    assert "/from-stored-secret" not in env.values()
    cache_dir = settings.resolved_cache_root() / "tasks" / idem
    assert env["TASKORCH_CACHE_DIR"] == str(cache_dir)
    assert env["TASKORCH_ARTIFACTS_DIR"] == str(cache_dir / "artifacts")
