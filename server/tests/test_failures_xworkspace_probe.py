"""Temporary probe: cross-workspace isolation of GET /api/v1/insights/failures.

Verifies empirically that a workspace A PAT does NOT see failures seeded in
workspace B, and that B's PAT does see them. Covers the gap that the comment in
test_admin_failures.py claims (incorrectly) is already covered here.
"""

import uuid

import pytest
from httpx import AsyncClient

from tests.conftest import DEFAULT_WORKSPACE_ID, mint_pat_via_app
from tests.test_admin_enforcement import _create_workspace
from tests.test_api_v1_insights_failures import _seed_failed_task, _seed_worker
from tests.test_api_v1_tasks_read import _bearer

_SECRET_B = "SecretError: leak-from-ws-b"


@pytest.mark.asyncio
async def test_failures_v1_cross_workspace_isolation(app_client: AsyncClient) -> None:
    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")

    token_a, _ = await mint_pat_via_app(workspace_id=DEFAULT_WORKSPACE_ID)
    token_b, _ = await mint_pat_via_app(
        workspace_id=ws_b, email=f"b-{uuid.uuid4().hex[:8]}@example.com"
    )

    worker_b = await _seed_worker(workspace_id=ws_b)
    await _seed_failed_task(
        app_client,
        token_b,
        worker_b,
        exit_code=7,
        stderr_lines=[_SECRET_B],
    )

    # A's PAT: must not see anything from B.
    resp_a = await app_client.get("/api/v1/insights/failures?limit=1000", headers=_bearer(token_a))
    assert resp_a.status_code == 200, resp_a.text
    body_a = resp_a.json()
    assert _SECRET_B.lower() not in resp_a.text.lower(), "LEAK: A sees the stderr of B"
    assert body_a["by_signature"] == [], f"LEAK: A sees signatures of B: {body_a['by_signature']}"
    assert body_a["total_failed_tasks"] == 0, f"LEAK: A counts failures of B: {body_a}"

    # B's PAT: must see its own failure.
    resp_b = await app_client.get("/api/v1/insights/failures?limit=1000", headers=_bearer(token_b))
    assert resp_b.status_code == 200, resp_b.text
    assert _SECRET_B.lower() in resp_b.text.lower(), "B does not see its own failure"
