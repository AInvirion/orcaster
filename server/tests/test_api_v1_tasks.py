"""Contract tests: public API `/api/v1`, `POST /tasks`.

`POST /api/v1/tasks` reuses `_create_task` after mapping the public body
(`ApiCreateTaskRequest`) to the internal `AdminCreateTaskRequest`. The public contract
EXCLUDES `delivery_mode`/`target_worker_id` (push is an admin control operation) and
`signed` (deprecated): sending them violates `extra="forbid"` → 422. Responds 201.
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import mint_pat_via_app


def _db_url() -> str:
    from orcaster_server.config import settings

    return settings.database_url


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_count_201(app_client: AsyncClient) -> None:
    """Minimal body (target='1') → 201 with the creation contract."""
    token, _uid = await mint_pat_via_app()
    resp = await app_client.post(
        "/api/v1/tasks",
        headers=_bearer(token),
        json={"payload": {"cmd": "echo hi"}},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert isinstance(body["task_id"], int)
    assert body["status"] == "queued"
    assert body["target"] == "1"
    assert body["target_mode"] == "count"
    assert body["target_count"] == 1
    assert body["parent_task_id"] is None


@pytest.mark.asyncio
async def test_create_task_persists_in_callers_workspace(app_client: AsyncClient) -> None:
    """The task is inserted scoped to the PAT's workspace, with payload/eligibility."""
    token, _uid = await mint_pat_via_app()
    resp = await app_client.post(
        "/api/v1/tasks",
        headers=_bearer(token),
        json={
            "payload": {"cmd": "run", "n": 3},
            "eligibility": {"gpu": True},
            "priority": 5,
        },
    )
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["task_id"]

    from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID

    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT payload, eligibility, priority, workspace_id, "
                        "parent_task_id FROM tasks WHERE id = :id"
                    ),
                    {"id": task_id},
                )
            ).one()
    finally:
        await engine.dispose()
    payload = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    eligibility = row[1] if isinstance(row[1], dict) else json.loads(row[1])
    assert payload == {"cmd": "run", "n": 3}
    assert eligibility == {"gpu": True}
    assert row[2] == 5
    assert str(row[3]) == DEFAULT_WORKSPACE_ID
    assert row[4] is None


# ---------------------------------------------------------------------------
# fields excluded from the public contract (extra="forbid")
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"delivery_mode": "push"},
        {"target_worker_id": "00000000-0000-0000-0000-000000000000"},
        {"signed": False},
        {"bogus_field": 1},
    ],
)
@pytest.mark.asyncio
async def test_create_task_rejects_non_public_fields(app_client: AsyncClient, extra: dict) -> None:
    """delivery_mode/target_worker_id/signed and unknown fields → 422."""
    token, _uid = await mint_pat_via_app()
    resp = await app_client.post(
        "/api/v1/tasks",
        headers=_bearer(token),
        json={"payload": {}, **extra},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"] == "unprocessable_entity"


@pytest.mark.asyncio
async def test_create_task_invalid_target_422(app_client: AsyncClient) -> None:
    """target with invalid syntax → 422 (validation reused from the admin schema)."""
    token, _uid = await mint_pat_via_app()
    resp = await app_client.post(
        "/api/v1/tasks",
        headers=_bearer(token),
        json={"payload": {}, "target": "not-a-target"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"] == "unprocessable_entity"


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_requires_token_401(app_client: AsyncClient) -> None:
    """No Bearer → 401 JSON; nothing is created."""
    resp = await app_client.post("/api/v1/tasks", json={"payload": {}})
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"] == "unauthorized"


# ---------------------------------------------------------------------------
# idempotency_key
# ---------------------------------------------------------------------------


async def _create_workspace(name: str, slug: str) -> str:
    from orcaster_server import db as app_db
    from orcaster_server.workspaces.service import create_workspace

    sm = app_db.get_sessionmaker()
    assert sm is not None
    async with sm() as s:
        ws_id = await create_workspace(s, name=name, slug=slug)
        await s.commit()
        return ws_id


@pytest.mark.asyncio
async def test_create_task_idempotency_replay_same_task(app_client: AsyncClient) -> None:
    """Resending the same idempotency_key returns the existing task, not a 500.

    This is the key's use case: a client retrying a creation (timeout,
    network) must receive the same task. The INSERT must not collide with the
    partial UNIQUE index `tasks_idempotency_key_idx` and surface as a 500.
    """
    import uuid

    token, _uid = await mint_pat_via_app()
    key = str(uuid.uuid4())

    first = await app_client.post(
        "/api/v1/tasks",
        headers=_bearer(token),
        json={"payload": {"cmd": "echo idem"}, "idempotency_key": key},
    )
    assert first.status_code == 201, first.text
    task_id = first.json()["task_id"]

    replay = await app_client.post(
        "/api/v1/tasks",
        headers=_bearer(token),
        json={"payload": {"cmd": "echo idem"}, "idempotency_key": key},
    )
    assert replay.status_code == 201, replay.text
    body = replay.json()
    assert body["task_id"] == task_id
    assert body["status"] == "queued"

    # No second row was inserted.
    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    text("SELECT count(*) FROM tasks WHERE idempotency_key = :k"),
                    {"k": key},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert count == 1


@pytest.mark.asyncio
async def test_create_task_idempotency_key_cross_workspace_409(app_client: AsyncClient) -> None:
    """A key already used in ANOTHER workspace → 409, without leaking the other tenant's data.

    The UNIQUE index is global: returning the foreign task would be a cross-tenant leak,
    and a 500 reveals that the key exists. 409 with a generic message.
    """
    import uuid

    token_a, _ = await mint_pat_via_app(email="idem-a@example.com")
    key = str(uuid.uuid4())
    first = await app_client.post(
        "/api/v1/tasks",
        headers=_bearer(token_a),
        json={"payload": {"cmd": "echo a"}, "idempotency_key": key},
    )
    assert first.status_code == 201, first.text
    task_a = first.json()["task_id"]

    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    token_b, _ = await mint_pat_via_app(workspace_id=ws_b, email="idem-b@example.com")

    clash = await app_client.post(
        "/api/v1/tasks",
        headers=_bearer(token_b),
        json={"payload": {"cmd": "echo b"}, "idempotency_key": key},
    )
    assert clash.status_code == 409, clash.text
    body = clash.json()
    assert body["error"] == "conflict"
    # The message must not reveal the task_id or the workspace of the other tenant.
    assert str(task_a) not in body["message"]


# ---------------------------------------------------------------------------
# idempotency_key + target all_available → 422 (unsupported)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        "all-available",
        "all:00000000-0000-0000-0000-000000000001",
    ],
)
@pytest.mark.asyncio
async def test_create_task_idempotency_key_all_available_422(
    app_client: AsyncClient, target: str
) -> None:
    """`idempotency_key` with a target that maps to all_available → 422.

    The all_available INSERT includes neither `idempotency_key` nor `ON CONFLICT`,
    so accepting it would silently drop the idempotency guarantee. It is rejected
    early with 422, without leaking data and without wasted work.
    """
    import uuid

    token, _uid = await mint_pat_via_app()
    resp = await app_client.post(
        "/api/v1/tasks",
        headers=_bearer(token),
        json={
            "payload": {"cmd": "echo idem"},
            "target": target,
            "idempotency_key": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"] == "unprocessable_entity"
    assert "idempotency_key" in body["message"]


# ---------------------------------------------------------------------------
# idempotent replay with a different payload → 422 (same workspace)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_idempotency_same_payload_replays_201(app_client: AsyncClient) -> None:
    """Same key + same payload (different key order) → 201 replay, one row.

    The payload is compared semantically (dict vs dict), so a change in
    key order is NOT a mismatch.
    """
    import uuid

    token, _uid = await mint_pat_via_app()
    key = str(uuid.uuid4())

    first = await app_client.post(
        "/api/v1/tasks",
        headers=_bearer(token),
        json={"payload": {"a": 1, "b": 2}, "idempotency_key": key},
    )
    assert first.status_code == 201, first.text
    task_id = first.json()["task_id"]

    replay = await app_client.post(
        "/api/v1/tasks",
        headers=_bearer(token),
        # same dict, different key order
        json={"payload": {"b": 2, "a": 1}, "idempotency_key": key},
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["task_id"] == task_id

    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    text("SELECT count(*) FROM tasks WHERE idempotency_key = :k"),
                    {"k": key},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert count == 1


@pytest.mark.asyncio
async def test_create_task_idempotency_different_payload_422(app_client: AsyncClient) -> None:
    """Same key + DIFFERENT payload in the same workspace → 422.

    Reusing a key with a different payload is a client error: the first
    task is already materialized and will not be recreated. 422 (not 409) so that the
    client can tell it apart from the cross-workspace conflict. The original
    payload is not leaked.
    """
    import uuid

    token, _uid = await mint_pat_via_app()
    key = str(uuid.uuid4())

    first = await app_client.post(
        "/api/v1/tasks",
        headers=_bearer(token),
        json={"payload": {"cmd": "echo one"}, "idempotency_key": key},
    )
    assert first.status_code == 201, first.text

    clash = await app_client.post(
        "/api/v1/tasks",
        headers=_bearer(token),
        json={"payload": {"cmd": "echo TWO"}, "idempotency_key": key},
    )
    assert clash.status_code == 422, clash.text
    body = clash.json()
    assert body["error"] == "unprocessable_entity"
    assert body["message"] == "idempotency_key reused with different payload"
    # The original payload is not leaked.
    assert "echo one" not in body["message"]
