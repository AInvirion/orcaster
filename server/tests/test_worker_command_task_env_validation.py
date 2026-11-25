"""Validation of `payload.task_env` on the worker push routes.

`POST /admin/workers/{id}/command` and `POST /admin/workers/{id}/control-op`
store a free-form payload as a signed push task. The worker runs a non-reserved command
with `_run_external`, which reads `payload.task_env`, so these routes
apply the same rule as creating a task (`validate_payload_task_env`): 422 without
inserting a row, with the index of the entry and without repeating it in any field of the
body (`msg`, `loc`, `input`, `ctx`). Validation happens only on write: a row
stored earlier is still delivered in the poll unchanged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server import signing
from orcaster_server.config import settings
from tests.conftest import mint_token_via_app

# Distinctive marker: it must not appear in ANY byte of the 422 response.
MARK = "hf_abc123"

ROUTES = ["command", "control-op"]

REGISTER = {
    "hostname": "w",
    "os": "linux",
    "arch": "x86_64",
    "ram_mb": 1024,
    "disk_mb": 10000,
    "tags": [],
    "capabilities": {},
}


@pytest.fixture(scope="module")
def _keypair(tmp_path_factory: pytest.TempPathFactory) -> Path:
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path_factory.mktemp("ed25519_cmd") / "server.pem"
    path.write_bytes(pem)
    return path


@pytest.fixture
def _signing(_keypair: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`/command` answers 503 without a signing key: we set it for each test."""
    monkeypatch.setattr(settings, "ed25519_private_key_path", str(_keypair))
    signing.reset_key_cache()


async def _register(client: AsyncClient) -> tuple[str, str]:
    resp = await client.post(
        "/v1/workers/register", json={**REGISTER, "enrollment_token": await mint_token_via_app()}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["worker_id"], resp.json()["api_key"]


async def _task_count() -> int:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            return int((await conn.execute(text("SELECT count(*) FROM tasks"))).scalar_one())
    finally:
        await engine.dispose()


async def _push(client: AsyncClient, route: str, wid: str, payload: Any) -> Response:
    if route == "command":
        expires = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        body: dict[str, Any] = {"command": "run_batch", "expires_at": expires}
        if payload is not _ABSENT:
            body["payload"] = payload
        return await client.post(f"/admin/workers/{wid}/command", json=body)
    assert route == "control-op", route
    body = {"control_op": "__reload_config__"}
    if payload is not _ABSENT:
        body["payload"] = payload
    return await client.post(f"/admin/workers/{wid}/control-op", json=body)


_ABSENT = object()


def _assert_no_echo(resp: Response, *fragments: str) -> None:
    """422 without `input` or `ctx` in any error and without any fragment in the body."""
    assert resp.status_code == 422, resp.text
    errors = resp.json()["detail"]
    assert isinstance(errors, list) and errors
    for err in errors:
        assert "input" not in err, err
        assert "ctx" not in err, err
    for fragment in fragments:
        assert fragment not in resp.text


@pytest.mark.asyncio
@pytest.mark.usefixtures("_signing")
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    ("task_env", "message"),
    [
        (["API_TOKEN=hf_abc123"], "payload.task_env[0] is not a valid environment variable name"),
        (["hf_abc123"], "payload.task_env[0] looks like a credential (known token prefix)"),
        (["OK", "ghp_abc123"], "payload.task_env[1] looks like a credential (known token prefix)"),
        (["ORCH_X"], "payload.task_env[0] may not declare ORCH_ vars"),
        (["A B"], "payload.task_env[0] is not a valid environment variable name"),
        ("x", "payload.task_env must be a list of strings"),
        ([1], "payload.task_env[0] must be a string"),
    ],
    ids=["name-eq-value", "hf-prefix", "ghp-prefix", "orch", "space", "not-a-list", "not-str"],
)
async def test_invalid_task_env_is_422_and_inserts_nothing(
    authed_admin_client: AsyncClient, route: str, task_env: Any, message: str
) -> None:
    wid, _ = await _register(authed_admin_client)
    before = await _task_count()
    resp = await _push(authed_admin_client, route, wid, {"task_env": task_env})
    _assert_no_echo(resp, MARK, "abc123")
    assert message in resp.text
    assert await _task_count() == before


@pytest.mark.asyncio
@pytest.mark.usefixtures("_signing")
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "payload",
    [
        {"task_env": ["API_TOKEN"]},
        {"task_env": []},
        # `null` counts as an absent key: it is not validated.
        {"task_env": None},
        {"reason": "x"},
        {},
        _ABSENT,
    ],
    ids=["named", "empty-list", "null", "no-task-env", "empty-payload", "payload-absent"],
)
async def test_valid_payload_is_accepted(
    authed_admin_client: AsyncClient, route: str, payload: Any
) -> None:
    wid, _ = await _register(authed_admin_client)
    before = await _task_count()
    resp = await _push(authed_admin_client, route, wid, payload)
    assert resp.status_code == 200, resp.text
    assert await _task_count() == before + 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("_signing")
async def test_command_with_null_payload_is_accepted(authed_admin_client: AsyncClient) -> None:
    wid, _ = await _register(authed_admin_client)
    resp = await _push(authed_admin_client, "command", wid, None)
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
@pytest.mark.usefixtures("_signing")
async def test_row_stored_before_the_guard_is_still_delivered(
    authed_admin_client: AsyncClient,
) -> None:
    """Only on write: a push task stored before this validation is still delivered."""
    wid, key = await _register(authed_admin_client)
    resp = await _push(authed_admin_client, "command", wid, {"task_env": ["API_TOKEN"]})
    assert resp.status_code == 200, resp.text
    task_id = int(resp.json()["task_id"])
    legacy = {"task_env": ["API_TOKEN=legacy_value", "hf_legacy"]}
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET payload = CAST(:p AS jsonb) WHERE id = :id"),
                {"p": json.dumps(legacy), "id": task_id},
            )
    finally:
        await engine.dispose()

    poll = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll", headers={"X-API-Key": key}, json={"status": "idle"}
    )
    assert poll.status_code == 200, poll.text
    task = poll.json()["task"]
    assert task is not None and task["task_id"] == task_id
    assert task["payload"] == legacy
