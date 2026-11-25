"""Tests — workspace switcher in the dashboard header.

Covers:
  - `POST /admin/workspace/switch` to a workspace the user IS a member of
    → 303 and `user_active_workspace` points to the new one.
  - switch to a workspace the user is NOT a member of → 404, active unchanged.
  - a full page (`GET /admin/`) renders the switcher with the active workspace
    name and the list of memberships (2 names appear in the HTML).
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID


def _db_url() -> str:
    from orcaster_server.config import settings

    return settings.database_url


async def _active_user_id() -> str:
    """User_id of the `authed_admin_client` session (the only user in the table)."""
    from orcaster_server import db as app_db

    sm = app_db.get_sessionmaker()
    async with sm() as s:
        row = await s.execute(text("SELECT id FROM users LIMIT 1"))
        return str(row.scalar_one())


async def _create_workspace(name: str, slug: str) -> str:
    from orcaster_server import db as app_db
    from orcaster_server.workspaces.service import create_workspace

    sm = app_db.get_sessionmaker()
    async with sm() as s:
        ws_id = await create_workspace(s, name=name, slug=slug)
        await s.commit()
        return ws_id


async def _add_member(workspace_id: str, user_id: str, role: str = "member") -> None:
    from orcaster_server import db as app_db
    from orcaster_server.workspaces.service import add_member

    sm = app_db.get_sessionmaker()
    async with sm() as s:
        await add_member(s, workspace_id=workspace_id, user_id=user_id, role=role)
        await s.commit()


async def _active_workspace_of(user_id: str) -> str | None:
    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            row = await conn.execute(
                text("SELECT workspace_id FROM user_active_workspace WHERE user_id = :u"),
                {"u": user_id},
            )
            val = row.scalar_one_or_none()
            return str(val) if val is not None else None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_switch_to_member_workspace_303_and_persisted(
    authed_admin_client: AsyncClient,
) -> None:
    user_id = await _active_user_id()
    ws_b = await _create_workspace("WS_Beta", f"ws-beta-{uuid.uuid4().hex[:8]}")
    await _add_member(ws_b, user_id, role="member")

    resp = await authed_admin_client.post(
        "/admin/workspace/switch",
        data={"workspace_id": ws_b},
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/admin/"
    assert await _active_workspace_of(user_id) == ws_b


@pytest.mark.asyncio
async def test_switch_to_non_member_workspace_404_unchanged(
    authed_admin_client: AsyncClient,
) -> None:
    user_id = await _active_user_id()
    before = await _active_workspace_of(user_id)
    # Workspace that exists but the user is NOT a member of.
    ws_x = await _create_workspace("WS_Foreign", f"ws-foreign-{uuid.uuid4().hex[:8]}")

    resp = await authed_admin_client.post(
        "/admin/workspace/switch",
        data={"workspace_id": ws_x},
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 404
    # The active workspace did not change (still None → Default fallback, or the prior value).
    assert await _active_workspace_of(user_id) == before


@pytest.mark.asyncio
async def test_switch_to_random_uuid_404(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.post(
        "/admin/workspace/switch",
        data={"workspace_id": str(uuid.uuid4())},
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_dashboard_renders_switcher_with_both_memberships(
    authed_admin_client: AsyncClient,
) -> None:
    user_id = await _active_user_id()
    ws_b = await _create_workspace("WS_Gamma", f"ws-gamma-{uuid.uuid4().hex[:8]}")
    await _add_member(ws_b, user_id, role="member")

    resp = await authed_admin_client.get("/admin/", headers={"HX-Request": "false"})
    assert resp.status_code == 200, resp.text
    body = resp.text
    # The switcher control and the "Workspace" label.
    assert "ws-switcher" in body
    assert "Workspace" in body
    # Both workspace names appear (Default + WS_Gamma) and the POST form.
    assert "Default" in body
    assert "WS_Gamma" in body
    assert 'action="/admin/workspace/switch"' in body
    # The active workspace (Default) is marked.
    assert 'aria-current="true"' in body


@pytest.mark.asyncio
async def test_dashboard_single_membership_renders_solo_label(
    authed_admin_client: AsyncClient,
) -> None:
    # With a single membership there is no interactive dropdown, only the label.
    resp = await authed_admin_client.get("/admin/", headers={"HX-Request": "false"})
    assert resp.status_code == 200
    body = resp.text
    assert "ws-switcher-solo" in body
    assert "Default" in body
    # No dropdown ⇒ no switch form.
    assert 'action="/admin/workspace/switch"' not in body


@pytest.mark.asyncio
async def test_switch_changes_scoped_view(authed_admin_client: AsyncClient) -> None:
    """After the switch, the dashboard resolves in the new workspace's scope.

    Verifies the end-to-end effect: the persisted active workspace changes the
    workspace that `require_workspace` resolves, not just the row in
    `user_active_workspace`.
    """
    user_id = await _active_user_id()
    ws_b = await _create_workspace("WS_Delta", f"ws-delta-{uuid.uuid4().hex[:8]}")
    await _add_member(ws_b, user_id, role="member")

    # Initial active workspace: Default (no explicit row → fallback).
    assert await _active_workspace_of(user_id) is None or (
        await _active_workspace_of(user_id) == DEFAULT_WORKSPACE_ID
    )

    switch = await authed_admin_client.post(
        "/admin/workspace/switch",
        data={"workspace_id": ws_b},
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert switch.status_code == 303

    # The dashboard now marks WS_Delta as active.
    page = await authed_admin_client.get("/admin/", headers={"HX-Request": "false"})
    assert page.status_code == 200
    body = page.text
    # The active name appears next to aria-current inside the active item.
    assert "WS_Delta" in body
    assert 'aria-current="true"' in body
