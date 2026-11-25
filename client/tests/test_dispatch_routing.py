"""Tests for _dispatch_poll_task routing with command=None and command='run_batch'.

Work tasks arrive with command='run_batch' (or None for compat).
Both must route to _run_external, not _run_reserved.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orcaster_worker.api import OrchestratorAPI
from orcaster_worker.config import Settings
from orcaster_worker.main import BusyState, _dispatch_poll_task
from orcaster_worker.poll import PollTaskPayload
from orcaster_worker.signing import build_task_message, reset_key_cache

WID = "11111111-1111-1111-1111-111111111111"
BASE = "http://server.test"


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


def _sign(
    priv: Ed25519PrivateKey,
    task_id: int,
    command: str,
    payload: dict,
    created_at: str = "2026-05-22T00:00:00Z",
) -> str:
    msg = build_task_message(
        task_id=task_id,
        worker_id=WID,
        command=command,
        payload=payload,
        idempotency_key=None,
        created_at_iso=created_at,
    )
    return base64.b64encode(priv.sign(msg)).decode("ascii")


@pytest.mark.asyncio
async def test_work_task_with_run_batch_routes_to_external(
    settings: Settings,
    tmp_path: Path,
) -> None:
    """command='run_batch' → _run_external (not _run_reserved)."""
    priv, pub_path = _gen_keypair(tmp_path)
    reset_key_cache()
    settings.ed25519_public_key_path = pub_path

    sig = _sign(priv, 1, "run_batch", {"requirements": [], "commands": ["echo ok"]})
    task = PollTaskPayload(
        task_id=1,
        command="run_batch",
        payload={"requirements": [], "commands": ["echo ok"]},
        created_at="2026-05-22T00:00:00Z",
        delivery_mode="claim",
        signed=True,
        signature=sig,
        idempotency_key=None,
        worker_id=WID,
        target_worker_id=None,
        lease_token="lease-1",
        lease_expires_at="2030-01-01T00:00:00Z",
    )

    with (
        patch("orcaster_worker.main._run_external", new_callable=AsyncMock) as mock_ext,
        patch("orcaster_worker.main._run_reserved", new_callable=AsyncMock) as mock_res,
    ):
        async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
            busy = BusyState()
            stop = asyncio.Event()
            await _dispatch_poll_task(api, settings, busy, stop, task)

    mock_ext.assert_awaited_once()
    mock_res.assert_not_awaited()


@pytest.mark.asyncio
async def test_control_op_routes_to_reserved(
    settings: Settings,
    tmp_path: Path,
) -> None:
    """command='__reload_config__' (control op) → _run_reserved."""
    priv, pub_path = _gen_keypair(tmp_path)
    reset_key_cache()
    settings.ed25519_public_key_path = pub_path

    sig = _sign(priv, 2, "__reload_config__", {})
    task = PollTaskPayload(
        task_id=2,
        command="__reload_config__",
        payload={},
        created_at="2026-05-22T00:00:00Z",
        delivery_mode="push",
        signed=True,
        signature=sig,
        idempotency_key=None,
        worker_id=WID,
        target_worker_id=WID,
        lease_token="lease-2",
        lease_expires_at="2030-01-01T00:00:00Z",
    )

    with (
        patch("orcaster_worker.main._run_reserved", new_callable=AsyncMock) as mock_res,
        patch("orcaster_worker.main._run_external", new_callable=AsyncMock) as mock_ext,
    ):
        async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
            busy = BusyState()
            stop = asyncio.Event()
            await _dispatch_poll_task(api, settings, busy, stop, task)

    mock_res.assert_awaited_once()
    mock_ext.assert_not_awaited()


@pytest.mark.asyncio
async def test_work_task_with_none_command_routes_to_external(
    settings: Settings,
    tmp_path: Path,
) -> None:
    """command=None (legacy compat path) → treated as 'run_batch', routes to _run_external."""
    priv, pub_path = _gen_keypair(tmp_path)
    reset_key_cache()
    settings.ed25519_public_key_path = pub_path

    sig = _sign(priv, 3, "run_batch", {"requirements": [], "commands": ["ls"]})
    task = PollTaskPayload(
        task_id=3,
        command=None,
        payload={"requirements": [], "commands": ["ls"]},
        created_at="2026-05-22T00:00:00Z",
        delivery_mode="claim",
        signed=True,
        signature=sig,
        idempotency_key=None,
        worker_id=WID,
        target_worker_id=None,
        lease_token="lease-3",
        lease_expires_at="2030-01-01T00:00:00Z",
    )

    with (
        patch("orcaster_worker.main._run_external", new_callable=AsyncMock) as mock_ext,
        patch("orcaster_worker.main._run_reserved", new_callable=AsyncMock) as mock_res,
    ):
        async with OrchestratorAPI(BASE, worker_id=WID, api_key="k") as api:
            busy = BusyState()
            stop = asyncio.Event()
            await _dispatch_poll_task(api, settings, busy, stop, task)

    mock_ext.assert_awaited_once()
    mock_res.assert_not_awaited()
