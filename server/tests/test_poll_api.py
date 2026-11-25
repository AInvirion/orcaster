"""Integration tests for the unified endpoint POST /v1/workers/{id}/poll.

Covers the cases introduced by the unified endpoint:
- directed push delivery (delivery_mode='push' + target_worker_id);
- exactly-once cancel signals (cancel_notified_at);
- 401 when the api_key does not match the worker_id;
- server-driven next_poll_after_seconds;
- a paused worker receives neither tasks nor pushes.

The regular claim flows (count and all_available) are covered in
test_tasks_api.py.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server import signing
from orcaster_server.config import settings
from orcaster_server.signing import canonical_json, verify_task_signature
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
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


# --- Fixtures --------------------------------------------------------------


@pytest.fixture(scope="session")
def ed25519_keypair(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Ed25519PrivateKey]:
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pem_path = tmp_path_factory.mktemp("ed25519_poll") / "server.pem"
    pem_path.write_bytes(pem)
    return pem_path, priv


@pytest.fixture
def _configure_signing(
    ed25519_keypair: tuple[Path, Ed25519PrivateKey],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pem_path, _ = ed25519_keypair
    monkeypatch.setattr(settings, "ed25519_private_key_path", str(pem_path))
    signing.reset_key_cache()


async def _register(client: AsyncClient, hostname: str = "w") -> tuple[str, str]:
    token = await mint_token_via_app()
    resp = await client.post(
        "/v1/workers/register",
        json={**REGISTER, "hostname": hostname, "enrollment_token": token},
    )
    assert resp.status_code == 200, resp.text
    j = resp.json()
    return j["worker_id"], j["api_key"]


async def _insert_task(db_url: str, command: str = "echo") -> int:
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        INSERT INTO tasks (workspace_id, command, payload,
                                           target_mode, target_count,
                                           priority, status, run_at,
                                           signed, signature)
                        VALUES (:workspace_id, :cmd, '{}'::jsonb, 'count', 1, 0,
                                'queued', now(),
                                true, decode(repeat('00', 64), 'hex'))
                        RETURNING id
                        """
                    ),
                    {"cmd": command, "workspace_id": DEFAULT_WORKSPACE_ID},
                )
            ).first()
        return int(row[0])
    finally:
        await engine.dispose()


# --- Basic tests -----------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_returns_empty_when_no_task(authed_admin_client: AsyncClient) -> None:
    wid, key = await _register(authed_admin_client)
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["task"] is None
    assert body["cancel_signals"] == []
    # Idle with no task → 30s.
    assert body["next_poll_after_seconds"] == 30


@pytest.mark.asyncio
async def test_poll_returns_1s_after_task(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    wid, key = await _register(authed_admin_client)
    await _insert_task(migrated_db_url)
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle"},
    )
    body = resp.json()
    assert body["task"] is not None
    assert body["next_poll_after_seconds"] == 1


@pytest.mark.asyncio
async def test_poll_busy_returns_5s(authed_admin_client: AsyncClient) -> None:
    wid, key = await _register(authed_admin_client)
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "busy"},
    )
    body = resp.json()
    assert body["task"] is None
    assert body["next_poll_after_seconds"] == 5


@pytest.mark.asyncio
async def test_poll_cross_worker_key_is_401(authed_admin_client: AsyncClient) -> None:
    wid_a, _ = await _register(authed_admin_client, "a")
    _, key_b = await _register(authed_admin_client, "b")
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid_a}/poll",
        headers={"X-API-Key": key_b},
        json={"status": "idle"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_poll_busy_does_not_claim_open_task(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """A `busy` worker must not receive open-claim tasks. Push does (another test)."""
    wid, key = await _register(authed_admin_client)
    await _insert_task(migrated_db_url)
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "busy"},
    )
    body = resp.json()
    assert body["task"] is None


@pytest.mark.asyncio
async def test_poll_paused_worker_gets_nothing(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    wid, key = await _register(authed_admin_client)
    # Pause the worker via the admin endpoint.
    pr = await authed_admin_client.post(f"/admin/workers/{wid}/pause", json={})
    assert pr.status_code == 200
    await _insert_task(migrated_db_url)
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle"},
    )
    body = resp.json()
    assert body["task"] is None


@pytest.mark.asyncio
async def test_poll_updates_telemetry(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    wid, key = await _register(authed_admin_client)
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={
            "status": "idle",
            "ram_mb": 2048,
            "ram_used_mb": 512,
            "disk_mb": 20000,
            "disk_used_mb": 1000,
            "name": "new-label",
        },
    )
    assert resp.status_code == 200
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT ram_mb, ram_used_mb, disk_mb, disk_used_mb, name "
                            "FROM workers WHERE id=:i"
                        ),
                        {"i": wid},
                    )
                )
                .mappings()
                .first()
            )
    finally:
        await engine.dispose()
    assert row is not None
    assert row["ram_mb"] == 2048
    assert row["ram_used_mb"] == 512
    assert row["disk_mb"] == 20000
    assert row["disk_used_mb"] == 1000
    assert row["name"] == "new-label"


@pytest.mark.asyncio
async def test_poll_persists_cpu_pct(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    wid, key = await _register(authed_admin_client)
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "cpu_pct": 37},
    )
    assert resp.status_code == 200, resp.text
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            row = (
                (await conn.execute(text("SELECT cpu_pct FROM workers WHERE id=:i"), {"i": wid}))
                .mappings()
                .first()
            )
    finally:
        await engine.dispose()
    assert row is not None
    assert row["cpu_pct"] == 37


@pytest.mark.asyncio
async def test_poll_without_cpu_pct_preserves_previous(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    wid, key = await _register(authed_admin_client)
    # First poll sets cpu_pct.
    await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "cpu_pct": 42},
    )
    # Second poll without cpu_pct: must not overwrite with NULL.
    await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle"},
    )
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            row = (
                (await conn.execute(text("SELECT cpu_pct FROM workers WHERE id=:i"), {"i": wid}))
                .mappings()
                .first()
            )
    finally:
        await engine.dispose()
    assert row is not None
    assert row["cpu_pct"] == 42


# --- Cancel signals --------------------------------------------------------


async def _seed_running_then_cancel(client: AsyncClient, db_url: str) -> tuple[str, str, int, str]:
    """Helper: registers, polls to obtain an execution, cancels the task.

    Returns (worker_id, api_key, task_id, lease_token).
    """
    wid, key = await _register(client)
    task_id = await _insert_task(db_url)
    poll = await client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle"},
    )
    token = poll.json()["task"]["lease_token"]
    cancel = await client.post(f"/admin/tasks/{task_id}/cancel", json={"reason": "stop"})
    assert cancel.status_code == 200
    return wid, key, task_id, token


@pytest.mark.asyncio
async def test_poll_delivers_cancel_signal_once(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    wid, key, task_id, token = await _seed_running_then_cancel(authed_admin_client, migrated_db_url)
    # First poll: delivers the signal.
    r1 = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "busy"},
    )
    body1 = r1.json()
    assert len(body1["cancel_signals"]) == 1
    sig = body1["cancel_signals"][0]
    assert sig["task_id"] == task_id
    assert sig["lease_token"] == token

    # Second poll: the signal is already notified → it is not re-delivered.
    r2 = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "busy"},
    )
    assert r2.json()["cancel_signals"] == []


# --- Push delivery ---------------------------------------------------------


async def _create_push_task(db_url: str, target_worker_id: str, command: str = "ping") -> int:
    """Inserts a push task directed at the worker, unsigned (that is not what
    these tests evaluate); it carries a uuid idempotency_key."""
    import uuid

    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        INSERT INTO tasks (workspace_id, command, payload,
                                           target_mode, target_count,
                                           priority, status, run_at,
                                           delivery_mode, target_worker_id,
                                           idempotency_key,
                                           signed, signature)
                        VALUES (:workspace_id, :cmd, '{}'::jsonb, 'count', 1, 0,
                                'queued', now(),
                                'push', :w, :ik,
                                true, decode(repeat('00', 64), 'hex'))
                        RETURNING id
                        """
                    ),
                    {
                        "cmd": command,
                        "w": target_worker_id,
                        "ik": str(uuid.uuid4()),
                        "workspace_id": DEFAULT_WORKSPACE_ID,
                    },
                )
            ).first()
        return int(row[0])
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_poll_push_task_delivered_even_when_busy(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Push tasks directed at the worker are delivered even if it reports busy."""
    wid, key = await _register(authed_admin_client)
    task_id = await _create_push_task(migrated_db_url, wid, command="halt")

    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "busy"},
    )
    body = resp.json()
    assert body["task"] is not None, body
    assert body["task"]["task_id"] == task_id
    assert body["task"]["delivery_mode"] == "push"
    assert body["task"]["target_worker_id"] == wid


@pytest.mark.asyncio
async def test_poll_push_task_other_worker_not_delivered(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """A push task directed at worker A must not be delivered to worker B."""
    wid_a, _ = await _register(authed_admin_client, "a")
    wid_b, key_b = await _register(authed_admin_client, "b")
    await _create_push_task(migrated_db_url, wid_a, command="for-a-only")

    resp = await authed_admin_client.post(
        f"/v1/workers/{wid_b}/poll",
        headers={"X-API-Key": key_b},
        json={"status": "idle"},
    )
    body = resp.json()
    assert body["task"] is None


# --- Signed push via admin endpoint ----------------------------------------


@pytest.mark.asyncio
async def test_admin_create_push_task_signed_and_poll_delivers(
    authed_admin_client: AsyncClient, _configure_signing: None
) -> None:
    """End-to-end: admin push command → signed task → /poll returns a signature
    verifiable byte-identically over the 6 canonical fields."""
    wid, key = await _register(authed_admin_client)
    expires = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    push = await authed_admin_client.post(
        f"/admin/workers/{wid}/command",
        json={"command": "restart", "payload": {"force": True}, "expires_at": expires},
    )
    assert push.status_code == 200, push.text
    pushed = push.json()
    task_id = int(pushed["task_id"])
    sig_b64 = pushed["signature"]

    poll = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle"},
    )
    task = poll.json()["task"]
    assert task is not None
    assert task["task_id"] == task_id
    assert task["signed"] is True
    assert task["signature"] == sig_b64

    # Verify the signature with the public key.
    pem = Path(settings.ed25519_private_key_path).read_bytes()
    priv = serialization.load_pem_private_key(pem, password=None)
    pub = priv.public_key()
    sig = base64.b64decode(sig_b64)
    assert verify_task_signature(
        pub,
        sig,
        task_id=task_id,
        worker_id=wid,
        command="restart",
        payload={"force": True},
        idempotency_key=task["idempotency_key"],
        created_at_iso=task["created_at"],
    )


# --- Canonical message determinism -----------------------------------------


def test_task_canonical_message_lex_order() -> None:
    """canonical_json over the 6 fields produces a stable lex order and identical
    bytes. The lex order of the canonical message keys is:
    command, created_at, idempotency_key, payload, task_id, worker_id.
    """
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
    # Rebuilding from an unordered dict must give exactly the same result.
    msg2 = canonical_json(
        {
            "command": "ping",
            "created_at": "2026-05-18T00:00:00+00:00",
            "idempotency_key": None,
            "payload": {"k": "v"},
            "task_id": 42,
            "worker_id": "11111111-1111-1111-1111-111111111111",
        }
    )
    assert msg == msg2
    parsed = json.loads(msg)
    assert list(parsed.keys()) == [
        "command",
        "created_at",
        "idempotency_key",
        "payload",
        "task_id",
        "worker_id",
    ]


# --- Reserved commands -----------------------------------------------------


@pytest.mark.asyncio
async def test_admin_create_task_command_field_rejected(
    authed_admin_client: AsyncClient,
) -> None:
    """`command` is no longer a valid field in POST /admin/tasks (extra='forbid')."""
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"command": "__reload_config__", "target": "1"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_control_op_signed_field_ignored_via_control_op_endpoint(
    authed_admin_client: AsyncClient, _configure_signing: None
) -> None:
    """Control ops go through POST /admin/workers/{id}/control-op.
    The endpoint signs unconditionally (legacy signed flag ignored).
    """
    wid, _ = await _register(authed_admin_client)
    resp = await authed_admin_client.post(
        f"/admin/workers/{wid}/control-op",
        json={"control_op": "__reload_config__", "payload": {}},
    )
    assert resp.status_code == 200, resp.text


# --- Legacy endpoints return 404 -------------------------------------------


@pytest.mark.asyncio
async def test_legacy_heartbeat_endpoint_gone(authed_admin_client: AsyncClient) -> None:
    wid, key = await _register(authed_admin_client)
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/heartbeat",
        headers={"X-API-Key": key},
        json={"status": "idle"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_legacy_claim_endpoint_gone(authed_admin_client: AsyncClient) -> None:
    wid, key = await _register(authed_admin_client)
    resp = await authed_admin_client.post(
        "/v1/tasks/claim",
        headers={"X-API-Key": key},
        json={"worker_id": wid, "lease_seconds": 30},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_legacy_commands_ack_endpoint_gone(authed_admin_client: AsyncClient) -> None:
    fake = "00000000-0000-0000-0000-000000000000"
    _, key = await _register(authed_admin_client)
    resp = await authed_admin_client.post(
        f"/v1/commands/{fake}/ack",
        headers={"X-API-Key": key},
        json={"status": "success"},
    )
    assert resp.status_code == 404


# --- `taken` counts every non-expired execution ----------------------------


@pytest.mark.asyncio
async def test_poll_predicate_counts_non_expired_terminals(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The `taken` predicate in /poll counts every non-expired
    execution — active AND real terminal ones consume budget; only
    `expired` (worker crash) gives its slot back.

    Three assertions on a task with `attempt_budget=1` reset to `queued`:

    1. With a previous `completed` execution and `attempt_budget=1`, the claim
       does NOT fire: the terminal one consumes the budget (before,
       with an active-only predicate, the slot reopened and a
       worker slipped in with an extra attempt).
    2. After the retry bump (`attempt_budget=2`, contract: every
       retry reopens budget by +1), the claim fires.
    3. If the previous execution is `expired`, it does NOT consume budget: the
       claim fires without a bump (crash recovery).
    """
    target_worker_id, target_key = await _register(authed_admin_client, "wTarget")
    other_worker_id, _ = await _register(authed_admin_client, "wOther")

    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    # Simulates a previous `completed` attempt for `other_worker_id`. The task
    # goes back to `queued` (simulating a post-retry reset) but
    # WITHOUT the retry bump (intermediate state, only reachable here).
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO task_executions
                        (task_id, worker_id, status, started_at,
                         finished_at, exit_code, attempt_index)
                    VALUES (:tid, :wid, 'completed', now() - interval '1 minute',
                            now() - interval '30 seconds', 0, 1)
                    """
                ),
                {"tid": task_id, "wid": other_worker_id},
            )
            await conn.execute(
                text(
                    "UPDATE tasks SET status='queued'::task_status, completed_at=NULL WHERE id=:tid"
                ),
                {"tid": task_id},
            )
    finally:
        await engine.dispose()

    # (1) Without a bump, the completed one consumes the budget: NO claim.
    poll = await authed_admin_client.post(
        f"/v1/workers/{target_worker_id}/poll",
        headers={"X-API-Key": target_key},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert poll.status_code == 200, poll.text
    assert poll.json()["task"] is None, "a non-expired terminal must consume budget"

    # (2) With the retry bump (attempt_budget=2), there is a slot: claim OK.
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET attempt_budget = 2 WHERE id=:tid"),
                {"tid": task_id},
            )
    finally:
        await engine.dispose()
    poll = await authed_admin_client.post(
        f"/v1/workers/{target_worker_id}/poll",
        headers={"X-API-Key": target_key},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert poll.status_code == 200, poll.text
    task = poll.json()["task"]
    assert task is not None, "the retry bump must reopen the slot"
    assert task["task_id"] == task_id

    # (3) `expired` does not consume budget: on a new task with a crashed
    # attempt, the claim fires without a bump.
    create2 = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert create2.status_code == 200, create2.text
    task2_id = create2.json()["task_id"]
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO task_executions
                        (task_id, worker_id, status, started_at,
                         finished_at, attempt_index)
                    VALUES (:tid, :wid, 'expired', now() - interval '1 minute',
                            now() - interval '30 seconds', 1)
                    """
                ),
                {"tid": task2_id, "wid": other_worker_id},
            )
    finally:
        await engine.dispose()
    poll = await authed_admin_client.post(
        f"/v1/workers/{target_worker_id}/poll",
        headers={"X-API-Key": target_key},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert poll.status_code == 200, poll.text
    task = poll.json()["task"]
    assert task is not None, "expired must return its slot (crash recovery)"
    assert task["task_id"] == task2_id


# --- Cache flush control-lane tests ----------------------------------------


async def _request_flush(migrated_db_url: str, worker_id: str) -> None:
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE workers SET cache_flush_requested_at = now() WHERE id = :id"),
                {"id": worker_id},
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_poll_delivers_clear_cache_when_idle_and_pending(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    wid, key = await _register(authed_admin_client)
    await _request_flush(migrated_db_url, wid)
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle"},
    )
    assert resp.status_code == 200
    assert resp.json()["clear_cache"] is True


@pytest.mark.asyncio
async def test_poll_defers_clear_cache_when_busy(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    wid, key = await _register(authed_admin_client)
    await _request_flush(migrated_db_url, wid)
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "busy"},
    )
    assert resp.json()["clear_cache"] is False


@pytest.mark.asyncio
async def test_poll_no_clear_cache_when_not_requested(
    authed_admin_client: AsyncClient,
) -> None:
    wid, key = await _register(authed_admin_client)
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle"},
    )
    assert resp.json()["clear_cache"] is False


@pytest.mark.asyncio
async def test_poll_ack_seals_done_and_clears_pending(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    wid, key = await _register(authed_admin_client)
    await _request_flush(migrated_db_url, wid)
    ack = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "cache_flush_completed": True},
    )
    assert ack.status_code == 200
    # The ack's own poll does NOT re-deliver (guards the ACK-before-PICK order:
    # if PICK ran before ACK, this poll would return clear_cache=True).
    assert ack.json()["clear_cache"] is False

    # done_at was stamped on the worker's row.
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            done_at = (
                await conn.execute(
                    text("SELECT cache_flush_done_at FROM workers WHERE id = :id"),
                    {"id": wid},
                )
            ).scalar_one()
            assert done_at is not None
    finally:
        await engine.dispose()

    nxt = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle"},
    )
    assert nxt.json()["clear_cache"] is False


# --- pause task delivery by quota/disk --------------------------------------


async def _set_ws_usage(db_url: str, total: int) -> None:
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO workspace_artifact_usage (workspace_id, total_bytes) "
                    "VALUES (:w, :t) ON CONFLICT (workspace_id) DO UPDATE SET total_bytes=:t"
                ),
                {"w": DEFAULT_WORKSPACE_ID, "t": total},
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_poll_paused_on_quota_does_not_deliver(
    authed_admin_client: AsyncClient, migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifact_workspace_max_total_bytes", 1000)
    wid, key = await _register(authed_admin_client)
    await _insert_task(migrated_db_url)
    await _set_ws_usage(migrated_db_url, 1000)  # at the cap

    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle"},
    )
    body = resp.json()
    assert body["task"] is None  # not delivered despite a queued task
    assert body["storage_paused"] == "quota"


@pytest.mark.asyncio
async def test_poll_paused_on_workspace_quota_override(
    authed_admin_client: AsyncClient, migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The poll pauses on the workspace override, not on the default."""
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifact_workspace_max_total_bytes", 10 * 1024**3)
    wid, key = await _register(authed_admin_client)
    await _insert_task(migrated_db_url)
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE workspaces SET artifact_quota_bytes = 1000 WHERE id = :w"),
                {"w": DEFAULT_WORKSPACE_ID},
            )
    finally:
        await engine.dispose()
    await _set_ws_usage(migrated_db_url, 1000)  # at the override cap

    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle"},
    )
    body = resp.json()
    assert body["task"] is None
    assert body["storage_paused"] == "quota"


@pytest.mark.asyncio
async def test_poll_paused_on_disk_floor(
    authed_admin_client: AsyncClient, migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orcaster_server import storage

    monkeypatch.setattr(storage, "disk_free_bytes", lambda _root: 1024)
    wid, key = await _register(authed_admin_client)
    await _insert_task(migrated_db_url)

    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle"},
    )
    body = resp.json()
    assert body["task"] is None
    assert body["storage_paused"] == "disk"


@pytest.mark.asyncio
async def test_poll_delivers_when_storage_ok(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    wid, key = await _register(authed_admin_client)
    await _insert_task(migrated_db_url)
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle"},
    )
    body = resp.json()
    assert body["task"] is not None
    assert body["storage_paused"] is None


async def _stored_worker_version(db_url: str, wid: str) -> str | None:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            row = (
                (await conn.execute(text("SELECT version FROM workers WHERE id=:i"), {"i": wid}))
                .mappings()
                .first()
            )
    finally:
        await engine.dispose()
    assert row is not None
    return row["version"]


@pytest.mark.asyncio
async def test_poll_updates_the_reported_version(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The worker corrects its version without re-enrolling.

    `register` runs only once per identity: an already enrolled worker that
    upgrades never goes through it again, so the version must travel in
    the poll like the rest of the telemetry.
    """
    wid, key = await _register(authed_admin_client)

    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "version": "0.16.0"},
    )
    assert resp.status_code == 200
    assert await _stored_worker_version(migrated_db_url, wid) == "0.16.0"

    # A poll without `version` (old worker) does not erase the one already stored.
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle"},
    )
    assert resp.status_code == 200
    assert await _stored_worker_version(migrated_db_url, wid) == "0.16.0"


@pytest.mark.asyncio
async def test_poll_normalizes_the_version_instead_of_rejecting_it(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """A degenerate `version` cannot take the poll down.

    The poll is the only thing that keeps the worker alive: a 422 here leaves it
    retrying with backoff against a body that will never change, claiming no
    tasks and offline forever because of a misplaced `ORCH_VERSION` in the `.env`.
    An empty value is ignored (does not overwrite the stored one) and a long value is
    truncated to 64.
    """
    wid, key = await _register(authed_admin_client)

    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "version": "0.16.0"},
    )
    assert resp.status_code == 200

    # A bare `ORCH_VERSION=` in the .env: ignored, does not overwrite with "".
    for degenerate in ("", "   "):
        resp = await authed_admin_client.post(
            f"/v1/workers/{wid}/poll",
            headers={"X-API-Key": key},
            json={"status": "idle", "version": degenerate},
        )
        assert resp.status_code == 200
        assert await _stored_worker_version(migrated_db_url, wid) == "0.16.0"

    # Long build id (e.g. a full `git describe`): 200 and truncated, not 422.
    long = "v" * 200
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "version": long},
    )
    assert resp.status_code == 200
    assert await _stored_worker_version(migrated_db_url, wid) == long[:64]


# --- Server-side disk gate -------------------------------------------------
#
# The SERVER decides the threshold (`worker_claim_min_free_fraction`, default 0.20);
# the worker only reports its `free_disk_fraction` in the poll. Modeled on
# `storage_paused`: close steps 4-6 of the pick for a gated worker, but
# leave the `clear_cache` lane OPEN (conditioned only on `status=idle`).
# NULL = "unknown" = do not gate (fail-open, compat with old workers).


async def _stored_free_disk_fraction(db_url: str, worker_id: str) -> float | None:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT free_disk_fraction FROM workers WHERE id = :id"),
                    {"id": worker_id},
                )
            ).first()
        assert row is not None
        return row[0]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_poll_persists_free_disk_fraction(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The poll persists `free_disk_fraction` on the worker's row (migration 0054)."""
    wid, key = await _register(authed_admin_client)
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "free_disk_fraction": 0.42},
    )
    assert resp.status_code == 200
    assert await _stored_free_disk_fraction(migrated_db_url, wid) == pytest.approx(0.42)


@pytest.mark.asyncio
async def test_poll_omitting_free_disk_fraction_resets_to_null(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """`free_disk_fraction` is gate state, not cumulative telemetry: a poll
    that omits the field RESETS the column to NULL.

    Unlike disk_used_mb (`*_set` guard), here the value must reflect the
    current poll. If a stale 0.42 survived, the claim (reads the body → None →
    fail-open) would disagree with fairness/snapshot/UI (read the column → 0.42 →
    gated). Always overwriting with NULL keeps the four sites coherent
    in fail-open when the worker stops measuring."""
    wid, key = await _register(authed_admin_client)
    await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "free_disk_fraction": 0.42},
    )
    assert await _stored_free_disk_fraction(migrated_db_url, wid) == pytest.approx(0.42)
    # Second poll without the field: the column goes back to NULL (fail-open).
    await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle"},
    )
    assert await _stored_free_disk_fraction(migrated_db_url, wid) is None


@pytest.mark.asyncio
async def test_poll_low_free_disk_fraction_blocks_open_claim(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """An idle worker below the floor does NOT receive the open-claim task (gate)."""
    wid, key = await _register(authed_admin_client)
    await _insert_task(migrated_db_url)
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "free_disk_fraction": 0.05},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["task"] is None, "the disk-gated worker must not claim"
    assert body["disk_gated"] is True


@pytest.mark.asyncio
async def test_poll_null_free_disk_fraction_claims_as_today(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """NULL = "unknown" = fail-open: claims exactly as before the disk gate."""
    wid, key = await _register(authed_admin_client)
    tid = await _insert_task(migrated_db_url)
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle"},  # without free_disk_fraction
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["task"] is not None and body["task"]["task_id"] == tid
    assert body["disk_gated"] is False


@pytest.mark.asyncio
async def test_poll_healthy_free_disk_fraction_claims(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """A fraction above the floor does not gate: claims normally."""
    wid, key = await _register(authed_admin_client)
    tid = await _insert_task(migrated_db_url)
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "free_disk_fraction": 0.5},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["task"] is not None and body["task"]["task_id"] == tid
    assert body["disk_gated"] is False


@pytest.mark.asyncio
async def test_poll_gated_worker_blocks_push(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The gate also closes step 4 (push): a gated worker does not receive the
    directed push (the server withholds ALL delivery, not only the open-claim)."""
    wid, key = await _register(authed_admin_client)
    await _create_push_task(migrated_db_url, wid)
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "free_disk_fraction": 0.05},
    )
    assert resp.status_code == 200
    assert resp.json()["task"] is None


@pytest.mark.asyncio
async def test_poll_gated_worker_still_receives_clear_cache(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The flush lane stays OPEN for a gated worker (`storage_paused`
    pattern): we withhold tasks but can still clear its
    cache so it recovers space. And it does not claim the queued task."""
    wid, key = await _register(authed_admin_client)
    await _insert_task(migrated_db_url)
    await _request_flush(migrated_db_url, wid)
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "free_disk_fraction": 0.05},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["clear_cache"] is True, "the flush lane must stay open"
    assert body["task"] is None, "but must not claim new tasks"


@pytest.mark.asyncio
async def test_poll_gate_disabled_by_zero_floor(
    authed_admin_client: AsyncClient, migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`worker_claim_min_free_fraction=0.0` disables the gate: it claims even if
    it reports a very low fraction."""
    monkeypatch.setattr(settings, "worker_claim_min_free_fraction", 0.0)
    wid, key = await _register(authed_admin_client)
    tid = await _insert_task(migrated_db_url)
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "free_disk_fraction": 0.01},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["task"] is not None and body["task"]["task_id"] == tid
    assert body["disk_gated"] is False
