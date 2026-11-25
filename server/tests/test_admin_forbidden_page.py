"""A signed-in member who hits an owner-only /admin route sees a 403 page.

The member is authenticated: sending them to the sign-in page (303 for a browser
navigation, 401 + HX-Redirect for htmx) hides the real reason and, after signing
in again, lands them on the same refusal. Only a missing or invalid session (401)
redirects to sign-in.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from orcaster_server.config import settings
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from tests.conftest import browser_origin_hook

_SAME_ORIGIN = {"Origin": settings.webauthn_origin}
_HTML = {"Accept": "text/html,application/xhtml+xml"}


async def _member_client() -> AsyncClient:
    from httpx import ASGITransport

    from orcaster_server import db as app_db
    from orcaster_server.auth.config import AuthConfig
    from orcaster_server.auth.service.session import create_session
    from orcaster_server.main import app
    from orcaster_server.workspaces.service import add_member

    cfg = AuthConfig(rp_id="test", rp_name="Test", origin="http://test", cookie_name="session")
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        row = await s.execute(
            text("INSERT INTO users (display_name) VALUES (:n) RETURNING id"),
            {"n": "MemberForbiddenPage"},
        )
        user_id = str(row.scalar_one())
        await add_member(s, workspace_id=DEFAULT_WORKSPACE_ID, user_id=user_id, role="member")
        token = await create_session(s, cfg, user_id=user_id, user_agent=None, ip=None)
        await s.commit()

    client = AsyncClient(
        event_hooks={"request": [browser_origin_hook(settings.webauthn_origin)]},
        transport=ASGITransport(app=app),
        base_url="http://test",
    )
    client.cookies.set("session", token)
    return client


@pytest.mark.asyncio
async def test_member_browser_navigation_to_owner_page_gets_403_page(app_client) -> None:
    client = await _member_client()
    try:
        resp = await client.get("/admin/share-links", headers=_HTML, follow_redirects=False)
    finally:
        await client.aclose()
    assert resp.status_code == 403, (resp.status_code, resp.headers.get("location"))
    assert "location" not in resp.headers
    assert resp.headers["content-type"].startswith("text/html")
    assert "owner role required" in resp.text
    assert "/auth/login" not in resp.headers.get("location", "")


@pytest.mark.asyncio
async def test_member_htmx_post_to_owner_route_gets_403_not_login(app_client) -> None:
    client = await _member_client()
    try:
        resp = await client.post(
            "/admin/share-links",
            data={"label": "x"},
            headers={"HX-Request": "true", "HX-Current-URL": "http://test/admin/", **_SAME_ORIGIN},
            follow_redirects=False,
        )
    finally:
        await client.aclose()
    assert resp.status_code == 403, (resp.status_code, dict(resp.headers))
    assert "HX-Redirect" not in resp.headers


@pytest.mark.asyncio
async def test_anonymous_browser_navigation_still_redirects_to_sign_in(app_client) -> None:
    resp = await app_client.get(
        "/admin/share-links", headers={**_HTML, "HX-Request": "false"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/auth/login?next=")


@pytest.mark.asyncio
async def test_anonymous_htmx_request_still_gets_hx_redirect(app_client) -> None:
    resp = await app_client.get("/admin/share-links", follow_redirects=False)
    assert resp.status_code == 401
    assert resp.headers["HX-Redirect"].startswith("/auth/login")
