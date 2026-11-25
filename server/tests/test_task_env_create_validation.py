"""Validation of `payload.task_env` when CREATING a task, over HTTP.

Each entry must be an environment variable name (`[A-Za-z_][A-Za-z0-9_]*`,
ASCII). A `NAME=value`, a space, a trailing newline or the empty string are
rejected with a 422 that names the entry's INDEX and does not repeat it: not in `msg`,
nor in `detail[].input` (the full payload pydantic attaches), nor in `ctx`.
An entry that STARTS with a known token prefix (`hf_`, `ghp_`,
`glpat-`, `sk-`...) is rejected all the same, with its own message: a prefix guard,
not a secret detector.

The four creation routes (`POST /admin/tasks`, `POST /api/v1/tasks` and the two
forks) go through `AdminCreateTaskRequest`. Validation applies to creation only: an
old row with an invalid entry is still read, served in the poll and
returned unchanged; forking it does validate again.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import mint_pat_via_app, mint_token_via_app

# Distinctive marker: it must not appear in ANY byte of the 422 response.
MARK = "SECRETMARK123"

ROUTES = ["admin_create", "api_create", "admin_fork", "api_fork"]

CREDENTIAL_MSG = (
    "payload.task_env[{i}] looks like a credential (known token prefix), not a variable "
    "name; declare the variable name only"
)

REGISTER = {
    "hostname": "w",
    "os": "linux",
    "arch": "x86_64",
    "ram_mb": 1024,
    "disk_mb": 10000,
    "tags": [],
    "capabilities": {},
}


def _db_url() -> str:
    from orcaster_server.config import settings

    return settings.database_url


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _scalar(sql: str, params: dict[str, Any] | None = None) -> Any:
    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql), params or {})).scalar_one()
    finally:
        await engine.dispose()


async def _task_count() -> int:
    return int(await _scalar("SELECT count(*) FROM tasks"))


async def _stored_payload(task_id: int) -> dict:
    raw = await _scalar("SELECT payload FROM tasks WHERE id = :id", {"id": task_id})
    return raw if isinstance(raw, dict) else json.loads(raw)


async def _overwrite_payload(task_id: int, payload: dict) -> None:
    """Simulates a row from before creation-time validation: writes the payload without going
    through the request."""
    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET payload = CAST(:p AS jsonb) WHERE id = :id"),
                {"p": json.dumps(payload), "id": task_id},
            )
    finally:
        await engine.dispose()


async def _create_valid(client: AsyncClient, **body: Any) -> int:
    resp = await client.post("/admin/tasks", json={"target": "1", **body})
    assert resp.status_code == 200, resp.text
    return int(resp.json()["task_id"])


async def _submit(client: AsyncClient, route: str, task_env: Any) -> Response:
    """Sends `task_env` through the given route (the fork starts from a valid parent)."""
    payload = {"commands": ["echo hi"], "task_env": task_env}
    if route == "admin_create":
        return await client.post("/admin/tasks", json={"target": "1", "payload": payload})
    token, _ = await mint_pat_via_app()
    if route == "api_create":
        return await client.post("/api/v1/tasks", headers=_bearer(token), json={"payload": payload})
    parent_id = await _create_valid(client, payload={"commands": ["echo hi"]})
    if route == "admin_fork":
        return await client.post(
            f"/admin/tasks/{parent_id}/fork", json={"target": "1", "payload": payload}
        )
    assert route == "api_fork", route
    return await client.post(
        f"/api/v1/tasks/{parent_id}/fork", headers=_bearer(token), json={"payload": payload}
    )


def _assert_no_echo(resp: Response, *fragments: str) -> None:
    """422 with no `input` or `ctx` in any error and no fragment in the body."""
    assert resp.status_code == 422, resp.text
    body = resp.json()
    errors = body["detail"]
    assert isinstance(errors, list) and errors
    for err in errors:
        assert "input" not in err, err
        assert "ctx" not in err, err
    for fragment in fragments:
        assert fragment not in resp.text


# --- the four creation routes -----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "entry",
    [
        f"API_TOKEN={MARK}",
        f"ORCH_X={MARK}",
        f"{MARK} x",
        f"{MARK}\n",
        f"{MARK}-x",
        f"{MARK}.x",
        "",
    ],
    ids=["name-eq-value", "orch-eq-value", "space", "trailing-newline", "dash", "dot", "empty"],
)
async def test_invalid_entry_is_422_naming_the_index_without_echo(
    authed_admin_client: AsyncClient, route: str, entry: str
) -> None:
    before = await _task_count()
    resp = await _submit(authed_admin_client, route, ["OK", entry])
    _assert_no_echo(resp, MARK)
    assert "payload.task_env[1] is not a valid environment variable name" in resp.text
    assert "NAME=value" in resp.text
    # The fork starts from a parent: we only check that no new task was born.
    assert await _task_count() == before + (1 if route.endswith("fork") else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_orch_prefixed_name_is_422_naming_the_index_without_echo(
    authed_admin_client: AsyncClient, route: str
) -> None:
    """`ORCH_<marker>` has the shape of a valid name: the prefix rule stops it."""
    resp = await _submit(authed_admin_client, route, ["OK", "API_TOKEN", f"ORCH_{MARK}"])
    _assert_no_echo(resp, MARK)
    assert "payload.task_env[2] may not declare ORCH_ vars" in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_non_string_entry_is_422_naming_the_index_without_echo(
    authed_admin_client: AsyncClient, route: str
) -> None:
    resp = await _submit(authed_admin_client, route, ["OK", {"API_TOKEN": MARK}])
    _assert_no_echo(resp, MARK)
    assert "payload.task_env[1] must be a string" in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_non_list_task_env_is_422_without_echo(
    authed_admin_client: AsyncClient, route: str
) -> None:
    resp = await _submit(authed_admin_client, route, MARK)
    _assert_no_echo(resp, MARK)
    assert "payload.task_env must be a list of strings" in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_valid_names_are_accepted(authed_admin_client: AsyncClient, route: str) -> None:
    names = [
        "HF_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "SK_X",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "_X",
        "GIT_CONFIG_KEY_0",
    ]
    resp = await _submit(authed_admin_client, route, names)
    assert resp.status_code in (200, 201), resp.text
    assert (await _stored_payload(resp.json()["task_id"]))["task_env"] == names


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "prefix", ["hf_", "ghp_", "glpat-", "sk-or-"], ids=["hf", "ghp", "glpat", "sk-or"]
)
async def test_token_prefixed_entry_is_422_naming_the_index_without_echo(
    authed_admin_client: AsyncClient, route: str, prefix: str
) -> None:
    """Starts with a token prefix: 422 with the index and without any byte of the entry."""
    before = await _task_count()
    resp = await _submit(authed_admin_client, route, ["OK", f"{prefix}{MARK}"])
    _assert_no_echo(resp, MARK, prefix)
    assert CREDENTIAL_MSG.format(i=1) in resp.text
    assert await _task_count() == before + (1 if route.endswith("fork") else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("token", ["AKIAIOSFODNN7EXAMPLE", "AbCdEf1234567890"])
async def test_credential_without_a_known_prefix_is_accepted_by_design(
    authed_admin_client: AsyncClient, route: str, token: str
) -> None:
    """The rule is a PREFIX and SHAPE guard, not a secret detector.

    A token without a prefix from the closed list (an AWS key, an opaque token) is
    a string of letters, digits and underscore: a valid name, and it is stored. The
    server uses no entropy or heuristics beyond the prefix; extending the list is a
    deliberate change, not a case of this test.
    """
    resp = await _submit(authed_admin_client, route, [token])
    assert resp.status_code in (200, 201), resp.text
    assert (await _stored_payload(resp.json()["task_id"]))["task_env"] == [token]


# --- which routes and which 422 fields are redacted ---------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/admin/tasks",
        "/admin/tasks/7/fork",
        "/api/v1/tasks",
        "/api/v1/tasks/7/fork",
        "/api/v1/secrets/API_TOKEN",
        "/admin/workers/abc/command",
        "/admin/workers/abc/control-op",
    ],
)
def test_echo_free_paths(path: str) -> None:
    from orcaster_server.main import _echoes_no_input

    assert _echoes_no_input(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/admin/tasks/bulk/cancel",
        "/admin/tasks/7/retry",
        "/api/v1/tasks/7",
        "/api/v1/tasks/7/cancel",
        "/api/v1/workers",
        "/v1/workers/abc/poll",
        "/admin/workers/abc/pause",
        "/admin/workers/abc/command/x",
    ],
)
def test_other_paths_keep_the_default_error_model(path: str) -> None:
    from orcaster_server.main import _echoes_no_input

    assert _echoes_no_input(path) is False


def test_drop_echoed_input_keeps_the_useful_ctx() -> None:
    from orcaster_server.main import _drop_echoed_input

    errors = [
        {
            "type": "value_error",
            "loc": ("payload",),
            "msg": "m",
            "input": {"a": MARK},
            "ctx": {"error": ValueError(MARK)},
        },
        {
            "type": "greater_than_equal",
            "loc": ("priority",),
            "msg": "m",
            "input": -1,
            "ctx": {"ge": 1},
        },
    ]
    assert _drop_echoed_input(errors) == [
        {"type": "value_error", "loc": ("payload",), "msg": "m"},
        {"type": "greater_than_equal", "loc": ("priority",), "msg": "m", "ctx": {"ge": 1}},
    ]


@pytest.mark.asyncio
async def test_422_of_another_field_keeps_its_constraint_in_ctx(
    authed_admin_client: AsyncClient,
) -> None:
    resp = await authed_admin_client.post(
        "/admin/tasks", json={"target": "1", "claim_timeout_seconds": 0}
    )
    assert resp.status_code == 422, resp.text
    err = resp.json()["detail"][0]
    assert err["ctx"] == {"ge": 1}
    assert "input" not in err


# --- the dashboard shows `loc: msg` truncated to 300 characters ---------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad", [f"API_TOKEN={MARK}", f"ORCH_{MARK}", f"ghp_{MARK}", f"glpat-{MARK}", {"k": MARK}]
)
async def test_dashboard_error_line_fits_in_300_chars_without_echo(
    authed_admin_client: AsyncClient, bad: Any
) -> None:
    """`submitNewTask` builds `Error <status>: <loc>: <msg>` and truncates to 300 (app.js).

    With the widest index of the test the text fits entirely, so the
    truncation neither hides the index nor lets through anything not already in `msg`.
    """
    entries = ["OK"] * 99 + [bad]
    resp = await authed_admin_client.post(
        "/admin/tasks", json={"target": "1", "payload": {"task_env": entries}}
    )
    _assert_no_echo(resp, MARK)
    err = resp.json()["detail"][0]
    line = f"Error {resp.status_code}: {'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
    assert "task_env[99]" in line
    assert len(line) <= 300, len(line)


# --- idempotency -------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotency_replay_of_a_valid_task_returns_the_stored_task(
    authed_admin_client: AsyncClient,
) -> None:
    key = str(uuid.uuid4())
    body = {"target": "1", "payload": {"task_env": ["API_TOKEN"]}, "idempotency_key": key}
    first = await authed_admin_client.post("/admin/tasks", json=body)
    assert first.status_code == 200, first.text
    replay = await authed_admin_client.post("/admin/tasks", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["task_id"] == first.json()["task_id"]
    assert await _scalar("SELECT count(*) FROM tasks WHERE idempotency_key = :k", {"k": key}) == 1


@pytest.mark.asyncio
async def test_idempotency_key_with_an_invalid_entry_is_422_and_creates_nothing(
    authed_admin_client: AsyncClient,
) -> None:
    key = str(uuid.uuid4())
    body = {
        "target": "1",
        "payload": {"task_env": [f"API_TOKEN={MARK}"]},
        "idempotency_key": key,
    }
    resp = await authed_admin_client.post("/admin/tasks", json=body)
    _assert_no_echo(resp, MARK)
    assert await _scalar("SELECT count(*) FROM tasks WHERE idempotency_key = :k", {"k": key}) == 0


@pytest.mark.asyncio
async def test_idempotency_replay_of_a_legacy_row_with_an_invalid_entry_is_422(
    authed_admin_client: AsyncClient,
) -> None:
    """The body is validated before looking up the key: resending the body of a row from before
    creation-time validation with an invalid entry answers 422, not the stored task,
    and stores nothing."""
    key = str(uuid.uuid4())
    body = {
        "target": "1",
        "payload": {"commands": ["echo hi"], "task_env": ["OK"]},
        "idempotency_key": key,
    }
    first = await authed_admin_client.post("/admin/tasks", json=body)
    assert first.status_code == 200, first.text
    task_id = int(first.json()["task_id"])
    legacy = {"commands": ["echo hi"], "task_env": ["OK", f"API_TOKEN={MARK}"]}
    await _overwrite_payload(task_id, legacy)

    replay = await authed_admin_client.post("/admin/tasks", json={**body, "payload": legacy})
    _assert_no_echo(replay, MARK)
    assert "payload.task_env[1] is not a valid environment variable name" in replay.text
    assert await _scalar("SELECT count(*) FROM tasks WHERE idempotency_key = :k", {"k": key}) == 1
    assert await _stored_payload(task_id) == legacy


# --- older rows: still read, served and returned unchanged -------------------

LEGACY_PAYLOADS = [
    {"commands": ["echo hi"], "task_env": ["API_TOKEN=legacy_value", "weird-name", "", "A B"]},
    {"commands": ["echo hi"], "task_env": {"NOTE": "example_tool==0.7.5"}},
    # Stored before the token-prefix rule: still read and served.
    {"commands": ["echo hi"], "task_env": ["hf_legacy", "ghp_legacy", "glpat-legacy", "sk-or-x"]},
]
LEGACY_IDS = ["invalid-entries", "dict", "token-prefixed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", LEGACY_PAYLOADS, ids=LEGACY_IDS)
async def test_legacy_row_is_still_read_unchanged(
    authed_admin_client: AsyncClient, legacy: dict
) -> None:
    task_id = await _create_valid(authed_admin_client, payload={"commands": ["echo hi"]})
    await _overwrite_payload(task_id, legacy)

    token, _ = await mint_pat_via_app()
    resp = await authed_admin_client.get(f"/api/v1/tasks/{task_id}", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["payload"] == legacy

    page = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    assert page.status_code == 200, page.text
    assert await _stored_payload(task_id) == legacy


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", LEGACY_PAYLOADS, ids=LEGACY_IDS)
async def test_legacy_row_is_still_served_by_poll_unchanged(
    authed_admin_client: AsyncClient, legacy: dict
) -> None:
    task_id = await _create_valid(authed_admin_client, payload={"commands": ["echo hi"]})
    await _overwrite_payload(task_id, legacy)

    reg = await authed_admin_client.post(
        "/v1/workers/register", json={**REGISTER, "enrollment_token": await mint_token_via_app()}
    )
    assert reg.status_code == 200, reg.text
    worker_id, api_key = reg.json()["worker_id"], reg.json()["api_key"]
    poll = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/poll", headers={"X-API-Key": api_key}, json={"status": "idle"}
    )
    assert poll.status_code == 200, poll.text
    task = poll.json()["task"]
    assert task is not None and task["task_id"] == task_id
    assert task["payload"] == legacy
    assert task["secrets"] == {}


@pytest.mark.asyncio
async def test_fork_of_a_legacy_row_with_an_invalid_entry_is_now_422(
    authed_admin_client: AsyncClient,
) -> None:
    """Accepted behavior change: forking is creating, so it validates again.

    The fork inherits the parent's stored `task_env`; if it carries an invalid entry
    the fork answers 422 with the index (and without the entry). Editing the entry in
    the fork's override unblocks it.
    """
    legacy = {"commands": ["echo hi"], "task_env": ["OK", f"API_TOKEN={MARK}"]}
    parent_id = await _create_valid(authed_admin_client, payload={"commands": ["echo hi"]})
    await _overwrite_payload(parent_id, legacy)

    token, _ = await mint_pat_via_app()
    resp = await authed_admin_client.post(
        f"/api/v1/tasks/{parent_id}/fork", headers=_bearer(token), json={}
    )
    _assert_no_echo(resp, MARK)
    assert "payload.task_env[1] is not a valid environment variable name" in resp.text

    fixed = await authed_admin_client.post(
        f"/api/v1/tasks/{parent_id}/fork",
        headers=_bearer(token),
        json={"payload": {"task_env": ["OK", "API_TOKEN"]}},
    )
    assert fixed.status_code == 201, fixed.text


@pytest.mark.asyncio
async def test_fork_of_a_legacy_row_with_a_token_prefixed_entry_is_now_422(
    authed_admin_client: AsyncClient,
) -> None:
    """Forking is creating: the token-prefix entry is rejected without repeating it.

    The parent stays stored and readable as it was; a corrected override unblocks the fork.
    """
    legacy = {"commands": ["echo hi"], "task_env": ["OK", f"ghp_{MARK}"]}
    parent_id = await _create_valid(authed_admin_client, payload={"commands": ["echo hi"]})
    await _overwrite_payload(parent_id, legacy)

    token, _ = await mint_pat_via_app()
    resp = await authed_admin_client.post(
        f"/api/v1/tasks/{parent_id}/fork", headers=_bearer(token), json={}
    )
    _assert_no_echo(resp, MARK, "ghp_")
    assert CREDENTIAL_MSG.format(i=1) in resp.text
    assert await _stored_payload(parent_id) == legacy

    fixed = await authed_admin_client.post(
        f"/api/v1/tasks/{parent_id}/fork",
        headers=_bearer(token),
        json={"payload": {"task_env": ["OK", "GITHUB_TOKEN"]}},
    )
    assert fixed.status_code == 201, fixed.text


# --- what CAN come back in `loc` and `msg` -------------------------------------
#
# Decision: `loc` and `msg` are kept and may repeat text the caller put
# in ANOTHER field of the body — the 422 has to say which field failed and why, and without
# that the error is not actionable. What NEVER comes back is the entry of
# `payload.task_env` (its messages carry only the index), the `input` pydantic
# attaches and `ctx["error"]`. It is documented in `docs/user-manual.md`
# («What the 422 error returns and what it does not»): whoever stores a 422 body in a log or
# in a client has to know that those two fields are their own text coming back.
#
# The tests below pin both halves of that decision. The one asserting that the
# `target` marker DOES appear is deliberate: if someone trims `msg` or `loc`, this
# test fails and tells them the decision and the manual change with it.


@pytest.mark.asyncio
async def test_a_malformed_target_is_repeated_in_msg_but_task_env_is_not(
    authed_admin_client: AsyncClient,
) -> None:
    """A request that fails on both fields at once separates the two rules.

    Invalid `target`: the validator names the received value, and that comes back in `msg`.
    Invalid `payload.task_env[1]`: its message carries only the index. Both things
    in the same response, so neither can pass through the other.
    """
    target_mark = "TARGETMARK685"
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={
            "target": f"worker:{target_mark}",
            "payload": {"commands": ["echo hi"], "task_env": ["OK", f"API_TOKEN={MARK}"]},
        },
    )
    assert resp.status_code == 422, resp.text
    errors = resp.json()["detail"]
    locs = [err["loc"] for err in errors]
    assert ["body", "target"] in locs, locs
    assert ["body", "payload"] in locs, locs
    for err in errors:
        assert "input" not in err, err
        assert "error" not in (err.get("ctx") or {}), err
    # The `task_env` entry comes back nowhere, nor does its message repeat it.
    assert MARK not in resp.text
    assert "payload.task_env[1] is not a valid environment variable name" in resp.text
    # The `target` the client sent does: it is their text in a field that is not a
    # secret, and without it the 422 does not say which part of the target is wrong.
    assert target_mark in resp.text
    target_msg = next(err["msg"] for err in errors if err["loc"] == ["body", "target"])
    assert target_mark in target_msg


@pytest.mark.asyncio
async def test_an_unknown_key_comes_back_in_loc_and_only_there(
    authed_admin_client: AsyncClient,
) -> None:
    """The creation models are `extra="forbid"`: the unknown key goes in `loc`.

    It is the other path by which client text comes back in a redacted 422. The
    value accompanying that key does not come back: that is what `input` used to carry.
    """
    unknown = "KEYMARK685"
    resp = await authed_admin_client.post(
        "/admin/tasks", json={"target": "1", unknown: f"hf_{MARK}"}
    )
    assert resp.status_code == 422, resp.text
    errors = resp.json()["detail"]
    extra = [err for err in errors if err["loc"] == ["body", unknown]]
    assert extra, errors
    assert extra[0]["type"] == "extra_forbidden", extra
    for err in errors:
        assert "input" not in err, err
        assert "error" not in (err.get("ctx") or {}), err
    # The KEY comes back (once, in `loc`) and never the value that went with it.
    assert f"hf_{MARK}" not in resp.text
