"""Tests specific to the universal task signature.

Covers the guarantees introduced by the universal signature:
- Lifespan fail-fast if `ED25519_PRIVATE_KEY_PATH` is not configured.
- The `/admin/tasks` endpoint returns 503 if the key does not load at runtime.
- `admin_create_task` signs EVERY task (claim count, claim all-available,
  push). The final signature delivered to the client via `/poll` verifies with
  the server pub-key.
- `/poll` re-signs CLAIM tasks with the UUID of the polling worker.
- Compat: the wire `signed` field is deprecated; any value is accepted
  without changing the behavior (it always signs).
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server import signing
from orcaster_server.config import settings
from orcaster_server.signing import verify_task_signature
from tests.conftest import mint_token_via_app

REGISTER = {
    "hostname": "w",
    "os": "linux",
    "arch": "x86_64",
    "ram_mb": 1024,
    "disk_mb": 10000,
    "tags": [],
    "capabilities": {},
}


async def _register(client: AsyncClient, hostname: str = "w") -> tuple[str, str]:
    token = await mint_token_via_app()
    resp = await client.post(
        "/v1/workers/register",
        json={**REGISTER, "hostname": hostname, "enrollment_token": token},
    )
    assert resp.status_code == 200, resp.text
    j = resp.json()
    return j["worker_id"], j["api_key"]


# --- fail-fast lifespan -------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_fails_when_ed25519_key_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `ED25519_PRIVATE_KEY_PATH` is not configured, the lifespan aborts.

    We reproduce the lifespan manually (without starting uvicorn) and
    verify that `RuntimeError` (or a subclass) propagates to the caller.
    """
    from orcaster_server import db as app_db
    from orcaster_server.main import lifespan

    monkeypatch.setattr(settings, "ed25519_private_key_path", None)
    signing.reset_key_cache()
    monkeypatch.setattr(settings, "migrate_on_startup", False)
    monkeypatch.setattr(settings, "housekeeping_enabled", False)
    monkeypatch.setattr(app_db, "init_engine", lambda: None)

    from fastapi import FastAPI

    app = FastAPI()
    with pytest.raises(RuntimeError, match="ED25519_PRIVATE_KEY_PATH"):
        async with lifespan(app):
            pass  # pragma: no cover  # __aenter__ must have failed.


@pytest.mark.asyncio
async def test_lifespan_succeeds_with_valid_key(
    _session_ed25519_key: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a loadable Ed25519 key, the lifespan starts normally."""
    from orcaster_server import db as app_db
    from orcaster_server.main import lifespan

    monkeypatch.setattr(settings, "ed25519_private_key_path", str(_session_ed25519_key))
    signing.reset_key_cache()
    monkeypatch.setattr(settings, "migrate_on_startup", False)
    monkeypatch.setattr(settings, "housekeeping_enabled", False)
    monkeypatch.setattr(app_db, "init_engine", lambda: None)

    async def _noop_dispose() -> None:
        return None

    monkeypatch.setattr(app_db, "dispose_engine", _noop_dispose)

    from fastapi import FastAPI

    app = FastAPI()
    async with lifespan(app):
        # __aenter__ must not have raised anything.
        pass


@pytest.mark.asyncio
async def test_lifespan_fails_when_key_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the path exists but is not a valid PEM, the lifespan aborts."""
    from orcaster_server import db as app_db
    from orcaster_server.main import lifespan

    bogus = tmp_path / "not-a-pem.bin"
    bogus.write_bytes(b"\x00\x01\x02not a pem")
    monkeypatch.setattr(settings, "ed25519_private_key_path", str(bogus))
    signing.reset_key_cache()
    monkeypatch.setattr(settings, "migrate_on_startup", False)
    monkeypatch.setattr(settings, "housekeeping_enabled", False)
    monkeypatch.setattr(app_db, "init_engine", lambda: None)

    from fastapi import FastAPI

    app = FastAPI()
    # `load_pem_private_key` may raise ValueError or its subclasses depending on
    # the exact PEM failure (structure, encoding). We accept any of them.
    with pytest.raises((ValueError, RuntimeError)):
        async with lifespan(app):
            pass  # pragma: no cover


# --- race: create endpoint without a loaded key -> 503 ----------------------


@pytest.mark.asyncio
async def test_admin_create_task_503_when_key_unset_at_request_time(
    authed_admin_client: AsyncClient,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Theoretical race: the lifespan started with a key, but between boot and
    the request someone drops it (reset_key_cache + clear setting). The endpoint
    must answer 503 with Retry-After, not 500.
    """
    # Simulate the race: the key was loaded (authed_admin_client sets it), but
    # now we unconfigure it at runtime.
    monkeypatch.setattr(settings, "ed25519_private_key_path", None)
    signing.reset_key_cache()

    resp = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert resp.status_code == 503, resp.text
    assert "Retry-After" in resp.headers
    body = resp.json()
    assert "signing" in body["detail"].lower()


# --- signature at INSERT verifiable end-to-end ------------------------------


@pytest.mark.asyncio
async def test_admin_create_task_count_target_1_is_signed_at_insert(
    authed_admin_client: AsyncClient,
    migrated_db_url: str,
) -> None:
    """Every count task created via `/admin/tasks` has `signed=true` and
    a non-null `signature` in the row (universal signature).
    """
    resp = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT signed, signature IS NOT NULL AS has_sig FROM tasks WHERE id = :tid"
                    ),
                    {"tid": task_id},
                )
            ).first()
    finally:
        await engine.dispose()

    assert row is not None
    assert row[0] is True
    assert row[1] is True


@pytest.mark.asyncio
async def test_admin_create_task_all_available_is_signed_at_insert(
    authed_admin_client: AsyncClient,
    migrated_db_url: str,
) -> None:
    """`all_available` tasks are also born signed (placeholder with
    worker_id=""; /poll will re-sign with the real UUID).
    """
    resp = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT signed, signature IS NOT NULL AS has_sig FROM tasks WHERE id = :tid"
                    ),
                    {"tid": task_id},
                )
            ).first()
    finally:
        await engine.dispose()

    assert row is not None
    assert row[0] is True
    assert row[1] is True


@pytest.mark.asyncio
async def test_poll_claim_returns_signature_verifiable_against_polling_worker(
    authed_admin_client: AsyncClient,
) -> None:
    """Universal signature E2E: admin creates claim task -> worker polls -> delivered signature
    verifies with the server pub-key over the UUID of the polling worker
    (re-signed in /poll, not the placeholder signature from the INSERT).
    """
    worker_id, key = await _register(authed_admin_client)
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    poll = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert poll.status_code == 200, poll.text
    task = poll.json()["task"]
    assert task is not None
    assert task["signed"] is True
    assert task["signature"], "claim task must carry signature post-universal-signature"

    # Rebuild and verify with the server pub key.
    pem = Path(settings.ed25519_private_key_path).read_bytes()
    priv = serialization.load_pem_private_key(pem, password=None)
    assert isinstance(priv, Ed25519PrivateKey)
    pub = priv.public_key()
    sig = base64.b64decode(task["signature"])

    assert verify_task_signature(
        pub,
        sig,
        task_id=task_id,
        worker_id=worker_id,  # polling worker, not placeholder ""
        command="run_batch",
        payload={},
        idempotency_key=None,
        created_at_iso=task["created_at"],
    )


@pytest.mark.asyncio
async def test_poll_all_available_returns_signature_verifiable(
    authed_admin_client: AsyncClient,
) -> None:
    """E2E: all-available snapshot + poll by the pre-assigned worker -> the delivered
    signature verifies with the UUID of the polling worker.
    """
    worker_id, key = await _register(authed_admin_client)
    create = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    poll = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert poll.status_code == 200
    task = poll.json()["task"]
    assert task is not None and task["task_id"] == task_id
    assert task["signed"] is True
    assert task["signature"]

    pem = Path(settings.ed25519_private_key_path).read_bytes()
    priv = serialization.load_pem_private_key(pem, password=None)
    assert isinstance(priv, Ed25519PrivateKey)
    pub = priv.public_key()
    sig = base64.b64decode(task["signature"])

    assert verify_task_signature(
        pub,
        sig,
        task_id=task_id,
        worker_id=worker_id,
        command="run_batch",
        payload={},
        idempotency_key=None,
        created_at_iso=task["created_at"],
    )


@pytest.mark.asyncio
async def test_poll_re_signs_when_other_worker_re_claims_after_lease_expiry(
    authed_admin_client: AsyncClient,
    migrated_db_url: str,
) -> None:
    """If worker A claimed (re-signs with A), the lease expires, worker B reclaims:
    the server re-signs with the UUID of B. The final signature delivered to B
    verifies with worker_id=B, not with A.
    """
    w_a, k_a = await _register(authed_admin_client, "a")
    w_b, k_b = await _register(authed_admin_client, "b")

    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    # A claims and is signed for A.
    poll_a = await authed_admin_client.post(
        f"/v1/workers/{w_a}/poll",
        headers={"X-API-Key": k_a},
        json={"status": "idle", "lease_seconds": 30},
    )
    task_a = poll_a.json()["task"]
    assert task_a is not None and task_a["task_id"] == task_id

    # Expire the lease of A so that B can claim again.
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE task_executions SET lease_expires_at = now() "
                    "- interval '10 seconds', status='claimed' "
                    "WHERE task_id = :tid AND worker_id = :wid"
                ),
                {"tid": task_id, "wid": w_a},
            )
            # A takes 1 slot. With the reclaim in the background job the poll does NOT expire
            # A's execution (the reclaim lives in the background job). So that B can claim
            # without depending on that loop, we raise attempt_budget to 2 (the
            # claim counts against the budget, not against target_count): the
            # `taken` counts A's execution (lapsed lease, not yet expired)
            # as 1, and a 2nd slot is left free for B.
            await conn.execute(
                text("UPDATE tasks SET attempt_budget = 2 WHERE id = :tid"),
                {"tid": task_id},
            )
    finally:
        await engine.dispose()

    # B claims. The poll does not expire A's execution; B ends up with its own
    # execution + signature recomputed with w_b.
    poll_b = await authed_admin_client.post(
        f"/v1/workers/{w_b}/poll",
        headers={"X-API-Key": k_b},
        json={"status": "idle", "lease_seconds": 30},
    )
    task_b = poll_b.json()["task"]
    assert task_b is not None, poll_b.json()
    assert task_b["task_id"] == task_id

    # Verify that the signature of B is really computed with worker_id=B.
    pem = Path(settings.ed25519_private_key_path).read_bytes()
    priv = serialization.load_pem_private_key(pem, password=None)
    assert isinstance(priv, Ed25519PrivateKey)
    pub = priv.public_key()
    sig_b = base64.b64decode(task_b["signature"])

    assert verify_task_signature(
        pub,
        sig_b,
        task_id=task_id,
        worker_id=w_b,  # critical: the signature must be for w_b, not w_a
        command="run_batch",
        payload={},
        idempotency_key=None,
        created_at_iso=task_b["created_at"],
    )

    # And it must NOT verify for w_a (re-signing effectively overwrote it).
    assert not verify_task_signature(
        pub,
        sig_b,
        task_id=task_id,
        worker_id=w_a,
        command="run_batch",
        payload={},
        idempotency_key=None,
        created_at_iso=task_b["created_at"],
    )


# --- wire signed=field-deprecated -------------------------------------------


@pytest.mark.asyncio
async def test_admin_create_task_signed_field_is_ignored(
    authed_admin_client: AsyncClient,
    migrated_db_url: str,
) -> None:
    """`signed=False` on the wire does NOT prevent signing (deprecated field)."""
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "signed": False},
    )
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT signed, signature IS NOT NULL FROM tasks WHERE id=:tid"),
                    {"tid": task_id},
                )
            ).first()
    finally:
        await engine.dispose()
    assert row is not None
    assert row[0] is True  # the wire False is ignored: the server always signs.
    assert row[1] is True
