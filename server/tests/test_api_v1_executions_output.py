# server/tests/test_api_v1_executions_output.py
"""Tests for the GET /api/v1/executions/{id}/output endpoint."""

import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import DEFAULT_WORKSPACE_ID, mint_pat_via_app
from tests.test_api_v1_insights_failures import _seed_failed_task, _seed_worker
from tests.test_api_v1_tasks_read import _bearer, _db_url


@pytest.mark.asyncio
async def test_execution_output_jsonl(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    _, exec_id = await _seed_failed_task(
        app_client,
        token,
        worker,
        exit_code=1,
        stderr_lines=["boom one", "boom two"],
        stdout_lines=["hello"],
        command_line="example-tool run",
    )
    resp = await app_client.get(
        f"/api/v1/executions/{exec_id}/output?format=jsonl", headers=_bearer(token)
    )
    assert resp.status_code == 200, resp.text
    lines = [json.loads(ln) for ln in resp.text.splitlines() if ln.strip()]
    streams = [(e["stream"], e["text"].rstrip("\n")) for e in lines]
    assert ("command", "example-tool run") in streams
    assert ("stdout", "hello") in streams
    assert ("stderr", "boom two") in streams


@pytest.mark.asyncio
async def test_execution_output_text(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    _, exec_id = await _seed_failed_task(
        app_client, token, worker, exit_code=1, stderr_lines=["only error line"]
    )
    resp = await app_client.get(
        f"/api/v1/executions/{exec_id}/output?format=text", headers=_bearer(token)
    )
    assert resp.status_code == 200, resp.text
    assert "only error line" in resp.text


@pytest.mark.asyncio
async def test_execution_output_bad_uuid_404(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    resp = await app_client.get("/api/v1/executions/not-a-uuid/output", headers=_bearer(token))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_execution_output_unknown_404(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    resp = await app_client.get(f"/api/v1/executions/{uuid.uuid4()}/output", headers=_bearer(token))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_execution_output_cross_workspace_404(app_client: AsyncClient) -> None:
    """An execution in workspace A is 404 when requested with a PAT from workspace B."""
    # Workspace A: default workspace; seed task + execution.
    token_a, _ = await mint_pat_via_app(workspace_id=DEFAULT_WORKSPACE_ID)
    worker_a = await _seed_worker(workspace_id=DEFAULT_WORKSPACE_ID)
    _, exec_id = await _seed_failed_task(
        app_client, token_a, worker_a, exit_code=1, stderr_lines=["ws-a error"]
    )

    # Workspace B: insert a new row into workspaces and mint a PAT for it.
    ws_b_id = str(uuid.uuid4())
    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO workspaces (id, name, slug) VALUES (:id, :name, :slug)"),
                {"id": ws_b_id, "name": "Workspace B", "slug": f"workspace-b-{ws_b_id[:8]}"},
            )
    finally:
        await engine.dispose()

    token_b, _ = await mint_pat_via_app(workspace_id=ws_b_id)

    # The execution belongs to workspace A; with workspace B's PAT it must be 404.
    resp = await app_client.get(f"/api/v1/executions/{exec_id}/output", headers=_bearer(token_b))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_execution_output_invalid_format_422(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    _, exec_id = await _seed_failed_task(app_client, token, worker, exit_code=1, stderr_lines=["x"])
    resp = await app_client.get(
        f"/api/v1/executions/{exec_id}/output?format=csv", headers=_bearer(token)
    )
    assert resp.status_code == 422
