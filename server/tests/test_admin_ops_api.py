"""Integration tests for admin CRUD on tasks and workers
and cancellation with propagation.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

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


async def _exec_sql(db_url: str, sql: str, params: dict[str, Any]) -> None:
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params)
    finally:
        await engine.dispose()


async def _row(db_url: str, sql: str, params: dict[str, Any]) -> dict | None:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            r = (await conn.execute(text(sql), params)).mappings().first()
            return dict(r) if r else None
    finally:
        await engine.dispose()


async def _rows(db_url: str, sql: str, params: dict[str, Any]) -> list[dict]:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            r = (await conn.execute(text(sql), params)).mappings().all()
            return [dict(x) for x in r]
    finally:
        await engine.dispose()


async def _set_task_status(db_url: str, task_id: int, status: str) -> None:
    await _exec_sql(
        db_url,
        "UPDATE tasks SET status = CAST(:s AS task_status) WHERE id = :tid",
        {"s": status, "tid": task_id},
    )


async def _set_worker_status(db_url: str, worker_id: str, status: str) -> None:
    await _exec_sql(
        db_url,
        "UPDATE workers SET status = :s WHERE id = :wid",
        {"s": status, "wid": worker_id},
    )


# === tasks: pause / resume / delete ========================================


@pytest.mark.asyncio
async def test_pause_task_from_queued_succeeds(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    resp = await authed_admin_client.post(f"/admin/tasks/{task_id}/pause", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "paused"}

    row = await _row(migrated_db_url, "SELECT status FROM tasks WHERE id=:id", {"id": task_id})
    assert row is not None and row["status"] == "paused"


@pytest.mark.asyncio
async def test_pause_task_completed_returns_409(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]
    await _set_task_status(migrated_db_url, task_id, "completed")

    resp = await authed_admin_client.post(f"/admin/tasks/{task_id}/pause", json={})
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_pause_task_missing_returns_404(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.post("/admin/tasks/9999/pause", json={})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_resume_paused_count_returns_to_queued(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    create = await authed_admin_client.post("/admin/tasks", json={"target": "3"})
    task_id = create.json()["task_id"]
    await _set_task_status(migrated_db_url, task_id, "paused")

    resp = await authed_admin_client.post(f"/admin/tasks/{task_id}/resume", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "queued"}

    row = await _row(migrated_db_url, "SELECT status FROM tasks WHERE id=:id", {"id": task_id})
    assert row is not None and row["status"] == "queued"


@pytest.mark.asyncio
async def test_resume_paused_all_available_returns_to_dispatching(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    create = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    task_id = create.json()["task_id"]
    await _set_task_status(migrated_db_url, task_id, "paused")

    resp = await authed_admin_client.post(f"/admin/tasks/{task_id}/resume", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "dispatching"}


@pytest.mark.asyncio
async def test_resume_non_paused_returns_409(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]
    # status='queued' by default.

    resp = await authed_admin_client.post(f"/admin/tasks/{task_id}/resume", json={})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_delete_completed_task_returns_204_and_cascades(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, _ = await _register(authed_admin_client)
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]
    # Insert an execution to verify CASCADE.
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO task_executions (task_id, worker_id, status) VALUES (:tid, :wid, 'completed')",
        {"tid": task_id, "wid": worker_id},
    )
    await _set_task_status(migrated_db_url, task_id, "completed")

    resp = await authed_admin_client.delete(f"/admin/tasks/{task_id}")
    assert resp.status_code == 204

    row = await _row(migrated_db_url, "SELECT id FROM tasks WHERE id=:id", {"id": task_id})
    assert row is None
    execs = await _rows(
        migrated_db_url,
        "SELECT 1 FROM task_executions WHERE task_id=:tid",
        {"tid": task_id},
    )
    assert execs == []


@pytest.mark.asyncio
async def test_delete_queued_task_returns_409(authed_admin_client: AsyncClient) -> None:
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    resp = await authed_admin_client.delete(f"/admin/tasks/{task_id}")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_delete_missing_task_returns_404(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.delete("/admin/tasks/9999")
    assert resp.status_code == 404


# === workers: pause / resume / delete ======================================


@pytest.mark.asyncio
async def test_pause_online_worker(authed_admin_client: AsyncClient, migrated_db_url: str) -> None:
    worker_id, _ = await _register(authed_admin_client)

    resp = await authed_admin_client.post(f"/admin/workers/{worker_id}/pause", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "paused"}

    row = await _row(migrated_db_url, "SELECT status FROM workers WHERE id=:id", {"id": worker_id})
    assert row is not None and row["status"] == "paused"


@pytest.mark.asyncio
async def test_pause_offline_worker_returns_409(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, _ = await _register(authed_admin_client)
    await _set_worker_status(migrated_db_url, worker_id, "offline")

    resp = await authed_admin_client.post(f"/admin/workers/{worker_id}/pause", json={})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_resume_paused_worker(authed_admin_client: AsyncClient, migrated_db_url: str) -> None:
    worker_id, _ = await _register(authed_admin_client)
    await _set_worker_status(migrated_db_url, worker_id, "paused")

    resp = await authed_admin_client.post(f"/admin/workers/{worker_id}/resume", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "online"}


@pytest.mark.asyncio
async def test_resume_online_worker_returns_409(authed_admin_client: AsyncClient) -> None:
    worker_id, _ = await _register(authed_admin_client)

    resp = await authed_admin_client.post(f"/admin/workers/{worker_id}/resume", json={})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_pause_missing_worker_returns_404(authed_admin_client: AsyncClient) -> None:
    fake = "00000000-0000-0000-0000-000000000000"
    resp = await authed_admin_client.post(f"/admin/workers/{fake}/pause", json={})
    assert resp.status_code == 404


# === Audit and reason of worker pause/resume ==================================


@pytest.mark.asyncio
async def test_pause_worker_writes_audit_row(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, _ = await _register(authed_admin_client)

    resp = await authed_admin_client.post(f"/admin/workers/{worker_id}/pause", json={})
    assert resp.status_code == 200, resp.text

    rows = await _rows(
        migrated_db_url,
        "SELECT action, target_kind, target_id, reason, actor, payload "
        "FROM admin_actions WHERE target_id = :tid",
        {"tid": worker_id},
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "pause_worker"
    assert r["target_kind"] == "worker"
    assert r["target_id"] == worker_id
    assert r["reason"] is None
    assert r["payload"] == {"prior_status": "online"}
    # The actor comes from the session (ctx.user.id), same as in the credential
    # actions of admin_ui.py. Same assertion as
    # test_mint_enrollment_token_audited (tests/test_pats_page.py:547).
    assert r["actor"] is not None


@pytest.mark.asyncio
async def test_resume_worker_writes_audit_row(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, _ = await _register(authed_admin_client)
    await _set_worker_status(migrated_db_url, worker_id, "paused")

    resp = await authed_admin_client.post(f"/admin/workers/{worker_id}/resume", json={})
    assert resp.status_code == 200, resp.text

    rows = await _rows(
        migrated_db_url,
        "SELECT action, target_kind, actor, payload FROM admin_actions WHERE target_id = :tid",
        {"tid": worker_id},
    )
    assert len(rows) == 1
    assert rows[0]["action"] == "resume_worker"
    assert rows[0]["target_kind"] == "worker"
    assert rows[0]["payload"] == {"prior_status": "paused"}
    assert rows[0]["actor"] is not None


@pytest.mark.asyncio
async def test_pause_worker_reason_from_body(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, _ = await _register(authed_admin_client)

    resp = await authed_admin_client.post(
        f"/admin/workers/{worker_id}/pause",
        json={"reason": "draining for kernel upgrade"},
    )
    assert resp.status_code == 200, resp.text

    row = await _row(
        migrated_db_url,
        "SELECT reason FROM admin_actions WHERE target_id = :tid",
        {"tid": worker_id},
    )
    assert row is not None
    assert row["reason"] == "draining for kernel upgrade"


@pytest.mark.asyncio
async def test_pause_worker_reason_from_hx_prompt_header(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The dashboard dialog sends the reason in the `HX-Prompt` header
    (app.js injects it in htmx:configRequest), with no body."""
    worker_id, _ = await _register(authed_admin_client)

    resp = await authed_admin_client.post(
        f"/admin/workers/{worker_id}/pause",
        headers={"HX-Prompt": "noisy neighbour on this host"},
    )
    assert resp.status_code == 200, resp.text

    row = await _row(
        migrated_db_url,
        "SELECT reason FROM admin_actions WHERE target_id = :tid",
        {"tid": worker_id},
    )
    assert row is not None
    assert row["reason"] == "noisy neighbour on this host"


@pytest.mark.asyncio
async def test_pause_worker_hx_prompt_blank_keeps_reason_null(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """A blank reason (the operator confirmed without typing) is the same as no reason."""
    worker_id, _ = await _register(authed_admin_client)

    resp = await authed_admin_client.post(
        f"/admin/workers/{worker_id}/pause",
        headers={"HX-Prompt": "   "},
    )
    assert resp.status_code == 200, resp.text

    row = await _row(
        migrated_db_url,
        "SELECT reason FROM admin_actions WHERE target_id = :tid",
        {"tid": worker_id},
    )
    assert row is not None
    assert row["reason"] is None


@pytest.mark.asyncio
async def test_pause_worker_body_reason_wins_over_header(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, _ = await _register(authed_admin_client)

    resp = await authed_admin_client.post(
        f"/admin/workers/{worker_id}/pause",
        json={"reason": "from body"},
        headers={"HX-Prompt": "from header"},
    )
    assert resp.status_code == 200, resp.text

    row = await _row(
        migrated_db_url,
        "SELECT reason FROM admin_actions WHERE target_id = :tid",
        {"tid": worker_id},
    )
    assert row is not None
    assert row["reason"] == "from body"


@pytest.mark.asyncio
async def test_resume_worker_reason_from_body(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Resume shares `_PauseBody` with pause, so it accepts `reason`."""
    worker_id, _ = await _register(authed_admin_client)
    await _set_worker_status(migrated_db_url, worker_id, "paused")

    resp = await authed_admin_client.post(
        f"/admin/workers/{worker_id}/resume",
        json={"reason": "maintenance window over"},
    )
    assert resp.status_code == 200, resp.text

    row = await _row(
        migrated_db_url,
        "SELECT reason FROM admin_actions WHERE target_id = :tid",
        {"tid": worker_id},
    )
    assert row is not None
    assert row["reason"] == "maintenance window over"


@pytest.mark.asyncio
async def test_pause_worker_body_reason_blank_keeps_reason_null(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """A blank `reason` in the body is the same as no reason, just as via the
    header: without this normalization the JSON API would persist ''/'   ' where
    the header path persists NULL, leaving two semantics for "no reason"
    depending on the transport."""
    worker_id, _ = await _register(authed_admin_client)

    resp = await authed_admin_client.post(
        f"/admin/workers/{worker_id}/pause",
        json={"reason": "   "},
    )
    assert resp.status_code == 200, resp.text

    row = await _row(
        migrated_db_url,
        "SELECT reason FROM admin_actions WHERE target_id = :tid",
        {"tid": worker_id},
    )
    assert row is not None
    assert row["reason"] is None


@pytest.mark.asyncio
async def test_pause_worker_body_reason_is_stripped(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The body gets the same strip as the header: same reason, same row."""
    worker_id, _ = await _register(authed_admin_client)

    resp = await authed_admin_client.post(
        f"/admin/workers/{worker_id}/pause",
        json={"reason": "  draining for kernel upgrade  "},
    )
    assert resp.status_code == 200, resp.text

    row = await _row(
        migrated_db_url,
        "SELECT reason FROM admin_actions WHERE target_id = :tid",
        {"tid": worker_id},
    )
    assert row is not None
    assert row["reason"] == "draining for kernel upgrade"


@pytest.mark.asyncio
async def test_pause_worker_blank_body_reason_falls_back_to_header(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """A blank body does not "win" over the header: blank = absent, so the
    fallback to `HX-Prompt` still applies."""
    worker_id, _ = await _register(authed_admin_client)

    resp = await authed_admin_client.post(
        f"/admin/workers/{worker_id}/pause",
        json={"reason": "   "},
        headers={"HX-Prompt": "from header"},
    )
    assert resp.status_code == 200, resp.text

    row = await _row(
        migrated_db_url,
        "SELECT reason FROM admin_actions WHERE target_id = :tid",
        {"tid": worker_id},
    )
    assert row is not None
    assert row["reason"] == "from header"


@pytest.mark.asyncio
async def test_pause_worker_hx_prompt_percent_decoded(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The dialog sends `HX-Prompt` percent-encoded (UTF-8): setRequestHeader
    throws on characters outside Latin-1, so a reason with an emoji or
    typographic quotes would kill the request before it is sent. The server
    percent-decodes; plain text without `%` passes through intact (unquote is
    the identity)."""
    worker_id, _ = await _register(authed_admin_client)

    resp = await authed_admin_client.post(
        f"/admin/workers/{worker_id}/pause",
        headers={"HX-Prompt": "ma%C3%B1ana%20mantenci%C3%B3n%20%F0%9F%94%A7"},
    )
    assert resp.status_code == 200, resp.text

    row = await _row(
        migrated_db_url,
        "SELECT reason FROM admin_actions WHERE target_id = :tid",
        {"tid": worker_id},
    )
    assert row is not None
    assert row["reason"] == "mañana mantención 🔧"


# === Race between the status read and the UPDATE =============================
# The handler validates the status with a SELECT and only then runs the
# UPDATE, which re-checks the status in its WHERE. If housekeeping (or another
# operator) changes the status between the two, the UPDATE affects 0 rows:
# without checking the RETURNING, the handler would answer 200 and audit an
# action that never happened. The real race is a window of milliseconds between
# concurrent transactions, impossible to interleave deterministically over
# HTTP: it is simulated by patching the SELECT's SQL so it returns a stale
# status while the DB already holds the final one, which is exactly the state
# the race produces.


@pytest.mark.asyncio
async def test_pause_worker_lost_race_returns_409_without_audit(
    authed_admin_client: AsyncClient,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orcaster_server.api import admin_workers

    worker_id, _ = await _register(authed_admin_client)
    await _set_worker_status(migrated_db_url, worker_id, "offline")
    # Stale read: the guard sees 'online' although the DB already says 'offline'.
    monkeypatch.setattr(
        admin_workers,
        "_SQL_GET_WORKER_STATUS",
        text(
            "SELECT 'online' FROM workers WHERE id = :id AND deleted_at IS NULL "
            "AND workspace_id = :workspace_id"
        ),
    )

    resp = await authed_admin_client.post(f"/admin/workers/{worker_id}/pause", json={})

    assert resp.status_code == 409, resp.text
    # Neither an audit row nor a state change: a 200 with an audit would be a
    # false fact in a log whose contract is to record facts.
    rows = await _rows(
        migrated_db_url,
        "SELECT id FROM admin_actions WHERE target_id = :tid",
        {"tid": worker_id},
    )
    assert rows == []
    row = await _row(
        migrated_db_url, "SELECT status FROM workers WHERE id = :id", {"id": worker_id}
    )
    assert row is not None
    assert row["status"] == "offline"


@pytest.mark.asyncio
async def test_resume_worker_lost_race_returns_409_without_audit(
    authed_admin_client: AsyncClient,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orcaster_server.api import admin_workers

    worker_id, _ = await _register(authed_admin_client)
    # DB already 'online' (e.g. another operator resumed first); the stale read
    # says 'paused'.
    monkeypatch.setattr(
        admin_workers,
        "_SQL_GET_WORKER_STATUS",
        text(
            "SELECT 'paused' FROM workers WHERE id = :id AND deleted_at IS NULL "
            "AND workspace_id = :workspace_id"
        ),
    )

    resp = await authed_admin_client.post(f"/admin/workers/{worker_id}/resume", json={})

    assert resp.status_code == 409, resp.text
    rows = await _rows(
        migrated_db_url,
        "SELECT id FROM admin_actions WHERE target_id = :tid",
        {"tid": worker_id},
    )
    assert rows == []


@pytest.mark.asyncio
async def test_delete_offline_worker_soft_deletes(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """DELETE is a soft-delete, not a hard-delete. The row persists
    to preserve the `task_executions` history (FK CASCADE)."""
    worker_id, _ = await _register(authed_admin_client)
    await _set_worker_status(migrated_db_url, worker_id, "offline")

    resp = await authed_admin_client.delete(f"/admin/workers/{worker_id}")
    assert resp.status_code == 204

    row = await _row(
        migrated_db_url,
        "SELECT id::text AS id, deleted_at FROM workers WHERE id=:id",
        {"id": worker_id},
    )
    assert row is not None, "soft-delete must preserve the row"
    assert row["deleted_at"] is not None, "deleted_at must be set"


@pytest.mark.asyncio
async def test_delete_online_worker_without_force_returns_409(
    authed_admin_client: AsyncClient,
) -> None:
    worker_id, _ = await _register(authed_admin_client)

    resp = await authed_admin_client.delete(f"/admin/workers/{worker_id}")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_delete_online_worker_with_force_soft_cancels_executions(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """`?force=true` keeps the semantics of soft-cancelling active
    executions BEFORE marking the worker as soft-deleted."""
    worker_id, key = await _register(authed_admin_client)
    # Create a queued task so the worker claims it on poll.
    await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    # Give the worker an active execution ('claimed') via poll.
    # ('running' is no longer produced; the poll leaves the execution in
    # 'claimed', which counts as an active state here.)
    poll = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert poll.status_code == 200, poll.text

    # The worker has an active execution. DELETE with force.
    resp = await authed_admin_client.delete(f"/admin/workers/{worker_id}?force=true")
    assert resp.status_code == 204, resp.text

    # Worker soft-deleted (the row survives).
    wrow = await _row(
        migrated_db_url,
        "SELECT deleted_at FROM workers WHERE id=:id",
        {"id": worker_id},
    )
    assert wrow is not None
    assert wrow["deleted_at"] is not None
    # Active executions: none remain alive.
    active = await _rows(
        migrated_db_url,
        (
            "SELECT 1 FROM task_executions "
            " WHERE worker_id = :wid AND status IN ('assigned','claimed','running')"
        ),
        {"wid": worker_id},
    )
    assert active == [], "force=true must soft-cancel active executions"


@pytest.mark.asyncio
async def test_delete_worker_cancels_push_tasks(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """In the same tx as the soft-delete, pending push tasks
    whose target is the deleted worker are cancelled with
    `cancelled_reason='target_worker_deleted'`. Pull tasks are NOT touched."""
    worker_id, _ = await _register(authed_admin_client)
    # Push task against the worker (delivery_mode='push' + target='worker:<id>').
    push_create = await authed_admin_client.post(
        "/admin/tasks",
        json={
            "target": "1",
            "delivery_mode": "push",
            "target_worker_id": worker_id,
        },
    )
    assert push_create.status_code == 200, push_create.text
    push_task_id = push_create.json()["task_id"]

    # Pull task (target='1', unrestricted): must NOT be touched.
    pull_create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert pull_create.status_code == 200
    pull_task_id = pull_create.json()["task_id"]

    # Set the worker offline so force is not required.
    await _set_worker_status(migrated_db_url, worker_id, "offline")
    resp = await authed_admin_client.delete(f"/admin/workers/{worker_id}")
    assert resp.status_code == 204

    push_row = await _row(
        migrated_db_url,
        "SELECT status::text AS status, cancelled_reason FROM tasks WHERE id=:id",
        {"id": push_task_id},
    )
    assert push_row is not None
    assert push_row["status"] == "cancelled"
    assert push_row["cancelled_reason"] == "target_worker_deleted"

    pull_row = await _row(
        migrated_db_url,
        "SELECT status::text AS status FROM tasks WHERE id=:id",
        {"id": pull_task_id},
    )
    assert pull_row is not None
    assert pull_row["status"] == "queued"


@pytest.mark.asyncio
async def test_delete_worker_does_not_cancel_pool_tasks(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Tasks with a `target_worker_pool` that includes the deleted
    worker are NOT cancelled (other workers in the pool can still take them)."""
    w1_id, _ = await _register(authed_admin_client, hostname="w1")
    w2_id, _ = await _register(authed_admin_client, hostname="w2")
    # Task with a two-worker pool (syntax `workers:<u1>,<u2>`).
    create = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": f"workers:{w1_id},{w2_id}"},
    )
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    await _set_worker_status(migrated_db_url, w1_id, "offline")
    resp = await authed_admin_client.delete(f"/admin/workers/{w1_id}")
    assert resp.status_code == 204

    row = await _row(
        migrated_db_url,
        "SELECT status::text AS status FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["status"] == "queued"


@pytest.mark.asyncio
async def test_delete_worker_idempotent(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """A second DELETE on an already soft-deleted worker is
    idempotent (answers 204 silently, with no side effects or
    extra admin_actions)."""
    worker_id, _ = await _register(authed_admin_client)
    await _set_worker_status(migrated_db_url, worker_id, "offline")

    r1 = await authed_admin_client.delete(f"/admin/workers/{worker_id}")
    assert r1.status_code == 204
    # Capture the original deleted_at to verify it is NOT overwritten.
    first_row = await _row(
        migrated_db_url,
        "SELECT deleted_at FROM workers WHERE id=:id",
        {"id": worker_id},
    )
    assert first_row is not None and first_row["deleted_at"] is not None
    first_deleted_at = first_row["deleted_at"]

    # Second DELETE: idempotent (silent no-op, 204 all the same).
    r2 = await authed_admin_client.delete(f"/admin/workers/{worker_id}")
    assert r2.status_code == 204

    # The row is still soft-deleted with the original deleted_at (not rewritten).
    row = await _row(
        migrated_db_url,
        "SELECT deleted_at FROM workers WHERE id=:id",
        {"id": worker_id},
    )
    assert row is not None
    assert row["deleted_at"] == first_deleted_at

    # Only ONE row in admin_actions (the second DELETE does not audit).
    audits = await _rows(
        migrated_db_url,
        "SELECT 1 FROM admin_actions WHERE target_id = :tid AND action = 'delete_worker'",
        {"tid": worker_id},
    )
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_soft_deleted_worker_not_listed(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """GET /admin/workers does not list soft-deleted workers."""
    worker_id, _ = await _register(authed_admin_client)
    await _set_worker_status(migrated_db_url, worker_id, "offline")
    await authed_admin_client.delete(f"/admin/workers/{worker_id}")

    resp = await authed_admin_client.get("/admin/workers")
    assert resp.status_code == 200
    assert worker_id not in resp.text

    # Also the modal selector's json endpoint.
    resp_json = await authed_admin_client.get("/admin/workers.json")
    assert resp_json.status_code == 200
    ids = [w["id"] for w in resp_json.json()]
    assert worker_id not in ids


@pytest.mark.asyncio
async def test_soft_deleted_worker_detail_shows_badge(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The soft-deleted worker's detail view is still accessible and
    shows the 'deleted' badge. Mutating actions are hidden."""
    worker_id, _ = await _register(authed_admin_client)
    await _set_worker_status(migrated_db_url, worker_id, "offline")
    await authed_admin_client.delete(f"/admin/workers/{worker_id}")

    resp = await authed_admin_client.get(f"/admin/workers/{worker_id}")
    assert resp.status_code == 200
    body = resp.text
    assert "deleted" in body.lower()
    # The Delete button must not be rendered (mutating action hidden).
    assert "hx-delete=" not in body


@pytest.mark.asyncio
async def test_soft_deleted_worker_poll_returns_410(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Endpoints authenticated by worker token answer 410 Gone
    when the authenticated worker is soft-deleted."""
    worker_id, key = await _register(authed_admin_client)
    await _set_worker_status(migrated_db_url, worker_id, "offline")
    await authed_admin_client.delete(f"/admin/workers/{worker_id}")

    poll = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert poll.status_code == 410, poll.text
    assert "soft-deleted" in poll.text.lower()

    goodbye = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/goodbye",
        headers={"X-API-Key": key},
    )
    assert goodbye.status_code == 410


# === cancel + propagation ===================================================


@pytest.mark.asyncio
async def test_cancel_task_with_active_execution_propagates(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Cancel propagates to the worker's active execution.

    The poll leaves the execution in 'claimed'. Cancel propagates equally to
    active executions (`assigned`/`claimed`/`running`). The cancel signal to the
    worker travels over `/poll` (covered in test_poll_api.py).
    """
    worker_id, key = await _register(authed_admin_client)
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    poll = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert poll.status_code == 200, poll.text
    # The execution stays 'claimed' (active state).

    cancel = await authed_admin_client.post(
        f"/admin/tasks/{task_id}/cancel", json={"reason": "user asked"}
    )
    assert cancel.status_code == 200, cancel.text
    assert cancel.json() == {"status": "cancelled"}

    trow = await _row(
        migrated_db_url,
        "SELECT status, cancelled_reason FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert trow is not None
    assert trow["status"] == "cancelled"
    assert trow["cancelled_reason"] == "user asked"

    erow = await _row(
        migrated_db_url,
        "SELECT status FROM task_executions WHERE task_id=:tid AND worker_id=:wid",
        {"tid": task_id, "wid": worker_id},
    )
    assert erow is not None and erow["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_terminal_task_returns_409(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]
    await _set_task_status(migrated_db_url, task_id, "completed")

    resp = await authed_admin_client.post(f"/admin/tasks/{task_id}/cancel", json={})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_cancel_missing_task_returns_404(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.post("/admin/tasks/9999/cancel", json={})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cancel_without_body_works(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    resp = await authed_admin_client.post(f"/admin/tasks/{task_id}/cancel", json={})
    assert resp.status_code == 200

    trow = await _row(
        migrated_db_url,
        "SELECT status, cancelled_reason FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert trow is not None
    assert trow["status"] == "cancelled"
    assert trow["cancelled_reason"] is None


# === audit log of admin actions ==============================================
# Every destructive action must persist a row in `admin_actions` *in the same
# transaction* as the effect. Rejections (409/404) must NOT write.


@pytest.mark.asyncio
async def test_audit_logs_delete_worker_offline(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, _ = await _register(authed_admin_client)
    await _set_worker_status(migrated_db_url, worker_id, "offline")

    resp = await authed_admin_client.delete(f"/admin/workers/{worker_id}")
    assert resp.status_code == 204

    rows = await _rows(
        migrated_db_url,
        "SELECT action, target_kind, target_id, reason, actor, payload "
        "FROM admin_actions WHERE target_id = :tid",
        {"tid": worker_id},
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "delete_worker"
    assert r["target_kind"] == "worker"
    assert r["target_id"] == worker_id
    assert r["reason"] is None
    # The actor comes from the session (ctx.user.id), same as pause_worker and
    # resume_worker.
    assert r["actor"] is not None
    assert r["payload"] == {"force": False, "prior_status": "offline"}


@pytest.mark.asyncio
async def test_audit_logs_delete_worker_force(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, _ = await _register(authed_admin_client)

    resp = await authed_admin_client.delete(f"/admin/workers/{worker_id}?force=true")
    assert resp.status_code == 204

    rows = await _rows(
        migrated_db_url,
        "SELECT action, payload FROM admin_actions WHERE target_id = :tid",
        {"tid": worker_id},
    )
    assert len(rows) == 1
    assert rows[0]["action"] == "delete_worker"
    assert rows[0]["payload"] == {"force": True, "prior_status": "online"}


@pytest.mark.asyncio
async def test_audit_does_not_log_failed_delete_worker(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, _ = await _register(authed_admin_client)

    # online + no force → 409, must not write.
    resp = await authed_admin_client.delete(f"/admin/workers/{worker_id}")
    assert resp.status_code == 409

    rows = await _rows(
        migrated_db_url,
        "SELECT 1 FROM admin_actions WHERE target_id = :tid",
        {"tid": worker_id},
    )
    assert rows == []


@pytest.mark.asyncio
async def test_audit_logs_cancel_task_with_reason(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    resp = await authed_admin_client.post(
        f"/admin/tasks/{task_id}/cancel",
        json={"reason": "operator stopped run"},
    )
    assert resp.status_code == 200

    rows = await _rows(
        migrated_db_url,
        "SELECT action, target_kind, target_id, reason, payload "
        "FROM admin_actions WHERE target_id = :tid",
        {"tid": str(task_id)},
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "cancel_task"
    assert r["target_kind"] == "task"
    assert r["target_id"] == str(task_id)
    assert r["reason"] == "operator stopped run"
    assert r["payload"] is None


@pytest.mark.asyncio
async def test_audit_logs_cancel_task_without_reason(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    resp = await authed_admin_client.post(f"/admin/tasks/{task_id}/cancel", json={})
    assert resp.status_code == 200

    rows = await _rows(
        migrated_db_url,
        "SELECT reason FROM admin_actions WHERE target_id = :tid",
        {"tid": str(task_id)},
    )
    assert len(rows) == 1
    assert rows[0]["reason"] is None


@pytest.mark.asyncio
async def test_audit_does_not_log_failed_cancel(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]
    await _set_task_status(migrated_db_url, task_id, "completed")

    resp = await authed_admin_client.post(f"/admin/tasks/{task_id}/cancel", json={})
    assert resp.status_code == 409

    rows = await _rows(
        migrated_db_url,
        "SELECT 1 FROM admin_actions WHERE target_id = :tid",
        {"tid": str(task_id)},
    )
    assert rows == []


@pytest.mark.asyncio
async def test_audit_logs_delete_task(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]
    await _set_task_status(migrated_db_url, task_id, "completed")

    resp = await authed_admin_client.delete(f"/admin/tasks/{task_id}")
    assert resp.status_code == 204

    rows = await _rows(
        migrated_db_url,
        "SELECT action, target_kind, target_id, payload FROM admin_actions WHERE target_id = :tid",
        {"tid": str(task_id)},
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "delete_task"
    assert r["target_kind"] == "task"
    assert r["payload"] == {"prior_status": "completed"}


@pytest.mark.asyncio
async def test_audit_does_not_log_failed_delete_task(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    # queued → not terminal → 409
    resp = await authed_admin_client.delete(f"/admin/tasks/{task_id}")
    assert resp.status_code == 409

    rows = await _rows(
        migrated_db_url,
        "SELECT 1 FROM admin_actions WHERE target_id = :tid",
        {"tid": str(task_id)},
    )
    assert rows == []


# === Bulk cancel / delete =======================================


async def _make_task(client: AsyncClient) -> int:
    r = await client.post("/admin/tasks", json={"target": "1"})
    assert r.status_code == 200, r.text
    return int(r.json()["task_id"])


@pytest.mark.asyncio
async def test_bulk_cancel_mixed_terminal_nonterminal_and_missing(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    queued_a = await _make_task(authed_admin_client)
    queued_b = await _make_task(authed_admin_client)
    done = await _make_task(authed_admin_client)
    await _set_task_status(migrated_db_url, done, "completed")

    missing = 9_999_999

    resp = await authed_admin_client.post(
        "/admin/tasks/bulk/cancel",
        json={"task_ids": [queued_a, queued_b, done, missing], "reason": "cleanup"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert sorted(body["cancelled"]) == sorted([queued_a, queued_b])
    assert [s["id"] for s in body["skipped"]] == [done]
    assert "already terminal: completed" in body["skipped"][0]["reason"]
    assert body["not_found"] == [missing]

    rows = await _rows(
        migrated_db_url,
        "SELECT id, status, cancelled_reason FROM tasks WHERE id = ANY(:ids) ORDER BY id",
        {"ids": [queued_a, queued_b]},
    )
    for r in rows:
        assert r["status"] == "cancelled"
        assert r["cancelled_reason"] == "cleanup"


@pytest.mark.asyncio
async def test_bulk_cancel_propagates_to_active_executions(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """`bulk/cancel` soft-cancels the worker's active execution ('claimed').

    The execution stays in 'claimed'; `_SQL_BULK_CANCEL_EXECUTIONS` covers
    `assigned`/`claimed`/`running`. The cancel signal to the worker travels over
    `/poll` (covered in test_poll_api.py).
    """
    worker_id, key = await _register(authed_admin_client)
    task_id = await _make_task(authed_admin_client)
    poll = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert poll.status_code == 200

    resp = await authed_admin_client.post(
        "/admin/tasks/bulk/cancel",
        json={"task_ids": [task_id]},
    )
    assert resp.status_code == 200
    assert resp.json()["cancelled"] == [task_id]

    erow = await _row(
        migrated_db_url,
        "SELECT status FROM task_executions WHERE task_id=:tid AND worker_id=:wid",
        {"tid": task_id, "wid": worker_id},
    )
    assert erow is not None and erow["status"] == "cancelled"


@pytest.mark.asyncio
async def test_bulk_cancel_audit_log_one_row_per_affected(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    a = await _make_task(authed_admin_client)
    b = await _make_task(authed_admin_client)
    done = await _make_task(authed_admin_client)
    await _set_task_status(migrated_db_url, done, "completed")

    resp = await authed_admin_client.post(
        "/admin/tasks/bulk/cancel",
        json={"task_ids": [a, b, done, 9_999_998], "reason": "x"},
    )
    assert resp.status_code == 200

    rows = await _rows(
        migrated_db_url,
        "SELECT target_id, action, reason FROM admin_actions "
        "WHERE action = 'bulk_cancel_task' ORDER BY target_id",
        {},
    )
    target_ids = {r["target_id"] for r in rows}
    assert target_ids == {str(a), str(b)}
    assert all(r["reason"] == "x" for r in rows)

    # `done` (skipped) and the nonexistent one must NOT appear.
    rows_done = await _rows(
        migrated_db_url,
        "SELECT 1 FROM admin_actions WHERE target_id = :tid",
        {"tid": str(done)},
    )
    assert rows_done == []


@pytest.mark.asyncio
async def test_bulk_cancel_reason_from_hx_prompt_header(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    a = await _make_task(authed_admin_client)
    resp = await authed_admin_client.post(
        "/admin/tasks/bulk/cancel",
        json={"task_ids": [a]},
        headers={"HX-Prompt": "via header"},
    )
    assert resp.status_code == 200
    row = await _row(
        migrated_db_url,
        "SELECT cancelled_reason FROM tasks WHERE id = :id",
        {"id": a},
    )
    assert row is not None and row["cancelled_reason"] == "via header"


@pytest.mark.asyncio
async def test_bulk_cancel_validation_errors(authed_admin_client: AsyncClient) -> None:
    # empty
    assert (
        await authed_admin_client.post("/admin/tasks/bulk/cancel", json={"task_ids": []})
    ).status_code == 422
    # > 500
    assert (
        await authed_admin_client.post(
            "/admin/tasks/bulk/cancel",
            json={"task_ids": list(range(1, 502))},
        )
    ).status_code == 422
    # duplicates
    assert (
        await authed_admin_client.post(
            "/admin/tasks/bulk/cancel",
            json={"task_ids": [1, 1, 2]},
        )
    ).status_code == 422
    # negatives
    assert (
        await authed_admin_client.post(
            "/admin/tasks/bulk/cancel",
            json={"task_ids": [-1, 2]},
        )
    ).status_code == 422


@pytest.mark.asyncio
async def test_bulk_cancel_idempotent_on_replay(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    a = await _make_task(authed_admin_client)
    payload = {"task_ids": [a], "reason": "first"}
    r1 = await authed_admin_client.post("/admin/tasks/bulk/cancel", json=payload)
    assert r1.status_code == 200 and r1.json()["cancelled"] == [a]

    r2 = await authed_admin_client.post("/admin/tasks/bulk/cancel", json=payload)
    assert r2.status_code == 200
    body = r2.json()
    assert body["cancelled"] == []
    assert [s["id"] for s in body["skipped"]] == [a]
    assert "already terminal: cancelled" in body["skipped"][0]["reason"]


@pytest.mark.asyncio
async def test_bulk_delete_mixed_terminal_nonterminal_and_missing(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    completed = await _make_task(authed_admin_client)
    failed = await _make_task(authed_admin_client)
    cancelled_t = await _make_task(authed_admin_client)
    running = await _make_task(authed_admin_client)
    await _set_task_status(migrated_db_url, completed, "completed")
    await _set_task_status(migrated_db_url, failed, "failed")
    await _set_task_status(migrated_db_url, cancelled_t, "cancelled")
    # running stays in 'queued' (not terminal); the endpoint must skip it.

    resp = await authed_admin_client.post(
        "/admin/tasks/bulk/delete",
        json={"task_ids": [completed, failed, cancelled_t, running, 9_999_997]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert sorted(body["deleted"]) == sorted([completed, failed, cancelled_t])
    skipped_ids = [s["id"] for s in body["skipped"]]
    assert skipped_ids == [running]
    assert "not terminal: queued" in body["skipped"][0]["reason"]
    assert body["not_found"] == [9_999_997]

    rows = await _rows(
        migrated_db_url,
        "SELECT id FROM tasks WHERE id = ANY(:ids)",
        {"ids": [completed, failed, cancelled_t]},
    )
    assert rows == []


@pytest.mark.asyncio
async def test_bulk_delete_audit_log_with_prior_status(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    completed = await _make_task(authed_admin_client)
    failed = await _make_task(authed_admin_client)
    running = await _make_task(authed_admin_client)
    await _set_task_status(migrated_db_url, completed, "completed")
    await _set_task_status(migrated_db_url, failed, "failed")

    resp = await authed_admin_client.post(
        "/admin/tasks/bulk/delete",
        json={"task_ids": [completed, failed, running, 9_999_996]},
    )
    assert resp.status_code == 200

    rows = await _rows(
        migrated_db_url,
        "SELECT target_id, action, payload FROM admin_actions "
        "WHERE action = 'bulk_delete_task' ORDER BY target_id",
        {},
    )
    by_target = {r["target_id"]: r for r in rows}
    assert set(by_target) == {str(completed), str(failed)}
    assert by_target[str(completed)]["payload"] == {"prior_status": "completed"}
    assert by_target[str(failed)]["payload"] == {"prior_status": "failed"}

    # running (skipped) and the nonexistent one do not audit.
    extra = await _rows(
        migrated_db_url,
        "SELECT 1 FROM admin_actions WHERE action = 'bulk_delete_task' AND target_id = ANY(:tids)",
        {"tids": [str(running), str(9_999_996)]},
    )
    assert extra == []


@pytest.mark.asyncio
async def test_bulk_delete_validation_errors(authed_admin_client: AsyncClient) -> None:
    assert (
        await authed_admin_client.post("/admin/tasks/bulk/delete", json={"task_ids": []})
    ).status_code == 422
    assert (
        await authed_admin_client.post(
            "/admin/tasks/bulk/delete",
            json={"task_ids": list(range(1, 502))},
        )
    ).status_code == 422
    assert (
        await authed_admin_client.post(
            "/admin/tasks/bulk/delete",
            json={"task_ids": [3, 3]},
        )
    ).status_code == 422
    assert (
        await authed_admin_client.post(
            "/admin/tasks/bulk/delete",
            json={"task_ids": [0]},
        )
    ).status_code == 422


# === cache flush — clear-cache endpoint =====================================


@pytest.mark.asyncio
async def test_clear_cache_stamps_requested_and_audits(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    wid, _key = await _register(authed_admin_client)
    resp = await authed_admin_client.post(f"/admin/workers/{wid}/clear-cache")
    assert resp.status_code == 200
    assert resp.json() == {"status": "requested"}

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            req_at = (
                await conn.execute(
                    text("SELECT cache_flush_requested_at FROM workers WHERE id = :id"),
                    {"id": wid},
                )
            ).scalar_one()
            assert req_at is not None
            n = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM admin_actions "
                        "WHERE action = 'clear_worker_cache' AND target_id = :id"
                    ),
                    {"id": wid},
                )
            ).scalar_one()
            assert n == 1
            # The actor comes from the session (ctx.user.id), same as pause_worker and
            # resume_worker.
            actor = (
                await conn.execute(
                    text(
                        "SELECT actor FROM admin_actions "
                        "WHERE action = 'clear_worker_cache' AND target_id = :id"
                    ),
                    {"id": wid},
                )
            ).scalar_one()
            assert actor is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_clear_cache_404_unknown_worker(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.post(
        "/admin/workers/00000000-0000-0000-0000-000000000000/clear-cache"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_clear_cache_re_request_restamps_and_re_audits(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Requesting again re-stamps requested_at (more recent now) and audits again.

    This is the harmless idempotency: each request re-arms the pending state and
    leaves its own audit row (unlike delete_worker).
    """
    wid, _key = await _register(authed_admin_client)

    assert (await authed_admin_client.post(f"/admin/workers/{wid}/clear-cache")).status_code == 200
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            first_at = (
                await conn.execute(
                    text("SELECT cache_flush_requested_at FROM workers WHERE id = :id"),
                    {"id": wid},
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    assert (await authed_admin_client.post(f"/admin/workers/{wid}/clear-cache")).status_code == 200
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            second_at = (
                await conn.execute(
                    text("SELECT cache_flush_requested_at FROM workers WHERE id = :id"),
                    {"id": wid},
                )
            ).scalar_one()
            n = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM admin_actions "
                        "WHERE action = 'clear_worker_cache' AND target_id = :id"
                    ),
                    {"id": wid},
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    assert second_at >= first_at  # re-stamped (monotonic now())
    assert n == 2  # each request leaves its own audit row


@pytest.mark.asyncio
async def test_bulk_delete_idempotent_on_replay(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    a = await _make_task(authed_admin_client)
    await _set_task_status(migrated_db_url, a, "completed")

    r1 = await authed_admin_client.post(
        "/admin/tasks/bulk/delete",
        json={"task_ids": [a]},
    )
    assert r1.status_code == 200 and r1.json()["deleted"] == [a]

    r2 = await authed_admin_client.post(
        "/admin/tasks/bulk/delete",
        json={"task_ids": [a]},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["deleted"] == []
    assert body["not_found"] == [a]
    assert body["skipped"] == []


# === bulk retry =============================================================


@pytest.mark.asyncio
async def test_bulk_retry_mixed_failed_nonfailed_and_missing(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Unsuccessful terminal tasks (`failed`/`cancelled`) are retried; successful
    terminal tasks (`completed`) and non-terminal ones (`queued`) fall into
    `skipped` with reason `not failed/cancelled: <status>` (stricter than the
    individual retry, which accepts any terminal); nonexistent IDs →
    `not_found`."""
    failed_a = await _make_task(authed_admin_client)
    failed_b = await _make_task(authed_admin_client)
    completed = await _make_task(authed_admin_client)
    cancelled_t = await _make_task(authed_admin_client)
    queued = await _make_task(authed_admin_client)
    await _set_task_status(migrated_db_url, failed_a, "failed")
    await _set_task_status(migrated_db_url, failed_b, "failed")
    await _set_task_status(migrated_db_url, completed, "completed")
    await _set_task_status(migrated_db_url, cancelled_t, "cancelled")
    # `queued` stays in its creation state (not terminal).

    missing = 9_999_995

    resp = await authed_admin_client.post(
        "/admin/tasks/bulk/retry",
        json={"task_ids": [failed_a, failed_b, completed, cancelled_t, queued, missing]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert sorted(body["retried"]) == sorted([failed_a, failed_b, cancelled_t])
    skipped_by_id = {s["id"]: s["reason"] for s in body["skipped"]}
    assert set(skipped_by_id) == {completed, queued}
    assert skipped_by_id[completed] == "not failed/cancelled: completed"
    assert skipped_by_id[queued] == "not failed/cancelled: queued"
    assert body["not_found"] == [missing]

    # The failed and cancelled ones went back to `queued`; the rest was NOT mutated.
    rows = await _rows(
        migrated_db_url,
        "SELECT id, status::text AS status FROM tasks WHERE id = ANY(:ids) ORDER BY id",
        {"ids": [failed_a, failed_b, cancelled_t]},
    )
    assert all(r["status"] == "queued" for r in rows)
    for tid, expected in (
        (completed, "completed"),
        (queued, "queued"),
    ):
        row = await _row(
            migrated_db_url,
            "SELECT status::text AS status FROM tasks WHERE id=:id",
            {"id": tid},
        )
        assert row is not None and row["status"] == expected


@pytest.mark.asyncio
async def test_bulk_retry_validation_errors(authed_admin_client: AsyncClient) -> None:
    # empty
    assert (
        await authed_admin_client.post("/admin/tasks/bulk/retry", json={"task_ids": []})
    ).status_code == 422
    # > 500
    assert (
        await authed_admin_client.post(
            "/admin/tasks/bulk/retry",
            json={"task_ids": list(range(1, 502))},
        )
    ).status_code == 422
    # duplicates
    assert (
        await authed_admin_client.post(
            "/admin/tasks/bulk/retry",
            json={"task_ids": [1, 1, 2]},
        )
    ).status_code == 422
    # zero
    assert (
        await authed_admin_client.post(
            "/admin/tasks/bulk/retry",
            json={"task_ids": [0]},
        )
    ).status_code == 422
    # negatives
    assert (
        await authed_admin_client.post(
            "/admin/tasks/bulk/retry",
            json={"task_ids": [-1, 2]},
        )
    ).status_code == 422


@pytest.mark.asyncio
async def test_bulk_retry_audit_log_one_row_per_retried(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """One `action='bulk_retry_task'` row per retried task, with a payload
    mirroring the individual retry; skipped and not_found do NOT audit
    (convention: rejections are not persisted)."""
    failed_a = await _make_task(authed_admin_client)
    failed_b = await _make_task(authed_admin_client)
    completed = await _make_task(authed_admin_client)
    await _set_task_status(migrated_db_url, failed_a, "failed")
    await _set_task_status(migrated_db_url, failed_b, "failed")
    await _set_task_status(migrated_db_url, completed, "completed")

    missing = 9_999_994
    resp = await authed_admin_client.post(
        "/admin/tasks/bulk/retry",
        json={"task_ids": [failed_a, failed_b, completed, missing]},
    )
    assert resp.status_code == 200, resp.text
    assert sorted(resp.json()["retried"]) == sorted([failed_a, failed_b])

    rows = await _rows(
        migrated_db_url,
        "SELECT target_id, target_kind, payload FROM admin_actions "
        "WHERE action = 'bulk_retry_task' ORDER BY target_id",
        {},
    )
    by_target = {r["target_id"]: r for r in rows}
    assert set(by_target) == {str(failed_a), str(failed_b)}
    for tid in (failed_a, failed_b):
        r = by_target[str(tid)]
        assert r["target_kind"] == "task"
        payload = r["payload"]
        assert payload["prior_status"] == "failed"
        assert payload["force_same_worker"] is False
        # count-mode without retarget: original targeting (claim, no worker).
        assert payload["delivery_mode"] == "claim"
        assert payload["target_worker_id"] is None
        assert payload["attempt_number"] == 1
        assert payload["retry_count"] == 1

    # `completed` (skipped) and the nonexistent one must NOT appear.
    extra = await _rows(
        migrated_db_url,
        "SELECT 1 FROM admin_actions WHERE action = 'bulk_retry_task' AND target_id = ANY(:tids)",
        {"tids": [str(completed), str(missing)]},
    )
    assert extra == []
