"""Contract tests: public `/api/v1` API, cancel / retry / fork.

- `POST /tasks/{id}/cancel` reuses the admin cancel SQL (404/409).
- `POST /tasks/{id}/retry` reuses the logic of `admin_retry_task`.
- `POST /tasks/{id}/fork` reuses `_create_task` + **server-side deep merge** (`merge_fork_params`):
  payload/eligibility recursive deep merge, scalars + auto_retry override-if-present.
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


async def _create_workspace(name: str, slug: str) -> str:
    from orcaster_server import db as app_db
    from orcaster_server.workspaces.service import create_workspace

    sm = app_db.get_sessionmaker()
    assert sm is not None
    async with sm() as s:
        ws_id = await create_workspace(s, name=name, slug=slug)
        await s.commit()
        return ws_id


async def _create_task(client: AsyncClient, token: str, **body) -> int:
    resp = await client.post("/api/v1/tasks", headers=_bearer(token), json=body or {"payload": {}})
    assert resp.status_code == 201, resp.text
    return resp.json()["task_id"]


async def _fetch_task(task_id: int) -> dict:
    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT payload, eligibility, priority, target_mode, target_count, "
                            "auto_retry_policy, parent_task_id, claim_timeout_seconds "
                            "FROM tasks WHERE id = :id"
                        ),
                        {"id": task_id},
                    )
                )
                .mappings()
                .one()
            )
            return dict(row)
    finally:
        await engine.dispose()


def _as_dict(v) -> dict:
    return v if isinstance(v, dict) else json.loads(v)


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_task_200(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)
    resp = await app_client.post(
        f"/api/v1/tasks/{task_id}/cancel", headers=_bearer(token), json={"reason": "stop"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"task_id": task_id, "status": "cancelled"}


@pytest.mark.asyncio
async def test_cancel_task_no_body_200(app_client: AsyncClient) -> None:
    """reason is optional: without a body it still cancels."""
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)
    resp = await app_client.post(f"/api/v1/tasks/{task_id}/cancel", headers=_bearer(token))
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_cancel_task_already_terminal_409(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)
    first = await app_client.post(
        f"/api/v1/tasks/{task_id}/cancel", headers=_bearer(token), json={}
    )
    assert first.status_code == 200
    second = await app_client.post(
        f"/api/v1/tasks/{task_id}/cancel", headers=_bearer(token), json={}
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"] == "conflict"


@pytest.mark.asyncio
async def test_cancel_task_not_found_404(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    resp = await app_client.post("/api/v1/tasks/999999/cancel", headers=_bearer(token), json={})
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_cancel_task_cross_tenant_404(app_client: AsyncClient) -> None:
    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    token_b, _ = await mint_pat_via_app(workspace_id=ws_b, email=None, label="b")
    theirs = await _create_task(app_client, token_b)

    token, _ = await mint_pat_via_app()
    resp = await app_client.post(f"/api/v1/tasks/{theirs}/cancel", headers=_bearer(token), json={})
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_terminal_task_200(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)
    await app_client.post(f"/api/v1/tasks/{task_id}/cancel", headers=_bearer(token), json={})

    resp = await app_client.post(f"/api/v1/tasks/{task_id}/retry", headers=_bearer(token), json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] == task_id
    assert body["status"] in ("queued", "dispatching", "failed")
    assert body["attempt_number"] >= 1
    assert "retry_count" in body
    assert "delivery_mode" in body


@pytest.mark.asyncio
async def test_retry_non_terminal_task_409(app_client: AsyncClient) -> None:
    """A queued (non-terminal) task is not retryable -> 409."""
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)
    resp = await app_client.post(f"/api/v1/tasks/{task_id}/retry", headers=_bearer(token), json={})
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"] == "conflict"


@pytest.mark.asyncio
async def test_retry_not_found_404(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    resp = await app_client.post("/api/v1/tasks/999999/retry", headers=_bearer(token), json={})
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_retry_no_body_defaults_to_any_worker(app_client: AsyncClient) -> None:
    """Without a body (body=None) the v1 handler uses the fallback `force_same_worker=False`:
    the `body is None` branch does not crash and retries on any worker."""
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)
    await app_client.post(f"/api/v1/tasks/{task_id}/cancel", headers=_bearer(token), json={})

    resp = await app_client.post(f"/api/v1/tasks/{task_id}/retry", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] == task_id
    # Without a previous attempt started on a worker, the retry keeps the general pool.
    assert body["target_worker_id"] is None


# ---------------------------------------------------------------------------
# fork — deep-merge server-side
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fork_inherits_parent_when_no_override(app_client: AsyncClient) -> None:
    """A fork without overrides inherits payload/eligibility/priority from the parent; new id."""
    token, _ = await mint_pat_via_app()
    parent_id = await _create_task(
        app_client,
        token,
        payload={"cmd": "build", "flags": {"opt": 2}},
        eligibility={"gpu": True},
        priority=7,
    )
    resp = await app_client.post(f"/api/v1/tasks/{parent_id}/fork", headers=_bearer(token), json={})
    assert resp.status_code == 201, resp.text
    child_id = resp.json()["task_id"]
    assert child_id != parent_id
    assert resp.json()["parent_task_id"] == parent_id

    child = await _fetch_task(child_id)
    assert _as_dict(child["payload"]) == {"cmd": "build", "flags": {"opt": 2}}
    assert _as_dict(child["eligibility"]) == {"gpu": True}
    assert child["priority"] == 7
    assert child["parent_task_id"] == parent_id


@pytest.mark.asyncio
async def test_fork_deep_merges_payload(app_client: AsyncClient) -> None:
    """payload override does a recursive deep merge: the override wins, the rest is inherited."""
    token, _ = await mint_pat_via_app()
    parent_id = await _create_task(
        app_client,
        token,
        payload={"cmd": "build", "flags": {"opt": 2, "verbose": False}, "keep": 1},
    )
    resp = await app_client.post(
        f"/api/v1/tasks/{parent_id}/fork",
        headers=_bearer(token),
        json={"payload": {"flags": {"opt": 3}, "extra": "y"}},
    )
    assert resp.status_code == 201, resp.text
    child = await _fetch_task(resp.json()["task_id"])
    assert _as_dict(child["payload"]) == {
        "cmd": "build",
        "flags": {"opt": 3, "verbose": False},
        "keep": 1,
        "extra": "y",
    }


@pytest.mark.asyncio
async def test_fork_overrides_scalar(app_client: AsyncClient) -> None:
    """Scalar (priority) override-if-present; the rest is inherited."""
    token, _ = await mint_pat_via_app()
    parent_id = await _create_task(app_client, token, payload={"a": 1}, priority=1)
    resp = await app_client.post(
        f"/api/v1/tasks/{parent_id}/fork", headers=_bearer(token), json={"priority": 9}
    )
    assert resp.status_code == 201, resp.text
    child = await _fetch_task(resp.json()["task_id"])
    assert child["priority"] == 9
    assert _as_dict(child["payload"]) == {"a": 1}


@pytest.mark.asyncio
async def test_fork_parent_not_found_404(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    resp = await app_client.post("/api/v1/tasks/999999/fork", headers=_bearer(token), json={})
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_fork_cross_tenant_404(app_client: AsyncClient) -> None:
    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    token_b, _ = await mint_pat_via_app(workspace_id=ws_b, email=None, label="b")
    theirs = await _create_task(app_client, token_b)

    token, _ = await mint_pat_via_app()
    resp = await app_client.post(f"/api/v1/tasks/{theirs}/fork", headers=_bearer(token), json={})
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_fork_rejects_non_public_fields(app_client: AsyncClient) -> None:
    """The fork body is also public: delivery_mode/signed/etc -> 422."""
    token, _ = await mint_pat_via_app()
    parent_id = await _create_task(app_client, token)
    resp = await app_client.post(
        f"/api/v1/tasks/{parent_id}/fork",
        headers=_bearer(token),
        json={"delivery_mode": "push"},
    )
    assert resp.status_code == 422, resp.text


# --- Invalid payload.outcomes -> 422, not 500 ------------
#
# `to_admin_request()`/`merge_fork_params()` build the effective `AdminCreateTaskRequest`
# INSIDE the `/api/v1` handler; without the `build_admin_request` wrapper
# (orcaster_server/schemas/tasks.py) a `pydantic.ValidationError` there would propagate
# with no handler registered in `orcaster_server/main.py` and FastAPI would render it as a 500.


@pytest.mark.asyncio
async def test_create_with_invalid_outcomes_gives_422_not_500(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    bad_outcomes = {"exit_codes": {"3": {"name": "queued", "closes_as": "failed"}}}
    resp = await app_client.post(
        "/api/v1/tasks",
        headers=_bearer(token),
        json={"payload": {"outcomes": bad_outcomes}},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"] == "unprocessable_entity"
    # The message must be actionable: it mentions the field that failed.
    assert "outcomes" in json.dumps(body["detail"])


@pytest.mark.asyncio
async def test_fork_with_invalid_payload_override_gives_422_not_500(
    app_client: AsyncClient,
) -> None:
    token, _ = await mint_pat_via_app()
    parent_id = await _create_task(app_client, token)
    resp = await app_client.post(
        f"/api/v1/tasks/{parent_id}/fork",
        headers=_bearer(token),
        json={"payload": {"outcomes": "no-un-dict"}},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"] == "unprocessable_entity"
    assert "outcomes" in json.dumps(body["detail"])


@pytest.mark.asyncio
async def test_create_with_invalid_task_env_gives_422_not_500(app_client: AsyncClient) -> None:
    """Also covers the task_env/egress_allowlist handling in the v1 API
    (same mechanism: build_admin_request wraps to_admin_request())."""
    token, _ = await mint_pat_via_app()
    resp = await app_client.post(
        "/api/v1/tasks",
        headers=_bearer(token),
        json={"payload": {"task_env": "not-a-list"}},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"] == "unprocessable_entity"
    assert "task_env" in json.dumps(body["detail"])


@pytest.mark.asyncio
async def test_create_with_huge_task_env_gives_422_without_echoing_the_input(
    app_client: AsyncClient,
) -> None:
    """More than 256 entries or a name longer than 128 characters -> 422, and the
    error body does not return any of the entries."""
    token, _ = await mint_pat_via_app()
    for env in (
        [f"SECRETMARK_{i}" for i in range(257)],
        ["SECRETMARK" + "A" * 200],
    ):
        resp = await app_client.post(
            "/api/v1/tasks", headers=_bearer(token), json={"payload": {"task_env": env}}
        )
        assert resp.status_code == 422, resp.text
        assert "SECRETMARK" not in resp.text


@pytest.mark.asyncio
async def test_body_over_the_cap_gives_413_on_an_anonymous_route(app_client: AsyncClient) -> None:
    """The cap lives in the app; an anonymous route does not read an unbounded body."""
    resp = await app_client.post("/auth/login", content=b"x" * (5 * 1024 * 1024))
    assert resp.status_code == 413, resp.text
