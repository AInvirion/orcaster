"""Shape of `payload.egress_allowlist` on the three routes that store a payload.

Creating a task already validated the shape; `POST
/admin/workers/{id}/command` and `POST /admin/workers/{id}/control-op` did not
check it, so an `"egress_allowlist": "example.com"` (without the brackets) was
stored and signed, and the worker turned a value that is not a list of strings
into `None` — that is, no egress restriction. Now all three go through
`validate_payload_egress_allowlist`, the same helper and the same message.

Validation happens only on write: a row stored earlier is still
delivered in the poll unchanged. `test_worker_command_task_env_validation.py`
covers that for `task_env`; here it is covered for
`egress_allowlist` on the `command` route.
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
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server import signing
from orcaster_server.config import settings
from orcaster_server.env_names import validate_payload_egress_allowlist
from orcaster_server.schemas.tasks import AdminCreateTaskRequest
from tests.conftest import mint_token_via_app

# The only message: the one that creating a task already used.
MESSAGE = "payload.egress_allowlist must be a list of strings"

# Distinctive marker: it must not appear in any byte of the 422 response. A
# mistyped value may be an internal host that we do not want echoed in the error.
MARK = "secret-host.internal"

# Values that all three routes must reject with `MESSAGE`.
INVALID = [
    "example.com",
    f"{MARK}",
    [1],
    [MARK, 2],
    {"host": "example.com"},
    123,
    True,
]
INVALID_IDS = ["string", "marked-string", "int-entry", "str-then-int", "dict", "int", "bool"]

# Values that all three routes must accept, as today.
VALID = [
    ["example.com"],
    ["pypi.org", ".pythonhosted.org"],
    [],
    None,
]
VALID_IDS = ["one-host", "two-hosts", "empty-list", "null"]

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

_ABSENT = object()


# --- The shared helper ------------------------------------------------------


@pytest.mark.parametrize("value", INVALID, ids=INVALID_IDS)
def test_helper_rejects_a_value_that_is_not_a_list_of_strings(value: Any) -> None:
    with pytest.raises(ValueError) as exc:
        validate_payload_egress_allowlist({"egress_allowlist": value})
    assert str(exc.value) == MESSAGE


@pytest.mark.parametrize("value", VALID, ids=VALID_IDS)
def test_helper_accepts_a_list_of_strings_and_null(value: Any) -> None:
    validate_payload_egress_allowlist({"egress_allowlist": value})


def test_helper_accepts_an_absent_key() -> None:
    validate_payload_egress_allowlist({})
    validate_payload_egress_allowlist({"commands": ["echo hi"]})


# --- Route 1: create a task -------------------------------------------------


def _create_request(payload: dict) -> AdminCreateTaskRequest:
    return AdminCreateTaskRequest(target="1", payload=payload)


@pytest.mark.parametrize("value", INVALID, ids=INVALID_IDS)
def test_create_rejects_a_value_that_is_not_a_list_of_strings(value: Any) -> None:
    """Creating a task keeps the message for the same inputs."""
    with pytest.raises(ValidationError) as exc:
        _create_request({"commands": ["echo hi"], "egress_allowlist": value})
    assert MESSAGE in str(exc.value)


@pytest.mark.parametrize("value", VALID, ids=VALID_IDS)
def test_create_accepts_a_list_of_strings_and_null(value: Any) -> None:
    req = _create_request({"commands": ["echo hi"], "egress_allowlist": value})
    assert req.payload["egress_allowlist"] == value


# --- Routes 2 and 3: command and control-op ---------------------------------


@pytest.fixture(scope="module")
def _keypair(tmp_path_factory: pytest.TempPathFactory) -> Path:
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path_factory.mktemp("ed25519_egress") / "server.pem"
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


@pytest.mark.asyncio
@pytest.mark.usefixtures("_signing")
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("value", INVALID, ids=INVALID_IDS)
async def test_push_route_rejects_a_value_that_is_not_a_list_of_strings(
    authed_admin_client: AsyncClient, route: str, value: Any
) -> None:
    """422 with the create message, no row inserted and no echo of the payload."""
    wid, _ = await _register(authed_admin_client)
    before = await _task_count()
    resp = await _push(authed_admin_client, route, wid, {"egress_allowlist": value})
    assert resp.status_code == 422, resp.text
    assert MESSAGE in resp.text
    errors = resp.json()["detail"]
    assert isinstance(errors, list) and errors
    for err in errors:
        assert "input" not in err, err
        assert "ctx" not in err, err
    assert MARK not in resp.text
    assert await _task_count() == before


@pytest.mark.asyncio
@pytest.mark.usefixtures("_signing")
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("value", VALID, ids=VALID_IDS)
async def test_push_route_accepts_a_list_of_strings_and_null(
    authed_admin_client: AsyncClient, route: str, value: Any
) -> None:
    wid, _ = await _register(authed_admin_client)
    before = await _task_count()
    resp = await _push(authed_admin_client, route, wid, {"egress_allowlist": value})
    assert resp.status_code == 200, resp.text
    assert await _task_count() == before + 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("_signing")
@pytest.mark.parametrize("route", ROUTES)
async def test_push_route_accepts_an_absent_key(
    authed_admin_client: AsyncClient, route: str
) -> None:
    wid, _ = await _register(authed_admin_client)
    before = await _task_count()
    resp = await _push(authed_admin_client, route, wid, {"reason": "x"})
    assert resp.status_code == 200, resp.text
    assert await _task_count() == before + 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("_signing")
async def test_row_stored_before_the_guard_is_still_delivered(
    authed_admin_client: AsyncClient,
) -> None:
    """Only on write: a row with a mistyped `egress_allowlist` is still
    delivered in the poll as stored (validation happens only on write)."""
    wid, key = await _register(authed_admin_client)
    resp = await _push(authed_admin_client, "command", wid, {"egress_allowlist": ["example.com"]})
    assert resp.status_code == 200, resp.text
    task_id = int(resp.json()["task_id"])
    legacy = {"egress_allowlist": "example.com"}
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
