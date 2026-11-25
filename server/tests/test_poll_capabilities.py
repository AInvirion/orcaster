"""The worker updates its capabilities in the poll, without re-enrolling.

Registration used to be the only writer of `workers.capabilities`, and the
client always sent `{}`. Changing them required re-enrolling, which creates a
new UUID. They now travel in the poll, with the same contract as `version`:

- absent, the server leaves the stored value alone (older worker);
- present and valid, they overwrite it, and the pick of the SAME poll already
  uses them;
- present and invalid, they are ignored without a 422 (the poll is a hot path)
  and the stored value is kept.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_eligibility_capabilities import _register

MODEL_ELIGIBILITY = {"capabilities": ["model", "model_context>=5200"]}
MODEL_CAPABILITIES = {"model": True, "model_context": 8192}


async def _poll(client: AsyncClient, worker_id: str, api_key: str, **extra: Any) -> dict[str, Any]:
    resp = await client.post(
        f"/v1/workers/{worker_id}/poll",
        headers={"X-API-Key": api_key},
        json={"status": "idle", **extra},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _stored_capabilities(db_url: str, worker_id: str) -> dict:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            return (
                await conn.execute(
                    text("SELECT capabilities FROM workers WHERE id = :w"), {"w": worker_id}
                )
            ).scalar_one()
    finally:
        await engine.dispose()


async def _worker_count(db_url: str) -> int:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text("SELECT count(*) FROM workers"))).scalar_one()
    finally:
        await engine.dispose()


async def _create_model_task(client: AsyncClient) -> int:
    resp = await client.post("/admin/tasks", json={"target": "1", "eligibility": MODEL_ELIGIBILITY})
    assert resp.status_code == 200, resp.text
    return resp.json()["task_id"]


# --- Acceptance: the job with a model --------------------------------------------


@pytest.mark.asyncio
async def test_worker_that_declares_in_the_poll_takes_the_job_with_model(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Enrolled with `{}` like the production workers, it declares in the poll and
    takes the task in THAT same poll, with the same UUID and no new register."""
    wid, key = await _register(authed_admin_client, "with-model", capabilities={})
    task_id = await _create_model_task(authed_admin_client)
    assert (await _poll(authed_admin_client, wid, key))["task"] is None

    body = await _poll(authed_admin_client, wid, key, capabilities=MODEL_CAPABILITIES)

    assert body["task"] is not None
    assert body["task"]["task_id"] == task_id
    assert body["task"]["worker_id"] == wid
    assert await _stored_capabilities(migrated_db_url, wid) == MODEL_CAPABILITIES
    assert await _worker_count(migrated_db_url) == 1


@pytest.mark.parametrize(
    "capabilities",
    [
        {},
        {"model": True},
        {"model": False, "model_context": 8192},
        {"model": True, "model_context": 5199},
        {"model": True, "model_context": True},
    ],
)
@pytest.mark.asyncio
async def test_worker_without_the_capabilities_does_not_take_the_job_with_model(
    authed_admin_client: AsyncClient, capabilities: dict
) -> None:
    wid, key = await _register(authed_admin_client, "without-model", capabilities={})
    await _create_model_task(authed_admin_client)
    body = await _poll(authed_admin_client, wid, key, capabilities=capabilities)
    assert body["task"] is None


@pytest.mark.asyncio
async def test_between_two_workers_only_the_eligible_one_takes_the_job_with_model(
    authed_admin_client: AsyncClient,
) -> None:
    eligible, eligible_key = await _register(authed_admin_client, "eligible", capabilities={})
    plain, plain_key = await _register(authed_admin_client, "plain", capabilities={})
    task_id = await _create_model_task(authed_admin_client)

    # The plain worker polls first and declares nothing: it does not get the task.
    assert (await _poll(authed_admin_client, plain, plain_key))["task"] is None
    body = await _poll(authed_admin_client, eligible, eligible_key, capabilities=MODEL_CAPABILITIES)
    assert body["task"]["task_id"] == task_id


# --- Regression: tasks without requirements still go to any worker ------------


@pytest.mark.parametrize(
    ("declared", "eligibility"),
    [
        (None, None),  # older worker, task without `eligibility`
        (MODEL_CAPABILITIES, None),  # worker with capabilities, task without them
        ({}, None),
        (None, {}),
        (MODEL_CAPABILITIES, {}),
        ({}, {"gpu": True}),  # other eligibility keys still have no reader
    ],
)
@pytest.mark.asyncio
async def test_task_without_capabilities_still_matches_any_worker(
    authed_admin_client: AsyncClient, declared: dict | None, eligibility: dict | None
) -> None:
    """Tasks that do not declare `eligibility.capabilities` still match any
    worker: declaring capabilities on a worker cannot take ordinary tasks away
    from it."""
    wid, key = await _register(authed_admin_client, "anyone", capabilities={})
    task_body: dict[str, Any] = {"target": "1"}
    if eligibility is not None:
        task_body["eligibility"] = eligibility
    resp = await authed_admin_client.post("/admin/tasks", json=task_body)
    assert resp.status_code == 200, resp.text

    extra = {} if declared is None else {"capabilities": declared}
    body = await _poll(authed_admin_client, wid, key, **extra)
    assert body["task"] is not None
    assert body["task"]["task_id"] == resp.json()["task_id"]


# --- Write semantics ------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_without_capabilities_keeps_the_stored_ones(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Older worker (does not send the field): keeps what it declared at enrolment."""
    wid, key = await _register(authed_admin_client, "old", capabilities=MODEL_CAPABILITIES)
    await _poll(authed_admin_client, wid, key)
    assert await _stored_capabilities(migrated_db_url, wid) == MODEL_CAPABILITIES


@pytest.mark.asyncio
async def test_poll_with_null_keeps_the_stored_ones(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    wid, key = await _register(authed_admin_client, "null", capabilities=MODEL_CAPABILITIES)
    await _poll(authed_admin_client, wid, key, capabilities=None)
    assert await _stored_capabilities(migrated_db_url, wid) == MODEL_CAPABILITIES


@pytest.mark.asyncio
async def test_poll_with_empty_map_clears_them(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Only an explicit `{}` clears the stored ones. Removing `ORCH_CAPABILITIES`
    from the environment does not send the field and keeps them
    (`test_poll_sin_capabilities_...`)."""
    wid, key = await _register(authed_admin_client, "clears", capabilities=MODEL_CAPABILITIES)
    await _create_model_task(authed_admin_client)
    body = await _poll(authed_admin_client, wid, key, capabilities={})
    assert body["task"] is None
    assert await _stored_capabilities(migrated_db_url, wid) == {}


@pytest.mark.asyncio
async def test_poll_replaces_the_whole_map(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Replacement, not merge: a capability the worker stops declaring is dropped."""
    wid, key = await _register(authed_admin_client, "reemplazo", capabilities=MODEL_CAPABILITIES)
    await _poll(authed_admin_client, wid, key, capabilities={"gpu": 1})
    assert await _stored_capabilities(migrated_db_url, wid) == {"gpu": 1}


@pytest.mark.parametrize(
    "capabilities",
    [
        {"model": "llama"},
        {"model": None},
        {"model": ["a"]},
        {"model": {"nested": True}},
        {"model ctx": True},
        {"model\n": True},
        "model",
        ["model"],
        42,
        True,
    ],
)
@pytest.mark.asyncio
async def test_poll_with_invalid_capabilities_does_not_return_422_and_keeps(
    authed_admin_client: AsyncClient,
    migrated_db_url: str,
    capabilities: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 422 in the poll would leave the worker retrying forever with a body that
    never changes: out of service over a typo. It is ignored, logged and kept."""
    wid, key = await _register(authed_admin_client, "typo", capabilities=MODEL_CAPABILITIES)
    task_id = await _create_model_task(authed_admin_client)

    with caplog.at_level("WARNING", logger="orcaster_server.api.poll"):
        body = await _poll(authed_admin_client, wid, key, capabilities=capabilities)

    assert await _stored_capabilities(migrated_db_url, wid) == MODEL_CAPABILITIES
    assert body["task"]["task_id"] == task_id
    assert any(wid in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.asyncio
async def test_poll_with_non_finite_capability_does_not_return_422_or_500(
    authed_admin_client: AsyncClient, migrated_db_url: str, literal: str
) -> None:
    """`json.loads` accepts `NaN`; writing it to JSONB would be a 500 in the UPDATE."""
    wid, key = await _register(authed_admin_client, "nan", capabilities=MODEL_CAPABILITIES)
    raw = json.dumps({"status": "idle", "capabilities": {"model_context": 1}})
    raw = raw.replace('"model_context": 1', f'"model_context": {literal}')
    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key, "Content-Type": "application/json"},
        content=raw,
    )
    assert resp.status_code == 200, resp.text
    assert await _stored_capabilities(migrated_db_url, wid) == MODEL_CAPABILITIES
