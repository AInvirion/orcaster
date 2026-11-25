"""Tests — auth enforcement + cross-tenant isolation on `/admin/*`.

Covers:
  - no session: GET HTML → 303 to /auth/login; API POST → 401; HX partial →
    401 with an `HX-Redirect` header.
  - `/healthz` and the worker API (register/poll with API key) stay open without
    a session.
  - cross-tenant isolation: an owner admin of workspace A neither sees nor mutates
    tasks of workspace B (listing excludes them; detail/cancel/delete → 404).
  - a task created by the admin is stamped with its active workspace.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from orcaster_server.config import settings
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from tests.conftest import browser_origin_hook, mint_token_via_app

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


async def _make_authed_client_for_workspace(workspace_id: str) -> AsyncClient:
    """Create an AsyncClient authenticated as owner of `workspace_id`.

    Inserts a User, makes it owner of the workspace and mints a session on the
    app's live engine. Returns a new AsyncClient (same ASGI app) with the
    `session` cookie already set. The caller closes the client.
    """
    from orcaster_server import db as app_db
    from orcaster_server.auth.config import AuthConfig
    from orcaster_server.auth.service.session import create_session
    from orcaster_server.main import app
    from orcaster_server.workspaces.service import add_member

    cfg = AuthConfig(rp_id="test", rp_name="Test", origin="http://test", cookie_name="session")
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        row = await s.execute(
            text("INSERT INTO users (display_name) VALUES ('B-Admin') RETURNING id")
        )
        user_id = str(row.scalar_one())
        await add_member(s, workspace_id=workspace_id, user_id=user_id, role="owner")
        token = await create_session(s, cfg, user_id=user_id, user_agent=None, ip=None)
        await s.commit()

    client = AsyncClient(
        event_hooks={"request": [browser_origin_hook(settings.webauthn_origin)]},
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"HX-Request": "true"},
    )
    client.cookies.set("session", token)
    return client


async def _create_workspace(name: str, slug: str) -> str:
    from orcaster_server.workspaces.service import create_workspace

    engine = create_async_engine(_db_url())
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            ws_id = await create_workspace(s, name=name, slug=slug)
            await s.commit()
            return ws_id
    finally:
        await engine.dispose()


# --- 1. No-auth: HTML 303, API 401, HX HX-Redirect -------------------------


@pytest.mark.asyncio
async def test_unauthenticated_html_page_redirects_303(app_client: AsyncClient) -> None:
    # HTML page (Accept text/html, no HX) → 303 to login with next=.
    resp = await app_client.get("/admin/", headers={"Accept": "text/html", "HX-Request": "false"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login?next=%2Fadmin%2F"


@pytest.mark.asyncio
async def test_unauthenticated_api_post_returns_401(app_client: AsyncClient) -> None:
    resp = await app_client.post(
        "/admin/tasks", json={"target": "1"}, headers={"HX-Request": "false"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_unauthenticated_hx_partial_sets_hx_redirect(app_client: AsyncClient) -> None:
    # app_client sends HX-Request: true by default, but WITHOUT HX-Current-URL.
    # Without HX-Current-URL → HX-Redirect without next= (no reference URL).
    resp = await app_client.get("/admin/tasks")
    assert resp.status_code == 401
    assert resp.headers.get("HX-Redirect") == "/auth/login"


# --- 2. Health + worker API stay open --------------------------------------


@pytest.mark.asyncio
async def test_root_redirects_to_dashboard(app_client: AsyncClient) -> None:
    # `/` is a shortcut: 307 to `/admin/`. It does not follow the redirect (httpx
    # does not by default) so as not to chain into the auth enforcement of `/admin/`.
    resp = await app_client.get("/")
    assert resp.status_code == 307
    assert resp.headers["location"] == "/admin/"


@pytest.mark.asyncio
async def test_healthz_open_without_session(app_client: AsyncClient) -> None:
    resp = await app_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_worker_api_open_without_session(app_client: AsyncClient) -> None:
    # register + poll use an API key, not a session. app_client has no cookie.
    token = await mint_token_via_app()
    reg = await app_client.post(
        "/v1/workers/register",
        json={**REGISTER, "hostname": "open", "enrollment_token": token},
    )
    assert reg.status_code == 200, reg.text
    wid, key = reg.json()["worker_id"], reg.json()["api_key"]
    poll = await app_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert poll.status_code == 200, poll.text


# --- 3. Cross-tenant isolation ---------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_task_invisible_and_404(authed_admin_client: AsyncClient) -> None:
    """Admin A (Default) neither sees nor mutates a task created in workspace B."""
    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    client_b = await _make_authed_client_for_workspace(ws_b)
    try:
        # B creates a task in its own workspace.
        create_b = await client_b.post("/admin/tasks", json={"target": "1"})
        assert create_b.status_code == 200, create_b.text
        task_b = create_b.json()["task_id"]
    finally:
        await client_b.aclose()

    # A (Default) creates its own task so there is something visible.
    create_a = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert create_a.status_code == 200
    task_a = create_a.json()["task_id"]

    # A's listing excludes B's task and includes its own. We check the detail
    # link `/admin/tasks/{id}` to avoid false positives from stray numbers in
    # the markup (counts, etc.).
    listing = await authed_admin_client.get("/admin/tasks")
    assert listing.status_code == 200
    body = listing.text
    assert f"/admin/tasks/{task_a}" in body
    assert f"/admin/tasks/{task_b}" not in body

    # Detail of B's task → 404 (without leaking existence).
    detail = await authed_admin_client.get(
        f"/admin/tasks/{task_b}", headers={"Accept": "application/json"}
    )
    assert detail.status_code == 404

    # cancel + delete of B's task → 404.
    cancel = await authed_admin_client.post(f"/admin/tasks/{task_b}/cancel", json={"reason": "x"})
    assert cancel.status_code == 404
    delete = await authed_admin_client.request("DELETE", f"/admin/tasks/{task_b}")
    assert delete.status_code == 404


@pytest.mark.asyncio
async def test_cross_tenant_worker_404(authed_admin_client: AsyncClient) -> None:
    """Admin A cannot pause/delete a worker of workspace B."""
    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    token = await mint_token_via_app(workspace_id=ws_b)
    reg = await authed_admin_client.post(
        "/v1/workers/register",
        json={**REGISTER, "hostname": "ws-b-worker", "enrollment_token": token},
    )
    assert reg.status_code == 200, reg.text
    wid = reg.json()["worker_id"]

    pause = await authed_admin_client.post(f"/admin/workers/{wid}/pause", json={})
    assert pause.status_code == 404
    detail = await authed_admin_client.get(
        f"/admin/workers/{wid}", headers={"Accept": "application/json"}
    )
    assert detail.status_code == 404


# --- 4. Created task is stamped with the admin's active workspace -----------


@pytest.mark.asyncio
async def test_created_task_stamped_with_active_workspace(
    authed_admin_client: AsyncClient,
) -> None:
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            ws = (
                await conn.execute(
                    text("SELECT workspace_id FROM tasks WHERE id = :id"), {"id": task_id}
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert str(ws) == DEFAULT_WORKSPACE_ID


# --- 5. Cross-tenant audit-log isolation -----------------------------


@pytest.mark.asyncio
async def test_cross_tenant_audit_log_isolation(authed_admin_client: AsyncClient) -> None:
    """Admin B does not see admin A's destructive action in `/admin/actions`.

    Closes the cross-tenant leak of `admin_actions`: each audit row is stamped
    with the workspace of the action and the listing is scoped by
    `ctx.workspace_id`.
    """
    # A (Default) creates and then cancels a task → generates an admin_action
    # (cancel_task) stamped in Default.
    create_a = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert create_a.status_code == 200, create_a.text
    task_a = create_a.json()["task_id"]
    cancel_a = await authed_admin_client.post(
        f"/admin/tasks/{task_a}/cancel", json={"reason": "audit-isolation-probe"}
    )
    assert cancel_a.status_code == 200, cancel_a.text
    target_a = str(task_a)

    # B creates + cancels a task in its own workspace.
    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    client_b = await _make_authed_client_for_workspace(ws_b)
    try:
        create_b = await client_b.post("/admin/tasks", json={"target": "1"})
        assert create_b.status_code == 200, create_b.text
        task_b = create_b.json()["task_id"]
        cancel_b = await client_b.post(
            f"/admin/tasks/{task_b}/cancel", json={"reason": "audit-isolation-probe"}
        )
        assert cancel_b.status_code == 200, cancel_b.text
        target_b = str(task_b)

        # B sees its own action but NOT A's in the scoped listing.
        # We use the detail href (`/admin/tasks/{id}"`, with the closing quote) to
        # avoid false positives from substrings (ids 5 vs 50).
        href_a = f'/admin/tasks/{target_a}"'
        href_b = f'/admin/tasks/{target_b}"'
        actions_b = await client_b.get("/admin/actions/partial")
        assert actions_b.status_code == 200
        body_b = actions_b.text
        assert href_b in body_b
        assert href_a not in body_b
    finally:
        await client_b.aclose()

    # A sees its own action but NOT B's.
    actions_a = await authed_admin_client.get("/admin/actions/partial")
    assert actions_a.status_code == 200
    body_a = actions_a.text
    assert href_a in body_a
    assert href_b not in body_a
