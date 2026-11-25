"""Contract tests: public `/api/v1` API, task output (log).

`GET /api/v1/tasks/{id}/output` serves the stdout/stderr of an execution via PAT,
reusing the on-demand gzip builder of the dashboard download. Inherits the
multi-tenant isolation of PatContext: cross-tenant -> 404. Attempt selection:
latest by default, `--attempt N` (1-based) to choose another.
"""

from __future__ import annotations

import json
import uuid

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


async def _create_task(client: AsyncClient, token: str, **body) -> int:
    resp = await client.post("/api/v1/tasks", headers=_bearer(token), json=body or {"payload": {}})
    assert resp.status_code == 201, resp.text
    return resp.json()["task_id"]


async def _create_workspace(name: str, slug: str) -> str:
    from orcaster_server import db as app_db
    from orcaster_server.workspaces.service import create_workspace

    sm = app_db.get_sessionmaker()
    assert sm is not None
    async with sm() as s:
        ws_id = await create_workspace(s, name=name, slug=slug)
        await s.commit()
        return ws_id


async def _seed_worker(workspace_id: str) -> str:
    """Insert a minimal worker in the given workspace; returns its id."""
    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            wid = (
                await conn.execute(
                    text(
                        "INSERT INTO workers (api_key_hash, hostname, os, arch, workspace_id) "
                        "VALUES (:h, 'host', 'linux', 'amd64', :ws) RETURNING id::text"
                    ),
                    {"h": uuid.uuid4().hex, "ws": workspace_id},
                )
            ).scalar_one()
            return str(wid)
    finally:
        await engine.dispose()


async def _seed_worker_default() -> str:
    from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID

    return await _seed_worker(DEFAULT_WORKSPACE_ID)


async def _seed_execution(task_id: int, worker_id: str, attempt: int) -> str:
    """Insert an execution and return its UUID (to seed output_events)."""
    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            exec_id = (
                await conn.execute(
                    text(
                        "INSERT INTO task_executions (task_id, worker_id, status, attempt_index) "
                        "VALUES (:t, :w, 'completed', :a) RETURNING id::text"
                    ),
                    {"t": task_id, "w": worker_id, "a": attempt},
                )
            ).scalar_one()
            return str(exec_id)
    finally:
        await engine.dispose()


async def _insert_output_events(execution_id: str, events: list[tuple[int, str, str]]) -> None:
    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            for seq, stream, txt in events:
                await conn.execute(
                    text(
                        "INSERT INTO output_events (execution_id, seq, ts, stream, text) "
                        "VALUES (CAST(:eid AS uuid), :seq, now(), :stream, :txt)"
                    ),
                    {"eid": execution_id, "seq": seq, "stream": stream, "txt": txt},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_output_text_latest_attempt(app_client: AsyncClient) -> None:
    """Without --attempt it serves the one with the highest attempt_index, as raw text."""
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)
    w = await _seed_worker_default()
    e1 = await _seed_execution(task_id, w, attempt=1)
    e2 = await _seed_execution(task_id, w, attempt=2)
    await _insert_output_events(e1, [(1, "stdout", "old\n")])
    await _insert_output_events(e2, [(1, "stdout", "one\n"), (2, "stderr", "two\n")])

    resp = await app_client.get(f"/api/v1/tasks/{task_id}/output", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-encoding"] == "gzip"
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text == "one\ntwo\n"  # httpx decompresses via Content-Encoding


@pytest.mark.asyncio
async def test_output_explicit_attempt(app_client: AsyncClient) -> None:
    """--attempt N serves exactly that attempt."""
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)
    w = await _seed_worker_default()
    e1 = await _seed_execution(task_id, w, attempt=1)
    await _seed_execution(task_id, w, attempt=2)
    await _insert_output_events(e1, [(1, "stdout", "old\n")])

    resp = await app_client.get(
        f"/api/v1/tasks/{task_id}/output", headers=_bearer(token), params={"attempt": 1}
    )
    assert resp.status_code == 200, resp.text
    assert resp.text == "old\n"


@pytest.mark.asyncio
async def test_output_attempt_with_multiple_workers_same_index(app_client: AsyncClient) -> None:
    """Fan-out: several workers share attempt_index=1. `?attempt=1` must not
    give a 500 (MultipleResultsFound); it serves one execution, deterministic per DB
    state (ORDER BY id DESC LIMIT 1). Since `task_executions.id` is a random UUID
    (gen_random_uuid), which worker wins cannot be assumed: it is enough that the
    output is that of ONE execution (no crash and no concatenation)."""
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)
    w1 = await _seed_worker_default()
    w2 = await _seed_worker_default()
    e1 = await _seed_execution(task_id, w1, attempt=1)
    e2 = await _seed_execution(task_id, w2, attempt=1)
    await _insert_output_events(e1, [(1, "stdout", "worker-one\n")])
    await _insert_output_events(e2, [(1, "stdout", "worker-two\n")])

    resp = await app_client.get(
        f"/api/v1/tasks/{task_id}/output", headers=_bearer(token), params={"attempt": 1}
    )
    assert resp.status_code == 200, resp.text
    # Random UUID -> we do not assume which wins; exactly one execution, no merge.
    assert resp.text in ("worker-one\n", "worker-two\n"), resp.text


@pytest.mark.asyncio
async def test_output_jsonl(app_client: AsyncClient) -> None:
    """format=jsonl: one JSON line per event with seq/ts/stream/text."""
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)
    w = await _seed_worker_default()
    e = await _seed_execution(task_id, w, attempt=1)
    await _insert_output_events(e, [(1, "stdout", "one\n"), (2, "stderr", "two\n")])

    resp = await app_client.get(
        f"/api/v1/tasks/{task_id}/output", headers=_bearer(token), params={"format": "jsonl"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in resp.text.splitlines()]
    assert [(ev["seq"], ev["stream"], ev["text"]) for ev in events] == [
        (1, "stdout", "one\n"),
        (2, "stderr", "two\n"),
    ]


@pytest.mark.asyncio
async def test_output_attempt_not_found(app_client: AsyncClient) -> None:
    """Nonexistent --attempt -> 404 with the uniform error model."""
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)
    w = await _seed_worker_default()
    await _seed_execution(task_id, w, attempt=1)

    resp = await app_client.get(
        f"/api/v1/tasks/{task_id}/output", headers=_bearer(token), params={"attempt": 99}
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_output_task_not_found(app_client: AsyncClient) -> None:
    """Nonexistent task -> 404."""
    token, _ = await mint_pat_via_app()
    resp = await app_client.get("/api/v1/tasks/999999/output", headers=_bearer(token))
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_output_cross_tenant_is_404(app_client: AsyncClient) -> None:
    """Task in another workspace -> 404 (indistinguishable from nonexistent)."""
    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    token_b, _ = await mint_pat_via_app(workspace_id=ws_b, email=None, label="b")
    task_id = await _create_task(app_client, token_b)

    token_a, _ = await mint_pat_via_app()
    resp = await app_client.get(f"/api/v1/tasks/{task_id}/output", headers=_bearer(token_a))
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_output_empty_when_execution_has_no_events(app_client: AsyncClient) -> None:
    """Execution without events -> 200 with an empty body (not an error)."""
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)
    w = await _seed_worker_default()
    await _seed_execution(task_id, w, attempt=1)

    resp = await app_client.get(f"/api/v1/tasks/{task_id}/output", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.text == ""


@pytest.mark.asyncio
async def test_output_empty_when_no_executions(app_client: AsyncClient) -> None:
    """Queued task with no executions yet -> empty 200."""
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)

    resp = await app_client.get(f"/api/v1/tasks/{task_id}/output", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.text == ""


@pytest.mark.asyncio
async def test_output_invalid_format_422(app_client: AsyncClient) -> None:
    """format outside the enum -> 422."""
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)
    resp = await app_client.get(
        f"/api/v1/tasks/{task_id}/output", headers=_bearer(token), params={"format": "xml"}
    )
    assert resp.status_code == 422, resp.text
