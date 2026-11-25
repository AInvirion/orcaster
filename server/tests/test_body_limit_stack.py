"""Order of `BodyLimitMiddleware` in the real app: no DB.

The limit must wrap the app and any middleware that can read the body
(`AuthOriginCheckMiddleware`), but sit INSIDE the middlewares that
stamp headers (`StatusSecurityHeadersMiddleware`, `PlaneSecurityHeadersMiddleware`,
`_admin_cache_control`): this way its early 413/400 also carries the `/status/*`
headers, the anti-framing headers of `/admin/*` and `/auth/*`, and the
`Cache-Control: no-store` of `/admin/*`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from fastapi import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute

from orcaster_server.auth.origin import AuthOriginCheckMiddleware
from orcaster_server.body_limit import BodyLimitMiddleware
from orcaster_server.config import settings
from orcaster_server.main import (
    _PLANE_CSP,
    _STATUS_CSP,
    PlaneSecurityHeadersMiddleware,
    StatusSecurityHeadersMiddleware,
    app,
)

OVER_DEFAULT = settings.max_request_body_bytes + 1


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _assert_status_plane_headers(r: httpx.Response) -> None:
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["content-security-policy"] == _STATUS_CSP
    assert r.headers["cache-control"] == "no-store"


@pytest.fixture
def status_post_route() -> Iterator[str]:
    """Temporary POST route under `/status/` that reads the body (the plane only has GET today).

    Without a route that reads the body, a chunked POST never reaches the limit.
    It is removed at the end so as to leave no trace in the shared app.
    """

    async def _read(request: Request) -> JSONResponse:
        return JSONResponse({"n": len(await request.body())})

    path = "/status/_body-limit-test"
    before = list(app.router.routes)
    app.router.add_route(path, _read, methods=["POST"])
    added: list[BaseRoute] = [r for r in app.router.routes if r not in before]
    try:
        yield path
    finally:
        for route in added:
            app.router.routes.remove(route)


@pytest.mark.asyncio
async def test_status_413_by_content_length_carries_the_security_headers() -> None:
    async with _client() as c:
        r = await c.post("/status/sometoken", content=b"x" * OVER_DEFAULT)
    assert r.status_code == 413
    _assert_status_plane_headers(r)


@pytest.mark.asyncio
async def test_status_400_invalid_content_length_carries_the_security_headers() -> None:
    async with _client() as c:
        r = await c.post("/status/sometoken", content=b"", headers={"content-length": "abc"})
    assert r.status_code == 400
    _assert_status_plane_headers(r)


@pytest.mark.asyncio
async def test_status_413_chunked_carries_the_security_headers(status_post_route: str) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(5):
            yield b"x" * (settings.max_request_body_bytes // 4)

    async with _client() as c:
        r = await c.post(status_post_route, content=chunks())
    assert r.status_code == 413
    _assert_status_plane_headers(r)


@pytest.mark.asyncio
async def test_admin_413_carries_no_store() -> None:
    async with _client() as c:
        r = await c.post("/admin/tasks", content=b"x" * OVER_DEFAULT)
    assert r.status_code == 413
    assert r.headers["cache-control"] == "no-store"
    _assert_plane_headers(r)


def _assert_plane_headers(r: httpx.Response) -> None:
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"] == _PLANE_CSP
    assert r.headers["referrer-policy"] == "same-origin"


@pytest.mark.asyncio
async def test_auth_413_carries_the_plane_headers() -> None:
    async with _client() as c:
        r = await c.post("/auth/login/begin", content=b"x" * OVER_DEFAULT)
    assert r.status_code == 413
    _assert_plane_headers(r)


def test_middleware_order_body_limit_inside_headers_outside_auth_origin() -> None:
    """`app.user_middleware[0]` is the outermost: a reorder must fail here."""
    order = [m.cls for m in app.user_middleware]
    body_limit = order.index(BodyLimitMiddleware)
    assert order.index(StatusSecurityHeadersMiddleware) < body_limit, (
        "BodyLimit must sit inside StatusSecurityHeaders so that its 413/400 "
        "carries the /status/* headers"
    )
    assert order.index(PlaneSecurityHeadersMiddleware) < body_limit, (
        "BodyLimit must sit inside PlaneSecurityHeaders so that its 413/400 "
        "carries the /admin and /auth headers"
    )
    assert body_limit < order.index(AuthOriginCheckMiddleware), (
        "BodyLimit must wrap AuthOriginCheck (and anything that can read the body)"
    )
