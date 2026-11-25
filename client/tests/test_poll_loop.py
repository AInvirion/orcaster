"""Tests for the poll_loop and _dispatch_poll_task."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import shutil
import sys
import threading
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orcaster_worker.api import OrchestratorAPI
from orcaster_worker.config import Settings
from orcaster_worker.main import (
    BusyState,
    _dispatch_poll_task,
    _process_cancel_signals,
    _run_cache_flush,
    _verify_poll_task_signature,
    poll_loop,
)
from orcaster_worker.poll import PollCancelSignal, PollTaskPayload
from orcaster_worker.signing import build_task_message, reset_key_cache

BASE = "http://server.test"
WID = "11111111-1111-1111-1111-111111111111"
POLL_URL = f"{BASE}/v1/workers/{WID}/poll"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        server_url="http://server.test",  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        poll_min_interval_seconds=0.1,
        poll_max_interval_seconds=2.0,
        lease_seconds=30,
    )


def _make_task_dict(
    *,
    task_id: int = 7,
    command: str = "/bin/echo",
    payload: dict | None = None,
    delivery_mode: str = "claim",
    signed: bool = False,
    signature: str | None = None,
    idempotency_key: str | None = None,
    lease_token: str = "33333333-3333-3333-3333-333333333333",
    created_at: str = "2026-05-18T00:00:00Z",
) -> dict:
    return {
        "task_id": task_id,
        "command": command,
        "payload": payload or {"args": ["hi"]},
        "created_at": created_at,
        "delivery_mode": delivery_mode,
        "signed": signed,
        "signature": signature,
        "idempotency_key": idempotency_key,
        "worker_id": WID,
        "target_worker_id": None,
        "lease_token": lease_token,
        "lease_expires_at": "2030-01-01T00:00:00Z",
    }


def _empty_resp(*, next_poll: int = 1) -> dict:
    return {"task": None, "cancel_signals": [], "next_poll_after_seconds": next_poll}


# --- Signature verification --------------------------------------------------


def _gen_keypair(tmp_path: Path) -> tuple[Ed25519PrivateKey, Path]:
    priv = Ed25519PrivateKey.generate()
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    p = tmp_path / "ed25519.pub.pem"
    p.write_bytes(pub_pem)
    return priv, p


def _sign(
    priv: Ed25519PrivateKey,
    *,
    task_id: int,
    worker_id: str,
    command: str,
    payload: dict,
    idempotency_key: str | None,
    created_at: str,
) -> str:
    """Signs a task with the private key and returns the base64 signature."""
    msg = build_task_message(
        task_id=task_id,
        worker_id=worker_id,
        command=command,
        payload=payload,
        idempotency_key=idempotency_key,
        created_at_iso=created_at,
    )
    return base64.b64encode(priv.sign(msg)).decode("ascii")


@pytest.fixture
def signed_task_factory(tmp_path: Path, settings: Settings):
    """Factory of PollTaskPayload signed with the server's key.

    Generates a keypair, loads it into settings.ed25519_public_key_path and
    returns a factory that produces PollTaskPayload ready for
    `_dispatch_poll_task`. Any test that no longer uses `_dispatch_poll_task`
    with signed-False tasks keeps working via this factory.
    """
    priv, pub_path = _gen_keypair(tmp_path)
    reset_key_cache()
    settings.ed25519_public_key_path = pub_path

    def _make(
        *,
        task_id: int,
        command: str,
        payload: dict | None = None,
        idempotency_key: str | None = None,
        delivery_mode: str = "claim",
        worker_id: str = WID,
        target_worker_id: str | None = None,
        created_at: str = "2026-05-18T00:00:00Z",
        lease_token: str = "lease-x",
    ) -> PollTaskPayload:
        payload = payload or {}
        sig_b64 = _sign(
            priv,
            task_id=task_id,
            worker_id=worker_id,
            command=command,
            payload=payload,
            idempotency_key=idempotency_key,
            created_at=created_at,
        )
        return PollTaskPayload(
            task_id=task_id,
            command=command,
            payload=payload,
            created_at=created_at,
            delivery_mode=delivery_mode,
            signed=True,
            signature=sig_b64,
            idempotency_key=idempotency_key,
            worker_id=worker_id,
            target_worker_id=target_worker_id,
            lease_token=lease_token,
            lease_expires_at="2030-01-01T00:00:00Z",
        )

    return _make


def test_verify_signed_task_valid(tmp_path: Path, settings: Settings) -> None:
    priv, pub_path = _gen_keypair(tmp_path)
    reset_key_cache()
    settings.ed25519_public_key_path = pub_path

    created_at = "2026-05-18T00:00:00Z"
    msg = build_task_message(
        task_id=42,
        worker_id=WID,
        command="__reload_config__",
        payload={"k": "v"},
        idempotency_key="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        created_at_iso=created_at,
    )
    sig = priv.sign(msg)
    task = PollTaskPayload(
        task_id=42,
        command="__reload_config__",
        payload={"k": "v"},
        created_at=created_at,
        delivery_mode="push",
        signed=True,
        signature=base64.b64encode(sig).decode("ascii"),
        idempotency_key="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        worker_id=WID,
        target_worker_id=WID,
        lease_token="lease-xyz",
        lease_expires_at="2030-01-01T00:00:00Z",
    )
    assert _verify_poll_task_signature(task, settings) is True


def test_verify_signed_task_tampered(tmp_path: Path, settings: Settings) -> None:
    priv, pub_path = _gen_keypair(tmp_path)
    reset_key_cache()
    settings.ed25519_public_key_path = pub_path

    created_at = "2026-05-18T00:00:00Z"
    msg = build_task_message(
        task_id=42,
        worker_id=WID,
        command="__reload_config__",
        payload={"k": "v"},
        idempotency_key=None,
        created_at_iso=created_at,
    )
    sig = priv.sign(msg)
    # Tamper the payload after signing.
    task = PollTaskPayload(
        task_id=42,
        command="__reload_config__",
        payload={"k": "evil"},  # tampered
        created_at=created_at,
        delivery_mode="push",
        signed=True,
        signature=base64.b64encode(sig).decode("ascii"),
        idempotency_key=None,
        worker_id=WID,
        target_worker_id=WID,
        lease_token="lease-xyz",
        lease_expires_at="2030-01-01T00:00:00Z",
    )
    assert _verify_poll_task_signature(task, settings) is False


def test_verify_signed_task_without_pubkey(settings: Settings) -> None:
    """If no key is configured, we reject signed tasks."""
    settings.ed25519_public_key_path = None
    task = PollTaskPayload(
        task_id=1,
        command="__reload_config__",
        payload={},
        created_at="2026-05-18T00:00:00Z",
        delivery_mode="push",
        signed=True,
        signature="AAAA",
        idempotency_key=None,
        worker_id=WID,
        target_worker_id=WID,
        lease_token="x",
        lease_expires_at="2030-01-01T00:00:00Z",
    )
    assert _verify_poll_task_signature(task, settings) is False


def test_canonical_message_byte_identical_to_server() -> None:
    """Reproduces the server fixture test_task_canonical_message_lex_order.

    Guarantees byte-exact compatibility of the canonical signed message.
    """
    from orcaster_worker.signing import canonical_json

    msg = canonical_json(
        {
            "worker_id": "11111111-1111-1111-1111-111111111111",
            "task_id": 42,
            "payload": {"k": "v"},
            "idempotency_key": None,
            "created_at": "2026-05-18T00:00:00+00:00",
            "command": "ping",
        }
    )
    expected = (
        b'{"command":"ping","created_at":"2026-05-18T00:00:00+00:00",'
        b'"idempotency_key":null,"payload":{"k":"v"},"task_id":42,'
        b'"worker_id":"11111111-1111-1111-1111-111111111111"}'
    )
    assert msg == expected


# --- Cancel signals ----------------------------------------------------------


def test_cancel_signal_matches_busy_sets_abandon() -> None:
    busy = BusyState()
    busy.task_id = 77
    busy.lease_token = "lease-A"
    signals = [PollCancelSignal(task_id=77, lease_token="lease-A")]
    _process_cancel_signals(signals, busy)
    assert busy.abandon_event.is_set()


def test_cancel_signal_no_match_when_lease_differs() -> None:
    busy = BusyState()
    busy.task_id = 77
    busy.lease_token = "lease-A"
    signals = [PollCancelSignal(task_id=77, lease_token="lease-B")]
    _process_cancel_signals(signals, busy)
    assert not busy.abandon_event.is_set()


def test_cancel_signal_no_match_when_no_busy() -> None:
    busy = BusyState()
    signals = [PollCancelSignal(task_id=77, lease_token="lease-A")]
    _process_cancel_signals(signals, busy)
    assert not busy.abandon_event.is_set()


# --- _dispatch_poll_task -----------------------------------------------------


@respx.mock
async def test_dispatch_reserved_reload_config_completes(
    settings: Settings,
    signed_task_factory,
) -> None:
    """__reload_config__ runs in-process and completes with ok."""
    task = signed_task_factory(
        task_id=10,
        command="__reload_config__",
        payload={},
        delivery_mode="push",
        target_worker_id=WID,
    )
    complete_route = respx.post(f"{BASE}/v1/tasks/10/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        busy = BusyState()
        stop = asyncio.Event()
        await _dispatch_poll_task(api, settings, busy, stop, task)
    assert complete_route.called
    body = complete_route.calls.last.request.read().decode()
    assert '"status":"completed"' in body or '"status": "completed"' in body


@respx.mock
async def test_dispatch_push_with_non_reserved_command_routes_to_external(
    settings: Settings, signed_task_factory
) -> None:
    """A push task with a non-reserved command is no longer rejected in routing;
    it falls to _run_external, which fails it with the payload error (no requirements/commands).
    The guard 'non-reserved push tasks not supported' was removed.
    """
    complete_route = respx.post(f"{BASE}/v1/tasks/11/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    output_route = respx.post(f"{BASE}/v1/tasks/11/output").mock(
        return_value=httpx.Response(202, json={"ok": True})
    )
    task = signed_task_factory(
        task_id=11,
        command="__install_dep__",
        payload={"spec_hash": "sha256:xx"},
        delivery_mode="push",
        target_worker_id=WID,
    )
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        busy = BusyState()
        stop = asyncio.Event()
        await _dispatch_poll_task(api, settings, busy, stop, task)
    assert complete_route.called
    body = complete_route.calls.last.request.read().decode()
    assert '"exit_code":2' in body or '"exit_code": 2' in body
    # Payload validation error: the text goes out as an output_event.
    import json as _json

    output_events = _json.loads(output_route.calls.last.request.read()).get("events", [])
    assert any(
        "requirements" in e.get("text", "") or "commands" in e.get("text", "")
        for e in output_events
    ), f"payload error text not found in events={output_events}"


@respx.mock
async def test_dispatch_invalid_signature_does_not_complete(
    settings: Settings, tmp_path: Path
) -> None:
    """Invalid signature -> not executed and not completed."""
    _, pub_path = _gen_keypair(tmp_path)
    reset_key_cache()
    settings.ed25519_public_key_path = pub_path

    complete_route = respx.post(f"{BASE}/v1/tasks/13/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    task = PollTaskPayload(
        task_id=13,
        command="__reload_config__",
        payload={},
        created_at="2026-05-18T00:00:00Z",
        delivery_mode="push",
        signed=True,
        signature=base64.b64encode(b"\x00" * 64).decode("ascii"),
        idempotency_key=None,
        worker_id=WID,
        target_worker_id=WID,
        lease_token="ll",
        lease_expires_at="2030-01-01T00:00:00Z",
    )
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        busy = BusyState()
        stop = asyncio.Event()
        await _dispatch_poll_task(api, settings, busy, stop, task)
    assert not complete_route.called


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX echo path")
@pytest.mark.skipif(shutil.which("uv") is None, reason="requires uv binary")
@respx.mock
async def test_dispatch_external_task_executes_and_completes(
    settings: Settings, signed_task_factory, tmp_path: Path
) -> None:
    """A regular claimed task runs in an ephemeral venv +
    dispatch_run_batch and completes."""
    settings.uv_bin = shutil.which("uv") or "uv"
    settings.uv_cache_dir = tmp_path / "uv-cache"
    complete_route = respx.post(f"{BASE}/v1/tasks/14/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    # Output is streamed via /output; mock so the streamer doesn't fail.
    respx.post(f"{BASE}/v1/tasks/14/output").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    task = signed_task_factory(
        task_id=14,
        command="__run_batch__",
        payload={"requirements": [], "commands": ["echo hello"]},
        delivery_mode="claim",
        target_worker_id=None,
        lease_token="lease-14",
    )
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        busy = BusyState()
        stop = asyncio.Event()
        await _dispatch_poll_task(api, settings, busy, stop, task)
    assert complete_route.called
    body = complete_route.calls.last.request.read().decode()
    # Post-dispatch: stdout was streamed via /output; /complete does not re-send it.
    assert '"status":"completed"' in body or '"status": "completed"' in body


@respx.mock
async def test_dispatch_idempotency_replays_result(settings: Settings, signed_task_factory) -> None:
    """If idempotency_key already has a stored result, it is re-sent without executing."""
    from orcaster_worker.idempotency import store_push_result

    key = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    store_push_result(
        settings.state_dir,
        key,
        status="completed",
        exit_code=0,
        stdout="cached stdout",
        stderr="",
        result={"cached": True},
    )

    complete_route = respx.post(f"{BASE}/v1/tasks/15/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    task = signed_task_factory(
        task_id=15,
        command="__reload_config__",
        payload={"should_not_run": True},
        delivery_mode="push",
        target_worker_id=WID,
        idempotency_key=key,
    )
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        busy = BusyState()
        stop = asyncio.Event()
        await _dispatch_poll_task(api, settings, busy, stop, task)
    assert complete_route.called
    body = complete_route.calls.last.request.read().decode()
    # Idempotency: the stored status/exit_code is re-sent; the previous output
    # is not re-emitted (the original lease already expired; loss policy).
    assert '"status":"completed"' in body or '"status": "completed"' in body
    assert '"exit_code":0' in body or '"exit_code": 0' in body


# --- poll_loop integration --------------------------------------------------


@respx.mock
async def test_poll_loop_processes_cancel_signal(settings: Settings) -> None:
    """The loop receives cancel_signals matching busy and sets abandon_event."""
    busy = BusyState()
    busy.task_id = 99
    busy.lease_token = "lease-99"

    iter_count = {"n": 0}

    def _handler(request):
        iter_count["n"] += 1
        if iter_count["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "task": None,
                    "cancel_signals": [{"task_id": 99, "lease_token": "lease-99"}],
                    "next_poll_after_seconds": 1,
                },
            )
        return httpx.Response(200, json=_empty_resp(next_poll=1))

    respx.post(POLL_URL).mock(side_effect=_handler)

    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()

        async def stopper():
            # Wait for abandon_event to be set and then stop the loop.
            for _ in range(50):
                if busy.abandon_event.is_set():
                    break
                await asyncio.sleep(0.05)
            stop.set()

        loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop))
        await asyncio.wait_for(stopper(), timeout=5.0)
        await asyncio.wait_for(loop_task, timeout=2.0)

    assert busy.abandon_event.is_set()


@respx.mock
async def test_poll_loop_idle_no_task(settings: Settings) -> None:
    """Empty poll loop: makes at least one poll and honors the cadence hint."""
    route = respx.post(POLL_URL).mock(
        return_value=httpx.Response(200, json=_empty_resp(next_poll=1))
    )
    busy = BusyState()
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop))
        for _ in range(50):
            if route.called:
                break
            await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=2.0)
    assert route.called
    body = route.calls[0].request.read().decode()
    assert '"status":"idle"' in body or '"status": "idle"' in body


@respx.mock
async def test_poll_loop_reports_the_worker_version(settings: Settings) -> None:
    """The poll carries the version.

    `register` runs only once per identity: an updated worker keeps
    its state.json and does not register again, so the version can only be
    corrected through this path.
    """
    route = respx.post(POLL_URL).mock(
        return_value=httpx.Response(200, json=_empty_resp(next_poll=1))
    )
    busy = BusyState()
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop))
        for _ in range(50):
            if route.called:
                break
            await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=2.0)
    assert route.called
    body = route.calls[0].request.read().replace(b" ", b"")
    assert f'"version":"{settings.version}"'.encode() in body


@respx.mock
async def test_poll_loop_declares_the_configured_capabilities(tmp_path: Path) -> None:
    """`ORCH_CAPABILITIES` travels in every poll: changing them does not re-enroll."""
    settings = Settings(
        server_url="http://server.test",  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        poll_min_interval_seconds=0.1,
        poll_max_interval_seconds=2.0,
        lease_seconds=30,
        capabilities_json='{"model": true, "model_context": 8192}',
    )
    route = respx.post(POLL_URL).mock(
        return_value=httpx.Response(200, json=_empty_resp(next_poll=1))
    )
    busy = BusyState()
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop))
        for _ in range(50):
            if route.called:
                break
            await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=2.0)
    assert route.called
    body = json.loads(route.calls[0].request.read())
    assert body["capabilities"] == {"model": True, "model_context": 8192}


@respx.mock
async def test_poll_loop_without_capabilities_omits_the_field(settings: Settings) -> None:
    """Without `ORCH_CAPABILITIES`, the server keeps what it has stored."""
    route = respx.post(POLL_URL).mock(
        return_value=httpx.Response(200, json=_empty_resp(next_poll=1))
    )
    busy = BusyState()
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop))
        for _ in range(50):
            if route.called:
                break
            await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=2.0)
    assert route.called
    assert "capabilities" not in json.loads(route.calls[0].request.read())


# --- Cache flush integration --------------------------------------------------


@respx.mock
async def test_poll_loop_clear_cache_runs_flush_and_acks(
    settings: Settings, monkeypatch, tmp_path
) -> None:
    """clear_cache=true idle -> launches flush, reports busy while it runs, acks when done."""
    import orcaster_worker.main as main_mod

    flush_calls: list[tuple] = []

    def fake_flush(cache_root, uv_cache_dir):
        from orcaster_worker.cache_flush import FlushStats

        flush_calls.append((cache_root, uv_cache_dir))
        return FlushStats(0, 0)

    monkeypatch.setattr(main_mod, "flush_caches", fake_flush)

    responses = [
        httpx.Response(200, json={**_empty_resp(next_poll=1), "clear_cache": True}),
        httpx.Response(200, json=_empty_resp(next_poll=1)),
        httpx.Response(200, json=_empty_resp(next_poll=1)),
        httpx.Response(200, json=_empty_resp(next_poll=1)),
    ]
    route = respx.post(POLL_URL).mock(side_effect=responses)
    busy = BusyState()
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop))
        for _ in range(100):
            if len(route.calls) >= 3:
                break
            await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=2.0)

    assert len(flush_calls) == 1
    acked = any(
        b'"cache_flush_completed":true' in c.request.read().replace(b" ", b"") for c in route.calls
    )
    assert acked


@respx.mock
async def test_poll_loop_reports_busy_while_flushing(settings: Settings, monkeypatch) -> None:
    """While the flush runs, the next poll reports status=busy (it occupies the slot)."""
    import threading

    import orcaster_worker.main as main_mod

    # threading.Event is thread-safe: slow_flush runs in a worker thread
    # (asyncio.to_thread) and the test releases from the event loop. An asyncio.Event
    # would be thread-unsafe across that boundary.
    release = threading.Event()

    def slow_flush(cache_root, uv_cache_dir):
        from orcaster_worker.cache_flush import FlushStats

        release.wait()
        return FlushStats(0, 0)

    monkeypatch.setattr(main_mod, "flush_caches", slow_flush)
    route = respx.post(POLL_URL).mock(
        return_value=httpx.Response(200, json={**_empty_resp(next_poll=1), "clear_cache": True})
    )
    busy = BusyState()
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop))
        for _ in range(100):
            if len(route.calls) >= 2:
                break
            await asyncio.sleep(0.02)
        second = route.calls[1].request.read().replace(b" ", b"")
        assert b'"status":"busy"' in second
        release.set()
        stop.set()
        await asyncio.wait_for(loop_task, timeout=2.0)


# --- Cache flush timeout ------------------------------------------------------


async def test_run_cache_flush_normal_acks_without_warning(
    settings: Settings, monkeypatch, caplog
) -> None:
    """Fast flush: pending_ack stays True and the timeout warning is not logged."""
    import orcaster_worker.main as main_mod

    def fast_flush(cache_root, uv_cache_dir):
        from orcaster_worker.cache_flush import FlushStats

        return FlushStats(0, 0)

    monkeypatch.setattr(main_mod, "flush_caches", fast_flush)
    busy = BusyState()
    with caplog.at_level(logging.WARNING, logger="orcaster.worker"):
        await _run_cache_flush(settings, busy)

    assert busy.cache_flush_pending_ack is True
    assert not any("exceeded" in r.getMessage() for r in caplog.records)


async def test_run_cache_flush_timeout_acks_and_warns(monkeypatch, caplog, tmp_path) -> None:
    """A flush that exceeds the timeout: returns, logs a WARNING and leaves pending_ack True.

    The ``to_thread`` thread is not cancelled, so we unblock it at the end
    so as not to leave dangling threads in the suite.
    """
    import orcaster_worker.main as main_mod

    settings = Settings(
        server_url="http://server.test",  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
    )
    # The field requires ge=1; for the test we bound the timeout to a minimal value
    # by mutating the attribute (no assignment validation), enough to
    # trigger the TimeoutError deterministically and quickly.
    settings.cache_flush_timeout_seconds = 0.05

    release = threading.Event()

    def slow_flush(cache_root, uv_cache_dir):
        from orcaster_worker.cache_flush import FlushStats

        # Blocks longer than the timeout; released from the test at the end so as not to
        # leave the thread dangling (at most 5s of hard guard).
        release.wait(timeout=5.0)
        return FlushStats(0, 0)

    monkeypatch.setattr(main_mod, "flush_caches", slow_flush)
    busy = BusyState()
    with caplog.at_level(logging.WARNING, logger="orcaster.worker"):
        await _run_cache_flush(settings, busy)

    assert busy.cache_flush_pending_ack is True
    assert any("exceeded" in r.getMessage() for r in caplog.records)
    # Unblock the orphaned thread of the simulated rmtree.
    release.set()


# --- Self-flush on the disk preflight ------------------------------

_DISK_MARKER = "infrastructure constraint: Insufficient disk space"


async def test_disk_preflight_scanner_forwards_and_detects_single_chunk() -> None:
    """The scanner forwards each chunk intact to the sink and detects the marker."""
    from orcaster_worker.main import _DiskPreflightScanner

    seen: list[tuple] = []

    async def sink(stdout=None, stderr=None, command=None):
        seen.append((stdout, stderr, command))

    line = f"tool: {_DISK_MARKER}: need 40GiB, free 3GiB\n"
    scanner = _DiskPreflightScanner(sink, (_DISK_MARKER,))
    await scanner.feed(stderr=line)

    assert scanner.marker_seen is True
    assert seen == [(None, line, None)]


async def test_disk_preflight_scanner_detects_across_chunk_boundary() -> None:
    """A marker split between two stderr chunks is still detected."""
    from orcaster_worker.main import _DiskPreflightScanner

    async def sink(stdout=None, stderr=None, command=None):
        return None

    scanner = _DiskPreflightScanner(sink, (_DISK_MARKER,))
    mid = len(_DISK_MARKER) // 2
    await scanner.feed(stderr="tool: " + _DISK_MARKER[:mid])
    assert scanner.marker_seen is False
    await scanner.feed(stderr=_DISK_MARKER[mid:] + ": need 40GiB\n")
    assert scanner.marker_seen is True


async def test_disk_preflight_scanner_no_marker() -> None:
    """Output without the marker does not trigger detection (neither stdout nor stderr)."""
    from orcaster_worker.main import _DiskPreflightScanner

    async def sink(stdout=None, stderr=None, command=None):
        return None

    scanner = _DiskPreflightScanner(sink, (_DISK_MARKER,))
    await scanner.feed(stdout="loading model shards\n")
    await scanner.feed(stderr="tool: some unrelated permanent error\n")
    assert scanner.marker_seen is False


async def test_disk_preflight_scanner_ignores_marker_on_stdout() -> None:
    """The marker on stdout does NOT trigger detection: only stderr is scanned,
    to avoid a false positive if a tool prints the text on its stdout."""
    from orcaster_worker.main import _DiskPreflightScanner

    async def sink(stdout=None, stderr=None, command=None):
        return None

    scanner = _DiskPreflightScanner(sink, (_DISK_MARKER,))
    await scanner.feed(stdout=f"tool: {_DISK_MARKER}: need 40GiB, free 3GiB\n")
    assert scanner.marker_seen is False


def test_should_self_flush_true_on_marker_and_failure(settings: Settings) -> None:
    """Failed batch + marker + flag enabled -> the self-flush is requested."""
    from orcaster_worker.main import _should_self_flush_on_disk_preflight

    assert _should_self_flush_on_disk_preflight(settings, "error", True) is True


def test_should_self_flush_false_without_marker(settings: Settings) -> None:
    """Batch failure WITHOUT the marker -> not triggered.

    exit 1 is a catch-all of the task (permanent failure / infra / transient):
    the marker on stderr is the real signal, not the exit code.
    """
    from orcaster_worker.main import _should_self_flush_on_disk_preflight

    assert _should_self_flush_on_disk_preflight(settings, "error", False) is False


def test_should_self_flush_false_on_success_even_with_marker(settings: Settings) -> None:
    """Only triggered if the batch reported failure; not with status ok."""
    from orcaster_worker.main import _should_self_flush_on_disk_preflight

    assert _should_self_flush_on_disk_preflight(settings, "ok", True) is False


def test_should_self_flush_false_when_flag_disabled(settings: Settings) -> None:
    """Flag disabled -> not triggered even with the marker and a failure."""
    from orcaster_worker.main import _should_self_flush_on_disk_preflight

    settings.cache_self_flush_on_disk_preflight = False
    assert _should_self_flush_on_disk_preflight(settings, "error", True) is False


@respx.mock
async def test_poll_loop_self_flush_requested_runs_flush_and_acks(
    settings: Settings, monkeypatch
) -> None:
    """self_flush_requested=True idle -> launches flush, acks, and resets the flag."""
    import orcaster_worker.main as main_mod

    flush_calls: list[tuple] = []

    def fake_flush(cache_root, uv_cache_dir):
        from orcaster_worker.cache_flush import FlushStats

        flush_calls.append((cache_root, uv_cache_dir))
        return FlushStats(0, 0)

    monkeypatch.setattr(main_mod, "flush_caches", fake_flush)

    responses = [
        httpx.Response(200, json=_empty_resp(next_poll=1)),
        httpx.Response(200, json=_empty_resp(next_poll=1)),
        httpx.Response(200, json=_empty_resp(next_poll=1)),
        httpx.Response(200, json=_empty_resp(next_poll=1)),
    ]
    route = respx.post(POLL_URL).mock(side_effect=responses)
    busy = BusyState()
    busy.self_flush_requested = True
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop))
        for _ in range(100):
            if len(route.calls) >= 3:
                break
            await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=2.0)

    assert len(flush_calls) == 1
    assert busy.self_flush_requested is False
    acked = any(
        b'"cache_flush_completed":true' in c.request.read().replace(b" ", b"") for c in route.calls
    )
    assert acked


async def test_busy_state_reset_preserves_self_flush_requested() -> None:
    """reset() (the reap) must NOT clear self_flush_requested. The flag
    has to survive the reap between the executor's end and the next poll;
    a reset() that cleared it would silently disable the feature. Regression
    guard for the invariant that the field's declaration states in main.py."""
    busy = BusyState()
    busy.self_flush_requested = True
    busy.reset()
    assert busy.self_flush_requested is True


@respx.mock
async def test_poll_loop_reports_busy_while_self_flush_pending(
    settings: Settings, monkeypatch
) -> None:
    """With a self-flush pending, the poll reports status=busy, so that
    the server does not assign a task in that same poll. The worker just reaped the
    executor (is_busy/is_flushing False) but the flush has not started yet; without the
    busy status the server would see idle, deliver a task that the worker ignores
    when the flush starts, and that execution would hang until the
    lease expires."""
    import orcaster_worker.main as main_mod

    def fake_flush(cache_root, uv_cache_dir):
        from orcaster_worker.cache_flush import FlushStats

        return FlushStats(0, 0)

    monkeypatch.setattr(main_mod, "flush_caches", fake_flush)
    route = respx.post(POLL_URL).mock(
        return_value=httpx.Response(200, json=_empty_resp(next_poll=1))
    )
    busy = BusyState()
    busy.self_flush_requested = True
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop))
        for _ in range(100):
            if len(route.calls) >= 1:
                break
            await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=2.0)

    first = route.calls[0].request.read().replace(b" ", b"")
    assert b'"status":"busy"' in first


@respx.mock
async def test_poll_loop_no_self_flush_when_flag_absent(settings: Settings, monkeypatch) -> None:
    """Without self_flush_requested no flush is launched (default idle)."""
    import orcaster_worker.main as main_mod

    flush_calls: list[int] = []

    def fake_flush(cache_root, uv_cache_dir):
        from orcaster_worker.cache_flush import FlushStats

        flush_calls.append(1)
        return FlushStats(0, 0)

    monkeypatch.setattr(main_mod, "flush_caches", fake_flush)
    route = respx.post(POLL_URL).mock(
        return_value=httpx.Response(200, json=_empty_resp(next_poll=1))
    )
    busy = BusyState()  # self_flush_requested stays False (default)
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop))
        for _ in range(100):
            if len(route.calls) >= 3:
                break
            await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=2.0)

    assert flush_calls == []


# --- Disk gate for claiming ----------------------------------------------------


def _gated(settings: Settings, **update) -> Settings:
    """`settings` with a fast cadence even while gated.

    A gated worker stays on its local idle cadence
    (`poll_max_interval_seconds`) instead of accepting the 5 s the server gives to
    `busy` workers, so tests that need several polls in a row have to lower
    that ceiling: with the fixture default (2 s) the second poll does not fit in the
    wait budget. `model_copy` does not revalidate, and that is why the field's
    `ge=1.0` admits a low value.
    """
    return settings.model_copy(update={"poll_max_interval_seconds": 0.1, **update})


def _disk(free_fraction: float) -> object:
    """`DiskSnapshot` with the requested free fraction (fixed total of 100 GB)."""
    from orcaster_worker.sysinfo import DiskSnapshot

    total_mb = 100 * 1024
    free_mb = int(total_mb * free_fraction)
    return DiskSnapshot(
        total_mb=total_mb,
        used_mb=total_mb - free_mb,
        free_fraction=free_fraction,
    )


@respx.mock
async def test_poll_loop_does_not_claim_below_the_disk_floor(
    settings: Settings, monkeypatch, caplog
) -> None:
    """The disk gate now lives in the server. The worker sends the free
    fraction in every poll and reports `idle` while gated, so that the server
    withholds its tasks and can still hand it the `clear_cache` lane. The
    `disk_gated` flag stays alive locally (cadence throttle, log), but no longer
    influences the reported status."""
    import orcaster_worker.main as main_mod

    monkeypatch.setattr(main_mod, "detect_disk", lambda _root: _disk(0.03))
    route = respx.post(POLL_URL).mock(
        return_value=httpx.Response(200, json=_empty_resp(next_poll=1))
    )
    busy = BusyState()
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        with caplog.at_level(logging.WARNING, logger="orcaster.worker"):
            loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop))
            for _ in range(50):
                if route.called:
                    break
                await asyncio.sleep(0.05)
            stop.set()
            await asyncio.wait_for(loop_task, timeout=2.0)

    body = route.calls[0].request.read().replace(b" ", b"")
    assert b'"status":"idle"' in body, "gated but idle: the gate is applied by the server"
    assert b'"current_task_id"' not in body, "there is no live task"
    assert b'"free_disk_fraction":0.03' in body, "the worker sends the measured free fraction"
    assert any("disk gate" in r.message for r in caplog.records)


@respx.mock
async def test_poll_loop_claims_normally_with_ample_disk(settings: Settings, monkeypatch) -> None:
    """Above the floor, the gate does not intervene: the worker keeps reporting idle."""
    import orcaster_worker.main as main_mod

    monkeypatch.setattr(main_mod, "detect_disk", lambda _root: _disk(0.50))
    route = respx.post(POLL_URL).mock(
        return_value=httpx.Response(200, json=_empty_resp(next_poll=1))
    )
    busy = BusyState()
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop))
        for _ in range(50):
            if route.called:
                break
            await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=2.0)

    assert b'"status":"idle"' in route.calls[0].request.read().replace(b" ", b"")


@respx.mock
async def test_poll_loop_disk_gate_disabled_with_zero(settings: Settings, monkeypatch) -> None:
    """`0.0` is the operator's escape hatch: no gate, previous behavior.

    It matters because the server's flush lane is only handed to
    `idle` workers: a gated worker cannot be flushed from the dashboard.
    """
    gateless = settings.model_copy(update={"cache_claim_min_free_fraction": 0.0})
    import orcaster_worker.main as main_mod

    monkeypatch.setattr(main_mod, "detect_disk", lambda _root: _disk(0.01))
    route = respx.post(POLL_URL).mock(
        return_value=httpx.Response(200, json=_empty_resp(next_poll=1))
    )
    busy = BusyState()
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        loop_task = asyncio.create_task(poll_loop(api, gateless, busy, stop))
        for _ in range(50):
            if route.called:
                break
            await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=2.0)

    assert b'"status":"idle"' in route.calls[0].request.read().replace(b" ", b"")


@respx.mock
async def test_poll_loop_disk_gate_warns_only_on_transition(
    settings: Settings, monkeypatch, caplog
) -> None:
    """Same criterion as `pressure_unrecoverable`: one WARNING per tick while
    the disk stays full buries the case the log exists to show."""
    import orcaster_worker.main as main_mod

    fast = _gated(settings)
    monkeypatch.setattr(main_mod, "detect_disk", lambda _root: _disk(0.03))
    # next_poll=0 clamps to poll_min_interval_seconds=0.1: the wait below
    # depends on >=3 polls fitting in its budget, and with
    # next_poll=1 the real cadence would be 1.0s (3rd poll at t=2.0s, tied with
    # the budget). With a 0.1s cadence the 3rd poll arrives at t=0.2s.
    route = respx.post(POLL_URL).mock(
        return_value=httpx.Response(200, json=_empty_resp(next_poll=0))
    )
    busy = BusyState()
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        with caplog.at_level(logging.WARNING, logger="orcaster.worker"):
            loop_task = asyncio.create_task(poll_loop(api, fast, busy, stop))
            for _ in range(100):
                if len(route.calls) >= 3:
                    break
                await asyncio.sleep(0.02)
            stop.set()
            await asyncio.wait_for(loop_task, timeout=2.0)

    assert len(route.calls) >= 3
    warns = [r for r in caplog.records if "disk gate" in r.message]
    assert len(warns) == 1, f"one WARNING per tick: {len(warns)} in {len(route.calls)} polls"


@respx.mock
async def test_poll_loop_disk_gate_reports_recovery(
    settings: Settings, monkeypatch, caplog
) -> None:
    """The all-clear requires the same evidence as the warning: it is measured and reported."""
    import orcaster_worker.main as main_mod

    fractions = iter([0.03, 0.03, 0.50, 0.50, 0.50, 0.50])

    def flaky_disk(_root):
        return _disk(next(fractions, 0.50))

    monkeypatch.setattr(main_mod, "detect_disk", flaky_disk)
    # next_poll=0 clamps to poll_min_interval_seconds=0.1: with next_poll=1 the
    # 4th poll would land at t=3.0s, outside the wait budget below.
    route = respx.post(POLL_URL).mock(
        return_value=httpx.Response(200, json=_empty_resp(next_poll=0))
    )
    busy = BusyState()
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        with caplog.at_level(logging.INFO, logger="orcaster.worker"):
            loop_task = asyncio.create_task(poll_loop(api, _gated(settings), busy, stop))
            for _ in range(100):
                if len(route.calls) >= 4:
                    break
                await asyncio.sleep(0.02)
            stop.set()
            await asyncio.wait_for(loop_task, timeout=2.0)

    assert any("disk gate released" in r.message for r in caplog.records)
    assert b'"status":"idle"' in route.calls[-1].request.read().replace(b" ", b"")


@respx.mock
async def test_poll_loop_disk_gate_does_not_fire_without_measurement(
    settings: Settings, monkeypatch
) -> None:
    """Without a measurement (`free_fraction=None`, e.g. `statvfs` failed) there is no gating:
    ceasing to take tasks because it could not measure would be worse than the bug."""
    import orcaster_worker.main as main_mod
    from orcaster_worker.sysinfo import DiskSnapshot

    monkeypatch.setattr(
        main_mod,
        "detect_disk",
        lambda _root: DiskSnapshot(total_mb=0, used_mb=0, free_fraction=None),
    )
    route = respx.post(POLL_URL).mock(
        return_value=httpx.Response(200, json=_empty_resp(next_poll=1))
    )
    busy = BusyState()
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop))
        for _ in range(50):
            if route.called:
                break
            await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=2.0)

    body = route.calls[0].request.read().replace(b" ", b"")
    assert b'"status":"idle"' in body
    assert b'"free_disk_fraction"' not in body, (
        "without a measurement (fallback to `/`) the field is omitted"
    )


@respx.mock
async def test_poll_loop_dispatches_an_already_delivered_task_even_when_gated(
    settings: Settings, monkeypatch
) -> None:
    """The gate decides whether we ASK for work, not whether we run what already arrived.

    It matters for directed push tasks: step 4 of the pick delivers them even if
    the worker reports `busy`, so a task pinned to a gated worker arrives
    anyway. It is dispatched, same policy as the residual window (the disk may
    drop between the idle poll and the dispatch): the task runs and fails once with the
    real cause, instead of churning an attempt per lease period by ignoring it.
    """
    import orcaster_worker.main as main_mod

    monkeypatch.setattr(main_mod, "detect_disk", lambda _root: _disk(0.03))
    dispatched: list[int] = []

    async def fake_dispatch(api, settings_, busy_, stop_, task):  # noqa: ANN001
        dispatched.append(task.task_id)

    monkeypatch.setattr(main_mod, "_dispatch_poll_task", fake_dispatch)

    responses = [
        httpx.Response(
            200,
            json={**_empty_resp(next_poll=1), "task": _make_task_dict(delivery_mode="push")},
        ),
        httpx.Response(200, json=_empty_resp(next_poll=1)),
        httpx.Response(200, json=_empty_resp(next_poll=1)),
    ]
    route = respx.post(POLL_URL).mock(side_effect=responses)
    busy = BusyState()
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop))
        for _ in range(100):
            if dispatched:
                break
            await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=2.0)

    assert dispatched == [7], "the task delivered to a gated worker must be dispatched"
    assert b'"status":"idle"' in route.calls[0].request.read().replace(b" ", b"")


@respx.mock
async def test_poll_loop_disk_gate_does_not_announce_release_when_going_busy(
    settings: Settings, monkeypatch, caplog
) -> None:
    """The gate's all-clear is given by the disk, not by being busy.

    A directed push arrives at a gated worker anyway (step 4 of the pick), and
    while executing it the worker reports `busy` for the task. If the gate's state
    were derived from that, the log would announce "disk gate released" with the
    volume still full, and would announce the closing again when the task ends: two
    false lines for every task on a disk that did not move.
    """
    import orcaster_worker.main as main_mod

    monkeypatch.setattr(main_mod, "detect_disk", lambda _root: _disk(0.03))
    release = asyncio.Event()

    async def blocking_dispatch(api, settings_, busy_, stop_, task):  # noqa: ANN001
        await release.wait()

    monkeypatch.setattr(main_mod, "_dispatch_poll_task", blocking_dispatch)

    # next_poll=0 clamps to poll_min_interval_seconds=0.1: with next_poll=1 the
    # 3rd poll would land at t=2.0s, tied with the wait budget below.
    responses = [
        httpx.Response(
            200,
            json={**_empty_resp(next_poll=0), "task": _make_task_dict(delivery_mode="push")},
        ),
        httpx.Response(200, json=_empty_resp(next_poll=0)),
        httpx.Response(200, json=_empty_resp(next_poll=0)),
    ]
    route = respx.post(POLL_URL).mock(side_effect=responses)
    busy = BusyState()
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        with caplog.at_level(logging.INFO, logger="orcaster.worker"):
            loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop))
            for _ in range(100):
                if len(route.calls) >= 3:
                    break
                await asyncio.sleep(0.02)
            # Checked before releasing the dispatch: after `release` the
            # executor_task ends and `is_busy` goes back to False.
            busy_during_the_polls = busy.is_busy
            stop.set()
            release.set()
            await asyncio.wait_for(loop_task, timeout=2.0)

    assert len(route.calls) >= 3, "polls with the live task are needed to prove the case"
    assert busy_during_the_polls, "the dispatch had to keep running during those polls"
    assert not any("disk gate released" in r.message for r in caplog.records), (
        "the gate was declared released because of being busy, with the disk still below the floor"
    )
    assert len([r for r in caplog.records if "disk gate closed" in r.message]) == 1


@respx.mock
async def test_poll_loop_disk_gate_measurement_loss_does_not_announce_recovery(
    settings: Settings, monkeypatch, caplog
) -> None:
    """Measurement can be lost while the gate is closed (unmounted volume,
    `OSError` that makes `detect_disk` fall back to `/`, etc.):
    that also turns off `disk_below_floor`, just like a real recovery.

    The worker must keep claiming (fail-open, unchanged here), but the log
    must NOT announce "disk gate released" when what happened is that it could no
    longer be measured, not that the volume was emptied.
    """
    import orcaster_worker.main as main_mod
    from orcaster_worker.sysinfo import DiskSnapshot

    lost = DiskSnapshot(total_mb=0, used_mb=0, free_fraction=None)
    snapshots = iter([_disk(0.03), lost])

    def flaky_disk(_root):
        return next(snapshots, lost)

    monkeypatch.setattr(main_mod, "detect_disk", flaky_disk)
    # next_poll=0 clamps to poll_min_interval_seconds=0.1: fast and
    # deterministic cadence so the 2nd poll fits within the budget below.
    route = respx.post(POLL_URL).mock(
        return_value=httpx.Response(200, json=_empty_resp(next_poll=0))
    )
    busy = BusyState()
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        with caplog.at_level(logging.INFO, logger="orcaster.worker"):
            loop_task = asyncio.create_task(poll_loop(api, _gated(settings), busy, stop))
            for _ in range(100):
                if len(route.calls) >= 2:
                    break
                await asyncio.sleep(0.02)
            stop.set()
            await asyncio.wait_for(loop_task, timeout=2.0)

    assert len(route.calls) >= 2, (
        "2 polls are needed: one below the floor and one with measurement lost"
    )
    assert b'"status":"idle"' in route.calls[0].request.read().replace(b" ", b"")
    assert any(
        r.levelname == "WARNING" and "disk gate closed" in r.message for r in caplog.records
    ), "the first poll must close the gate with the real measurement"
    assert any(
        r.levelname == "WARNING" and "loss of measurement" in r.message for r in caplog.records
    ), "the loss of measurement must emit its own WARNING, not the recovery INFO"
    assert not any("disk gate released" in r.message for r in caplog.records), (
        "there was no recovery of space: only the cache_root measurement was lost"
    )
    assert b'"status":"idle"' in route.calls[-1].request.read().replace(b" ", b"")


# --- Temporal fail-open of the disk gate -------------------------------------
#
# The deadlines of these tests are parametrized to 1 ms (no real sleeping and no
# clock patching): the worker measures with `time.monotonic()` the same interval
# that the loop's `_cooperative_sleep_stop` honors, and with `next_poll=0` that interval
# is `poll_min_interval_seconds` = 0.1 s. The margin is structural (100x), not a
# millisecond budget that could be lost on a loaded machine.
_DEADLINE_EXPIRED = "deadline expired"


@respx.mock
async def test_poll_loop_disk_gate_opens_when_the_deadline_expires(
    settings: Settings, monkeypatch, caplog
) -> None:
    """The `Settings` validator does not cover every path to a permanent gate.

    `run_gc` only deletes dirs under `cache_root/tasks/`, so a worker-wide uv cache
    or large blobs on the same volume keep it below the floor
    forever: with the gate default-on that takes the worker out of service leaving
    as the only trace a WARNING that by the time someone investigates has rotated.
    Once the deadline expires the worker claims again and the tasks fail with the real
    cause, which is what happened before this feature.
    """
    import orcaster_worker.main as main_mod

    impatient = _gated(settings, cache_claim_gate_max_seconds=0.001)
    monkeypatch.setattr(main_mod, "detect_disk", lambda _root: _disk(0.03))
    route = respx.post(POLL_URL).mock(
        return_value=httpx.Response(200, json=_empty_resp(next_poll=0))
    )
    busy = BusyState()
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        with caplog.at_level(logging.INFO, logger="orcaster.worker"):
            loop_task = asyncio.create_task(poll_loop(api, impatient, busy, stop))
            for _ in range(100):
                if len(route.calls) >= 4:
                    break
                await asyncio.sleep(0.02)
            stop.set()
            await asyncio.wait_for(loop_task, timeout=2.0)

    assert len(route.calls) >= 4
    first = route.calls[0].request.read().replace(b" ", b"")
    last = route.calls[-1].request.read().replace(b" ", b"")
    assert b'"status":"idle"' in first
    assert b'"status":"idle"' in last
    # Before the deadline, gated: it reports the low fraction so the server
    # withholds it. Once the deadline expires (fail-open), it OMITS the fraction so the
    # server stops gating it and the WARNING's "claims again" is real; without
    # this the server would keep withholding it with the low fraction despite the fail-open.
    assert b'"free_disk_fraction":0.03' in first, "gated pre-fail-open: reports the fraction"
    assert b'"free_disk_fraction"' not in last, (
        "fail-open: omits the fraction (the server does not gate)"
    )
    warns = [
        r for r in caplog.records if r.levelname == "WARNING" and _DEADLINE_EXPIRED in r.message
    ]
    assert len(warns) == 1, (
        f"the fail-open warns only once and is latched: {len(warns)} warnings "
        f"in {len(route.calls)} polls"
    )
    msg = warns[0].getMessage()
    assert "0.030" in msg, "the WARNING must state the current free fraction"
    assert "will fail" in msg, "the WARNING must state that the tasks will fail"


@respx.mock
async def test_poll_loop_disk_gate_without_deadline_stays_closed(
    settings: Settings, monkeypatch, caplog
) -> None:
    """`0` disables the fail-open: `disk_gated` stays `True` indefinitely. With
    the server-side gate the worker reports `idle` anyway (the server is the one
    withholding tasks), but the fail-open never expires, so the expired-deadline
    WARNING is never emitted."""
    import orcaster_worker.main as main_mod

    no_deadline = _gated(settings, cache_claim_gate_max_seconds=0.0)
    monkeypatch.setattr(main_mod, "detect_disk", lambda _root: _disk(0.03))
    route = respx.post(POLL_URL).mock(
        return_value=httpx.Response(200, json=_empty_resp(next_poll=0))
    )
    busy = BusyState()
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        with caplog.at_level(logging.INFO, logger="orcaster.worker"):
            loop_task = asyncio.create_task(poll_loop(api, no_deadline, busy, stop))
            for _ in range(100):
                if len(route.calls) >= 4:
                    break
                await asyncio.sleep(0.02)
            stop.set()
            await asyncio.wait_for(loop_task, timeout=2.0)

    assert len(route.calls) >= 4
    for call in route.calls:
        assert b'"status":"idle"' in call.request.read().replace(b" ", b"")
    assert not any(_DEADLINE_EXPIRED in r.message for r in caplog.records)


@respx.mock
async def test_poll_loop_fail_open_rearms_after_disk_recovery(
    settings: Settings, monkeypatch, caplog
) -> None:
    """The deadline counts continuous time below the floor, not lifetime-cumulative.

    After a real recovery the counter and the latch go back to zero, so a
    second drop closes the gate again (and gives the GC its full deadline) instead of
    staying open forever because of an old episode.
    """
    import orcaster_worker.main as main_mod

    impatient = _gated(settings, cache_claim_gate_max_seconds=0.001)
    # Close, fail-open, recovery, close again, fail-open again.
    fractions = iter([0.03, 0.03, 0.50, 0.03, 0.03])

    def flaky_disk(_root):
        return _disk(next(fractions, 0.03))

    monkeypatch.setattr(main_mod, "detect_disk", flaky_disk)
    route = respx.post(POLL_URL).mock(
        return_value=httpx.Response(200, json=_empty_resp(next_poll=0))
    )
    busy = BusyState()
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        with caplog.at_level(logging.INFO, logger="orcaster.worker"):
            loop_task = asyncio.create_task(poll_loop(api, impatient, busy, stop))
            for _ in range(100):
                if len(route.calls) >= 5:
                    break
                await asyncio.sleep(0.02)
            stop.set()
            await asyncio.wait_for(loop_task, timeout=2.0)

    assert len(route.calls) >= 5
    assert any("disk gate released" in r.message for r in caplog.records)
    assert len([r for r in caplog.records if "disk gate closed" in r.message]) == 2, (
        "the second drop must close the gate again"
    )
    assert len([r for r in caplog.records if _DEADLINE_EXPIRED in r.message]) == 2, (
        "the fail-open latch resets on recovery"
    )


@respx.mock
async def test_poll_loop_fail_open_deadline_does_not_run_while_busy(
    settings: Settings, monkeypatch, caplog
) -> None:
    """The deadline counts time in which the gate stops claims, not busy time.

    Counting it over `disk_below_floor` alone made a task longer than the deadline
    (30 min by default, and long-running tasks exceed it) latch the fail-open without
    the gate having blocked a single claim, because the `busy` of those minutes was
    set by the task. On finishing, the worker claimed immediately with the volume full
    and died in `uv venv`: exactly the failure the gate came to prevent. The deadline is
    paused while the worker is busy (not reset: with a reset, a worker with interleaved
    pushes would never give up).
    """
    import orcaster_worker.main as main_mod

    impatient = _gated(settings, cache_claim_gate_max_seconds=0.001)
    monkeypatch.setattr(main_mod, "detect_disk", lambda _root: _disk(0.03))
    route = respx.post(POLL_URL).mock(
        return_value=httpx.Response(200, json=_empty_resp(next_poll=0))
    )
    # Busy from BEFORE the first poll: without this the initial idle interval already
    # exceeds the 1 ms deadline and the test would not tell the bug from the fix.
    release = asyncio.Event()
    busy = BusyState()
    busy.cache_flush_task = asyncio.create_task(release.wait(), name="busy")
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        with caplog.at_level(logging.INFO, logger="orcaster.worker"):
            loop_task = asyncio.create_task(poll_loop(api, impatient, busy, stop))
            for _ in range(100):
                if len(route.calls) >= 4:
                    break
                await asyncio.sleep(0.02)
            busy_polls = len(route.calls)
            stop.set()
            release.set()
            await asyncio.wait_for(loop_task, timeout=2.0)

    assert busy_polls >= 4, "several polls are needed with the worker busy"
    assert not any(_DEADLINE_EXPIRED in r.message for r in caplog.records), (
        "the deadline was consumed with busy time: the gate never blocked a claim"
    )


@respx.mock
async def test_poll_loop_measurement_loss_does_not_revoke_fail_open(
    settings: Settings, monkeypatch, caplog
) -> None:
    """The fail-open latch is revoked by a measured recovery, not by a flap.

    The recovery branch cleared the clock and the latch together, and losing the
    measurement (remount, mount flap, container restarted while the volume attaches)
    goes through that same branch. A worker that had already given up closed the gate
    again for another full deadline, with a WARNING per cycle instead of the single
    latched give-up the fail-open promises.
    """
    import orcaster_worker.main as main_mod
    from orcaster_worker.sysinfo import DiskSnapshot

    impatient = _gated(settings, cache_claim_gate_max_seconds=0.001)
    lost = DiskSnapshot(total_mb=0, used_mb=0, free_fraction=None)
    # Close, fail-open, measurement lost, and back below the floor.
    snapshots = iter([_disk(0.03), _disk(0.03), lost, _disk(0.03), _disk(0.03)])

    def flaky_disk(_root):
        return next(snapshots, _disk(0.03))

    monkeypatch.setattr(main_mod, "detect_disk", flaky_disk)
    route = respx.post(POLL_URL).mock(
        return_value=httpx.Response(200, json=_empty_resp(next_poll=0))
    )
    busy = BusyState()
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        with caplog.at_level(logging.INFO, logger="orcaster.worker"):
            loop_task = asyncio.create_task(poll_loop(api, impatient, busy, stop))
            for _ in range(100):
                if len(route.calls) >= 5:
                    break
                await asyncio.sleep(0.02)
            stop.set()
            await asyncio.wait_for(loop_task, timeout=2.0)

    assert len(route.calls) >= 5
    assert len([r for r in caplog.records if _DEADLINE_EXPIRED in r.message]) == 1, (
        "the give-up is announced only once: the measurement flap does not re-arm the gate"
    )
    assert len([r for r in caplog.records if "disk gate closed" in r.message]) == 1, (
        "with the fail-open latched the gate did not close again: there is nothing to announce"
    )
    assert b'"status":"idle"' in route.calls[-1].request.read().replace(b" ", b"")


@respx.mock
async def test_poll_loop_gated_keeps_the_idle_cadence(settings: Settings, monkeypatch) -> None:
    """A gated worker does not accept the 5 s cadence the server gives to busy workers.

    That cadence exists for a live execution (renew lease, report progress). A gated
    worker has none, and the gate fires on pressure of a shared volume, so all the
    host's containers cross the floor together: 6x the poll and DB work
    for the whole fail-open window, precisely while nothing advances.
    """
    import orcaster_worker.main as main_mod

    # poll_max=1.0 is the floor the worker must impose on itself while gated; the
    # server sends next_poll=0, which without the fix clamps to poll_min=0.1.
    lento = settings.model_copy(update={"poll_max_interval_seconds": 1.0})
    monkeypatch.setattr(main_mod, "detect_disk", lambda _root: _disk(0.03))
    route = respx.post(POLL_URL).mock(
        return_value=httpx.Response(200, json=_empty_resp(next_poll=0))
    )
    busy = BusyState()
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        loop_task = asyncio.create_task(poll_loop(api, lento, busy, stop))
        for _ in range(50):
            if route.called:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.5)
        polls = len(route.calls)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=2.0)

    assert b'"status":"idle"' in route.calls[0].request.read().replace(b" ", b"")
    assert polls <= 2, f"gate cadence: {polls} polls in 0.5 s with a floor of 1 s"


@respx.mock
async def test_poll_loop_closing_the_gate_wakes_the_gc(settings: Settings, monkeypatch) -> None:
    """Closing the gate triggers a GC pass instead of waiting for its interval.

    The gate detects exactly the condition that the pressure pass of `run_gc`
    exists to fix, but the GC kept its own cadence (300 s by default) and the server's
    `clear_cache` lane stays closed because of the same `busy`. The worker stayed idle
    for up to five minutes before the only mechanism that could free space ran.
    """
    import orcaster_worker.main as main_mod

    monkeypatch.setattr(main_mod, "detect_disk", lambda _root: _disk(0.03))
    route = respx.post(POLL_URL).mock(
        return_value=httpx.Response(200, json=_empty_resp(next_poll=1))
    )
    gc_wakeup = asyncio.Event()
    busy = BusyState()
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        stop = asyncio.Event()
        loop_task = asyncio.create_task(poll_loop(api, settings, busy, stop, gc_wakeup=gc_wakeup))
        for _ in range(50):
            if route.called:
                break
            await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=2.0)

    assert gc_wakeup.is_set(), "closing the gate must ask the GC for an immediate pass"


async def test_disk_preflight_scanner_without_markers_never_detects() -> None:
    """Without markers (core default) there is no detection, but there is forwarding."""
    from orcaster_worker.main import _DiskPreflightScanner

    seen: list[str | None] = []

    async def sink(stdout=None, stderr=None, command=None):
        seen.append(stderr)

    scanner = _DiskPreflightScanner(sink)
    await scanner.feed(stderr=f"tool: {_DISK_MARKER}\n")
    assert scanner.marker_seen is False
    assert seen == [f"tool: {_DISK_MARKER}\n"]


async def test_disk_preflight_scanner_matches_any_marker_across_boundary() -> None:
    """With several markers one is enough; the tail covers a long split marker."""
    from orcaster_worker.main import _DiskPreflightScanner

    async def sink(stdout=None, stderr=None, command=None):
        return None

    long_marker = "x" * 400 + " no space left"
    scanner = _DiskPreflightScanner(sink, ("other marker", long_marker))
    mid = len(long_marker) - 3
    await scanner.feed(stderr="tool: " + long_marker[:mid])
    assert scanner.marker_seen is False
    await scanner.feed(stderr=long_marker[mid:])
    assert scanner.marker_seen is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ()),
        ("  ; ;", ()),
        ("a b: c", ("a b: c",)),
        (" one, two ;three; one ", ("one, two", "three", "one")),
        ("a;a;b", ("a", "b")),
    ],
)
def test_parse_flush_markers(raw: str, expected: tuple[str, ...]) -> None:
    from orcaster_worker.main import parse_flush_markers

    assert parse_flush_markers(raw) == expected


def test_flush_markers_default_empty_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from orcaster_worker.config import Settings

    monkeypatch.delenv("ORCH_FLUSH_MARKERS", raising=False)
    assert Settings().flush_markers == ""
    monkeypatch.setenv("ORCH_FLUSH_MARKERS", f"{_DISK_MARKER};other")
    assert Settings().flush_markers == f"{_DISK_MARKER};other"


def test_startup_logs_flush_markers(settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
    from orcaster_worker.main import _log_flush_markers

    with caplog.at_level("INFO"):
        _log_flush_markers(settings.model_copy(update={"flush_markers": f"{_DISK_MARKER};b"}))
        _log_flush_markers(settings.model_copy(update={"flush_markers": ""}))
    text = caplog.text
    assert f"'{_DISK_MARKER}', 'b'" in text
    assert "startup: flush markers used: (none)" in text
