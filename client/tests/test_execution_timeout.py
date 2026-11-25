"""Tests for the outer execution timeout in the worker.

Covers:
- Slow subprocess with outer=2s -> abandon_event fired, execution `failed`.
- Fast subprocess with outer=10s -> completes normally.
- Without outer timeout -> current behavior unchanged (no regression).
- The execution_timeout_seconds field is parsed correctly from the wire.
"""

from __future__ import annotations

import asyncio
import base64
import shutil
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orcaster_worker.api import OrchestratorAPI
from orcaster_worker.config import Settings
from orcaster_worker.main import BusyState, _run_external
from orcaster_worker.poll import PollResponse, PollTaskPayload
from orcaster_worker.signing import build_task_message, reset_key_cache

BASE = "http://server.test"
WID = "22222222-2222-2222-2222-222222222222"

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


@pytest.fixture
def signing_priv(tmp_path: Path, settings: Settings) -> Ed25519PrivateKey:
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


def _make_task_wire(
    *,
    task_id: int = 300,
    commands: list[str] | None = None,
    requirements: list[str] | None = None,
    execution_timeout_seconds: int | None = None,
    priv: Ed25519PrivateKey | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "requirements": requirements or [],
        "commands": commands or ["sleep 60"],
    }
    created_at = "2026-05-22T00:00:00Z"
    command = "__run_batch__"
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
    d: dict[str, Any] = {
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
        "lease_token": f"lease-{task_id}",
        "lease_expires_at": "2030-01-01T00:00:00Z",
    }
    if execution_timeout_seconds is not None:
        d["execution_timeout_seconds"] = execution_timeout_seconds
    return d


# --- Wire parsing -----------------------------------------------------------


def test_poll_response_from_wire_includes_execution_timeout() -> None:
    """execution_timeout_seconds is parsed from the server's JSON wire."""
    wire = {
        "task": {**_make_task_wire(execution_timeout_seconds=120), "signed": False},
        "cancel_signals": [],
        "next_poll_after_seconds": 1,
    }
    resp = PollResponse.from_wire(wire)
    assert resp.task is not None
    assert resp.task.execution_timeout_seconds == 120


def test_poll_response_from_wire_execution_timeout_null_when_absent() -> None:
    """execution_timeout_seconds is None when it is not in the wire."""
    wire = {
        "task": {**_make_task_wire(), "signed": False},
        "cancel_signals": [],
        "next_poll_after_seconds": 1,
    }
    resp = PollResponse.from_wire(wire)
    assert resp.task is not None
    assert resp.task.execution_timeout_seconds is None


# --- Enforcement in the worker ----------------------------------------------


@pytest.mark.skipif(_UV_BIN is None, reason="uv not available")
@respx.mock
@pytest.mark.asyncio
async def test_outer_timeout_kills_slow_subprocess(
    settings: Settings,
    signing_priv: Ed25519PrivateKey,
    tmp_path: Path,
) -> None:
    """Slow subprocess (sleep 30) with outer=2s -> abandon fired -> status=cancelled.

    The outer timeout reports `status='cancelled'` with
    `cancelled_reason='outer_timeout'`, so that the server can tell a
    timeout-driven cancel from a genuine failure. stderr still contains
    the human-readable marker.
    """
    complete_calls: list[dict[str, Any]] = []
    output_events: list[dict[str, Any]] = []

    def _capture_complete(request: httpx.Request) -> httpx.Response:
        import json

        complete_calls.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    def _capture_output(request: httpx.Request) -> httpx.Response:
        import json

        output_events.extend(json.loads(request.content).get("events", []))
        return httpx.Response(202, json={"ok": True})

    respx.post(f"{BASE}/v1/tasks/301/complete").mock(side_effect=_capture_complete)
    # The streamer drains via /output with aclose(drain_deadline=None) and retries
    # indefinitely if it is not mocked -> the test would hang. We mock /output
    # to capture the events (the "outer timeout" diagnostic travels here).
    respx.post(f"{BASE}/v1/tasks/301/output").mock(side_effect=_capture_output)

    task_wire = _make_task_wire(
        task_id=301,
        commands=['python3 -c "import time; time.sleep(30)"'],
        execution_timeout_seconds=2,
        priv=signing_priv,
    )
    task = PollTaskPayload(
        task_id=task_wire["task_id"],
        command=task_wire["command"],
        payload=task_wire["payload"],
        created_at=task_wire["created_at"],
        delivery_mode=task_wire["delivery_mode"],
        signed=task_wire["signed"],
        signature=task_wire["signature"],
        idempotency_key=None,
        worker_id=task_wire["worker_id"],
        target_worker_id=None,
        lease_token=task_wire["lease_token"],
        lease_expires_at=task_wire["lease_expires_at"],
        execution_timeout_seconds=2,
    )

    busy = BusyState()
    stop = asyncio.Event()

    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k", stop=stop) as api:
        start = time.monotonic()
        await _run_external(api, settings, busy, stop, task)
        elapsed = time.monotonic() - start

    # It must have finished well before the total 30s sleep
    assert elapsed < 15, f"took too long: {elapsed:.1f}s"
    assert len(complete_calls) == 1
    call = complete_calls[0]
    assert call["status"] == "cancelled", f"status inesperado: {call['status']}"
    assert call.get("cancelled_reason") == "outer_timeout", (
        f"cancelled_reason inesperado: {call.get('cancelled_reason')!r}"
    )
    # The human-readable marker no longer travels in /complete (deprecated);
    # it is now streamed as an output_event via /output.
    assert "stderr" not in call, f"complete must not include stderr: {call}"
    assert any("outer timeout" in e.get("text", "").lower() for e in output_events), (
        f"no output_event mentions outer timeout: {output_events}"
    )


@pytest.mark.skipif(_UV_BIN is None, reason="uv not available")
@respx.mock
@pytest.mark.asyncio
async def test_outer_timeout_does_not_interrupt_fast_subprocess(
    settings: Settings,
    signing_priv: Ed25519PrivateKey,
    tmp_path: Path,
) -> None:
    """Fast subprocess (1s) with outer=10s -> completes with status=completed."""
    complete_calls: list[dict[str, Any]] = []

    def _capture_complete(request: httpx.Request) -> httpx.Response:
        import json

        complete_calls.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    respx.post(f"{BASE}/v1/tasks/302/complete").mock(side_effect=_capture_complete)
    # The streamer drains via /output; mocking it keeps aclose() from hanging.
    respx.post(f"{BASE}/v1/tasks/302/output").mock(
        return_value=httpx.Response(202, json={"ok": True})
    )

    task_wire = _make_task_wire(
        task_id=302,
        commands=['python3 -c "import time; time.sleep(0.5)"'],
        execution_timeout_seconds=10,
        priv=signing_priv,
    )
    task = PollTaskPayload(
        task_id=task_wire["task_id"],
        command=task_wire["command"],
        payload=task_wire["payload"],
        created_at=task_wire["created_at"],
        delivery_mode=task_wire["delivery_mode"],
        signed=task_wire["signed"],
        signature=task_wire["signature"],
        idempotency_key=None,
        worker_id=task_wire["worker_id"],
        target_worker_id=None,
        lease_token=task_wire["lease_token"],
        lease_expires_at=task_wire["lease_expires_at"],
        execution_timeout_seconds=10,
    )

    busy = BusyState()
    stop = asyncio.Event()

    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k", stop=stop) as api:
        await _run_external(api, settings, busy, stop, task)

    assert len(complete_calls) == 1
    assert complete_calls[0]["status"] == "completed"


@pytest.mark.skipif(_UV_BIN is None, reason="uv not available")
@respx.mock
@pytest.mark.asyncio
async def test_no_outer_timeout_does_not_regress(
    settings: Settings,
    signing_priv: Ed25519PrivateKey,
    tmp_path: Path,
) -> None:
    """Without execution_timeout_seconds -> previous behavior (completes normally)."""
    complete_calls: list[dict[str, Any]] = []

    def _capture_complete(request: httpx.Request) -> httpx.Response:
        import json

        complete_calls.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    respx.post(f"{BASE}/v1/tasks/303/complete").mock(side_effect=_capture_complete)
    # The streamer drains via /output; mocking it keeps aclose() from hanging.
    respx.post(f"{BASE}/v1/tasks/303/output").mock(
        return_value=httpx.Response(202, json={"ok": True})
    )

    task_wire = _make_task_wire(
        task_id=303,
        commands=["python3 -c \"print('ok')\""],
        execution_timeout_seconds=None,  # no outer timeout
        priv=signing_priv,
    )
    task = PollTaskPayload(
        task_id=task_wire["task_id"],
        command=task_wire["command"],
        payload=task_wire["payload"],
        created_at=task_wire["created_at"],
        delivery_mode=task_wire["delivery_mode"],
        signed=task_wire["signed"],
        signature=task_wire["signature"],
        idempotency_key=None,
        worker_id=task_wire["worker_id"],
        target_worker_id=None,
        lease_token=task_wire["lease_token"],
        lease_expires_at=task_wire["lease_expires_at"],
        execution_timeout_seconds=None,
    )

    busy = BusyState()
    stop = asyncio.Event()

    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k", stop=stop) as api:
        await _run_external(api, settings, busy, stop, task)

    assert len(complete_calls) == 1
    assert complete_calls[0]["status"] == "completed"
