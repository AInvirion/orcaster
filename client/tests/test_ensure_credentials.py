"""Tests for `ensure_credentials` -- self-registration with an enrollment token.

They cover the worker's first-boot paths:
  - No persisted credentials and no enrollment token -> actionable fail-fast
    (the network is NOT touched; the server would answer an opaque 422).
  - No credentials but with a token -> register sends the token in the body and the
    credentials are persisted in state.json.
  - With persisted credentials -> the identity is reused without re-registering (the
    token is irrelevant on later boots).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from orcaster_worker.api import OrchestratorAPI
from orcaster_worker.config import Settings
from orcaster_worker.main import (
    EnrollmentTokenMissingError,
    EnrollmentTokenRejectedError,
    ensure_credentials,
)
from orcaster_worker.state import Credentials
from orcaster_worker.state import load as load_creds
from orcaster_worker.state import save as save_creds

BASE = "http://server.test"


def _settings(tmp_path: Path, *, enrollment_token: str) -> Settings:
    # ed25519_public_key_path=None: ensure_credentials does not touch the pubkey
    # (the pubkey fail-fast lives in run(), not here).
    return Settings(
        state_dir=tmp_path,
        enrollment_token=enrollment_token,
        ed25519_public_key_path=None,
    )


@respx.mock
@pytest.mark.asyncio
async def test_ensure_credentials_failfast_without_token(tmp_path: Path):
    # We mount the register route so we CAN assert it is NOT called: the
    # fail-fast on a missing token must abort before touching the network.
    register_route = respx.post(f"{BASE}/v1/workers/register").mock(
        return_value=httpx.Response(200, json={"worker_id": "x", "api_key": "y"})
    )
    settings = _settings(tmp_path, enrollment_token="")
    api = OrchestratorAPI(BASE, worker_id=None, api_key=None)
    try:
        with pytest.raises(EnrollmentTokenMissingError):
            await ensure_credentials(api, settings)
    finally:
        await api.aclose()
    # No state.json was written.
    assert load_creds(tmp_path) is None
    # The network was not touched: register was never invoked.
    assert register_route.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_ensure_credentials_rejected_token_raises_actionable(tmp_path: Path):
    """Token present but rejected by the server (401/422) -> actionable error.

    Instead of propagating the raw `httpx.HTTPStatusError` from `raise_for_status()`,
    `ensure_credentials` re-raises `EnrollmentTokenRejectedError` with context.
    """
    for code in (401, 422):
        route = respx.post(f"{BASE}/v1/workers/register").mock(
            return_value=httpx.Response(code, json={"detail": "bad token"})
        )
        settings = _settings(tmp_path, enrollment_token="stale-or-used-token")
        api = OrchestratorAPI(BASE, worker_id=None, api_key=None)
        try:
            with pytest.raises(EnrollmentTokenRejectedError):
                await ensure_credentials(api, settings)
        finally:
            await api.aclose()
        assert route.called
        # No identity was persisted after a rejected registration.
        assert load_creds(tmp_path) is None
        respx.reset()


@respx.mock
@pytest.mark.asyncio
async def test_ensure_credentials_registers_with_token(tmp_path: Path):
    route = respx.post(f"{BASE}/v1/workers/register").mock(
        return_value=httpx.Response(
            200,
            json={
                "worker_id": "11111111-1111-1111-1111-111111111111",
                "api_key": "secret-xyz",
            },
        )
    )
    settings = _settings(tmp_path, enrollment_token="enroll-tok-123")
    api = OrchestratorAPI(BASE, worker_id=None, api_key=None)
    try:
        creds = await ensure_credentials(api, settings)
    finally:
        await api.aclose()

    assert route.called
    body = route.calls.last.request.read().decode()
    assert "enroll-tok-123" in body
    assert creds.worker_id == "11111111-1111-1111-1111-111111111111"
    # The credentials were persisted for the next boot.
    persisted = load_creds(tmp_path)
    assert persisted is not None
    assert persisted.api_key == "secret-xyz"


@respx.mock
@pytest.mark.asyncio
async def test_ensure_credentials_reuses_persisted_state(tmp_path: Path):
    # Previously persisted state: it must not re-register even if the token is missing.
    save_creds(tmp_path, Credentials(worker_id="ws-1", api_key="kkk"))
    route = respx.post(f"{BASE}/v1/workers/register").mock(return_value=httpx.Response(500))
    settings = _settings(tmp_path, enrollment_token="")
    api = OrchestratorAPI(BASE, worker_id=None, api_key=None)
    try:
        creds = await ensure_credentials(api, settings)
    finally:
        await api.aclose()

    assert creds.worker_id == "ws-1"
    assert not route.called


@respx.mock
@pytest.mark.asyncio
async def test_ensure_credentials_registers_with_the_config_capabilities(tmp_path: Path):
    """A new worker enrolls already with `ORCH_CAPABILITIES`."""
    route = respx.post(f"{BASE}/v1/workers/register").mock(
        return_value=httpx.Response(
            200,
            json={"worker_id": "11111111-1111-1111-1111-111111111111", "api_key": "k"},
        )
    )
    settings = Settings(
        state_dir=tmp_path,
        enrollment_token="enroll-tok-123",
        ed25519_public_key_path=None,
        capabilities_json='{"model": true, "model_context": 8192}',
    )
    api = OrchestratorAPI(BASE, worker_id=None, api_key=None)
    try:
        await ensure_credentials(api, settings)
    finally:
        await api.aclose()
    body = json.loads(route.calls.last.request.read())
    assert body["capabilities"] == {"model": True, "model_context": 8192}
