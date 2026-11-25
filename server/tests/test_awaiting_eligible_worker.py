"""An `all-available` task with no eligible worker: explicit reason and a waiting mechanism.

Without this, an `all-available` task created with no eligible worker stayed
`dispatching` with `target_count=0` **forever**: the worker list is frozen
when the snapshot is materialized, so an eligible worker that joined later
never saw it, nothing closed it, and the only reason the system could give
was `no_eligible_workers`, which does not distinguish "no workers online" from "there are
workers online and none declares the capability".

Three blocks, one per acceptance criterion:

1. **Reason** (`no_eligible_reason` in the creation response, and `last_error`
   on the task that `all:<ids>` closes): one value per gate.
2. **Mechanism**: the housekeeping re-snapshot dispatches the task when an
   eligible worker joins, only once per epoch, and leaves everything else alone.
3. **Admin UI signal**: the list marks the wait and the detail says why,
   recalculated on every render.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server import housekeeping
from orcaster_server.config import Settings, settings
from orcaster_server.task_secrets.keyring import TaskSecretsKeyring
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
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

# Version that meets the task secret floor.
SUPPORTED_VERSION = "0.47.0"
API_TOKEN_VALUE = "secret_value_that_must_not_leak_0513"


async def _register(
    client: AsyncClient,
    hostname: str = "w",
    *,
    capabilities: dict | None = None,
    version: str | None = SUPPORTED_VERSION,
) -> tuple[str, str]:
    token = await mint_token_via_app()
    body: dict[str, Any] = {
        **REGISTER,
        "hostname": hostname,
        "capabilities": capabilities if capabilities is not None else {},
        "enrollment_token": token,
    }
    if version is not None:
        body["version"] = version
    resp = await client.post("/v1/workers/register", json=body)
    assert resp.status_code == 200, resp.text
    j = resp.json()
    return j["worker_id"], j["api_key"]


async def _create(client: AsyncClient, **body: Any) -> dict[str, Any]:
    resp = await client.post("/admin/tasks", json=body)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _poll(client: AsyncClient, wid: str, key: str) -> dict[str, Any]:
    resp = await client.post(
        f"/v1/workers/{wid}/poll", headers={"X-API-Key": key}, json={"status": "idle"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _rows(db_url: str, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            return [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]
    finally:
        await engine.dispose()


async def _exec(db_url: str, task_id: int) -> list[dict[str, Any]]:
    """Executions of the task, in a total order.

    `attempt_index` does not break ties: it is per (task, worker) pair, so a
    re-snapshot of a new epoch inserts another `attempt_index = 1` and two rows
    end up tied. Without `worker_id` in the `ORDER BY`, the row order is
    decided by Postgres and an assertion on the list passes on one machine and fails on
    another (this is what happened in CI).
    """
    return await _rows(
        db_url,
        "SELECT worker_id::text AS worker_id, status, attempt_index, snapshot_epoch "
        "FROM task_executions WHERE task_id = :t "
        "ORDER BY snapshot_epoch, attempt_index, worker_id",
        {"t": task_id},
    )


async def _task(db_url: str, task_id: int) -> dict[str, Any]:
    rows = await _rows(
        db_url,
        "SELECT status::text AS status, target_count, attempt_budget, snapshot_epoch, "
        "cancelled_reason, last_error FROM tasks WHERE id = :t",
        {"t": task_id},
    )
    assert len(rows) == 1
    return rows[0]


async def _exec_sql(db_url: str, sql: str, params: dict[str, Any]) -> None:
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params)
    finally:
        await engine.dispose()


async def _resnapshot(db_url: str) -> tuple[int, int]:
    """One pass of the re-snapshot, as housekeeping runs it."""
    engine = create_async_engine(db_url)
    try:
        return await housekeeping._resnapshot_awaiting_all_available(engine, Settings())
    finally:
        await engine.dispose()


@pytest.fixture
def keys_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from orcaster_server.task_secrets import keyring

    p = tmp_path / "task-secrets-keys"
    p.write_text(Fernet.generate_key().decode() + "\n")
    monkeypatch.setattr(settings, "task_secrets_keys_path", str(p))
    keyring.reset_keyring_cache()
    return p


async def _set_secret(name: str, value: str, ring: TaskSecretsKeyring | None = None) -> None:
    from orcaster_server import db as app_db
    from orcaster_server.task_secrets import keyring, service

    ring = ring or keyring.get_keyring()
    assert ring is not None
    sm = app_db.get_sessionmaker()
    assert sm is not None
    async with sm() as s:
        uid = (
            await s.execute(text("INSERT INTO users (display_name) VALUES ('Owner') RETURNING id"))
        ).scalar_one()
        await service.set_secret(
            s,
            ring,
            workspace_id=DEFAULT_WORKSPACE_ID,
            name=name,
            value=value,
            actor_user_id=str(uid),
        )
        await s.commit()


# --- 1. The reason distinguishes the gates ---------------------------------------


@pytest.mark.asyncio
async def test_with_no_worker_the_reason_is_no_workers_online(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    j = await _create(authed_admin_client, target="all-available")
    assert j["status"] == "dispatching"
    assert j["target_count"] == 0
    assert j["no_eligible_reason"] == "no_workers_online"
    assert await _exec(migrated_db_url, j["task_id"]) == []


@pytest.mark.asyncio
async def test_worker_online_without_the_capability_is_a_different_reason(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The case that must be told apart: there is an online worker, but it lacks the
    capability. Previously both cases were the same `target_count=0`."""
    await _register(authed_admin_client, "no-capability", capabilities={})
    j = await _create(
        authed_admin_client,
        target="all-available",
        eligibility={"capabilities": ["model", "model_context>=5200"]},
    )
    assert j["status"] == "dispatching"
    assert j["no_eligible_reason"] == "no_workers_with_capabilities"
    assert await _exec(migrated_db_url, j["task_id"]) == []


@pytest.mark.asyncio
async def test_eligible_worker_gated_by_disk_is_a_different_reason(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The worker meets the capability and is excluded for free disk. Saying
    "no workers with the capability" here would send the operator to enroll a
    worker that already exists."""
    wid, _ = await _register(authed_admin_client, "full", capabilities={"model": True})
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET free_disk_fraction = 0.05 WHERE id = :w",
        {"w": wid},
    )
    j = await _create(
        authed_admin_client, target="all-available", eligibility={"capabilities": ["model"]}
    )
    assert j["no_eligible_reason"] == "workers_low_on_disk"
    assert await _exec(migrated_db_url, j["task_id"]) == []


@pytest.mark.asyncio
async def test_worker_below_the_secrets_floor_is_a_different_reason(
    authed_admin_client: AsyncClient, keys_file: Path, migrated_db_url: str
) -> None:
    """The task uses a stored secret and the only worker would not receive it."""
    await _set_secret("API_TOKEN", API_TOKEN_VALUE)
    await _register(authed_admin_client, "old", version="0.46.0")
    j = await _create(
        authed_admin_client, target="all-available", payload={"task_env": ["API_TOKEN"]}
    )
    assert j["no_eligible_reason"] == "workers_below_secrets_version"
    assert await _exec(migrated_db_url, j["task_id"]) == []


@pytest.mark.asyncio
async def test_with_eligible_worker_there_is_no_reason_and_the_task_is_dispatched(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Positive control: the field is NULL when the snapshot was NOT empty, and
    the task has its pre-assignment. Without this, a `no_eligible_reason` that
    always returned a reason would pass the tests above."""
    wid, _ = await _register(authed_admin_client, "eligible", capabilities={"model": True})
    j = await _create(
        authed_admin_client, target="all-available", eligibility={"capabilities": ["model"]}
    )
    assert j["no_eligible_reason"] is None
    assert j["target_count"] == 1
    assert [(e["worker_id"], e["status"]) for e in await _exec(migrated_db_url, j["task_id"])] == [
        (wid, "assigned")
    ]


@pytest.mark.asyncio
async def test_count_mode_carries_no_reason(authed_admin_client: AsyncClient) -> None:
    """count-mode does not snapshot, so there is nothing to explain, not even when
    there is not a single worker in the workspace."""
    j = await _create(authed_admin_client, target="3")
    assert j["status"] == "queued"
    assert j["no_eligible_reason"] is None


# --- 1b. `all:<ids>` closes, and the close says which gate it was -----------------


@pytest.mark.asyncio
async def test_pool_offline_closes_failed_and_last_error_names_the_reason(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """`all:<ids>` still closes as `failed/no_eligible_workers` (the pool is
    explicit and finite). What is added is the detail, stored in
    `last_error` **measured at close time**: recalculating it when viewing the task would lie,
    because the worker may be online now."""
    wid, _ = await _register(authed_admin_client, "goes-down")
    await _exec_sql(
        migrated_db_url, "UPDATE workers SET status = 'offline' WHERE id = :w", {"w": wid}
    )
    j = await _create(authed_admin_client, target=f"all:{wid}")
    assert j["status"] == "failed"
    assert j["no_eligible_reason"] == "no_workers_online"

    row = await _task(migrated_db_url, j["task_id"])
    assert row["cancelled_reason"] == "no_eligible_workers"
    assert row["last_error"].startswith("no_eligible_workers (no_workers_online):")
    assert await _exec(migrated_db_url, j["task_id"]) == []


@pytest.mark.asyncio
async def test_pool_without_capability_closes_with_the_other_reason(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    wid, _ = await _register(authed_admin_client, "no-capability", capabilities={})
    j = await _create(
        authed_admin_client, target=f"all:{wid}", eligibility={"capabilities": ["model"]}
    )
    assert j["status"] == "failed"
    assert j["no_eligible_reason"] == "no_workers_with_capabilities"
    row = await _task(migrated_db_url, j["task_id"])
    assert row["cancelled_reason"] == "no_eligible_workers"
    assert "no_workers_with_capabilities" in row["last_error"]


# --- 2. The mechanism: housekeeping re-snapshots the wait ---------------


@pytest.mark.asyncio
async def test_worker_that_joins_later_receives_the_task(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The original defect, end to end: the task is created with no workers, the
    workers join later, and they execute it. Previously, their poll returned
    `task: null` forever.

    **Two** workers join on purpose: the task's `target_count` starts at 1
    (placeholder for the CHECK `target_count >= 1`), so with a single worker the
    nominal assertion would hold without anyone re-syncing it.
    """
    j = await _create(authed_admin_client, target="all-available")
    assert await _exec(migrated_db_url, j["task_id"]) == []

    wid, key = await _register(authed_admin_client, "arrived-late")
    other_worker, _ = await _register(authed_admin_client, "arrived-late-too")
    assert await _resnapshot(migrated_db_url) == (1, 2)

    row = await _task(migrated_db_url, j["task_id"])
    assert row["status"] == "dispatching"
    # The nominal and the budget are re-synced to the new snapshot;
    # without this the task would never close as `completed`.
    assert (row["target_count"], row["attempt_budget"]) == (2, 2)
    assert {
        (e["worker_id"], e["status"], e["snapshot_epoch"])
        for e in await _exec(migrated_db_url, j["task_id"])
    } == {
        (wid, "assigned", row["snapshot_epoch"]),
        (other_worker, "assigned", row["snapshot_epoch"]),
    }

    task = (await _poll(authed_admin_client, wid, key))["task"]
    assert task is not None and task["task_id"] == j["task_id"]


@pytest.mark.asyncio
async def test_re_snapshot_is_not_repeated_on_the_next_pass(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Bounded to once per epoch: as soon as it inserts, the epoch stops being a
    candidate. Otherwise each pass would add another attempt on the same worker."""
    j = await _create(authed_admin_client, target="all-available")
    await _register(authed_admin_client, "only")
    assert await _resnapshot(migrated_db_url) == (1, 1)
    assert await _resnapshot(migrated_db_url) == (0, 0)
    assert len(await _exec(migrated_db_url, j["task_id"])) == 1


@pytest.mark.asyncio
async def test_does_not_re_snapshot_an_epoch_that_already_has_executions(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Control: a task that WAS dispatched at creation is not a candidate.

    The case that matters is an **in-flight** task: one worker already finished and the
    other is still pre-assigned, so the task stays `dispatching` and its epoch
    has rows. Without the per-epoch `NOT EXISTS`, the pass would insert another
    pre-assignment for the worker that already finished and rewrite `target_count` to
    the size of that new snapshot, so the task would measure its close against a nominal
    that is not its fan-out.

    An epoch with a single `assigned` row does not pin this down: the claim's `NOT EXISTS`
    defense (worker with an ACTIVE execution of the same task) already
    excludes it on its own, so the test would pass with the epoch guard
    deleted.
    """
    first, key = await _register(authed_admin_client, "finishes")
    await _register(authed_admin_client, "stays-assigned")
    j = await _create(authed_admin_client, target="all-available")
    assert j["target_count"] == 2
    task = (await _poll(authed_admin_client, first, key))["task"]
    assert task is not None
    done = await authed_admin_client.post(
        f"/v1/tasks/{j['task_id']}/complete",
        headers={"X-API-Key": key},
        json={"lease_token": task["lease_token"], "status": "completed", "exit_code": 0},
    )
    assert done.status_code == 200, done.text
    assert (await _task(migrated_db_url, j["task_id"]))["status"] == "dispatching"

    assert await _resnapshot(migrated_db_url) == (0, 0)
    assert len(await _exec(migrated_db_url, j["task_id"])) == 2
    assert (await _task(migrated_db_url, j["task_id"]))["target_count"] == 2


@pytest.mark.asyncio
async def test_does_not_re_snapshot_to_a_worker_that_does_not_qualify(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Same gates as the creation snapshot: a worker that joins late and does not
    meet the capability is excluded. The wait continues, and remains legible."""
    j = await _create(
        authed_admin_client, target="all-available", eligibility={"capabilities": ["model"]}
    )
    await _register(authed_admin_client, "no-capability", capabilities={})
    assert await _resnapshot(migrated_db_url) == (0, 0)
    assert await _exec(migrated_db_url, j["task_id"]) == []

    # And when one that does meet it joins, the same pass dispatches it.
    eligible, _ = await _register(
        authed_admin_client, "with-capability", capabilities={"model": True}
    )
    assert await _resnapshot(migrated_db_url) == (1, 1)
    assert [e["worker_id"] for e in await _exec(migrated_db_url, j["task_id"])] == [eligible]


@pytest.mark.asyncio
async def test_does_not_re_snapshot_with_a_worker_of_another_workspace(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Multi-tenant isolation: a worker from another workspace does not dispatch
    the task waiting in this one, even if it is the only eligible one in the database."""
    ws_b = str(uuid.uuid4())
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO workspaces (id, name, slug) VALUES (:id, 'Workspace B', :slug)",
        {"id": ws_b, "slug": f"workspace-b-{ws_b[:8]}"},
    )
    j = await _create(authed_admin_client, target="all-available")
    token_b = await mint_token_via_app(workspace_id=ws_b)
    resp = await authed_admin_client.post(
        "/v1/workers/register",
        json={**REGISTER, "hostname": "other-tenant", "enrollment_token": token_b},
    )
    assert resp.status_code == 200, resp.text

    assert await _resnapshot(migrated_db_url) == (0, 0)
    assert await _exec(migrated_db_url, j["task_id"]) == []

    # Control: the same re-snapshot does dispatch with a worker from THIS workspace.
    own, _ = await _register(authed_admin_client, "own-tenant")
    assert await _resnapshot(migrated_db_url) == (1, 1)
    assert [e["worker_id"] for e in await _exec(migrated_db_url, j["task_id"])] == [own]


@pytest.mark.asyncio
async def test_does_not_re_snapshot_a_paused_task(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """`paused` is an operator decision: it is not dispatched behind their back."""
    j = await _create(authed_admin_client, target="all-available")
    await _register(authed_admin_client, "eligible")
    pause = await authed_admin_client.post(f"/admin/tasks/{j['task_id']}/pause")
    assert pause.status_code == 200, pause.text
    assert await _resnapshot(migrated_db_url) == (0, 0)
    assert await _exec(migrated_db_url, j["task_id"]) == []


@pytest.mark.asyncio
async def test_does_not_re_snapshot_a_task_with_a_pool(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """`all:<ids>` waits for nobody (the pool is explicit and finite),
    so the re-snapshot never touches it.

    The state is built with SQL on purpose: through the API an `all:<ids>` without a
    snapshot is born `failed`, and what must be pinned down here is the predicate,
    `target_worker_pool IS NULL`, not the path that leads to it. Without that
    predicate this task would reopen on its own.
    """
    j = await _create(authed_admin_client, target="all-available")
    assert await _exec(migrated_db_url, j["task_id"]) == []
    wid, _ = await _register(authed_admin_client, "pool-member")
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET target_worker_pool = CAST(:pool AS jsonb) WHERE id = :t",
        {"t": j["task_id"], "pool": f'["{wid}"]'},
    )
    assert await _resnapshot(migrated_db_url) == (0, 0)
    assert await _exec(migrated_db_url, j["task_id"]) == []


@pytest.mark.asyncio
async def test_does_not_re_snapshot_an_expired_task(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Expired `expires_at`: the poll would no longer deliver it, so pre-assigning it
    would only leave rows that expire. (The creation body does not accept
    `expires_at`; it is written directly.)"""
    j = await _create(authed_admin_client, target="all-available")
    await _register(authed_admin_client, "eligible")
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET expires_at = now() - interval '1 minute' WHERE id = :t",
        {"t": j["task_id"]},
    )
    assert await _resnapshot(migrated_db_url) == (0, 0)
    assert await _exec(migrated_db_url, j["task_id"]) == []


@pytest.mark.asyncio
async def test_re_snapshots_the_new_epoch_of_an_empty_retry(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Retrying an `all-available` task bumps the epoch. If there is no eligible
    worker on retry, the new epoch stays empty, and that is the one
    the re-snapshot picks up, preserving the history of the old one."""
    wid, key = await _register(authed_admin_client, "first")
    j = await _create(authed_admin_client, target="all-available")
    task = (await _poll(authed_admin_client, wid, key))["task"]
    assert task is not None
    done = await authed_admin_client.post(
        f"/v1/tasks/{j['task_id']}/complete",
        headers={"X-API-Key": key},
        json={"lease_token": task["lease_token"], "status": "failed", "exit_code": 1},
    )
    assert done.status_code == 200, done.text
    await _exec_sql(
        migrated_db_url, "UPDATE workers SET status = 'offline' WHERE id = :w", {"w": wid}
    )

    retry = await authed_admin_client.post(f"/admin/tasks/{j['task_id']}/retry", json={})
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "dispatching"
    epoch = (await _task(migrated_db_url, j["task_id"]))["snapshot_epoch"]
    assert [e["snapshot_epoch"] for e in await _exec(migrated_db_url, j["task_id"])] == [epoch - 1]

    new_worker, _ = await _register(authed_admin_client, "second")
    assert await _resnapshot(migrated_db_url) == (1, 1)
    rows = await _exec(migrated_db_url, j["task_id"])
    # A set, not a list: what is asserted is which (worker, epoch) pair exists.
    # `attempt_index` is per (task, worker) pair, not per task, so both
    # rows are 1 and their relative order is a fact about nothing; asserting it
    # passes on one machine and fails on another.
    assert {e["attempt_index"] for e in rows} == {1}
    assert {(e["worker_id"], e["snapshot_epoch"]) for e in rows} == {
        (wid, epoch - 1),
        (new_worker, epoch),
    }


# --- 3. The admin UI shows the wait ---------------------------------------


@pytest.mark.asyncio
async def test_listing_flags_the_waiting_task(authed_admin_client: AsyncClient) -> None:
    await _create(authed_admin_client, target="all-available")
    resp = await authed_admin_client.get("/admin/tasks")
    assert resp.status_code == 200, resp.text
    assert "waiting for an eligible worker" in resp.text


@pytest.mark.asyncio
async def test_listing_does_not_flag_a_task_that_was_dispatched(
    authed_admin_client: AsyncClient,
) -> None:
    """Control in the other direction: with an eligible worker the row does not carry
    the marker. Without this test, a marker set on EVERY `dispatching` row would pass
    the one above."""
    await _register(authed_admin_client, "eligible")
    await _create(authed_admin_client, target="all-available")
    resp = await authed_admin_client.get("/admin/tasks")
    assert resp.status_code == 200, resp.text
    assert "waiting for an eligible worker" not in resp.text


@pytest.mark.asyncio
async def test_detail_states_the_reason_and_recomputes_it(
    authed_admin_client: AsyncClient,
) -> None:
    """The reason describes the fleet NOW, not the fleet when the task was created: the
    same task goes from "no workers online" to "none has the capability"
    when a non-matching worker joins. A reason stored at creation would keep
    saying the former."""
    j = await _create(
        authed_admin_client, target="all-available", eligibility={"capabilities": ["model"]}
    )
    first = await authed_admin_client.get(f"/admin/tasks/{j['task_id']}")
    assert first.status_code == 200, first.text
    assert 'data-reason="no_workers_online"' in first.text
    assert "Waiting for an eligible worker." in first.text

    await _register(authed_admin_client, "no-capability", capabilities={})
    second = await authed_admin_client.get(f"/admin/tasks/{j['task_id']}")
    assert second.status_code == 200, second.text
    assert 'data-reason="no_workers_with_capabilities"' in second.text
    # The requirement is named: it is what the operator needs to start a
    # worker that can serve it.
    assert "<code>model</code>" in second.text


@pytest.mark.asyncio
async def test_dispatched_task_detail_does_not_mention_waiting(
    authed_admin_client: AsyncClient,
) -> None:
    await _register(authed_admin_client, "eligible")
    j = await _create(authed_admin_client, target="all-available")
    resp = await authed_admin_client.get(f"/admin/tasks/{j['task_id']}")
    assert resp.status_code == 200, resp.text
    assert "Waiting for an eligible worker" not in resp.text


@pytest.mark.asyncio
async def test_closed_task_detail_shows_the_closing_reason(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """A closed `all:<ids>` is not recalculated: what is shown is what was measured when
    it closed."""
    wid, _ = await _register(authed_admin_client, "offline-at-creation")
    await _exec_sql(
        migrated_db_url, "UPDATE workers SET status = 'offline' WHERE id = :w", {"w": wid}
    )
    j = await _create(authed_admin_client, target=f"all:{wid}")
    assert j["status"] == "failed"
    resp = await authed_admin_client.get(f"/admin/tasks/{j['task_id']}")
    assert resp.status_code == 200, resp.text
    assert "no_eligible_workers (no_workers_online)" in resp.text
    assert "Waiting for an eligible worker" not in resp.text
