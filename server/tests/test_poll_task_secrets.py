"""Delivery of task secrets in `/poll`.

Covers the three paths that go through `_build_task_payload` (push, the
`all-available` pre-assignment and the open claim), the intersection with
`payload.task_env`, the legacy mode (no `task_env`), the unconfigured store,
that nothing of the secret ends up in `tasks`/`task_executions` or in the
signature, the audit of the delivery and the visible failure when a declared
secret cannot be decrypted.

Also the worker version gate (a task that uses stored secrets does not reach a
worker below the floor, nor, as a push, a `busy` poll), the fairness-yield that
ignores workers below the floor for those tasks, the throttled `WARNING`, the
`all-available` snapshot that leaves those workers out and the Python/SQL
parity of the floor.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server.config import settings
from orcaster_server.signing import verify_task_signature
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

# Version that reaches the secrets floor. The workers in these tests declare it
# when they register, except those that exercise the gate.
SUPPORTED_VERSION = "0.47.0"

API_TOKEN_VALUE = "secret_value_that_must_not_leak_0001"
GH_VALUE = "ghp_value_that_must_not_leak_0002"


@pytest.fixture
def keys_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from orcaster_server.task_secrets import keyring

    p = tmp_path / "task-secrets-keys"
    p.write_text(Fernet.generate_key().decode() + "\n")
    monkeypatch.setattr(settings, "task_secrets_keys_path", str(p))
    keyring.reset_keyring_cache()
    return p


async def _register(
    client: AsyncClient, hostname: str = "w", version: str | None = SUPPORTED_VERSION
) -> tuple[str, str]:
    token = await mint_token_via_app()
    body = {**REGISTER, "hostname": hostname, "enrollment_token": token}
    if version is not None:
        body["version"] = version
    resp = await client.post("/v1/workers/register", json=body)
    assert resp.status_code == 200, resp.text
    j = resp.json()
    return j["worker_id"], j["api_key"]


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


async def _create_task(client: AsyncClient, **body) -> int:
    resp = await client.post("/admin/tasks", json=body)
    assert resp.status_code in (200, 201), resp.text
    return int(resp.json()["task_id"])


async def _poll(
    client: AsyncClient,
    wid: str,
    key: str,
    *,
    status: str = "idle",
    version: str | None = None,
) -> dict:
    body: dict = {"status": status}
    if version is not None:
        body["version"] = version
    resp = await client.post(f"/v1/workers/{wid}/poll", headers={"X-API-Key": key}, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _rows(db_url: str, sql: str, params: dict | None = None) -> list[dict]:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            return [dict(r) for r in (await conn.execute(text(sql), params or {})).mappings()]
    finally:
        await engine.dispose()


async def _exec(db_url: str, sql: str, params: dict | None = None) -> None:
    """Execute and commit (`_rows` only reads: its connection does not commit)."""
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params or {})
    finally:
        await engine.dispose()


async def _db_dump_text(db_url: str, task_id: int) -> str:
    """Text of the task rows and its executions, to look for leaks."""
    rows = await _rows(db_url, "SELECT t::text AS r FROM tasks t WHERE id = :id", {"id": task_id})
    rows += await _rows(
        db_url,
        "SELECT e::text AS r FROM task_executions e WHERE task_id = :id",
        {"id": task_id},
    )
    return "\n".join(r["r"] for r in rows)


async def _deliveries(db_url: str) -> list[dict]:
    return await _rows(
        db_url,
        "SELECT target_kind, target_id, actor, payload FROM admin_actions "
        "WHERE action = 'deliver_task_secrets' ORDER BY id",
    )


# --- Open claim ------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_claim_delivers_intersection_and_audits(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    migrated_db_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _set_secret("API_TOKEN", API_TOKEN_VALUE)
    await _set_secret("GITHUB_TOKEN", GH_VALUE)
    wid, key = await _register(authed_admin_client)
    task_id = await _create_task(
        authed_admin_client,
        target="1",
        payload={"task_env": ["API_TOKEN", "NOT_STORED"], "x": 1},
    )

    with caplog.at_level(logging.DEBUG):
        body = await _poll(authed_admin_client, wid, key)
    task = body["task"]
    assert task is not None and task["task_id"] == task_id
    assert task["secrets"] == {"API_TOKEN": API_TOKEN_VALUE}
    assert "secrets" not in task["payload"]

    # The signature still covers only the 6 canonical fields (no `secrets`).
    pem = Path(settings.ed25519_private_key_path).read_bytes()
    pub = serialization.load_pem_private_key(pem, password=None).public_key()
    assert verify_task_signature(
        pub,
        base64.b64decode(task["signature"]),
        task_id=task_id,
        worker_id=wid,
        command=task["command"] or "run_batch",
        payload=task["payload"],
        idempotency_key=task["idempotency_key"],
        created_at_iso=task["created_at"],
    )

    dump = await _db_dump_text(migrated_db_url, task_id)
    assert API_TOKEN_VALUE not in dump and GH_VALUE not in dump

    deliveries = await _deliveries(migrated_db_url)
    assert deliveries == [
        {
            "target_kind": "task",
            "target_id": str(task_id),
            "actor": None,
            # `worker_scoped` empty: the value came from the workspace row.
            "payload": {"worker_id": wid, "names": ["API_TOKEN"], "worker_scoped": []},
        }
    ]
    infos = [r.getMessage() for r in caplog.records if "task_secrets.delivered" in r.getMessage()]
    assert len(infos) == 1
    assert f"task_id={task_id}" in infos[0] and f"worker_id={wid}" in infos[0]
    assert "API_TOKEN" in infos[0]
    # No record contains a value.
    for r in caplog.records:
        assert API_TOKEN_VALUE not in r.getMessage() and GH_VALUE not in r.getMessage()


@pytest.mark.asyncio
async def test_legacy_task_without_task_env_gets_no_secrets(
    authed_admin_client: AsyncClient, keys_file: Path, migrated_db_url: str
) -> None:
    await _set_secret("API_TOKEN", API_TOKEN_VALUE)
    wid, key = await _register(authed_admin_client)
    await _create_task(authed_admin_client, target="1", payload={"x": 1})
    task = (await _poll(authed_admin_client, wid, key))["task"]
    assert task is not None
    assert task["secrets"] == {}
    assert await _deliveries(migrated_db_url) == []


@pytest.mark.asyncio
async def test_unconfigured_store_without_rows_delivers_empty(
    authed_admin_client: AsyncClient, migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orcaster_server.task_secrets import keyring

    # Without TASK_SECRETS_KEYS_PATH and without stored rows: the task runs normally.
    monkeypatch.setattr(settings, "task_secrets_keys_path", None)
    keyring.reset_keyring_cache()
    wid, key = await _register(authed_admin_client)
    await _create_task(authed_admin_client, target="1", payload={"task_env": ["API_TOKEN"]})
    task = (await _poll(authed_admin_client, wid, key))["task"]
    assert task is not None
    assert task["secrets"] == {}
    assert await _deliveries(migrated_db_url) == []


@pytest.mark.asyncio
async def test_unconfigured_store_with_stored_row_keeps_task_queued(
    authed_admin_client: AsyncClient,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Without keys the task stays `queued`, with no execution or attempt; the poll
    answers with the normal cadence. Once the file is configured, the next poll
    delivers it, as a first attempt."""
    from orcaster_server.task_secrets import keyring

    key = Fernet.generate_key()
    ring = TaskSecretsKeyring([key])
    monkeypatch.setattr(settings, "task_secrets_keys_path", None)
    keyring.reset_keyring_cache()
    await _set_secret("API_TOKEN", API_TOKEN_VALUE, ring=ring)
    wid, wkey = await _register(authed_admin_client)
    task_id = await _create_task(
        authed_admin_client,
        target="1",
        payload={"task_env": ["API_TOKEN", "NOT_STORED"]},
        auto_retry={"max_attempts": 3},
    )
    for _ in range(2):
        body = await _poll(authed_admin_client, wid, wkey)
        assert body["task"] is None
        assert body["next_poll_after_seconds"] == 30
    assert await _task_status(migrated_db_url, task_id) == "queued"
    assert await _exec_statuses(migrated_db_url, task_id) == []
    assert await _deliveries(migrated_db_url) == []
    # The worker heartbeat was committed (the savepoint only covers the claim).
    rows = await _rows(
        migrated_db_url,
        "SELECT last_reported_status FROM workers WHERE id = :id",
        {"id": wid},
    )
    assert rows == [{"last_reported_status": "idle"}]

    keys_path = tmp_path / "task-secrets-keys"
    keys_path.write_text(key.decode() + "\n")
    monkeypatch.setattr(settings, "task_secrets_keys_path", str(keys_path))
    keyring.reset_keyring_cache()
    body = await _poll(authed_admin_client, wid, wkey)
    task = body["task"]
    assert task is not None and task["task_id"] == task_id
    assert task["secrets"] == {"API_TOKEN": API_TOKEN_VALUE}
    assert body["next_poll_after_seconds"] == 1
    rows = await _rows(
        migrated_db_url,
        "SELECT status::text AS status, attempt_index FROM task_executions WHERE task_id = :id",
        {"id": task_id},
    )
    assert rows == [{"status": "claimed", "attempt_index": 1}]


# --- Push -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_task_receives_secrets(
    authed_admin_client: AsyncClient, keys_file: Path, migrated_db_url: str
) -> None:
    await _set_secret("API_TOKEN", API_TOKEN_VALUE)
    wid, key = await _register(authed_admin_client)
    task_id = await _create_task(
        authed_admin_client,
        target="1",
        delivery_mode="push",
        target_worker_id=wid,
        payload={"task_env": ["API_TOKEN"]},
    )
    stored_sig = (
        await _rows(migrated_db_url, "SELECT signature FROM tasks WHERE id = :id", {"id": task_id})
    )[0]["signature"]
    task = (await _poll(authed_admin_client, wid, key))["task"]
    assert task is not None and task["delivery_mode"] == "push"
    assert task["secrets"] == {"API_TOKEN": API_TOKEN_VALUE}
    # Push: the signature stored at INSERT travels intact.
    assert base64.b64decode(task["signature"]) == bytes(stored_sig)
    assert API_TOKEN_VALUE not in await _db_dump_text(migrated_db_url, task_id)
    assert len(await _deliveries(migrated_db_url)) == 1


async def _task_status(db_url: str, task_id: int) -> str:
    rows = await _rows(
        db_url, "SELECT status::text AS status FROM tasks WHERE id = :id", {"id": task_id}
    )
    return rows[0]["status"]


async def _exec_statuses(db_url: str, task_id: int) -> list[str]:
    rows = await _rows(
        db_url,
        "SELECT status::text AS status FROM task_executions WHERE task_id = :id ORDER BY id",
        {"id": task_id},
    )
    return [r["status"] for r in rows]


@pytest.mark.asyncio
async def test_busy_push_with_stored_secret_waits_for_idle(
    authed_admin_client: AsyncClient, keys_file: Path, migrated_db_url: str
) -> None:
    """A push that uses stored secrets is not chosen in a `busy` poll.

    No execution is inserted: the task stays `queued` and an `idle` poll receives
    it with its secrets (it used to go without secrets and without checks, and a
    worker that finished while the poll was in flight would run it anyway).
    """
    await _set_secret("API_TOKEN", API_TOKEN_VALUE)
    wid, key = await _register(authed_admin_client)
    task_id = await _create_task(
        authed_admin_client,
        target="1",
        delivery_mode="push",
        target_worker_id=wid,
        payload={"task_env": ["API_TOKEN"]},
    )
    body = await _poll(authed_admin_client, wid, key, status="busy")
    assert body["task"] is None
    assert await _exec_statuses(migrated_db_url, task_id) == []
    assert await _task_status(migrated_db_url, task_id) == "queued"
    assert await _deliveries(migrated_db_url) == []

    task = (await _poll(authed_admin_client, wid, key))["task"]
    assert task is not None and task["task_id"] == task_id
    assert task["secrets"] == {"API_TOKEN": API_TOKEN_VALUE}
    assert len(await _deliveries(migrated_db_url)) == 1


@pytest.mark.asyncio
async def test_busy_push_with_stored_secret_and_no_keys_stays_queued_on_idle(
    authed_admin_client: AsyncClient,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a keys file, the push with a stored secret does not go out in
    `busy` either; the next `idle` poll applies the fail-closed behaviour of the
    unconfigured store instead of delivering it without the secret."""
    from orcaster_server.task_secrets import keyring

    ring = TaskSecretsKeyring([Fernet.generate_key()])
    monkeypatch.setattr(settings, "task_secrets_keys_path", None)
    keyring.reset_keyring_cache()
    await _set_secret("API_TOKEN", API_TOKEN_VALUE, ring=ring)
    wid, key = await _register(authed_admin_client)
    task_id = await _create_task(
        authed_admin_client,
        target="1",
        delivery_mode="push",
        target_worker_id=wid,
        payload={"task_env": ["API_TOKEN"]},
    )
    body = await _poll(authed_admin_client, wid, key, status="busy")
    assert body["task"] is None
    assert await _exec_statuses(migrated_db_url, task_id) == []
    assert await _task_status(migrated_db_url, task_id) == "queued"

    body = await _poll(authed_admin_client, wid, key)
    assert body["task"] is None
    assert body["next_poll_after_seconds"] == 30
    assert await _exec_statuses(migrated_db_url, task_id) == []
    assert await _task_status(migrated_db_url, task_id) == "queued"
    assert await _deliveries(migrated_db_url) == []


@pytest.mark.asyncio
async def test_busy_push_without_stored_secret_is_still_delivered(
    authed_admin_client: AsyncClient, keys_file: Path, migrated_db_url: str
) -> None:
    """The gate does not change a push that uses no stored secrets: it still goes out
    in `busy` (previous behaviour), with empty `secrets` and no audit."""
    await _set_secret("API_TOKEN", API_TOKEN_VALUE)
    wid, key = await _register(authed_admin_client)
    task_id = await _create_task(
        authed_admin_client,
        target="1",
        delivery_mode="push",
        target_worker_id=wid,
        payload={"task_env": ["NOT_STORED"]},
    )
    task = (await _poll(authed_admin_client, wid, key, status="busy"))["task"]
    assert task is not None and task["task_id"] == task_id
    assert task["secrets"] == {}
    assert await _exec_statuses(migrated_db_url, task_id) == ["claimed"]
    assert await _deliveries(migrated_db_url) == []


# --- Pre-assignment (all-available) ----------------------------------------


@pytest.mark.asyncio
async def test_preassigned_all_available_receives_secrets(
    authed_admin_client: AsyncClient, keys_file: Path, migrated_db_url: str
) -> None:
    await _set_secret("API_TOKEN", API_TOKEN_VALUE)
    wid, key = await _register(authed_admin_client)
    # An empty poll leaves the worker online and with a heartbeat for the snapshot.
    assert (await _poll(authed_admin_client, wid, key))["task"] is None
    task_id = await _create_task(
        authed_admin_client, target="all-available", payload={"task_env": ["API_TOKEN"]}
    )
    assigned = await _rows(
        migrated_db_url,
        "SELECT status::text AS status FROM task_executions WHERE task_id = :id",
        {"id": task_id},
    )
    assert assigned == [{"status": "assigned"}]
    task = (await _poll(authed_admin_client, wid, key))["task"]
    assert task is not None and task["task_id"] == task_id
    assert task["secrets"] == {"API_TOKEN": API_TOKEN_VALUE}
    assert API_TOKEN_VALUE not in await _db_dump_text(migrated_db_url, task_id)


# --- Secret that cannot be decrypted -------------------------------------


@pytest.mark.asyncio
async def test_undecryptable_secret_keeps_task_queued(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    migrated_db_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With keys that do not decrypt: the task stays `queued`, with no
    execution, and the operator log names only the affected secret."""
    # Encrypted with a key that is not in the configured file.
    foreign = TaskSecretsKeyring([Fernet.generate_key()])
    await _set_secret("API_TOKEN", API_TOKEN_VALUE, ring=foreign)
    await _set_secret("GITHUB_TOKEN", GH_VALUE)
    wid, key = await _register(authed_admin_client)
    task_id = await _create_task(
        authed_admin_client,
        target="1",
        payload={"task_env": ["API_TOKEN", "GITHUB_TOKEN"]},
        auto_retry={"max_attempts": 3},
    )

    with caplog.at_level(logging.DEBUG):
        for _ in range(2):
            body = await _poll(authed_admin_client, wid, key)
            assert body["task"] is None
            assert body["next_poll_after_seconds"] == 30
    assert await _task_status(migrated_db_url, task_id) == "queued"
    assert await _exec_statuses(migrated_db_url, task_id) == []
    assert await _deliveries(migrated_db_url) == []

    errors = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.ERROR and "task_secrets.undecryptable" in r.getMessage()
    ]
    assert len(errors) == 1
    assert "API_TOKEN" in errors[0] and "GITHUB_TOKEN" not in errors[0]
    assert "left queued" in errors[0]
    for r in caplog.records:
        assert API_TOKEN_VALUE not in r.getMessage() and GH_VALUE not in r.getMessage()


@pytest.mark.asyncio
async def test_keys_file_changed_on_disk_is_reloaded_without_restart(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    migrated_db_url: str,
) -> None:
    """The keys file changes on disk (the key the secret was re-encrypted with was
    added). The next poll reloads and delivers, without restarting the server or
    resetting the cache."""
    new_key = Fernet.generate_key()
    await _set_secret("API_TOKEN", API_TOKEN_VALUE, ring=TaskSecretsKeyring([new_key]))
    wid, key = await _register(authed_admin_client)
    task_id = await _create_task(
        authed_admin_client, target="1", payload={"task_env": ["API_TOKEN"]}
    )
    # The cache only has the original key: the first poll does not decrypt.
    assert (await _poll(authed_admin_client, wid, key))["task"] is None
    assert await _task_status(migrated_db_url, task_id) == "queued"

    # The operator adds the new key to the file (the original stays primary).
    original = keys_file.read_text().strip()
    keys_file.write_text(original + "\n" + new_key.decode() + "\n")
    task = (await _poll(authed_admin_client, wid, key))["task"]
    assert task is not None and task["task_id"] == task_id
    assert task["secrets"] == {"API_TOKEN": API_TOKEN_VALUE}
    assert await _exec_statuses(migrated_db_url, task_id) == ["claimed"]


def test_poll_task_payload_repr_hides_secrets() -> None:
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
        secrets={"API_TOKEN": API_TOKEN_VALUE},
    )
    assert API_TOKEN_VALUE not in repr(p)
    assert API_TOKEN_VALUE not in str(p)
    assert (
        PollTaskPayload(
            task_id=1,
            created_at=datetime.now(UTC),
            delivery_mode="claim",
            worker_id=uuid.uuid4(),
            lease_token=uuid.uuid4(),
            lease_expires_at=datetime.now(UTC),
        ).secrets
        == {}
    )


# --- Worker version gate ---------------------------------------------------


def _delivered_logs(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "task_secrets.delivered" in r.getMessage()]


# `no_version`: the worker never reported a version (NULL in `workers.version`,
# poll without `version`). `below_floor`: the poll declares a version below the floor.
_BELOW_FLOOR_CASES = [
    pytest.param(None, id="no_version"),
    pytest.param("0.46.0", id="below_floor"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("poll_version", _BELOW_FLOOR_CASES)
async def test_open_claim_skips_stored_secret_task_for_unsupported_worker(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    migrated_db_url: str,
    caplog: pytest.LogCaptureFixture,
    poll_version: str | None,
) -> None:
    """A worker below the floor does not claim the task with stored secrets: it
    stays `queued`, with no audit or delivery log. The task without secrets that
    comes after it in the queue is claimed (no head-of-line blocking)."""
    await _set_secret("API_TOKEN", API_TOKEN_VALUE)
    wid, key = await _register(authed_admin_client, version=None)
    secret_task = await _create_task(
        authed_admin_client, target="1", payload={"task_env": ["API_TOKEN"]}
    )
    plain_task = await _create_task(
        authed_admin_client, target="1", payload={"task_env": ["NOT_STORED"]}
    )
    with caplog.at_level(logging.DEBUG):
        task = (await _poll(authed_admin_client, wid, key, version=poll_version))["task"]
    assert task is not None and task["task_id"] == plain_task
    assert task["secrets"] == {}
    assert await _task_status(migrated_db_url, secret_task) == "queued"
    assert await _exec_statuses(migrated_db_url, secret_task) == []
    assert await _deliveries(migrated_db_url) == []
    assert _delivered_logs(caplog) == []
    for r in caplog.records:
        assert API_TOKEN_VALUE not in r.getMessage()


@pytest.mark.asyncio
@pytest.mark.parametrize("poll_version", _BELOW_FLOOR_CASES)
async def test_push_skips_stored_secret_task_for_unsupported_worker(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    migrated_db_url: str,
    caplog: pytest.LogCaptureFixture,
    poll_version: str | None,
) -> None:
    await _set_secret("API_TOKEN", API_TOKEN_VALUE)
    wid, key = await _register(authed_admin_client, version=None)
    secret_task = await _create_task(
        authed_admin_client,
        target="1",
        delivery_mode="push",
        target_worker_id=wid,
        payload={"task_env": ["API_TOKEN"]},
    )
    plain_task = await _create_task(
        authed_admin_client,
        target="1",
        delivery_mode="push",
        target_worker_id=wid,
        payload={"x": 1},
    )
    with caplog.at_level(logging.DEBUG):
        task = (await _poll(authed_admin_client, wid, key, version=poll_version))["task"]
    assert task is not None and task["task_id"] == plain_task
    assert await _task_status(migrated_db_url, secret_task) == "queued"
    assert await _exec_statuses(migrated_db_url, secret_task) == []
    assert await _deliveries(migrated_db_url) == []
    assert _delivered_logs(caplog) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("poll_version", _BELOW_FLOOR_CASES)
async def test_preassigned_skips_stored_secret_task_for_unsupported_worker(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    migrated_db_url: str,
    caplog: pytest.LogCaptureFixture,
    poll_version: str | None,
) -> None:
    """Residual of the version gate: the snapshot leaves out the worker below the
    floor only if the secret was already stored. If it is stored afterwards, the
    pre-assignment exists and the poll skips it without blocking the next one."""
    wid, key = await _register(authed_admin_client, version=None)
    # An empty poll leaves the worker online and with a heartbeat for the snapshot.
    assert (await _poll(authed_admin_client, wid, key, version=poll_version))["task"] is None
    secret_task = await _create_task(
        authed_admin_client, target="all-available", payload={"task_env": ["API_TOKEN"]}
    )
    plain_task = await _create_task(authed_admin_client, target="all-available", payload={"x": 1})
    await _set_secret("API_TOKEN", API_TOKEN_VALUE)
    with caplog.at_level(logging.DEBUG):
        task = (await _poll(authed_admin_client, wid, key, version=poll_version))["task"]
    assert task is not None and task["task_id"] == plain_task
    # The execution of the task with secrets stays `assigned`, unclaimed.
    assert await _exec_statuses(migrated_db_url, secret_task) == ["assigned"]
    assert await _deliveries(migrated_db_url) == []
    assert _delivered_logs(caplog) == []


@pytest.mark.asyncio
async def test_all_available_snapshot_leaves_out_unsupported_worker(
    authed_admin_client: AsyncClient, keys_file: Path, migrated_db_url: str
) -> None:
    """Mixed fleet (one worker below the floor, one capable) and an `all-available`
    task with a stored secret: only the capable worker enters the snapshot, so
    the task closes `completed` when it finishes (with the old one in the
    snapshot it stayed `dispatching` forever). The retry re-snapshot applies the
    same filter."""
    await _set_secret("API_TOKEN", API_TOKEN_VALUE)
    old_wid, old_key = await _register(authed_admin_client, hostname="old", version="0.46.0")
    new_wid, new_key = await _register(authed_admin_client, hostname="new", version="0.47.0")
    resp = await authed_admin_client.post(
        "/admin/tasks", json={"target": "all-available", "payload": {"task_env": ["API_TOKEN"]}}
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["target_count"] == 1
    task_id = int(resp.json()["task_id"])

    def snapshot_sql() -> str:
        return (
            "SELECT worker_id::text AS worker_id, status::text AS status "
            "FROM task_executions WHERE task_id = :id ORDER BY attempt_index"
        )

    assert await _rows(migrated_db_url, snapshot_sql(), {"id": task_id}) == [
        {"worker_id": new_wid, "status": "assigned"}
    ]

    assert (await _poll(authed_admin_client, old_wid, old_key, version="0.46.0"))["task"] is None
    task = (await _poll(authed_admin_client, new_wid, new_key, version="0.47.0"))["task"]
    assert task is not None and task["task_id"] == task_id
    assert task["secrets"] == {"API_TOKEN": API_TOKEN_VALUE}
    done = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": new_key},
        json={"lease_token": task["lease_token"], "exit_code": 0, "status": "completed"},
    )
    assert done.status_code == 200, done.text
    assert await _task_status(migrated_db_url, task_id) == "completed"

    retry = await authed_admin_client.post(f"/admin/tasks/{task_id}/retry", json={})
    assert retry.status_code == 200, retry.text
    rows = await _rows(migrated_db_url, snapshot_sql(), {"id": task_id})
    assert {r["worker_id"] for r in rows} == {new_wid}
    assert [r["status"] for r in rows] == ["completed", "assigned"]


@pytest.mark.asyncio
async def test_restricted_all_available_with_only_unsupported_workers_fails_visibly(
    authed_admin_client: AsyncClient, keys_file: Path, migrated_db_url: str
) -> None:
    """`all:<ids>` with a pool of only workers below the floor and a stored secret:
    the snapshot comes out empty and the task closes `failed`
    (`no_eligible_workers`), just like with an offline pool."""
    await _set_secret("API_TOKEN", API_TOKEN_VALUE)
    old_wid, _ = await _register(authed_admin_client, hostname="old", version="0.46.0")
    resp = await authed_admin_client.post(
        "/admin/tasks", json={"target": f"all:{old_wid}", "payload": {"task_env": ["API_TOKEN"]}}
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["status"] == "failed"
    assert await _exec_statuses(migrated_db_url, int(resp.json()["task_id"])) == []


@pytest.mark.asyncio
async def test_poll_version_at_floor_receives_secrets(
    authed_admin_client: AsyncClient, keys_file: Path, migrated_db_url: str
) -> None:
    """The poll version applies in the same poll: a worker registered without a
    version that declares the floor version when polling receives the secrets and
    the audit."""
    await _set_secret("API_TOKEN", API_TOKEN_VALUE)
    wid, key = await _register(authed_admin_client, version=None)
    task_id = await _create_task(
        authed_admin_client, target="1", payload={"task_env": ["API_TOKEN"]}
    )
    task = (await _poll(authed_admin_client, wid, key, version="0.47.0"))["task"]
    assert task is not None and task["task_id"] == task_id
    assert task["secrets"] == {"API_TOKEN": API_TOKEN_VALUE}
    assert len(await _deliveries(migrated_db_url)) == 1


@pytest.mark.asyncio
async def test_fairness_yield_ignores_unsupported_worker_for_stored_secret_task(
    authed_admin_client: AsyncClient, keys_file: Path, migrated_db_url: str
) -> None:
    """A worker below the floor, idle and with the oldest `assigned_at`, forces the
    yield for a task without secrets, but not for one that uses stored secrets:
    it will never claim it, and the capable worker would yield forever
    (fairness-yield deadlock)."""
    await _set_secret("API_TOKEN", API_TOKEN_VALUE)
    old_wid, old_key = await _register(authed_admin_client, hostname="old", version=None)
    # Online, idle, no executions: its `max(assigned_at)` is -infinity.
    assert (await _poll(authed_admin_client, old_wid, old_key))["task"] is None

    new_wid, new_key = await _register(authed_admin_client, hostname="new")
    first = await _create_task(authed_admin_client, target="1", payload={"x": 1})
    task = (await _poll(authed_admin_client, new_wid, new_key))["task"]
    assert task is not None and task["task_id"] == first

    # Now the capable worker has a newer `assigned_at` than the old one.
    plain_task = await _create_task(authed_admin_client, target="1", payload={"x": 2})
    secret_task = await _create_task(
        authed_admin_client, target="1", payload={"task_env": ["API_TOKEN"]}
    )
    task = (await _poll(authed_admin_client, new_wid, new_key))["task"]
    # The task without secrets (older) yields to the old worker; the one that uses
    # stored secrets does not.
    assert task is not None and task["task_id"] == secret_task
    assert task["secrets"] == {"API_TOKEN": API_TOKEN_VALUE}
    assert await _task_status(migrated_db_url, plain_task) == "queued"


@pytest.mark.asyncio
async def test_unsupported_worker_warning_is_throttled(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from orcaster_server.api import poll

    monkeypatch.setattr(poll, "_unsupported_worker_last_check", {})
    await _set_secret("API_TOKEN", API_TOKEN_VALUE)
    wid, key = await _register(authed_admin_client, version=None)
    await _create_task(authed_admin_client, target="1", payload={"task_env": ["API_TOKEN"]})

    def warnings() -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and "task_secrets.worker_unsupported" in r.getMessage()
        ]

    with caplog.at_level(logging.DEBUG):
        assert (await _poll(authed_admin_client, wid, key, version="0.46.0"))["task"] is None
        assert (await _poll(authed_admin_client, wid, key, version="0.46.0"))["task"] is None
    got = warnings()
    assert len(got) == 1
    assert f"worker_id={wid}" in got[0]
    assert "version=0.46.0" in got[0] and "min_version=0.47.0" in got[0]
    assert "queued_tasks=1" in got[0]
    assert "API_TOKEN" not in got[0] and API_TOKEN_VALUE not in got[0]

    # Once the window has expired, it warns again.
    monkeypatch.setattr(poll, "UNSUPPORTED_WORKER_WARN_INTERVAL_SECONDS", 0.0)
    with caplog.at_level(logging.DEBUG):
        assert (await _poll(authed_admin_client, wid, key, version="0.46.0"))["task"] is None
    assert len(warnings()) == 2


@pytest.mark.asyncio
async def test_resolve_for_unsupported_worker_never_decrypts_or_audits(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    migrated_db_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Race between the pick and the assembly (a secret set in between): for a
    worker below the floor it is not decrypted, delivered or audited; a
    `WARNING` with the names is left."""
    from orcaster_server import db as app_db
    from orcaster_server.api import poll

    await _set_secret("API_TOKEN", API_TOKEN_VALUE)
    sm = app_db.get_sessionmaker()
    assert sm is not None
    row = {
        "task_id": 4242,
        "workspace_id": DEFAULT_WORKSPACE_ID,
        "payload": {"task_env": ["API_TOKEN", "NOT_STORED"]},
    }
    with caplog.at_level(logging.DEBUG):
        async with sm() as s:
            got = await poll._resolve_task_secrets(
                row,
                "00000000-0000-4000-8000-000000000001",
                s,
                secrets_capable=False,
                worker_version="0.46.0",
                busy=False,
            )
            await s.commit()
    assert got.values == {} and got.worker_scoped == []
    assert await _deliveries(migrated_db_url) == []
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "task_secrets.worker_unsupported task_id=4242" in m and "names=API_TOKEN" in m for m in msgs
    )
    for r in caplog.records:
        assert API_TOKEN_VALUE not in r.getMessage()


def _unsupported_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
        and r.getMessage().startswith("task_secrets.worker_unsupported worker_id=")
    ]


@pytest.mark.asyncio
async def test_unsupported_worker_warning_only_on_idle_and_counts_dispatchable(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The notice does not run in a `busy` poll (the version is not what holds the
    worker back) and `queued_tasks` only counts `queued` tasks that are already
    dispatchable: one with a future `run_at` does not count."""
    from datetime import UTC, datetime, timedelta

    from orcaster_server.api import poll

    monkeypatch.setattr(poll, "_unsupported_worker_last_check", {})
    await _set_secret("API_TOKEN", API_TOKEN_VALUE)
    wid, key = await _register(authed_admin_client, version="0.46.0")
    await _create_task(authed_admin_client, target="1", payload={"task_env": ["API_TOKEN"]})
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await _create_task(
        authed_admin_client, target="1", payload={"task_env": ["API_TOKEN"]}, run_at=future
    )
    with caplog.at_level(logging.DEBUG):
        assert (await _poll(authed_admin_client, wid, key, status="busy"))["task"] is None
    assert _unsupported_warnings(caplog) == []
    assert wid not in poll._unsupported_worker_last_check

    with caplog.at_level(logging.DEBUG):
        assert (await _poll(authed_admin_client, wid, key))["task"] is None
    got = _unsupported_warnings(caplog)
    assert len(got) == 1 and "queued_tasks=1" in got[0]


@pytest.mark.asyncio
async def test_unsupported_worker_warning_failure_does_not_fail_poll(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    migrated_db_url: str,
) -> None:
    """An error in the count after the commit is only logged: the poll answers 200
    and the heartbeat has already persisted."""
    from orcaster_server.api import poll

    monkeypatch.setattr(poll, "_unsupported_worker_last_check", {})
    monkeypatch.setattr(poll, "_SQL_COUNT_PENDING_STORED_SECRET_TASKS", text("SELECT 1 / 0"))
    wid, key = await _register(authed_admin_client, version="0.46.0")
    with caplog.at_level(logging.DEBUG):
        body = await _poll(authed_admin_client, wid, key, version="0.46.0")
    assert body["task"] is None
    assert any(
        "task_secrets.worker_unsupported_check_failed" in r.getMessage() for r in caplog.records
    )
    rows = await _rows(
        migrated_db_url,
        "SELECT last_reported_status FROM workers WHERE id = :id",
        {"id": wid},
    )
    assert rows == [{"last_reported_status": "idle"}]


@pytest.mark.asyncio
async def test_unsupported_worker_throttle_dict_is_dropped_and_bounded(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker's key is deleted when it reaches the floor, and the dict is
    emptied when it passes the cap."""
    from orcaster_server.api import poll

    marks: dict[str, float] = {}
    monkeypatch.setattr(poll, "_unsupported_worker_last_check", marks)
    monkeypatch.setattr(poll, "_UNSUPPORTED_WORKER_MAX_ENTRIES", 3)
    wid, key = await _register(authed_admin_client, version="0.46.0")
    await _poll(authed_admin_client, wid, key, version="0.46.0")
    assert wid in marks
    await _poll(authed_admin_client, wid, key, version="0.47.0")
    assert wid not in marks

    marks.update({"a": 0.0, "b": 0.0, "c": 0.0})
    await _poll(authed_admin_client, wid, key, version="0.46.0")
    assert marks.keys() == {wid}


@pytest.mark.asyncio
async def test_resolve_on_busy_poll_never_decrypts_or_audits(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    migrated_db_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Race in a `busy` poll of a capable worker (a secret set between the push
    pick and the assembly): it is not decrypted, delivered or audited; a
    `task_secrets.busy_push` `WARNING` with the names is left."""
    from orcaster_server import db as app_db
    from orcaster_server.api import poll

    await _set_secret("API_TOKEN", API_TOKEN_VALUE)
    sm = app_db.get_sessionmaker()
    assert sm is not None
    row = {
        "task_id": 4343,
        "workspace_id": DEFAULT_WORKSPACE_ID,
        "payload": {"task_env": ["API_TOKEN", "NOT_STORED"]},
    }
    with caplog.at_level(logging.DEBUG):
        async with sm() as s:
            got = await poll._resolve_task_secrets(
                row,
                "00000000-0000-4000-8000-000000000001",
                s,
                secrets_capable=True,
                worker_version="0.47.0",
                busy=True,
            )
            await s.commit()
    assert got.values == {} and got.worker_scoped == []
    assert await _deliveries(migrated_db_url) == []
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("task_secrets.busy_push task_id=4343" in m and "names=API_TOKEN" in m for m in msgs)
    for r in caplog.records:
        assert API_TOKEN_VALUE not in r.getMessage()


_PARITY_VERSIONS = [
    (None, False),
    ("", False),
    ("0.46.0", False),
    ("0.46.9", False),
    ("0.47.0", True),
    ("v0.47.0", True),
    ("0.47.0+patched", True),
    ("0.47.0rc1", True),
    ("1.0.0", True),
    ("0.100.0", True),
    ("garbage", False),
    ("99999999999.0.0", True),
    ("0.0.0+unknown", False),
]


@pytest.mark.asyncio
async def test_worker_version_floor_python_sql_parity(migrated_db_url: str) -> None:
    """The floor in Python (poll, dashboard) and in SQL (fairness-yield) agree."""
    from orcaster_server.task_secrets.gate import (
        worker_supports_task_secrets,
        worker_supports_task_secrets_sql,
    )

    sql = f"SELECT {worker_supports_task_secrets_sql('CAST(:v AS text)')} AS ok"
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            for version, expected in _PARITY_VERSIONS:
                in_sql = (await conn.execute(text(sql), {"v": version})).scalar_one()
                assert worker_supports_task_secrets(version) is expected, version
                assert in_sql is expected, version
    finally:
        await engine.dispose()


_DECLARED_ENV_PAYLOADS: list[tuple[object, bool]] = [
    ({"task_env": ["API_TOKEN"]}, True),
    ({"task_env": ["A", "B"]}, True),
    ({"task_env": ["A", 1, None]}, True),
    ({"task_env": []}, False),
    ({"task_env": [1, None, {"a": 1}]}, False),
    ({"task_env": "API_TOKEN"}, False),
    ({"task_env": {"API_TOKEN": "x"}}, False),
    ({"task_env": None}, False),
    ({"x": 1}, False),
    ({}, False),
    ([], False),
    ("task_env", False),
    (None, False),
]


@pytest.mark.asyncio
async def test_payload_declares_task_env_python_sql_parity(migrated_db_url: str) -> None:
    """«The task declares some name in `task_env`» gives the same in SQL
    (housekeeping) as `declared_task_env` (poll): only a list with some string
    declares."""
    from orcaster_server.task_secrets.gate import payload_declares_task_env_sql
    from orcaster_server.task_secrets.service import declared_task_env

    sql = f"SELECT {payload_declares_task_env_sql('CAST(:p AS jsonb)')} AS declares"
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            for payload, expected in _DECLARED_ENV_PAYLOADS:
                in_sql = (await conn.execute(text(sql), {"p": json.dumps(payload)})).scalar_one()
                assert bool(declared_task_env(payload)) is expected, payload
                assert in_sql is expected, payload
    finally:
        await engine.dispose()


# --- The blocked task does not hold back the others ------------------------

_BLOCKED_SCENARIOS = ["keys_missing", "undecryptable"]


async def _block_api_token(monkeypatch: pytest.MonkeyPatch, keys_file: Path, scenario: str) -> None:
    """Leave `API_TOKEN` stored and undecryptable: without keys, or encrypted with another."""
    from orcaster_server.task_secrets import keyring

    if scenario == "keys_missing":
        monkeypatch.setattr(settings, "task_secrets_keys_path", None)
        keyring.reset_keyring_cache()
    await _set_secret(
        "API_TOKEN", API_TOKEN_VALUE, ring=TaskSecretsKeyring([Fernet.generate_key()])
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", _BLOCKED_SCENARIOS)
async def test_blocked_claim_does_not_starve_task_without_env(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    """A blocked task first in the queue does not prevent the poll from delivering
    the next one, which uses no stored secrets. The blocked one stays `queued`."""
    await _block_api_token(monkeypatch, keys_file, scenario)
    wid, key = await _register(authed_admin_client)
    blocked_id = await _create_task(
        authed_admin_client, target="1", payload={"task_env": ["API_TOKEN"]}
    )
    free_id = await _create_task(authed_admin_client, target="1", payload={"x": 1})

    body = await _poll(authed_admin_client, wid, key)
    task = body["task"]
    assert task is not None and task["task_id"] == free_id
    assert body["next_poll_after_seconds"] == 1
    assert await _task_status(migrated_db_url, blocked_id) == "queued"
    assert await _exec_statuses(migrated_db_url, blocked_id) == []
    assert await _exec_statuses(migrated_db_url, free_id) == ["claimed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", _BLOCKED_SCENARIOS)
async def test_blocked_push_does_not_starve_push_without_env(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    await _block_api_token(monkeypatch, keys_file, scenario)
    wid, key = await _register(authed_admin_client)
    blocked_id = await _create_task(
        authed_admin_client,
        target="1",
        delivery_mode="push",
        target_worker_id=wid,
        payload={"task_env": ["API_TOKEN"]},
    )
    free_id = await _create_task(
        authed_admin_client,
        target="1",
        delivery_mode="push",
        target_worker_id=wid,
        payload={"x": 1},
    )
    task = (await _poll(authed_admin_client, wid, key))["task"]
    assert task is not None and task["task_id"] == free_id
    assert await _task_status(migrated_db_url, blocked_id) == "queued"
    assert await _exec_statuses(migrated_db_url, blocked_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", _BLOCKED_SCENARIOS)
async def test_blocked_preassigned_keeps_its_assignment_and_does_not_starve(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    """The blocked pre-assignment stays `assigned`, as before the poll; nothing is
    marked `failed` and the next pre-assignment is delivered."""
    await _block_api_token(monkeypatch, keys_file, scenario)
    wid, key = await _register(authed_admin_client)
    # An empty poll leaves the worker online and with a heartbeat for the snapshot.
    assert (await _poll(authed_admin_client, wid, key))["task"] is None
    blocked_id = await _create_task(
        authed_admin_client, target="all-available", payload={"task_env": ["API_TOKEN"]}
    )
    free_id = await _create_task(authed_admin_client, target="all-available", payload={"x": 1})
    # Pre-assignments are born in `dispatching`; the poll must not touch their status.
    status_before = await _task_status(migrated_db_url, blocked_id)

    task = (await _poll(authed_admin_client, wid, key))["task"]
    assert task is not None and task["task_id"] == free_id
    assert await _exec_statuses(migrated_db_url, blocked_id) == ["assigned"]
    assert await _task_status(migrated_db_url, blocked_id) == status_before
    assert await _deliveries(migrated_db_url) == []


# --- Operator notice, throttled and without failing the poll ---------------


def _refusal_errors(caplog: pytest.LogCaptureFixture, kind: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.ERROR and f"task_secrets.{kind}" in r.getMessage()
    ]


@pytest.mark.asyncio
async def test_refusal_error_is_logged_once_per_window_with_names_only(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from orcaster_server.api import poll

    monkeypatch.setattr(poll, "_refusal_last_logged", {})
    await _block_api_token(monkeypatch, keys_file, "undecryptable")
    wid, key = await _register(authed_admin_client)
    task_id = await _create_task(
        authed_admin_client, target="1", payload={"task_env": ["API_TOKEN"]}
    )
    with caplog.at_level(logging.DEBUG):
        await _poll(authed_admin_client, wid, key)
        await _poll(authed_admin_client, wid, key)
    got = _refusal_errors(caplog, "undecryptable")
    assert len(got) == 1
    msg = got[0]
    assert f"worker_id={wid}" in msg and f"task_id={task_id}" in msg
    assert "names=API_TOKEN" in msg and "queued_tasks=1" in msg
    assert "left queued" in msg and "delivery was refused" in msg
    assert API_TOKEN_VALUE not in msg

    # Once the notice window and the refusal memory expire, it warns again.
    monkeypatch.setattr(poll, "REFUSAL_LOG_INTERVAL_SECONDS", 0.0)
    poll._refusal_memory.clear()
    with caplog.at_level(logging.DEBUG):
        await _poll(authed_admin_client, wid, key)
    assert len(_refusal_errors(caplog, "undecryptable")) == 2


@pytest.mark.asyncio
async def test_refusal_count_failure_does_not_fail_poll(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    migrated_db_url: str,
) -> None:
    from orcaster_server.api import poll

    monkeypatch.setattr(poll, "_refusal_last_logged", {})
    monkeypatch.setattr(poll, "_SQL_COUNT_PENDING_STORED_SECRET_TASKS", text("SELECT 1 / 0"))
    await _block_api_token(monkeypatch, keys_file, "undecryptable")
    wid, key = await _register(authed_admin_client)
    task_id = await _create_task(
        authed_admin_client, target="1", payload={"task_env": ["API_TOKEN"]}
    )
    with caplog.at_level(logging.DEBUG):
        body = await _poll(authed_admin_client, wid, key)
    assert body["task"] is None
    assert await _task_status(migrated_db_url, task_id) == "queued"
    got = _refusal_errors(caplog, "undecryptable")
    assert len(got) == 1 and "queued_tasks=unknown" in got[0]
    assert any("task_secrets.refusal_count_failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_recent_refusals_reports_kind_and_names_per_workspace(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orcaster_server.api import poll
    from orcaster_server.task_secrets import blocked

    monkeypatch.setattr(poll, "_refusal_last_logged", {})
    monkeypatch.setattr(blocked, "_REFUSALS", {})
    await _block_api_token(monkeypatch, keys_file, "undecryptable")
    wid, key = await _register(authed_admin_client)
    task_id = await _create_task(
        authed_admin_client, target="1", payload={"task_env": ["API_TOKEN"]}
    )
    await _poll(authed_admin_client, wid, key)

    got = blocked.recent_refusals(str(DEFAULT_WORKSPACE_ID))
    assert [(r.kind, r.names, r.task_id) for r in got] == [
        ("undecryptable", ("API_TOKEN",), task_id)
    ]
    assert blocked.recent_refusals("00000000-0000-0000-0000-000000000000") == []
    monkeypatch.setattr(blocked, "REFUSAL_WINDOW_SECONDS", 0.0)
    assert blocked.recent_refusals(str(DEFAULT_WORKSPACE_ID)) == []


# ---------------------------------------------------------------------------
# Short refusal memory (the blocked state does not repeat the claim and rollback
# round on every poll) and fairness-yield mark without rewrites.
# ---------------------------------------------------------------------------


def _count_attempts(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Spy on `_attempt_delivery`: one dict per attempt, with its exclusions."""
    from orcaster_server.api import poll

    calls: list[dict] = []
    real = poll._attempt_delivery

    async def spy(*args, **kwargs):
        calls.append(
            {
                "exclude_all": kwargs["exclude_stored_secrets"],
                "names": list(kwargs.get("exclude_secret_names") or []),
            }
        )
        return await real(*args, **kwargs)

    monkeypatch.setattr(poll, "_attempt_delivery", spy)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["keys_missing", "undecryptable"])
async def test_remembered_refusal_skips_the_refuse_round(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    """With the refusal in memory, the poll does a single pick, already with the exclusion."""
    await _block_api_token(monkeypatch, keys_file, scenario)
    wid, key = await _register(authed_admin_client)
    blocked = await _create_task(
        authed_admin_client, target="1", payload={"task_env": ["API_TOKEN"]}
    )
    plain = [await _create_task(authed_admin_client, target="1", payload={}) for _ in range(2)]
    calls = _count_attempts(monkeypatch)

    first = await _poll(authed_admin_client, wid, key)
    assert first["task"]["task_id"] == plain[0]
    assert len(calls) == 2  # the normal pick, refused, and the one that excludes

    await _exec(
        migrated_db_url,
        "UPDATE task_executions SET status='completed', finished_at=now() WHERE status='claimed'",
    )
    del calls[:]
    second = await _poll(authed_admin_client, wid, key)

    assert second["task"]["task_id"] == plain[1]
    assert len(calls) == 1
    if scenario == "keys_missing":
        assert calls[0] == {"exclude_all": True, "names": []}
    else:
        assert calls[0] == {"exclude_all": False, "names": ["API_TOKEN"]}
    assert await _task_status(migrated_db_url, blocked) == "queued"
    assert await _exec_statuses(migrated_db_url, blocked) == []


@pytest.mark.asyncio
async def test_expired_refusal_memory_probes_again_and_delivers_after_the_secret_is_fixed(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting the secret again is noticed when the memory expires, not before."""
    from orcaster_server.api import poll

    await _block_api_token(monkeypatch, keys_file, "undecryptable")
    wid, key = await _register(authed_admin_client)
    task_id = await _create_task(
        authed_admin_client, target="1", payload={"task_env": ["API_TOKEN"]}
    )
    assert (await _poll(authed_admin_client, wid, key))["task"] is None

    await _set_secret("API_TOKEN", API_TOKEN_VALUE)  # with the current key
    assert (await _poll(authed_admin_client, wid, key))["task"] is None  # memory still valid

    memory = poll._refusal_memory[wid]
    poll._refusal_memory[wid] = memory._replace(expires=0.0)
    body = await _poll(authed_admin_client, wid, key)

    assert body["task"] is not None and body["task"]["task_id"] == task_id
    assert body["task"]["secrets"] == {"API_TOKEN": API_TOKEN_VALUE}
    assert wid not in poll._refusal_memory


@pytest.mark.asyncio
async def test_refusal_memory_disabled_with_zero_seconds(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orcaster_server.api import poll

    monkeypatch.setattr(poll, "REFUSAL_MEMORY_SECONDS", 0.0)
    await _block_api_token(monkeypatch, keys_file, "keys_missing")
    wid, key = await _register(authed_admin_client)
    await _create_task(authed_admin_client, target="1", payload={"task_env": ["API_TOKEN"]})
    calls = _count_attempts(monkeypatch)

    await _poll(authed_admin_client, wid, key)
    await _poll(authed_admin_client, wid, key)

    assert len(calls) == 4
    assert poll._refusal_memory == {}


@pytest.mark.asyncio
async def test_refusal_mark_is_not_rewritten_within_its_interval(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fairness-yield mark is written once per interval, not on every poll."""
    from orcaster_server.api import poll

    monkeypatch.setattr(poll, "REFUSAL_MEMORY_SECONDS", 0.0)  # every poll refuses again
    await _block_api_token(monkeypatch, keys_file, "keys_missing")
    wid, key = await _register(authed_admin_client)
    await _create_task(authed_admin_client, target="1", payload={"task_env": ["API_TOKEN"]})

    async def mark() -> list[dict]:
        return await _rows(
            migrated_db_url,
            "SELECT task_secrets_refused_at AS at, xmin::text AS xmin FROM workers "
            "WHERE id = CAST(:id AS uuid)",
            {"id": wid},
        )

    assert (await mark())[0]["at"] is None
    await _poll(authed_admin_client, wid, key)
    first = (await mark())[0]
    assert first["at"] is not None
    await _poll(authed_admin_client, wid, key)
    assert (await mark())[0]["at"] == first["at"]

    # Once the interval has expired, it is renewed.
    poll._refusal_mark_written.clear()
    await _exec(
        migrated_db_url,
        "UPDATE workers SET task_secrets_refused_at = now() - interval '10 minutes' "
        "WHERE id = CAST(:id AS uuid)",
        {"id": wid},
    )
    await _poll(authed_admin_client, wid, key)
    assert (await mark())[0]["at"] > first["at"] - timedelta(minutes=5)


@pytest.mark.asyncio
async def test_refusal_mark_throttle_is_stamped_only_when_the_update_wrote(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local interval stamp is only set if the UPDATE wrote the mark. If another
    process wrote it recently, the UPDATE matches no row and the local stamp
    would keep this process from retrying for a whole interval."""
    from orcaster_server.api import poll

    monkeypatch.setattr(poll, "REFUSAL_MEMORY_SECONDS", 0.0)  # every poll refuses again
    await _block_api_token(monkeypatch, keys_file, "keys_missing")
    wid, key = await _register(authed_admin_client)
    await _create_task(authed_admin_client, target="1", payload={"task_env": ["API_TOKEN"]})

    async def mark() -> list[dict]:
        return await _rows(
            migrated_db_url,
            "SELECT task_secrets_refused_at AS at FROM workers WHERE id = CAST(:id AS uuid)",
            {"id": wid},
        )

    # Another process wrote the mark 30 s ago (< the 60 s interval): this process's
    # UPDATE matches no row.
    await _exec(
        migrated_db_url,
        "UPDATE workers SET task_secrets_refused_at = now() - interval '30 seconds' "
        "WHERE id = CAST(:id AS uuid)",
        {"id": wid},
    )
    before = (await mark())[0]["at"]
    poll._refusal_mark_written.clear()
    await _poll(authed_admin_client, wid, key)
    assert (await mark())[0]["at"] == before
    assert wid not in poll._refusal_mark_written

    # Once the interval has expired in the database, the UPDATE writes and then it is stamped.
    await _exec(
        migrated_db_url,
        "UPDATE workers SET task_secrets_refused_at = now() - interval '10 minutes' "
        "WHERE id = CAST(:id AS uuid)",
        {"id": wid},
    )
    stale = (await mark())[0]["at"]
    await _poll(authed_admin_client, wid, key)
    assert (await mark())[0]["at"] > stale
    assert wid in poll._refusal_mark_written


# ---------------------------------------------------------------------------
# A refused pre-assignment does not expire while the refusal lasts. Once
# expired, `_SQL_MAYBE_CLOSE_TASK` counts it as terminal and another worker's
# `/complete` would close the task as `failed`: the same defect as the refusal, by another route.
# ---------------------------------------------------------------------------


async def _past_claim_timeout(db_url: str, task_id: int) -> None:
    await _exec(
        db_url,
        "UPDATE task_executions SET assigned_at = now() - interval '2 hours' "
        "WHERE task_id = :task_id AND status = 'assigned'",
        {"task_id": task_id},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["keys_missing", "undecryptable"])
async def test_refused_preassigned_execution_is_not_expired_while_the_refusal_lasts(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    from orcaster_server.housekeeping import _expire_assigned

    await _block_api_token(monkeypatch, keys_file, scenario)
    wid, key = await _register(authed_admin_client)
    assert (await _poll(authed_admin_client, wid, key))["task"] is None
    task_id = await _create_task(
        authed_admin_client, target="all-available", payload={"task_env": ["API_TOKEN"]}
    )
    assert (await _poll(authed_admin_client, wid, key))["task"] is None  # refused
    await _past_claim_timeout(migrated_db_url, task_id)

    engine = create_async_engine(migrated_db_url)
    try:
        assert await _expire_assigned(engine) == 0
        assert await _exec_statuses(migrated_db_url, task_id) == ["assigned"]

        # Without a recent refusal (the worker stopped polling), it expires as usual.
        await _exec(
            migrated_db_url,
            "UPDATE workers SET task_secrets_refused_at = now() - interval '10 minutes' "
            "WHERE id = CAST(:id AS uuid)",
            {"id": wid},
        )
        assert await _expire_assigned(engine) == 1
        assert await _exec_statuses(migrated_db_url, task_id) == ["expired"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["keys_missing", "undecryptable"])
async def test_deleting_the_last_secret_does_not_expire_the_refused_preassigned_execution(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    """Deleting the last secret is the documented way out: the worker's next `idle`
    poll delivers the pre-assignment. The housekeeping exemption cannot depend
    on the row that this way out deletes: the mark stays fresh for up to 120 s
    and a housekeeping pass before that poll would expire it."""
    from orcaster_server.housekeeping import _expire_assigned

    await _block_api_token(monkeypatch, keys_file, scenario)
    wid, key = await _register(authed_admin_client)
    assert (await _poll(authed_admin_client, wid, key))["task"] is None
    task_id = await _create_task(
        authed_admin_client, target="all-available", payload={"task_env": ["API_TOKEN"]}
    )
    assert (await _poll(authed_admin_client, wid, key))["task"] is None  # refused, fresh mark
    await _past_claim_timeout(migrated_db_url, task_id)

    await _exec(migrated_db_url, "DELETE FROM task_secrets WHERE name = 'API_TOKEN'")

    engine = create_async_engine(migrated_db_url)
    try:
        assert await _expire_assigned(engine) == 0
    finally:
        await engine.dispose()
    assert await _exec_statuses(migrated_db_url, task_id) == ["assigned"]

    # The worker's idle cadence (30 s) is not below `REFUSAL_MEMORY_SECONDS`: its
    # next poll no longer finds the refusal memory.
    from orcaster_server.api import poll

    poll._refusal_memory.clear()
    task = (await _poll(authed_admin_client, wid, key))["task"]
    assert task is not None and task["task_id"] == task_id
    assert await _exec_statuses(migrated_db_url, task_id) == ["claimed"]


@pytest.mark.asyncio
async def test_expiry_exemption_is_still_gated_by_the_refusal_mark(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exemption does not depend on the `task_secrets` rows but on the mark:
    without a recent mark it expires even if the task declares `task_env`,
    whether or not there are rows."""
    from orcaster_server.housekeeping import _expire_assigned

    wid, key = await _register(authed_admin_client)
    assert (await _poll(authed_admin_client, wid, key))["task"] is None
    # Never refused (NULL mark): the task declares `task_env` and there are no secrets.
    never_refused_id = await _create_task(
        authed_admin_client, target="all-available", payload={"task_env": ["API_TOKEN"]}
    )
    await _past_claim_timeout(migrated_db_url, never_refused_id)
    engine = create_async_engine(migrated_db_url)
    try:
        assert await _expire_assigned(engine) == 1
        assert await _exec_statuses(migrated_db_url, never_refused_id) == ["expired"]

        # Refused and with the last secret already deleted, but with the mark expired.
        await _block_api_token(monkeypatch, keys_file, "keys_missing")
        refused_id = await _create_task(
            authed_admin_client, target="all-available", payload={"task_env": ["API_TOKEN"]}
        )
        assert (await _poll(authed_admin_client, wid, key))["task"] is None  # refused
        await _past_claim_timeout(migrated_db_url, refused_id)
        await _exec(migrated_db_url, "DELETE FROM task_secrets WHERE name = 'API_TOKEN'")
        assert await _expire_assigned(engine) == 0
        await _exec(
            migrated_db_url,
            "UPDATE workers SET task_secrets_refused_at = now() - interval '10 minutes' "
            "WHERE id = CAST(:id AS uuid)",
            {"id": wid},
        )
        assert await _expire_assigned(engine) == 1
        assert await _exec_statuses(migrated_db_url, refused_id) == ["expired"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recent_refusal_does_not_hold_the_expiry_of_tasks_without_stored_secrets(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mark is per worker: it only holds back the expiry of tasks with stored secrets."""
    from orcaster_server.housekeeping import _expire_assigned

    await _block_api_token(monkeypatch, keys_file, "keys_missing")
    wid, key = await _register(authed_admin_client)
    assert (await _poll(authed_admin_client, wid, key))["task"] is None
    blocked_id = await _create_task(
        authed_admin_client, target="all-available", payload={"task_env": ["API_TOKEN"]}
    )
    assert (await _poll(authed_admin_client, wid, key))["task"] is None  # refused
    plain_id = await _create_task(authed_admin_client, target="all-available", payload={"x": 1})
    await _past_claim_timeout(migrated_db_url, blocked_id)
    await _past_claim_timeout(migrated_db_url, plain_id)

    engine = create_async_engine(migrated_db_url)
    try:
        assert await _expire_assigned(engine) == 1
    finally:
        await engine.dispose()
    assert await _exec_statuses(migrated_db_url, blocked_id) == ["assigned"]
    assert await _exec_statuses(migrated_db_url, plain_id) == ["expired"]


# ---------------------------------------------------------------------------
# The residual limits of the exemption, pinned in a test so they are not
# documented from memory. The exemption is held by `task_secrets_refused_at`,
# and that mark is only renewed by a poll that reaches the choice of a task.
# ---------------------------------------------------------------------------


async def _refusal_mark(db_url: str, wid: str):
    rows = await _rows(
        db_url,
        "SELECT task_secrets_refused_at AS at FROM workers WHERE id = CAST(:id AS uuid)",
        {"id": wid},
    )
    return rows[0]["at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["none", "paused", "storage_paused", "disk_gated"])
async def test_gated_worker_does_not_renew_the_refusal_mark(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    gate: str,
) -> None:
    """Limit (b) of the exemption: the choice of the pre-assignment and of the open
    claim run under `new_status != "paused" and storage_paused is None and not
    disk_gated`, so a `paused` worker, one with paused storage or one under the
    disk gate receives no refusal and its mark ages even if it keeps polling:
    its pre-assignment expires like that of a `busy` worker. The `gate =
    "none"` case is the control: that same poll renews the mark and holds it."""
    from orcaster_server.api import poll
    from orcaster_server.housekeeping import _expire_assigned

    monkeypatch.setattr(poll, "REFUSAL_MEMORY_SECONDS", 0.0)  # every poll refuses again
    await _block_api_token(monkeypatch, keys_file, "keys_missing")
    wid, key = await _register(authed_admin_client)
    assert (await _poll(authed_admin_client, wid, key))["task"] is None
    task_id = await _create_task(
        authed_admin_client, target="all-available", payload={"task_env": ["API_TOKEN"]}
    )
    assert (await _poll(authed_admin_client, wid, key))["task"] is None  # refused
    await _past_claim_timeout(migrated_db_url, task_id)

    # The mark ages beyond the 120 s window (the worker could not poll, or the
    # server was down). A poll that reaches the choice renews it.
    await _exec(
        migrated_db_url,
        "UPDATE workers SET task_secrets_refused_at = now() - interval '10 minutes' "
        "WHERE id = CAST(:id AS uuid)",
        {"id": wid},
    )
    poll._refusal_mark_written.clear()
    stale = await _refusal_mark(migrated_db_url, wid)

    body: dict = {"status": "idle"}
    if gate == "paused":
        resp = await authed_admin_client.post(f"/admin/workers/{wid}/pause", json={})
        assert resp.status_code == 200, resp.text
    elif gate == "storage_paused":
        monkeypatch.setattr(settings, "artifact_workspace_max_total_bytes", 1000)
        await _exec(
            migrated_db_url,
            "INSERT INTO workspace_artifact_usage (workspace_id, total_bytes) "
            "VALUES (:w, 1000) ON CONFLICT (workspace_id) DO UPDATE SET total_bytes = 1000",
            {"w": DEFAULT_WORKSPACE_ID},
        )
    elif gate == "disk_gated":
        # Below `worker_claim_min_free_fraction` (0.20 by default).
        body["free_disk_fraction"] = 0.01

    resp = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll", headers={"X-API-Key": key}, json=body
    )
    assert resp.status_code == 200, resp.text
    polled = resp.json()
    assert polled["task"] is None
    # The gate must really be active: otherwise the test measures something else.
    if gate == "storage_paused":
        assert polled["storage_paused"] == "quota"
    elif gate == "disk_gated":
        assert polled["disk_gated"] is True
    else:
        assert polled["storage_paused"] is None and polled["disk_gated"] is False

    engine = create_async_engine(migrated_db_url)
    try:
        if gate == "none":
            assert await _refusal_mark(migrated_db_url, wid) > stale
            assert await _expire_assigned(engine) == 0
            assert await _exec_statuses(migrated_db_url, task_id) == ["assigned"]
        else:
            assert await _refusal_mark(migrated_db_url, wid) == stale
            assert await _expire_assigned(engine) == 1
            assert await _exec_statuses(migrated_db_url, task_id) == ["expired"]
    finally:
        await engine.dispose()
