"""Tests for the GET /api/v1/insights/signature/tasks endpoint.

Drill-down: lists ALL task ids of a normalized signature within the
window, not just the sample capped at 10 in `/insights/failures`. Reuses the
outcomes seeding harness (parametrizes status/outcome per task).
"""

import uuid

import pytest
from httpx import AsyncClient

from orcaster_server.error_signatures import signature_hash
from tests.conftest import DEFAULT_WORKSPACE_ID, mint_pat_via_app
from tests.test_admin_enforcement import _create_workspace
from tests.test_api_v1_insights_outcomes import _seed_terminal_task, _seed_worker
from tests.test_api_v1_tasks_read import _bearer

_BOOM = "runtimeerror: boom at step <n>"


async def _seed_boom(client: AsyncClient, token: str, worker: str, n: int, **kw) -> int:
    """Seed a failed task whose normalized signature is `_BOOM`."""
    task_id, _ = await _seed_terminal_task(
        client,
        token,
        worker,
        status="failed",
        exit_code=1,
        stderr_lines=[f"RuntimeError: boom at step {n}"],
        **kw,
    )
    return task_id


@pytest.mark.asyncio
async def test_signature_tasks_returns_all_ids_beyond_sample(app_client: AsyncClient) -> None:
    # 12 tasks with the SAME signature: /insights/failures only samples 10; the
    # drill-down must return all 12.
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    ids = [await _seed_boom(app_client, token, worker, i) for i in range(12)]

    sample = (await app_client.get("/api/v1/insights/failures", headers=_bearer(token))).json()[
        "by_signature"
    ][0]
    assert len(sample["sample_task_ids"]) == 10  # sample cap

    h = signature_hash(_BOOM)
    resp = await app_client.get(
        f"/api/v1/insights/signature/tasks?signature_hash={h}&limit=1000",
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["signature"] == _BOOM
    assert body["signature_hash"] == h
    assert sorted(body["task_ids"]) == sorted(ids)
    assert body["next_after"] is None


@pytest.mark.asyncio
async def test_signature_tasks_selectable_by_text(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    ids = [await _seed_boom(app_client, token, worker, i) for i in range(3)]

    resp = await app_client.get(
        "/api/v1/insights/signature/tasks",
        params={"signature": _BOOM, "limit": 1000},
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert sorted(body["task_ids"]) == sorted(ids)
    assert body["signature_hash"] == signature_hash(_BOOM)


@pytest.mark.asyncio
async def test_signature_tasks_paginates_by_id_descending(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    ids = [await _seed_boom(app_client, token, worker, i) for i in range(5)]

    h = signature_hash(_BOOM)
    collected: list[int] = []
    after: int | None = None
    for _ in range(10):  # safety bound against an infinite loop
        url = f"/api/v1/insights/signature/tasks?signature_hash={h}&limit=2"
        if after is not None:
            url += f"&after={after}"
        body = (await app_client.get(url, headers=_bearer(token))).json()
        page = body["task_ids"]
        assert page == sorted(page, reverse=True)  # descending id
        collected.extend(page)
        after = body["next_after"]
        if after is None:
            break

    assert sorted(collected) == sorted(ids)
    assert len(collected) == len(set(collected))  # no duplicates across pages


@pytest.mark.asyncio
async def test_signature_tasks_requires_exactly_one_selector(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    none_sel = await app_client.get("/api/v1/insights/signature/tasks", headers=_bearer(token))
    assert none_sel.status_code == 422, none_sel.text
    both_sel = await app_client.get(
        "/api/v1/insights/signature/tasks",
        params={"signature": _BOOM, "signature_hash": signature_hash(_BOOM)},
        headers=_bearer(token),
    )
    assert both_sel.status_code == 422, both_sel.text


@pytest.mark.asyncio
async def test_signature_tasks_rejects_malformed_hash(app_client: AsyncClient) -> None:
    # A malformed hash is a client typo: 422, not a scan that silently returns [].
    # A WELL-formed hash that does not exist does return [] (another test).
    token, _ = await mint_pat_via_app()
    for bad in ("not-a-hash", "ABCDEF0123456789", "abc123", "0" * 15, "0" * 17):
        resp = await app_client.get(
            "/api/v1/insights/signature/tasks",
            params={"signature_hash": bad},
            headers=_bearer(token),
        )
        assert resp.status_code == 422, (bad, resp.text)


@pytest.mark.asyncio
async def test_signature_tasks_matches_fallback_signature(app_client: AsyncClient) -> None:
    # Fallback signature (no stderr): must be drill-down-able just like the others.
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    task_id, _ = await _seed_terminal_task(
        app_client, token, worker, status="failed", exit_code=None, stderr_lines=[]
    )
    h = signature_hash("no-stderr:exit=null")
    body = (
        await app_client.get(
            f"/api/v1/insights/signature/tasks?signature_hash={h}", headers=_bearer(token)
        )
    ).json()
    assert body["task_ids"] == [task_id]
    assert body["signature"] == "no-stderr:exit=null"


@pytest.mark.asyncio
async def test_signature_tasks_respects_status_filter(app_client: AsyncClient) -> None:
    # One failed and one completed share a signature (same stderr). status=failed must
    # return only the failed one: same filter semantics as /insights/outcomes.
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    failed_id = await _seed_boom(app_client, token, worker, 1)
    await _seed_terminal_task(
        app_client,
        token,
        worker,
        status="completed",
        exit_code=1,
        stderr_lines=["RuntimeError: boom at step 2"],
    )
    h = signature_hash(_BOOM)
    body = (
        await app_client.get(
            f"/api/v1/insights/signature/tasks?signature_hash={h}&status=failed",
            headers=_bearer(token),
        )
    ).json()
    assert body["task_ids"] == [failed_id]
    assert body["filters"]["status"] == ["failed"]


@pytest.mark.asyncio
async def test_signature_tasks_empty_when_no_match(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    await _seed_boom(app_client, token, worker, 1)
    h = signature_hash("signature that does not exist")
    body = (
        await app_client.get(
            f"/api/v1/insights/signature/tasks?signature_hash={h}", headers=_bearer(token)
        )
    ).json()
    assert body["task_ids"] == []
    assert body["next_after"] is None
    assert body["signature_hash"] == h


@pytest.mark.asyncio
async def test_signature_tasks_cross_workspace_isolation(app_client: AsyncClient) -> None:
    # The drill-down is scoped to the PAT's workspace: A does not see B's ids.
    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    token_a, _ = await mint_pat_via_app(workspace_id=DEFAULT_WORKSPACE_ID)
    token_b, _ = await mint_pat_via_app(
        workspace_id=ws_b, email=f"b-{uuid.uuid4().hex[:8]}@example.com"
    )
    worker_b = await _seed_worker(workspace_id=ws_b)
    task_b = await _seed_boom(app_client, token_b, worker_b, 1)

    h = signature_hash(_BOOM)
    url = f"/api/v1/insights/signature/tasks?signature_hash={h}&limit=1000"

    body_a = (await app_client.get(url, headers=_bearer(token_a))).json()
    assert body_a["task_ids"] == [], f"FUGA: A ve ids de B: {body_a['task_ids']}"

    body_b = (await app_client.get(url, headers=_bearer(token_b))).json()
    assert body_b["task_ids"] == [task_b]
