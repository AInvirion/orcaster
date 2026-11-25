"""Matching of `tasks.eligibility.capabilities` against `workers.capabilities`.

Two blocks:

1. Input validation (no DB): worker registration and task creation reject
   with 422 anything the matcher cannot evaluate. `explicit > implicit`: a
   malformed requirement is not persisted only to "silently not match"; it is
   rejected right in the caller's face.
2. Dispatch (with DB): the shared SQL predicate excludes the worker that does
   not qualify, in the three paths that offer work (open-claim count, directed
   push and `all_available` snapshot).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server.schemas.tasks import AdminCreateTaskRequest
from orcaster_server.schemas.workers import WorkerRegisterRequest
from tests.conftest import mint_pat_via_app, mint_token_via_app

REGISTER = {
    "hostname": "w",
    "os": "linux",
    "arch": "x86_64",
    "ram_mb": 1024,
    "disk_mb": 10000,
    "tags": [],
    "capabilities": {},
}


async def _register(
    client: AsyncClient,
    hostname: str = "w",
    capabilities: dict | None = None,
) -> tuple[str, str]:
    token = await mint_token_via_app()
    resp = await client.post(
        "/v1/workers/register",
        json={
            **REGISTER,
            "hostname": hostname,
            "capabilities": capabilities if capabilities is not None else {},
            "enrollment_token": token,
        },
    )
    assert resp.status_code == 200, resp.text
    j = resp.json()
    return j["worker_id"], j["api_key"]


async def _poll(client: AsyncClient, worker_id: str, api_key: str) -> dict[str, Any]:
    resp = await client.post(
        f"/v1/workers/{worker_id}/poll",
        headers={"X-API-Key": api_key},
        json={"status": "idle"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _exec_worker_ids(db_url: str, task_id: int) -> set[str]:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT worker_id FROM task_executions WHERE task_id = :t"),
                    {"t": task_id},
                )
            ).all()
        return {str(r[0]) for r in rows}
    finally:
        await engine.dispose()


async def _force_eligibility(db_url: str, task_id: int, raw_json: str) -> None:
    """Write `eligibility` bypassing the endpoint validation.

    Simulates a row persisted before the endpoint validated the shape,
    when the column accepted any JSON.
    """
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET eligibility = CAST(:e AS jsonb) WHERE id = :t"),
                {"e": raw_json, "t": task_id},
            )
    finally:
        await engine.dispose()


# --- 1. Input validation ----------------------------------------------


@pytest.mark.parametrize(
    "capabilities",
    [
        {},
        {"model": True},
        {"model": False},
        {"model_context": 8192},
        {"model_context": 8192.5},
        {"model": True, "model_context": 8192, "cache_retained": False},
        {"gpu.count": 2, "os-family": 1, "_private": True},
    ],
)
def test_worker_capabilities_valid(capabilities: dict) -> None:
    req = WorkerRegisterRequest(
        enrollment_token="t", hostname="h", os="linux", arch="x86_64", capabilities=capabilities
    )
    assert req.capabilities == capabilities


@pytest.mark.parametrize(
    "capabilities",
    [
        {"model": "llama"},  # string: deliberately excluded (there is no total order)
        {"model": None},
        {"model": ["a"]},
        {"model": {"nested": True}},
        {"model ctx": True},  # the name cannot be expressed in a requirement
        {"model>=1": True},
        {"": True},
        {"1model": True},
        # `$` in `re.match` would accept the trailing `\n`; Postgres does not.
        {"model\n": True},
        # NaN/Infinity are not valid JSON for JSONB: 422, not 500 on the INSERT.
        {"model_context": float("nan")},
        {"model_context": float("inf")},
        {"model_context": float("-inf")},
    ],
)
def test_worker_capabilities_invalid(capabilities: dict) -> None:
    with pytest.raises(ValidationError):
        WorkerRegisterRequest(
            enrollment_token="t",
            hostname="h",
            os="linux",
            arch="x86_64",
            capabilities=capabilities,
        )


@pytest.mark.parametrize(
    "eligibility",
    [
        {},
        {"capabilities": []},
        {"capabilities": ["model"]},
        {"capabilities": ["model", "model_context>=5200"]},
        {"capabilities": ["a<=1", "b==2", "c>3", "d<4", "e>=-5.5"]},
        # Non-`capabilities` keys: still free-form, uninterpreted JSON.
        {"gpu": True, "min_ram_mb": 1024},
    ],
)
def test_eligibility_valid(eligibility: dict) -> None:
    req = AdminCreateTaskRequest(eligibility=eligibility)
    assert req.eligibility == eligibility


@pytest.mark.parametrize(
    "eligibility",
    [
        {"capabilities": {"docker": True}},  # object: old form from the DDL sketch
        {"capabilities": "model"},
        {"capabilities": [123]},
        {"capabilities": [None]},
        {"capabilities": ["model_context>=abc"]},
        {"capabilities": ["model_context>>5200"]},
        {"capabilities": ["model context"]},
        {"capabilities": ["model_context>="]},
        {"capabilities": [">=5200"]},
        {"capabilities": ["model_context=5200"]},
        {"capabilities": [""]},
        # Python/Postgres divergence on the trailing `\n`: the `$` of
        # `re.match` accepted it and `regexp_match` did not, so the task was created
        # with 200 and never dispatched.
        {"capabilities": ["model\n"]},
        {"capabilities": ["model_context>=5200\n"]},
    ],
)
def test_eligibility_invalid(eligibility: dict) -> None:
    with pytest.raises(ValidationError):
        AdminCreateTaskRequest(eligibility=eligibility)


@pytest.mark.asyncio
async def test_create_task_rejects_malformed_requirement(
    authed_admin_client: AsyncClient,
) -> None:
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "eligibility": {"capabilities": ["model_context>=abc"]}},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_create_task_rejects_requirement_with_trailing_newline(
    authed_admin_client: AsyncClient,
) -> None:
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "eligibility": {"capabilities": ["model\n"]}},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.asyncio
async def test_register_rejects_non_finite_capability(
    authed_admin_client: AsyncClient, literal: str
) -> None:
    """`json.loads` accepts `NaN`/`Infinity`; registration must answer 422."""
    token = await mint_token_via_app()
    body = json.dumps({**REGISTER, "enrollment_token": token, "capabilities": {"model": True}})
    body = body.replace('"model": true', f'"model_context": {literal}')
    resp = await authed_admin_client.post(
        "/v1/workers/register",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_register_rejects_string_capability(authed_admin_client: AsyncClient) -> None:
    token = await mint_token_via_app()
    resp = await authed_admin_client.post(
        "/v1/workers/register",
        json={**REGISTER, "capabilities": {"model": "llama"}, "enrollment_token": token},
    )
    assert resp.status_code == 422, resp.text


# --- 2. Dispatch: open-claim count-mode -------------------------------------


@pytest.mark.asyncio
async def test_open_claim_excludes_worker_without_the_capability(
    authed_admin_client: AsyncClient,
) -> None:
    wid, key = await _register(authed_admin_client, "without-model", capabilities={})
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "eligibility": {"capabilities": ["model"]}},
    )
    assert resp.status_code == 200, resp.text
    assert (await _poll(authed_admin_client, wid, key))["task"] is None


@pytest.mark.asyncio
async def test_open_claim_delivers_to_the_worker_with_the_capability(
    authed_admin_client: AsyncClient,
) -> None:
    wid, key = await _register(authed_admin_client, "with-model", capabilities={"model": True})
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "eligibility": {"capabilities": ["model"]}},
    )
    task_id = resp.json()["task_id"]
    body = await _poll(authed_admin_client, wid, key)
    assert body["task"] is not None
    assert body["task"]["task_id"] == task_id


@pytest.mark.asyncio
async def test_open_claim_capability_false_does_not_qualify(
    authed_admin_client: AsyncClient,
) -> None:
    wid, key = await _register(authed_admin_client, "model-off", capabilities={"model": False})
    await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "eligibility": {"capabilities": ["model"]}},
    )
    assert (await _poll(authed_admin_client, wid, key))["task"] is None


@pytest.mark.parametrize(
    ("worker_context", "qualifies"),
    [(4096, False), (5199, False), (5200, True), (8192, True)],
)
@pytest.mark.asyncio
async def test_open_claim_numeric_comparison(
    authed_admin_client: AsyncClient, worker_context: int, qualifies: bool
) -> None:
    wid, key = await _register(
        authed_admin_client,
        "ctx",
        capabilities={"model": True, "model_context": worker_context},
    )
    await authed_admin_client.post(
        "/admin/tasks",
        json={
            "target": "1",
            "eligibility": {"capabilities": ["model", "model_context>=5200"]},
        },
    )
    body = await _poll(authed_admin_client, wid, key)
    assert (body["task"] is not None) is qualifies


@pytest.mark.asyncio
async def test_open_claim_numeric_requirement_over_boolean_capability_does_not_qualify(
    authed_admin_client: AsyncClient,
) -> None:
    """`model_context: true` is not a number: the numeric requirement does not apply."""
    wid, key = await _register(authed_admin_client, "bool", capabilities={"model_context": True})
    await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "eligibility": {"capabilities": ["model_context>=1"]}},
    )
    assert (await _poll(authed_admin_client, wid, key))["task"] is None


@pytest.mark.asyncio
async def test_open_claim_empty_eligibility_still_matches(
    authed_admin_client: AsyncClient,
) -> None:
    """Regression: without `capabilities`, the previous behavior is preserved."""
    wid, key = await _register(authed_admin_client, "anyone", capabilities={})
    await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "eligibility": {"gpu": True}},
    )
    assert (await _poll(authed_admin_client, wid, key))["task"] is not None


@pytest.mark.asyncio
async def test_open_claim_legacy_row_with_object_capabilities_is_not_dispatched(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Row predating validation, with the object shape: fails closed, not open."""
    wid, key = await _register(authed_admin_client, "legacy", capabilities={"docker": True})
    resp = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = resp.json()["task_id"]
    await _force_eligibility(migrated_db_url, task_id, '{"capabilities": {"docker": true}}')
    assert (await _poll(authed_admin_client, wid, key))["task"] is None


# --- 2b. Dispatch: directed push --------------------------------------------


@pytest.mark.asyncio
async def test_push_respects_the_capabilities(authed_admin_client: AsyncClient) -> None:
    wid, key = await _register(authed_admin_client, "push-without-model", capabilities={})
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={
            "target": "1",
            "delivery_mode": "push",
            "target_worker_id": wid,
            "eligibility": {"capabilities": ["model"]},
        },
    )
    assert resp.status_code == 200, resp.text
    assert (await _poll(authed_admin_client, wid, key))["task"] is None


# --- 2c. Dispatch: all_available snapshot --------------------------------


@pytest.mark.asyncio
async def test_snapshot_all_available_filters_by_capabilities(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    eligible, _ = await _register(authed_admin_client, "eligible", capabilities={"model": True})
    await _register(authed_admin_client, "ineligible", capabilities={"model": False})

    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "all-available", "eligibility": {"capabilities": ["model"]}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["target_count"] == 1
    assert await _exec_worker_ids(migrated_db_url, body["task_id"]) == {eligible}


@pytest.mark.asyncio
async def test_restricted_snapshot_closes_without_eligible_worker(
    authed_admin_client: AsyncClient,
) -> None:
    """`all:<ids>` with no worker in the subset that qualifies -> `no_eligible_workers`."""
    wid, _ = await _register(authed_admin_client, "ineligible", capabilities={})
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": f"all:{wid}", "eligibility": {"capabilities": ["model"]}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert body["target_count"] == 0


# --- 3. Additional dispatch coverage -------------------------------------


@pytest.mark.parametrize(
    ("requirement", "worker_value", "qualifies"),
    [
        ("ctx<=5200", 5200, True),
        ("ctx<=5200", 5201, False),
        ("ctx==5200", 5200, True),
        ("ctx==5200", 5199, False),
        ("ctx==5200.5", 5200.5, True),
        ("ctx>5200", 5201, True),
        ("ctx>5200", 5200, False),
        ("ctx<5200", 5199, True),
        ("ctx<5200", 5200, False),
        ("ctx>=-1.5", -1.5, True),
        ("ctx>=-1.5", -2, False),
    ],
)
@pytest.mark.asyncio
async def test_open_claim_operators_evaluated_in_sql(
    authed_admin_client: AsyncClient, requirement: str, worker_value: float, qualifies: bool
) -> None:
    """Each operator is exercised in the SQL predicate, not only in the validator."""
    wid, key = await _register(authed_admin_client, "ops", capabilities={"ctx": worker_value})
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "eligibility": {"capabilities": [requirement]}},
    )
    assert resp.status_code == 200, resp.text
    body = await _poll(authed_admin_client, wid, key)
    assert (body["task"] is not None) is qualifies


@pytest.mark.parametrize(("worker_value", "qualifies"), [(0, False), (1, True), (0.5, True)])
@pytest.mark.asyncio
async def test_open_claim_presence_over_numeric_capability(
    authed_admin_client: AsyncClient, worker_value: float, qualifies: bool
) -> None:
    """Bare `name` against a number: qualifies only if it is non-zero."""
    wid, key = await _register(authed_admin_client, "num", capabilities={"gpu": worker_value})
    await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "eligibility": {"capabilities": ["gpu"]}},
    )
    body = await _poll(authed_admin_client, wid, key)
    assert (body["task"] is not None) is qualifies


@pytest.mark.asyncio
async def test_open_claim_ineligible_higher_priority_task_does_not_block(
    authed_admin_client: AsyncClient,
) -> None:
    """Starvation: the predicate is in the WHERE, so a higher-priority task
    that this worker cannot run does not block the one it can."""
    wid, key = await _register(authed_admin_client, "without-model", capabilities={})
    high = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "priority": 100, "eligibility": {"capabilities": ["model"]}},
    )
    assert high.status_code == 200, high.text
    low = await authed_admin_client.post("/admin/tasks", json={"target": "1", "priority": 0})
    assert low.status_code == 200, low.text

    body = await _poll(authed_admin_client, wid, key)
    assert body["task"] is not None
    assert body["task"]["task_id"] == low.json()["task_id"]


@pytest.mark.asyncio
async def test_target_workers_pool_respects_capabilities(
    authed_admin_client: AsyncClient,
) -> None:
    """`workers:<ids>` goes through the open-claim: being in the pool is not enough,
    the worker must also meet the requirements."""
    eligible, eligible_key = await _register(
        authed_admin_client, "eligible", capabilities={"model": True}
    )
    ineligible, ineligible_key = await _register(authed_admin_client, "ineligible", capabilities={})
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={
            "target": f"workers:{eligible},{ineligible}",
            "eligibility": {"capabilities": ["model"]},
        },
    )
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]

    assert (await _poll(authed_admin_client, ineligible, ineligible_key))["task"] is None
    body = await _poll(authed_admin_client, eligible, eligible_key)
    assert body["task"] is not None
    assert body["task"]["task_id"] == task_id


async def _complete_all_available(db_url: str, task_id: int) -> None:
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE task_executions SET status = 'completed', "
                    "started_at = now(), finished_at = now(), exit_code = 0 "
                    "WHERE task_id = :t"
                ),
                {"t": task_id},
            )
            await conn.execute(
                text("UPDATE tasks SET status = 'completed', completed_at = now() WHERE id = :t"),
                {"t": task_id},
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_retry_all_available_resnapshot_filters_by_capabilities(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The retry re-snapshot uses the same predicate as creation: the eligible
    worker that registered later gets in, the ineligible one stays out."""
    eligible_a, _ = await _register(authed_admin_client, "eligible-a", capabilities={"model": True})
    ineligible, _ = await _register(authed_admin_client, "ineligible", capabilities={})
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "all-available", "eligibility": {"capabilities": ["model"]}},
    )
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]
    assert await _exec_worker_ids(migrated_db_url, task_id) == {eligible_a}

    await _complete_all_available(migrated_db_url, task_id)
    eligible_b, _ = await _register(authed_admin_client, "eligible-b", capabilities={"model": True})

    retry = await authed_admin_client.post(f"/admin/tasks/{task_id}/retry", json={})
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "dispatching"
    assert await _exec_worker_ids(migrated_db_url, task_id) == {eligible_a, eligible_b}
    assert ineligible not in await _exec_worker_ids(migrated_db_url, task_id)


@pytest.mark.asyncio
async def test_api_v1_create_task_rejects_malformed_requirement(
    authed_admin_client: AsyncClient,
) -> None:
    token, _ = await mint_pat_via_app()
    resp = await authed_admin_client.post(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {token}"},
        json={"eligibility": {"capabilities": ["model_context>=abc"]}},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_api_v1_fork_rejects_malformed_requirement(
    authed_admin_client: AsyncClient,
) -> None:
    token, _ = await mint_pat_via_app()
    headers = {"Authorization": f"Bearer {token}"}
    parent = await authed_admin_client.post("/api/v1/tasks", headers=headers, json={})
    assert parent.status_code == 201, parent.text
    resp = await authed_admin_client.post(
        f"/api/v1/tasks/{parent.json()['task_id']}/fork",
        headers=headers,
        json={"eligibility": {"capabilities": ["model\n"]}},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_admin_fork_rejects_malformed_requirement(
    authed_admin_client: AsyncClient,
) -> None:
    parent = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert parent.status_code == 200, parent.text
    resp = await authed_admin_client.post(
        f"/admin/tasks/{parent.json()['task_id']}/fork",
        json={"target": "1", "eligibility": {"capabilities": ["model_context>>1"]}},
    )
    assert resp.status_code == 422, resp.text
