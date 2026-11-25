"""Delivery of worker-scoped secrets in `/poll`.

Each worker receives its own row with precedence over the workspace one; never
another worker's row, and it does not fail because of it (store without keys,
foreign encryption). The response names in `worker_scoped_secrets` those that
came from a worker row, and so do the audit and the delivery log. The worker
version gate still counts rows of any scope (conservative).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient
from sqlalchemy import text

from orcaster_server.config import settings
from orcaster_server.task_secrets.keyring import TaskSecretsKeyring
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from tests.test_poll_task_secrets import (  # noqa: F401 - `keys_file` is a fixture
    _create_task,
    _deliveries,
    _exec_statuses,
    _poll,
    _register,
    _task_status,
    keys_file,
)

WIDE = "ghp_workspace_value_must_not_leak_00"
W1 = "ghp_worker-a_value_must_not_leak_0001"
W2 = "ghp_worker-b_value_must_not_leak_0002"


async def _set_secret(
    name: str, value: str, *, worker_id: str | None = None, ring: TaskSecretsKeyring | None = None
) -> None:
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
            worker_id=worker_id,
        )
        await s.commit()


@pytest.mark.asyncio
async def test_each_worker_receives_its_own_value(
    authed_admin_client: AsyncClient,
    keys_file: Path,  # noqa: F811
    migrated_db_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    w1, k1 = await _register(authed_admin_client, hostname="worker-a")
    w2, k2 = await _register(authed_admin_client, hostname="worker-b")
    w3, k3 = await _register(authed_admin_client, hostname="other")
    await _set_secret("GITHUB_TOKENS", WIDE)
    await _set_secret("GITHUB_TOKENS", W1, worker_id=w1)
    await _set_secret("GITHUB_TOKENS", W2, worker_id=w2)
    payload = {"task_env": ["GITHUB_TOKENS", "API_TOKEN"]}
    t1 = await _create_task(authed_admin_client, target="1", payload=payload)
    t2 = await _create_task(authed_admin_client, target="1", payload=payload)
    t3 = await _create_task(authed_admin_client, target="1", payload=payload)

    with caplog.at_level(logging.DEBUG):
        got1 = (await _poll(authed_admin_client, w1, k1))["task"]
        got2 = (await _poll(authed_admin_client, w2, k2))["task"]
        got3 = (await _poll(authed_admin_client, w3, k3))["task"]
    assert [got1["task_id"], got2["task_id"], got3["task_id"]] == [t1, t2, t3]
    assert got1["secrets"] == {"GITHUB_TOKENS": W1}
    assert got1["worker_scoped_secrets"] == ["GITHUB_TOKENS"]
    assert got2["secrets"] == {"GITHUB_TOKENS": W2}
    assert got2["worker_scoped_secrets"] == ["GITHUB_TOKENS"]
    assert got3["secrets"] == {"GITHUB_TOKENS": WIDE}
    assert got3["worker_scoped_secrets"] == []

    assert [d["payload"] for d in await _deliveries(migrated_db_url)] == [
        {"worker_id": w1, "names": ["GITHUB_TOKENS"], "worker_scoped": ["GITHUB_TOKENS"]},
        {"worker_id": w2, "names": ["GITHUB_TOKENS"], "worker_scoped": ["GITHUB_TOKENS"]},
        {"worker_id": w3, "names": ["GITHUB_TOKENS"], "worker_scoped": []},
    ]
    delivered = [
        r.getMessage() for r in caplog.records if "task_secrets.delivered" in r.getMessage()
    ]
    assert len(delivered) == 3
    assert delivered[0].endswith("names=GITHUB_TOKENS worker_scoped=GITHUB_TOKENS")
    assert delivered[2].endswith("names=GITHUB_TOKENS")
    for r in caplog.records:
        for v in (WIDE, W1, W2):
            assert v not in r.getMessage()


@pytest.mark.asyncio
async def test_other_workers_row_is_never_delivered(
    authed_admin_client: AsyncClient,
    keys_file: Path,  # noqa: F811
    migrated_db_url: str,
) -> None:
    """Without a workspace row, a worker with no row of its own receives the task
    without that secret (it looks it up in its environment), like a name that is
    not stored."""
    w1, _ = await _register(authed_admin_client, hostname="worker-a")
    w2, k2 = await _register(authed_admin_client, hostname="worker-b")
    await _set_secret("GITHUB_TOKENS", W1, worker_id=w1)
    task_id = await _create_task(
        authed_admin_client, target="1", payload={"task_env": ["GITHUB_TOKENS"]}
    )
    task = (await _poll(authed_admin_client, w2, k2))["task"]
    assert task is not None and task["task_id"] == task_id
    assert task["secrets"] == {}
    assert task["worker_scoped_secrets"] == []
    assert await _deliveries(migrated_db_url) == []


@pytest.mark.asyncio
async def test_other_workers_undecryptable_row_does_not_fail(
    authed_admin_client: AsyncClient,
    keys_file: Path,  # noqa: F811
    migrated_db_url: str,
) -> None:
    foreign = TaskSecretsKeyring([Fernet.generate_key()])
    w1, _ = await _register(authed_admin_client, hostname="worker-a")
    w2, k2 = await _register(authed_admin_client, hostname="worker-b")
    await _set_secret("GITHUB_TOKENS", W1, worker_id=w1, ring=foreign)
    await _set_secret("GITHUB_TOKENS", WIDE)
    task_id = await _create_task(
        authed_admin_client, target="1", payload={"task_env": ["GITHUB_TOKENS"]}
    )
    task = (await _poll(authed_admin_client, w2, k2))["task"]
    assert task is not None and task["task_id"] == task_id
    assert task["secrets"] == {"GITHUB_TOKENS": WIDE}


@pytest.mark.asyncio
async def test_own_undecryptable_row_blocks_even_with_decryptable_workspace_row(
    authed_admin_client: AsyncClient,
    keys_file: Path,  # noqa: F811
    migrated_db_url: str,
) -> None:
    """The worker's row wins: if it cannot be decrypted, the task stays `queued`
    instead of silently falling back to the workspace value (no execution)."""
    foreign = TaskSecretsKeyring([Fernet.generate_key()])
    w1, k1 = await _register(authed_admin_client, hostname="worker-a")
    await _set_secret("GITHUB_TOKENS", W1, worker_id=w1, ring=foreign)
    await _set_secret("GITHUB_TOKENS", WIDE)
    task_id = await _create_task(
        authed_admin_client, target="1", payload={"task_env": ["GITHUB_TOKENS"]}
    )
    body = await _poll(authed_admin_client, w1, k1)
    assert body["task"] is None
    assert await _exec_statuses(migrated_db_url, task_id) == []
    assert await _task_status(migrated_db_url, task_id) == "queued"
    assert await _deliveries(migrated_db_url) == []


@pytest.mark.asyncio
async def test_unconfigured_store_only_blocks_applicable_rows(
    authed_admin_client: AsyncClient,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Store without keys, with scope (the task stays `queued`, no execution):
    it blocks for the worker that has its own row; another worker's row does
    not block this one."""
    from orcaster_server.task_secrets import keyring

    ring = TaskSecretsKeyring([Fernet.generate_key()])
    monkeypatch.setattr(settings, "task_secrets_keys_path", None)
    keyring.reset_keyring_cache()
    w1, k1 = await _register(authed_admin_client, hostname="worker-a")
    w2, k2 = await _register(authed_admin_client, hostname="worker-b")
    await _set_secret("GITHUB_TOKENS", W1, worker_id=w1, ring=ring)
    payload = {"task_env": ["GITHUB_TOKENS"]}
    t_a = await _create_task(authed_admin_client, target="1", payload=payload)
    t_b = await _create_task(authed_admin_client, target="1", payload=payload)

    got2 = (await _poll(authed_admin_client, w2, k2))["task"]
    assert got2 is not None and got2["task_id"] == t_a
    assert got2["secrets"] == {}

    assert (await _poll(authed_admin_client, w1, k1))["task"] is None
    assert await _exec_statuses(migrated_db_url, t_b) == []
    assert await _task_status(migrated_db_url, t_b) == "queued"


@pytest.mark.asyncio
async def test_gate_counts_other_workers_rows_conservatively(
    authed_admin_client: AsyncClient,
    keys_file: Path,  # noqa: F811
    migrated_db_url: str,
) -> None:
    """The version gate counts rows of any scope. A worker below the floor does
    not receive a task whose name only has another worker's row; it stays
    `queued` for a capable worker."""
    w1, _ = await _register(authed_admin_client, hostname="worker-a")
    old, k_old = await _register(authed_admin_client, hostname="old", version="0.46.0")
    await _set_secret("GITHUB_TOKENS", W1, worker_id=w1)
    task_id = await _create_task(
        authed_admin_client, target="1", payload={"task_env": ["GITHUB_TOKENS"]}
    )
    assert (await _poll(authed_admin_client, old, k_old, version="0.46.0"))["task"] is None
    assert await _task_status(migrated_db_url, task_id) == "queued"


def test_worker_scoped_secrets_default_and_wire_shape() -> None:
    """Additive field: `[]` by default, names only, outside the signature."""
    import uuid
    from datetime import UTC, datetime

    from orcaster_server.schemas.poll import PollTaskPayload

    p = PollTaskPayload(
        task_id=1,
        created_at=datetime.now(UTC),
        delivery_mode="claim",
        worker_id=uuid.uuid4(),
        lease_token=uuid.uuid4(),
        lease_expires_at=datetime.now(UTC),
    )
    assert p.worker_scoped_secrets == []
    assert p.model_dump(mode="json")["worker_scoped_secrets"] == []


# ---------------------------------------------------------------------------
# A refusal must not hold back work that can be delivered.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undecryptable_secret_does_not_block_tasks_with_healthy_secrets(
    authed_admin_client: AsyncClient,
    keys_file: Path,  # noqa: F811
    migrated_db_url: str,
) -> None:
    """A task whose secret cannot be decrypted does not hold back one declaring a healthy one."""
    await _set_secret("API_TOKEN", "x" * 20, ring=TaskSecretsKeyring([Fernet.generate_key()]))
    await _set_secret("GITHUB_TOKEN", "gh-value-000000000001")
    wid, key = await _register(authed_admin_client)
    bad = await _create_task(authed_admin_client, target="1", payload={"task_env": ["API_TOKEN"]})
    good = await _create_task(
        authed_admin_client, target="1", payload={"task_env": ["GITHUB_TOKEN"]}
    )

    body = await _poll(authed_admin_client, wid, key)

    assert body["task"] is not None and body["task"]["task_id"] == good
    assert body["task"]["secrets"] == {"GITHUB_TOKEN": "gh-value-000000000001"}
    assert await _task_status(migrated_db_url, bad) == "queued"
    assert await _exec_statuses(migrated_db_url, bad) == []


@pytest.mark.asyncio
async def test_several_undecryptable_names_fall_back_to_tasks_without_stored_secrets(
    authed_admin_client: AsyncClient,
    keys_file: Path,  # noqa: F811
    migrated_db_url: str,
) -> None:
    """More blocked names than retries: the last pick excludes every stored secret."""
    foreign = TaskSecretsKeyring([Fernet.generate_key()])
    names = [f"BAD_{i}" for i in range(6)]
    for name in names:
        await _set_secret(name, "x" * 20, ring=foreign)
    wid, key = await _register(authed_admin_client)
    blocked = [
        await _create_task(authed_admin_client, target="1", payload={"task_env": [name]})
        for name in names
    ]
    plain = await _create_task(authed_admin_client, target="1", payload={})

    body = await _poll(authed_admin_client, wid, key)

    assert body["task"] is not None and body["task"]["task_id"] == plain
    for task_id in blocked:
        assert await _task_status(migrated_db_url, task_id) == "queued"
        assert await _exec_statuses(migrated_db_url, task_id) == []


@pytest.mark.asyncio
async def test_fairness_yield_ignores_worker_whose_secret_delivery_was_refused(
    authed_admin_client: AsyncClient,
    keys_file: Path,  # noqa: F811
    migrated_db_url: str,
) -> None:
    """W1 cannot decrypt its own row; W2 does not yield to it forever the tasks it can run."""
    w1, k1 = await _register(authed_admin_client, hostname="w1")
    w2, k2 = await _register(authed_admin_client, hostname="w2")
    await _set_secret("API_TOKEN", "ws-value-00000000001")
    await _set_secret(
        "API_TOKEN", "x" * 20, worker_id=w1, ring=TaskSecretsKeyring([Fernet.generate_key()])
    )
    ids = [
        await _create_task(authed_admin_client, target="1", payload={"task_env": ["API_TOKEN"]})
        for _ in range(3)
    ]
    await _poll(authed_admin_client, w1, k1)
    first = (await _poll(authed_admin_client, w2, k2))["task"]
    assert first is not None and first["task_id"] == ids[0]
    from orcaster_server import db as app_db

    sessionmaker = app_db.get_sessionmaker()
    async with sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE task_executions SET status='completed', finished_at=now() "
                "WHERE status='claimed'"
            )
        )
        await session.commit()

    assert (await _poll(authed_admin_client, w1, k1))["task"] is None
    second = (await _poll(authed_admin_client, w2, k2))["task"]

    assert second is not None and second["task_id"] == ids[1]
    assert second["secrets"] == {"API_TOKEN": "ws-value-00000000001"}
