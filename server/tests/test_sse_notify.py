import json
from unittest.mock import AsyncMock

import pytest

from orcaster_server.sse_notify import DashboardNotifier


@pytest.mark.asyncio
async def test_noop_when_disabled(monkeypatch):
    from orcaster_server.config import settings

    monkeypatch.setattr(settings, "sse_enabled", False)
    executor = AsyncMock()
    await DashboardNotifier().output_appended(executor, execution_id="ex-1", workspace_id="ws-A")
    executor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_output_emits_pg_notify_with_correct_payload(monkeypatch):
    from orcaster_server.config import settings

    monkeypatch.setattr(settings, "sse_enabled", True)
    executor = AsyncMock()
    await DashboardNotifier().output_appended(executor, execution_id="ex-1", workspace_id="ws-A")
    executor.execute.assert_awaited_once()
    args, _ = executor.execute.call_args
    # args[0] is the pg_notify text(); args[1] is the params dict.
    assert "pg_notify" in str(args[0])
    params = args[1]
    assert params["ch"] == "dashboard_events"
    payload = json.loads(params["p"])
    assert payload == {
        "type": "output",
        "execution_id": "ex-1",
        "workspace_id": "ws-A",
    }


@pytest.mark.asyncio
async def test_task_changed_uses_id_key(monkeypatch):
    from orcaster_server.config import settings

    monkeypatch.setattr(settings, "sse_enabled", True)
    executor = AsyncMock()
    await DashboardNotifier().task_changed(executor, task_id=42, workspace_id="ws-A")
    _, _ = executor.execute.call_args
    args, _ = executor.execute.call_args
    payload = json.loads(args[1]["p"])
    assert payload == {"type": "task", "id": "42", "workspace_id": "ws-A"}


@pytest.mark.asyncio
async def test_worker_and_execution_changed_payloads(monkeypatch):
    from orcaster_server.config import settings

    monkeypatch.setattr(settings, "sse_enabled", True)
    executor = AsyncMock()
    n = DashboardNotifier()
    await n.worker_changed(executor, worker_id="w-1", workspace_id="ws-A")
    p1 = json.loads(executor.execute.call_args[0][1]["p"])
    assert p1 == {"type": "worker", "id": "w-1", "workspace_id": "ws-A"}
    await n.execution_changed(executor, execution_id="e-1", workspace_id="ws-A")
    p2 = json.loads(executor.execute.call_args[0][1]["p"])
    assert p2 == {"type": "execution", "id": "e-1", "workspace_id": "ws-A"}
