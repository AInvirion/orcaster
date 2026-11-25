"""SSE pilot tests for the output fragment.

Verify that:
- With sse_enabled=True the poller partial uses an SSE trigger instead of polling.
- With sse_enabled=False the partial keeps `every 2s` (fallback).
- With sse_enabled=True the output page declares sse-connect="/admin/events?topics=output".
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import mint_token_via_app

# ---------------------------------------------------------------------------
# Seeding helpers (test_dashboard.py pattern)
# ---------------------------------------------------------------------------

REGISTER = {
    "hostname": "sse-pilot-worker",
    "os": "linux",
    "arch": "x86_64",
    "ram_mb": 1024,
    "disk_mb": 10000,
    "tags": [],
    "capabilities": {},
}


async def _register(client: AsyncClient, hostname: str) -> str:
    token = await mint_token_via_app()
    resp = await client.post(
        "/v1/workers/register",
        json={**REGISTER, "hostname": hostname, "enrollment_token": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["worker_id"]


async def _create_task(client: AsyncClient, command: str = "echo sse-pilot") -> int:
    resp = await client.post(
        "/admin/tasks",
        json={
            "target": "1",
            "payload": {"commands": [command], "requirements": []},
        },
    )
    assert resp.status_code == 200, resp.text
    return int(resp.json()["task_id"])


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


# ---------------------------------------------------------------------------
# Fixture: seed a task + a live execution (running)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def seeded_live_execution(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> tuple[int, str, str]:
    """Seed a task with a task_execution in `running` state.

    Returns (task_id, worker_id, execution_id).
    """
    worker_id = await _register(authed_admin_client, hostname="sse-live-worker")
    task_id = await _create_task(authed_admin_client, command="echo sse-live")
    # Insert a live execution (no finished_at or exit_code, to simulate running).
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO task_executions (task_id, worker_id, status) VALUES (:tid, :wid, 'running')",
        {"tid": task_id, "wid": worker_id},
    )
    row = await _row(
        migrated_db_url,
        "SELECT id::text AS id FROM task_executions WHERE task_id=:tid AND worker_id=:wid",
        {"tid": task_id, "wid": worker_id},
    )
    assert row is not None, "The task_execution was not found after the INSERT"
    execution_id = row["id"]
    return task_id, worker_id, execution_id


# ---------------------------------------------------------------------------
# Focused tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_output_poll_uses_sse_trigger_when_enabled(
    authed_admin_client: AsyncClient,
    seeded_live_execution: tuple[int, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With sse_enabled=True the partial uses an SSE hx-trigger, not polling every 2s."""
    from orcaster_server.config import settings

    monkeypatch.setattr(settings, "sse_enabled", True)
    task_id, worker_id, execution_id = seeded_live_execution
    resp = await authed_admin_client.get(
        f"/admin/tasks/{task_id}/executions/{worker_id}/output?partial=1&after=0"
    )
    assert resp.status_code == 200
    assert f"sse:output-{execution_id}" in resp.text
    # The poller also listens for the execution's status change.
    assert f"sse:execution-{execution_id}" in resp.text
    assert "every 2s" not in resp.text


@pytest.mark.asyncio
async def test_output_poll_falls_back_to_polling_when_disabled(
    authed_admin_client: AsyncClient,
    seeded_live_execution: tuple[int, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With sse_enabled=False the partial keeps every 2s (polling fallback)."""
    from orcaster_server.config import settings

    monkeypatch.setattr(settings, "sse_enabled", False)
    task_id, worker_id, execution_id = seeded_live_execution
    resp = await authed_admin_client.get(
        f"/admin/tasks/{task_id}/executions/{worker_id}/output?partial=1&after=0"
    )
    assert resp.status_code == 200
    assert "every 2s" in resp.text
    assert "sse:output-" not in resp.text


@pytest.mark.asyncio
async def test_output_page_declares_sse_connect_when_enabled(
    authed_admin_client: AsyncClient,
    seeded_live_execution: tuple[int, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With sse_enabled=True the output page declares
    sse-connect="/admin/events?topics=output,execution" (consumes output-* for the
    lines and execution-* for the status change)."""
    from orcaster_server.config import settings

    monkeypatch.setattr(settings, "sse_enabled", True)
    task_id, worker_id, _ = seeded_live_execution
    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}/executions/{worker_id}/output")
    assert resp.status_code == 200
    assert 'sse-connect="/admin/events?topics=output,execution"' in resp.text


@pytest.mark.asyncio
async def test_task_output_handler_emits_output_notify(monkeypatch):
    """task_output calls dashboard_notifier.output_appended with the resolved
    execution_id and the worker's workspace_id, on the request's session."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from orcaster_server.api import tasks as tasks_module

    # Spy on the singleton's method (same object the handler uses).
    spy = AsyncMock()
    monkeypatch.setattr("orcaster_server.sse_notify.dashboard_notifier.output_appended", spy)

    # Mock session: execute(...).first() returns the resolved execution.
    resolved = MagicMock()
    resolved.first.return_value = SimpleNamespace(id="ex-1")
    session = MagicMock()
    session.execute = AsyncMock(return_value=resolved)
    session.commit = AsyncMock()

    worker = SimpleNamespace(id="w-1", workspace_id="ws-A")
    body = SimpleNamespace(
        lease_token="lease-xyz",
        events=[SimpleNamespace(seq=1, ts="2026-01-01T00:00:00Z", stream="stdout", text="hi")],
    )

    await tasks_module.task_output(task_id=7, body=body, worker=worker, session=session)

    spy.assert_awaited_once()
    _, kwargs = spy.call_args
    assert kwargs["execution_id"] == "ex-1"
    assert kwargs["workspace_id"] == "ws-A"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_task_complete_handler_emits_execution_notify(monkeypatch):
    """task_complete calls dashboard_notifier.execution_changed with the id of the
    execution just closed (RETURNING id) and the worker's workspace_id,
    so the Execution output page refreshes its status without further output."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from orcaster_server.api import tasks as tasks_module

    spy = AsyncMock()
    monkeypatch.setattr("orcaster_server.sse_notify.dashboard_notifier.execution_changed", spy)
    # Path without auto-retry: the close is directly terminal.
    monkeypatch.setattr(tasks_module, "_maybe_auto_retry", AsyncMock(return_value=False))

    # The handler runs a payload SELECT BEFORE _SQL_COMPLETE (to
    # classify with `classify_terminal`); here we return "no row" (task not
    # found by this generic mock) so the payload is None and the
    # classification falls to the default. _SQL_COMPLETE.first() returns the row
    # with the id of the closed execution. Path without auto-retry: a
    # third execute follows for `_SQL_MAYBE_CLOSE_TASK` (result unused).
    payload_resolved = MagicMock()
    payload_resolved.first.return_value = None
    resolved = MagicMock()
    resolved.first.return_value = SimpleNamespace(id="ex-9")
    close_resolved = MagicMock()
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[payload_resolved, resolved, close_resolved])
    session.commit = AsyncMock()

    worker = SimpleNamespace(id="w-1", workspace_id="ws-A")
    body = SimpleNamespace(
        lease_token="lease-xyz",
        status="failed",
        exit_code=1,
        cancelled_reason=None,
    )

    await tasks_module.task_complete(task_id=7, body=body, worker=worker, session=session)

    spy.assert_awaited_once()
    _, kwargs = spy.call_args
    assert kwargs["execution_id"] == "ex-9"
    assert kwargs["workspace_id"] == "ws-A"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_task_complete_handler_emits_execution_notify_on_auto_retry(monkeypatch):
    """task_complete emits execution_changed with the id of the execution just
    closed EVEN IF the close triggers auto-retry.

    When `_maybe_auto_retry` returns True the current execution was already
    terminal via `_SQL_COMPLETE`, the task is re-enqueued and the handler does
    `session.commit()` returning early WITHOUT touching `_SQL_MAYBE_CLOSE_TASK`.
    The SSE signal goes before that branch, so it must have been emitted anyway:
    this guards the placement "before any branch" against refactors that
    move the call and leave the retry path without a status notification.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from orcaster_server.api import tasks as tasks_module

    spy = AsyncMock()
    monkeypatch.setattr("orcaster_server.sse_notify.dashboard_notifier.execution_changed", spy)
    # Auto-retry path: the close is retryable and budget remains → re-enqueue.
    monkeypatch.setattr(tasks_module, "_maybe_auto_retry", AsyncMock(return_value=True))

    # Payload SELECT before _SQL_COMPLETE (see previous test).
    payload_resolved = MagicMock()
    payload_resolved.first.return_value = None
    resolved = MagicMock()
    resolved.first.return_value = SimpleNamespace(id="ex-9")
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[payload_resolved, resolved])
    session.commit = AsyncMock()

    worker = SimpleNamespace(id="w-1", workspace_id="ws-A")
    body = SimpleNamespace(
        lease_token="lease-xyz",
        status="failed",
        exit_code=1,
        cancelled_reason=None,
    )

    await tasks_module.task_complete(task_id=7, body=body, worker=worker, session=session)

    spy.assert_awaited_once()
    _, kwargs = spy.call_args
    assert kwargs["execution_id"] == "ex-9"
    assert kwargs["workspace_id"] == "ws-A"
    session.commit.assert_awaited_once()
