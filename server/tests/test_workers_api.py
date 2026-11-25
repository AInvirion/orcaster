"""Integration tests for `/v1/workers/*`.

DoD: cover registration, heartbeat with API key, goodbye and
401/404 errors against a real Postgres (via the `authed_admin_client` fixture).
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import mint_token_via_app

REGISTER_PAYLOAD = {
    "hostname": "worker-test-01",
    "os": "linux",
    "arch": "x86_64",
    "ram_mb": 8192,
    "disk_mb": 100000,
    "version": "0.1.0",
    "tags": ["test"],
    "capabilities": {"docker": False},
}


async def _register(client: AsyncClient, **overrides) -> tuple[str, str]:
    token = await mint_token_via_app()
    payload = {**REGISTER_PAYLOAD, "enrollment_token": token, **overrides}
    resp = await client.post("/v1/workers/register", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return data["worker_id"], data["api_key"]


@pytest.mark.asyncio
async def test_register_returns_worker_id_and_api_key(authed_admin_client: AsyncClient) -> None:
    token = await mint_token_via_app()
    resp = await authed_admin_client.post(
        "/v1/workers/register", json={**REGISTER_PAYLOAD, "enrollment_token": token}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # worker_id is a valid UUID
    parsed = uuid.UUID(data["worker_id"])
    assert str(parsed) == data["worker_id"]
    # api_key is a non-empty opaque string
    assert isinstance(data["api_key"], str) and len(data["api_key"]) >= 32


@pytest.mark.asyncio
async def test_register_validates_os_enum(authed_admin_client: AsyncClient) -> None:
    token = await mint_token_via_app()
    bad = {**REGISTER_PAYLOAD, "os": "plan9", "enrollment_token": token}
    resp = await authed_admin_client.post("/v1/workers/register", json=bad)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_heartbeat_ok_with_valid_key(authed_admin_client: AsyncClient) -> None:
    worker_id, api_key = await _register(authed_admin_client)
    resp = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/poll",
        headers={"X-API-Key": api_key},
        json={"status": "idle"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task"] is None
    assert body["cancel_signals"] == []
    assert "next_poll_after_seconds" in body


@pytest.mark.asyncio
async def test_heartbeat_reactivates_offline_worker(authed_admin_client: AsyncClient) -> None:
    """Goodbye -> status='offline'; a later heartbeat must bring it back to 'online'."""
    worker_id, api_key = await _register(authed_admin_client)
    headers = {"X-API-Key": api_key}

    bye = await authed_admin_client.post(f"/v1/workers/{worker_id}/goodbye", headers=headers)
    assert bye.status_code == 200

    hb = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/poll",
        headers=headers,
        json={"status": "idle"},
    )
    assert hb.status_code == 200


@pytest.mark.asyncio
async def test_heartbeat_missing_api_key_is_401(authed_admin_client: AsyncClient) -> None:
    worker_id, _ = await _register(authed_admin_client)
    resp = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/poll",
        json={"status": "idle"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_heartbeat_wrong_api_key_is_401(authed_admin_client: AsyncClient) -> None:
    worker_id, _ = await _register(authed_admin_client)
    resp = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/poll",
        headers={"X-API-Key": "definitely-not-the-key"},
        json={"status": "idle"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_heartbeat_unknown_worker_is_401(authed_admin_client: AsyncClient) -> None:
    """Which one failed is not leaked: an unknown worker is also 401, not 404."""
    fake_id = str(uuid.uuid4())
    resp = await authed_admin_client.post(
        f"/v1/workers/{fake_id}/poll",
        headers={"X-API-Key": "any-key"},
        json={"status": "idle"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_heartbeat_cross_worker_key_is_401(authed_admin_client: AsyncClient) -> None:
    """The api_key of worker A must not authorize worker B."""
    a_id, _ = await _register(authed_admin_client, hostname="worker-a")
    _, b_key = await _register(authed_admin_client, hostname="worker-b")
    resp = await authed_admin_client.post(
        f"/v1/workers/{a_id}/poll",
        headers={"X-API-Key": b_key},
        json={"status": "idle"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_goodbye_ok(authed_admin_client: AsyncClient) -> None:
    worker_id, api_key = await _register(authed_admin_client)
    resp = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/goodbye",
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


@pytest.mark.asyncio
async def test_goodbye_is_idempotent(authed_admin_client: AsyncClient) -> None:
    worker_id, api_key = await _register(authed_admin_client)
    headers = {"X-API-Key": api_key}
    first = await authed_admin_client.post(f"/v1/workers/{worker_id}/goodbye", headers=headers)
    second = await authed_admin_client.post(f"/v1/workers/{worker_id}/goodbye", headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200


@pytest.mark.asyncio
async def test_goodbye_unauthorized(authed_admin_client: AsyncClient) -> None:
    worker_id, _ = await _register(authed_admin_client)
    resp = await authed_admin_client.post(f"/v1/workers/{worker_id}/goodbye")
    assert resp.status_code == 401


# --- goodbye: preservation of `paused` --------------------------------------


async def _worker_status(db_url: str, worker_id: str) -> str | None:
    """Read the raw worker status: no public surface exposes it on its own."""
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT status FROM workers WHERE id = :wid"), {"wid": worker_id}
                )
            ).first()
            return row[0] if row else None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_goodbye_preserves_paused(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Goodbye does not override the operator intent.

    `paused` expresses operator intent, not observed connectivity, so
    a cooperative shutdown must not revert it.
    """
    worker_id, api_key = await _register(authed_admin_client)
    pause = await authed_admin_client.post(f"/admin/workers/{worker_id}/pause", json={})
    assert pause.status_code == 200, pause.text

    resp = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/goodbye", headers={"X-API-Key": api_key}
    )
    assert resp.status_code == 200

    assert await _worker_status(migrated_db_url, worker_id) == "paused"


@pytest.mark.asyncio
async def test_goodbye_from_online_marks_offline(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The normal `online -> offline` transition of goodbye does not change."""
    worker_id, api_key = await _register(authed_admin_client)

    resp = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/goodbye", headers={"X-API-Key": api_key}
    )
    assert resp.status_code == 200

    assert await _worker_status(migrated_db_url, worker_id) == "offline"


@pytest.mark.asyncio
async def test_paused_worker_does_not_resume_after_restart(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Restarting the container of a paused worker does not put it back to work.

    Reproduces the symptom observed in production: pausing the fleet,
    stopping the containers (goodbye) and starting them again (poll) left the
    workers `online` and claiming, without going through `resume`.
    """
    worker_id, api_key = await _register(authed_admin_client)
    headers = {"X-API-Key": api_key}
    pause = await authed_admin_client.post(f"/admin/workers/{worker_id}/pause", json={})
    assert pause.status_code == 200, pause.text
    goodbye = await authed_admin_client.post(f"/v1/workers/{worker_id}/goodbye", headers=headers)
    assert goodbye.status_code == 200

    poll = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/poll", headers=headers, json={"status": "idle"}
    )
    assert poll.status_code == 200, poll.text
    assert poll.json()["task"] is None

    assert await _worker_status(migrated_db_url, worker_id) == "paused"


# --- name (dashboard label) -------------------------------------------------


async def _get_worker_name(client: AsyncClient, worker_id: str) -> str | None:
    """Read the worker name via the dashboard partial (the only public surface)."""
    resp = await client.get(f"/admin/workers/{worker_id}")
    assert resp.status_code == 200, resp.text
    return resp.text


@pytest.mark.asyncio
async def test_register_persists_name_when_provided(authed_admin_client: AsyncClient) -> None:
    worker_id, _ = await _register(authed_admin_client, name="worker-prod-eu")
    body = await _get_worker_name(authed_admin_client, worker_id)
    assert "worker-prod-eu" in body


@pytest.mark.asyncio
async def test_register_without_name_is_nullable(authed_admin_client: AsyncClient) -> None:
    # The base payload does not include name -> it must still register OK.
    worker_id, _ = await _register(authed_admin_client)
    body = await _get_worker_name(authed_admin_client, worker_id)
    # Without a name, the template falls back to the hostname.
    assert "worker-test-01" in body


@pytest.mark.asyncio
async def test_heartbeat_updates_name_when_changed(authed_admin_client: AsyncClient) -> None:
    """Changing ORCH_WORKER_NAME in .env and restarting the client must be reflected."""
    worker_id, api_key = await _register(authed_admin_client, name="old-name")
    headers = {"X-API-Key": api_key}
    resp = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/poll",
        headers=headers,
        json={"status": "idle", "name": "new-name"},
    )
    assert resp.status_code == 200
    body = await _get_worker_name(authed_admin_client, worker_id)
    assert "new-name" in body
    assert "old-name" not in body


@pytest.mark.asyncio
async def test_heartbeat_omitted_name_preserves_existing(authed_admin_client: AsyncClient) -> None:
    """Heartbeat without a name must not erase the persisted name."""
    worker_id, api_key = await _register(authed_admin_client, name="keep-me")
    headers = {"X-API-Key": api_key}
    resp = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/poll",
        headers=headers,
        json={"status": "idle"},
    )
    assert resp.status_code == 200
    body = await _get_worker_name(authed_admin_client, worker_id)
    assert "keep-me" in body


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
async def test_register_treats_the_empty_version_as_absent(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """An empty `ORCH_VERSION=` cannot leave the column blank.

    `register` runs only once per identity: what is stamped here stays
    forever unless the worker enrolls again. The client already falls back to the
    package value when the override comes empty; this is the defense against
    an old client that does send it.
    """
    for degenerate in ("", "   "):
        wid, _ = await _register(authed_admin_client, version=degenerate)
        assert await _stored_worker_version(migrated_db_url, wid) is None


@pytest.mark.asyncio
async def test_register_trims_whitespace_of_the_version(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    wid, _ = await _register(authed_admin_client, version="  0.16.0  ")
    assert await _stored_worker_version(migrated_db_url, wid) == "0.16.0"


@pytest.mark.asyncio
async def test_register_rejects_a_version_that_is_too_long(
    authed_admin_client: AsyncClient,
) -> None:
    """Unlike the poll, `register` does reject: it is one-shot and fails loudly.

    The poll normalizes because a 422 there leaves the worker offline
    indefinitely; here the operator sees the 422 during enrollment.
    """
    token = await mint_token_via_app()
    resp = await authed_admin_client.post(
        "/v1/workers/register",
        json={**REGISTER_PAYLOAD, "enrollment_token": token, "version": "v" * 200},
    )
    assert resp.status_code == 422
