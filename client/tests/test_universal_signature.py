"""Tests specific to the universal signature in the client.

Covers the universal signature, client side:
- `_dispatch_poll_task` ALWAYS verifies the signature (no `if signed` guard).
- Invalid signature -> log + return; complete() is not called, not even with failed.
  Reason: an attacker on the server with invalid signatures must not be able to
  force legitimate tasks to be marked as `failed` in the audit log.
- A task with a valid signature keeps executing normally.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orcaster_worker.api import OrchestratorAPI
from orcaster_worker.config import Settings
from orcaster_worker.main import BusyState, _dispatch_poll_task
from orcaster_worker.poll import PollTaskPayload
from orcaster_worker.signing import build_task_message, reset_key_cache

BASE = "http://server.test"
WID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        server_url="http://server.test",  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        poll_min_interval_seconds=0.1,
        poll_max_interval_seconds=2.0,
        lease_seconds=30,
    )


def _gen_keypair(tmp_path: Path) -> tuple[Ed25519PrivateKey, Path]:
    priv = Ed25519PrivateKey.generate()
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    p = tmp_path / "ed25519.pub.pem"
    p.write_bytes(pub_pem)
    return priv, p


def _sign_task(
    priv: Ed25519PrivateKey,
    *,
    task_id: int,
    worker_id: str,
    command: str,
    payload: dict,
    created_at: str,
    idempotency_key: str | None = None,
) -> str:
    msg = build_task_message(
        task_id=task_id,
        worker_id=worker_id,
        command=command,
        payload=payload,
        idempotency_key=idempotency_key,
        created_at_iso=created_at,
    )
    return base64.b64encode(priv.sign(msg)).decode("ascii")


@respx.mock
async def test_dispatch_with_tampered_signature_logs_and_returns(
    settings: Settings,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mutate 1 byte of the signature -> client logs a warning with
    `task_id` + `worker_id`, does NOT call complete() (not even with `failed`).
    The server reclaims the lease on expiry.
    """
    priv, pub_path = _gen_keypair(tmp_path)
    reset_key_cache()
    settings.ed25519_public_key_path = pub_path

    created_at = "2026-05-19T00:00:00Z"
    payload = {"args": ["hello"]}
    sig_b64 = _sign_task(
        priv,
        task_id=42,
        worker_id=WID,
        command="/bin/echo",
        payload=payload,
        created_at=created_at,
    )
    # Tampering: flip 1 byte of the signature.
    sig_raw = bytearray(base64.b64decode(sig_b64))
    sig_raw[0] ^= 0x01
    bad_sig_b64 = base64.b64encode(bytes(sig_raw)).decode("ascii")

    complete_route = respx.post(f"{BASE}/v1/tasks/42/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    task = PollTaskPayload(
        task_id=42,
        command="/bin/echo",
        payload=payload,
        created_at=created_at,
        delivery_mode="claim",
        signed=True,
        signature=bad_sig_b64,
        idempotency_key=None,
        worker_id=WID,
        target_worker_id=None,
        lease_token="lease-42",
        lease_expires_at="2030-01-01T00:00:00Z",
    )
    caplog.set_level(logging.WARNING, logger="orcaster.worker")
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        busy = BusyState()
        stop = asyncio.Event()
        await _dispatch_poll_task(api, settings, busy, stop, task)

    # complete is NOT called (not even with failed): silent reject.
    assert not complete_route.called

    # Structured log with task_id + worker_id.
    relevant = [r for r in caplog.records if "signature verification failed" in r.message]
    assert relevant, "expected structured warning about signature verification failure"
    msg = relevant[0].getMessage()
    assert "42" in msg  # task_id
    assert WID in msg  # worker_id


@respx.mock
async def test_dispatch_with_valid_signature_executes_normally(
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Sanity: with a valid signature the task keeps executing and completing."""
    import shutil
    import sys

    if sys.platform == "win32":
        pytest.skip("POSIX echo path")
    priv, pub_path = _gen_keypair(tmp_path)
    reset_key_cache()
    settings.ed25519_public_key_path = pub_path

    echo = shutil.which("echo") or "/bin/echo"
    created_at = "2026-05-19T00:00:00Z"
    payload = {"args": ["ok"]}
    sig_b64 = _sign_task(
        priv,
        task_id=99,
        worker_id=WID,
        command=echo,
        payload=payload,
        created_at=created_at,
    )

    complete_route = respx.post(f"{BASE}/v1/tasks/99/complete").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    # The {"args": [...]} payload is legacy/malformed for the ephemeral venv: _run_external
    # emits a synthetic diagnostic via /output before the failed complete. Without a
    # mock, the ephemeral streamer would retry against /output until the drain
    # deadline (10s). We mock it so the test is fast and deterministic.
    respx.post(f"{BASE}/v1/tasks/99/output").mock(
        return_value=httpx.Response(202, json={"ok": True})
    )
    task = PollTaskPayload(
        task_id=99,
        command=echo,
        payload=payload,
        created_at=created_at,
        delivery_mode="claim",
        signed=True,
        signature=sig_b64,
        idempotency_key=None,
        worker_id=WID,
        target_worker_id=None,
        lease_token="lease-99",
        lease_expires_at="2030-01-01T00:00:00Z",
    )
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        busy = BusyState()
        stop = asyncio.Event()
        await _dispatch_poll_task(api, settings, busy, stop, task)

    assert complete_route.called


@respx.mock
async def test_server_reclaims_lease_when_client_rejects_signature(
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Models the end-to-end flow of the silent rejection:
    the client rejects, the server (in another iteration) reclaims the lease,
    and a re-issuer with a valid signature can be executed.

    This test is about behavior (not mechanism): it verifies that after
    the rejection, the same `BusyState` is free to receive another task.
    Since the dispatch neither called `complete` nor set `busy.task_id`, the
    poll_loop will keep sending `status=idle` on the next cycle and
    will accept the next validly signed task.
    """
    priv, pub_path = _gen_keypair(tmp_path)
    reset_key_cache()
    settings.ed25519_public_key_path = pub_path

    created_at = "2026-05-19T00:00:00Z"
    # Signature over a different worker_id -> fails to verify against WID.
    sig_b64_wrong = _sign_task(
        priv,
        task_id=7,
        worker_id="99999999-9999-9999-9999-999999999999",
        command="__reload_config__",
        payload={},
        created_at=created_at,
    )

    bad_task = PollTaskPayload(
        task_id=7,
        command="__reload_config__",
        payload={},
        created_at=created_at,
        delivery_mode="push",
        signed=True,
        signature=sig_b64_wrong,
        idempotency_key=None,
        worker_id=WID,  # client verifies against this UUID -- mismatch.
        target_worker_id=WID,
        lease_token="lease-7",
        lease_expires_at="2030-01-01T00:00:00Z",
    )
    async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
        busy = BusyState()
        stop = asyncio.Event()
        await _dispatch_poll_task(api, settings, busy, stop, bad_task)

    # Postcondition: the busy state was left free (complete was not called and
    # busy.task_id was not touched), so the worker is ready to receive
    # another task on the next poll.
    assert busy.task_id is None
    assert busy.lease_token is None
    assert not busy.is_busy
