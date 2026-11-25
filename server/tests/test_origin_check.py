"""CSRF check by `Origin`.

Every POST/PUT/PATCH/DELETE authenticated by the session cookie must come from
its own origin (`Origin` equal to `WEBAUTHN_ORIGIN` or, without it, `Sec-Fetch-Site`
`same-origin`/`none`). It is enforced by `orcaster_server.deps.require_session` (host
routes) and `AuthOriginCheckMiddleware` (`/auth/*`). Any `Authorization` header does not
exempt a request from the check, and a present cookie does not affect routes authenticated
with `X-API-Key` or a PAT.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from orcaster_server import db as app_db
from orcaster_server.config import settings
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from tests.conftest import NO_ORIGIN_HEADER, mint_pat_via_app
from tests.test_workers_api import _register

_FORM = {"HX-Request": "false"}
_FOREIGN = [
    {"Origin": "https://evil.example"},
    {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
    {NO_ORIGIN_HEADER: "1"},
]
_FOREIGN_IDS = ["foreign-origin", "null-origin", "cross-site", "same-site", "no-headers"]


async def _workspace_name() -> str:
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        row = await s.execute(
            text("SELECT name FROM workspaces WHERE id = :id"), {"id": DEFAULT_WORKSPACE_ID}
        )
        return row.scalar_one()


async def _session_is_live(client) -> bool:
    resp = await client.get("/admin/", headers=_FORM, follow_redirects=False)
    return resp.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", _FOREIGN, ids=_FOREIGN_IDS)
async def test_admin_form_post_refused_from_foreign_origin(authed_admin_client, headers) -> None:
    before = await _workspace_name()
    resp = await authed_admin_client.post(
        "/admin/workspaces/rename",
        data={"name": "Hijacked"},
        headers={**_FORM, **headers},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    assert resp.json() == {"detail": "cross-origin request refused"}
    assert await _workspace_name() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": settings.webauthn_origin},
        {"Sec-Fetch-Site": "same-origin"},
        {"Sec-Fetch-Site": "none"},
    ],
    ids=["same-origin", "fetch-metadata-same-origin", "fetch-metadata-none"],
)
async def test_admin_form_post_accepted_from_own_origin(authed_admin_client, headers) -> None:
    new_name = f"Renamed {uuid.uuid4().hex[:8]}"
    resp = await authed_admin_client.post(
        "/admin/workspaces/rename",
        data={"name": new_name},
        headers={**_FORM, **headers},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert await _workspace_name() == new_name


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", _FOREIGN, ids=_FOREIGN_IDS)
async def test_auth_post_refused_from_foreign_origin(authed_admin_client, headers) -> None:
    resp = await authed_admin_client.post(
        "/auth/logout", headers={**_FORM, **headers}, follow_redirects=False
    )
    assert resp.status_code == 403
    assert await _session_is_live(authed_admin_client)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": settings.webauthn_origin},
        {"Sec-Fetch-Site": "same-origin"},
        {"Sec-Fetch-Site": "none"},
    ],
    ids=["same-origin", "fetch-metadata-same-origin", "fetch-metadata-none"],
)
async def test_auth_post_accepted_from_own_origin(authed_admin_client, headers) -> None:
    resp = await authed_admin_client.post(
        "/auth/logout", headers={**_FORM, **headers}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


@pytest.mark.asyncio
async def test_delete_refused_from_foreign_origin(authed_admin_client) -> None:
    resp = await authed_admin_client.delete(
        f"/admin/tasks/{uuid.uuid4()}",
        headers={**_FORM, "Origin": "https://evil.example"},
    )
    assert resp.status_code == 403
    assert resp.json() == {"detail": "cross-origin request refused"}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
async def test_auth_middleware_refuses_every_unsafe_method(authed_admin_client, method) -> None:
    """The `/auth/*` middleware cuts in before routing: any unsafe method."""
    resp = await authed_admin_client.request(
        method, "/auth/logout", headers={**_FORM, "Origin": "https://evil.example"}
    )
    assert resp.status_code == 403
    assert resp.json() == {"detail": "cross-origin request refused"}


@pytest.mark.asyncio
async def test_safe_methods_are_not_checked(authed_admin_client) -> None:
    resp = await authed_admin_client.get(
        "/admin/", headers={**_FORM, "Origin": "https://evil.example"}, follow_redirects=False
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_bearer_api_call_is_unaffected(app_client) -> None:
    """PAT via `Authorization: Bearer`: without a session cookie the check does not apply."""
    token, _ = await mint_pat_via_app()
    resp = await app_client.delete(
        "/api/v1/tasks/999999",
        headers={"Authorization": f"Bearer {token}", "Origin": "https://evil.example"},
    )
    # The request reaches the handler (task does not exist -> 404); the middleware does not cut it.
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_pat_route_ignores_a_session_cookie(authed_admin_client) -> None:
    """The cookie does not authenticate `/api/v1`: with a valid PAT, a cookie and a foreign
    origin, the request passes."""
    token, _ = await mint_pat_via_app()
    resp = await authed_admin_client.delete(
        "/api/v1/tasks/999999",
        headers={"Authorization": f"Bearer {token}", "Origin": "https://evil.example"},
    )
    assert resp.status_code == 404


async def _share_link_count() -> int:
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        row = await s.execute(text("SELECT count(*) FROM workspace_share_token"))
        return row.scalar_one()


_EVIL_WITH_GARBAGE_BEARER = {
    **_FORM,
    "Origin": "https://evil.example",
    "Authorization": "Bearer garbage",
}


@pytest.mark.asyncio
async def test_garbage_bearer_does_not_bypass_check_on_share_link_mint(
    authed_admin_client,
) -> None:
    """First defect of the check: a Bearer that authenticates nothing does not exempt it.

    The handler authenticates with the cookie, so the cookie check applies.
    """
    before = await _share_link_count()
    resp = await authed_admin_client.post(
        "/admin/share-links",
        data={"label": "csrf"},
        headers=_EVIL_WITH_GARBAGE_BEARER,
        follow_redirects=False,
    )
    assert resp.status_code == 403
    assert resp.json() == {"detail": "cross-origin request refused"}
    assert await _share_link_count() == before


@pytest.mark.asyncio
async def test_garbage_bearer_does_not_bypass_check_on_workspace_rename(
    authed_admin_client,
) -> None:
    before = await _workspace_name()
    resp = await authed_admin_client.post(
        "/admin/workspaces/rename",
        data={"name": "Hijacked"},
        headers=_EVIL_WITH_GARBAGE_BEARER,
        follow_redirects=False,
    )
    assert resp.status_code == 403
    assert await _workspace_name() == before


@pytest.mark.asyncio
async def test_garbage_bearer_does_not_bypass_check_on_logout(authed_admin_client) -> None:
    resp = await authed_admin_client.post(
        "/auth/logout", headers=_EVIL_WITH_GARBAGE_BEARER, follow_redirects=False
    )
    assert resp.status_code == 403
    assert await _session_is_live(authed_admin_client)


async def _worker_status(worker_id: str) -> str:
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        row = await s.execute(text("SELECT status FROM workers WHERE id = :id"), {"id": worker_id})
        return row.scalar_one()


@pytest.mark.asyncio
async def test_stale_session_cookie_does_not_block_worker_api_key(app_client) -> None:
    """Second defect of the check: the cookie does not authenticate `/v1/workers/*`, so it
    is ignored.

    A worker with `X-API-Key` and a stale session cookie, without `Origin` or
    `Sec-Fetch-Site`, must complete the goodbye just like without a cookie.
    """
    worker_id, api_key = await _register(app_client)
    assert await _worker_status(worker_id) != "offline"
    app_client.cookies.set("session", "stale-token")
    resp = await app_client.post(
        f"/v1/workers/{worker_id}/goodbye",
        headers={"X-API-Key": api_key, NO_ORIGIN_HEADER: "1"},
    )
    assert resp.status_code == 200, resp.text
    assert await _worker_status(worker_id) == "offline"


@pytest.mark.asyncio
async def test_pat_route_with_stale_cookie_and_no_origin_is_unaffected(app_client) -> None:
    token, _ = await mint_pat_via_app()
    app_client.cookies.set("session", "stale-token")
    resp = await app_client.delete(
        "/api/v1/tasks/999999",
        headers={"Authorization": f"Bearer {token}", NO_ORIGIN_HEADER: "1"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_stale_cookie_on_admin_route_without_origin_is_refused(app_client) -> None:
    """On a cookie-authenticated route the check runs before it is resolved."""
    app_client.cookies.set("session", "stale-token")
    resp = await app_client.post(
        "/admin/workspaces/rename",
        data={"name": "Hijacked"},
        headers={**_FORM, NO_ORIGIN_HEADER: "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_post_without_session_cookie_is_not_checked(app_client) -> None:
    """Without a cookie there is no ambient credential to abuse: it passes to the handler
    (422 here)."""
    resp = await app_client.post(
        "/v1/workers/register", json={}, headers={"Origin": "https://evil.example"}
    )
    assert resp.status_code == 422
