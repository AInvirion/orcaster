"""The 422 is redacted by the route that ran, not by the raw request path.

`_validation_exception_handler` decides two things by looking at the path: whether the 422 may
return what the client sent (`_echoes_no_input`) and whether the response uses the error
model of `/api/v1`. If the app is served under a `root_path` and the server
leaves the prefix in the scope path, `request.url.path` is `/prefix/admin/tasks`:
Starlette strips the prefix to route, so the same route runs, but a
prefix or pattern check on the raw path stops matching **silently
and in the unsafe direction**: the 422 brings back the whole payload in
`detail[].input`, with the `task_env` entry inside.

Today nothing in the repo sets `root_path`, `--root-path`, `strip_prefix` or
`handle_path`, so these tests cover a future deployment behind a path prefix.
All three forms a deployment can take are tested:

1. without `root_path` (every current deployment);
2. with `root_path` and the proxy leaving the prefix in the path;
3. with `root_path` and the proxy stripping it.

The negative control is part of the test: a route that is NOT in the list keeps its
`input` in all three forms. Without it, an `_echoes_no_input` that returned `True`
for everything would pass every absence assertion in this file.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from starlette.requests import Request

from tests.conftest import browser_origin_hook

# Distinctive marker: it must not appear in any byte of the 422 of a redacted route.
MARK = "SECRETMARK685"
PREFIX = "/prefix"

# Routes whose body may carry the value of a secret: the four that create and
# fork a task, the push command and the control-op of a worker, and the
# `/api/v1` secrets prefix.
REDACTED_PATHS = [
    "/admin/tasks",
    "/admin/tasks/7/fork",
    "/api/v1/tasks",
    "/api/v1/tasks/7/fork",
    "/admin/workers/abc/command",
    "/admin/workers/abc/control-op",
    "/api/v1/secrets/API_TOKEN",
]
# Neighboring routes that keep the default FastAPI error model, with `input`.
ECHOING_PATHS = ["/admin/tasks/7/retry", "/api/v1/tasks/7/cancel"]

# (scope root_path, how the scope path is built). See the docstring.
DEPLOYMENTS = [
    ("", lambda p: p),
    (PREFIX, lambda p: PREFIX + p),
    (PREFIX, lambda p: p),
]
DEPLOYMENT_IDS = ["no-root-path", "prefix-kept-in-path", "prefix-stripped-by-proxy"]


def _real_errors() -> list[dict]:
    """The list of errors pydantic really produces for an invalid `task_env`.

    Building it by hand would be a test written from an assumption of what it carries: what
    matters here is that the `input` pydantic attaches is the ENTIRE payload and that
    `ctx["error"]` is the validator exception, whose text names the entry.
    """
    from orcaster_server.schemas.tasks import AdminCreateTaskRequest

    with pytest.raises(ValidationError) as caught:
        AdminCreateTaskRequest(
            target="1",
            payload={"commands": ["echo hi"], "task_env": ["OK", f"API_TOKEN={MARK}"]},
        )
    errors = caught.value.errors()
    # Control of the control: if pydantic stopped attaching the payload, every absence
    # assertion below would pass without the handler doing anything.
    assert any(MARK in json.dumps(err.get("input"), default=repr) for err in errors), errors
    return errors


def _request(scope_path: str, root_path: str) -> Request:
    """A `Request` with the scope an ASGI server builds under `root_path`."""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("127.0.0.1", 1234),
            "path": scope_path,
            "root_path": root_path,
            "query_string": b"",
            "headers": [],
        }
    )


async def _handle(scope_path: str, root_path: str) -> tuple[int, Any]:
    from orcaster_server.main import _validation_exception_handler

    resp = await _validation_exception_handler(
        _request(scope_path, root_path), RequestValidationError(_real_errors())
    )
    return resp.status_code, json.loads(bytes(resp.body))


def _detail(body: Any) -> list[dict]:
    return body["detail"]


# --- the handler, in the three deployment forms ------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", REDACTED_PATHS)
@pytest.mark.parametrize("root_path,build", DEPLOYMENTS, ids=DEPLOYMENT_IDS)
async def test_redacted_route_drops_the_echo_under_any_root_path(
    path: str, root_path: str, build: Any
) -> None:
    status, body = await _handle(build(path), root_path)
    assert status == 422
    errors = _detail(body)
    assert errors
    for err in errors:
        assert "input" not in err, err
        assert "error" not in (err.get("ctx") or {}), err
    assert MARK not in json.dumps(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ECHOING_PATHS)
@pytest.mark.parametrize("root_path,build", DEPLOYMENTS, ids=DEPLOYMENT_IDS)
async def test_other_routes_keep_their_input_under_any_root_path(
    path: str, root_path: str, build: Any
) -> None:
    """Negative control: redaction is still per route and did not become global."""
    status, body = await _handle(build(path), root_path)
    assert status == 422
    assert any("input" in err for err in _detail(body)), body
    assert MARK in json.dumps(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("root_path,build", DEPLOYMENTS, ids=DEPLOYMENT_IDS)
async def test_api_v1_error_model_survives_a_root_path(root_path: str, build: Any) -> None:
    """The `/api/v1` 422 carries `error`/`message` also under a prefix."""
    _, body = await _handle(build("/api/v1/tasks"), root_path)
    assert body["error"] == "unprocessable_entity"
    assert body["message"] == "request validation failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("root_path,build", DEPLOYMENTS, ids=DEPLOYMENT_IDS)
async def test_admin_422_keeps_the_default_shape_under_a_root_path(
    root_path: str, build: Any
) -> None:
    """And the `/admin` one is still `{"detail": [...]}`, which the dashboard consumes."""
    _, body = await _handle(build("/admin/tasks"), root_path)
    assert set(body) == {"detail"}


# --- the whole app, over HTTP, behind a path prefix --------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("root_path,build", DEPLOYMENTS, ids=DEPLOYMENT_IDS)
async def test_create_task_422_has_no_echo_served_under_a_prefix(
    authed_admin_client: AsyncClient, root_path: str, build: Any
) -> None:
    """The real app, mounted under `root_path`, requesting the path that would reach the server.

    `authed_admin_client` already left the app with engine, signing key and session; here
    only the transport changes so that the scope carries the `root_path`.
    """
    from orcaster_server.config import settings
    from orcaster_server.main import app

    transport = ASGITransport(app=app, root_path=root_path)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"HX-Request": "true"},
        cookies=authed_admin_client.cookies,
        event_hooks={"request": [browser_origin_hook(settings.webauthn_origin)]},
    ) as client:
        resp = await client.post(
            build("/admin/tasks"),
            json={
                "target": "1",
                "payload": {"commands": ["echo hi"], "task_env": ["OK", f"API_TOKEN={MARK}"]},
            },
        )
    assert resp.status_code == 422, resp.text
    assert "payload.task_env[1] is not a valid environment variable name" in resp.text
    for err in resp.json()["detail"]:
        assert "input" not in err, err
        assert "ctx" not in err, err
    assert MARK not in resp.text


# --- `_app_relative_path` directly ------------------------------------------


@pytest.mark.parametrize(
    "scope_path,root_path,expected",
    [
        # Without `root_path`: identity. It is every deployment today.
        ("/admin/tasks", "", "/admin/tasks"),
        # The proxy leaves the prefix in the path.
        ("/prefix/admin/tasks", "/prefix", "/admin/tasks"),
        ("/a/b/admin/tasks", "/a/b", "/admin/tasks"),
        # The proxy already stripped it: there is nothing to strip.
        ("/admin/tasks", "/prefix", "/admin/tasks"),
        # Trailing slash in `root_path` (the docs ask not to add it and nothing validates it).
        ("/prefix/admin/tasks", "/prefix/", "/admin/tasks"),
        # The whole prefix, with no path below it.
        ("/prefix", "/prefix", "/"),
        # It is NOT a segment prefix: `/prefix/x` starts with `/pre` and is not
        # under it. Stripping it would give `fix/x`, which is not anyone's route.
        ("/prefix/admin/tasks", "/pre", "/prefix/admin/tasks"),
    ],
    ids=[
        "no-root-path",
        "prefix-kept",
        "nested-prefix-kept",
        "prefix-stripped",
        "trailing-slash-root-path",
        "prefix-only",
        "not-a-segment-prefix",
    ],
)
def test_app_relative_path(scope_path: str, root_path: str, expected: str) -> None:
    from orcaster_server.main import _app_relative_path

    assert _app_relative_path(_request(scope_path, root_path)) == expected
