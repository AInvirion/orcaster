"""Worker control from /admin is owner-only.

Pause, resume, clear-cache, delete, push command and control-op change what a
worker does for the whole workspace, so only the workspace owner may call them.
A member gets 403 and nothing changes; the workers deck does not offer the
actions to members.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from orcaster_server import db as app_db
from orcaster_server.config import settings
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from tests.conftest import _mint_session_for_workspace, browser_origin_hook, mint_token_via_app

REGISTER = {
    "hostname": "w",
    "os": "linux",
    "arch": "x86_64",
    "ram_mb": 1024,
    "disk_mb": 10000,
    "tags": [],
    "capabilities": {},
}


def _command_body() -> dict:
    expires = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    return {"command": "run_batch", "expires_at": expires}


async def _register(client: AsyncClient) -> str:
    resp = await client.post(
        "/v1/workers/register", json={**REGISTER, "enrollment_token": await mint_token_via_app()}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["worker_id"]


async def _worker_row(worker_id: str) -> dict:
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        row = (
            (
                await s.execute(
                    text("SELECT status, deleted_at FROM workers WHERE id = :id"), {"id": worker_id}
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


async def _task_count() -> int:
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        return int((await s.execute(text("SELECT count(*) FROM tasks"))).scalar_one())


async def _set_status(worker_id: str, status: str) -> None:
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        await s.execute(
            text("UPDATE workers SET status = :st WHERE id = :id"), {"st": status, "id": worker_id}
        )
        await s.commit()


@pytest.fixture
async def member_client(app_client: AsyncClient) -> AsyncIterator[AsyncClient]:
    from orcaster_server.main import app

    token = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID, role="member")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"HX-Request": "true"},
        event_hooks={"request": [browser_origin_hook(settings.webauthn_origin)]},
    ) as client:
        client.cookies.set("session", token)
        yield client


# (method, path suffix, json body factory, status the worker needs first)
_ROUTES = [
    ("post", "/pause", lambda: {}, "online"),
    ("post", "/resume", lambda: {}, "paused"),
    ("post", "/clear-cache", lambda: None, "online"),
    ("delete", "?force=true", lambda: None, "online"),
    ("post", "/command", _command_body, "online"),
    ("post", "/control-op", lambda: {"control_op": "__reload_config__"}, "online"),
]
_IDS = ["pause", "resume", "clear-cache", "delete", "command", "control-op"]


async def _call(client: AsyncClient, method: str, worker_id: str, suffix: str, body):
    url = f"/admin/workers/{worker_id}{suffix}"
    if method == "delete":
        return await client.delete(url)
    return await client.post(url, json=body)


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "suffix", "body", "needs"), _ROUTES, ids=_IDS)
async def test_owner_can_control_worker(
    authed_admin_client: AsyncClient, method, suffix, body, needs
) -> None:
    wid = await _register(authed_admin_client)
    await _set_status(wid, needs)
    resp = await _call(authed_admin_client, method, wid, suffix, body())
    assert 200 <= resp.status_code < 300, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "suffix", "body", "needs"), _ROUTES, ids=_IDS)
async def test_member_cannot_control_worker(
    authed_admin_client: AsyncClient, member_client: AsyncClient, method, suffix, body, needs
) -> None:
    wid = await _register(authed_admin_client)
    await _set_status(wid, needs)
    tasks_before = await _task_count()

    resp = await _call(member_client, method, wid, suffix, body())

    assert resp.status_code == 403, resp.text
    assert "HX-Redirect" not in resp.headers
    row = await _worker_row(wid)
    assert row["status"] == needs
    assert row["deleted_at"] is None
    assert await _task_count() == tasks_before


@pytest.mark.asyncio
async def test_member_deck_hides_worker_actions(
    authed_admin_client: AsyncClient, member_client: AsyncClient
) -> None:
    wid = await _register(authed_admin_client)
    resp = await member_client.get("/admin/workers")
    assert resp.status_code == 200, resp.text
    assert f'id="worker-card-{wid}"' in resp.text
    assert f"/admin/workers/{wid}/pause" not in resp.text
    assert f'hx-delete="/admin/workers/{wid}' not in resp.text
    assert "worker-card-kebab" not in resp.text


@pytest.mark.asyncio
async def test_owner_deck_offers_worker_actions(authed_admin_client: AsyncClient) -> None:
    wid = await _register(authed_admin_client)
    await _set_status(wid, "online")
    resp = await authed_admin_client.get("/admin/workers")
    assert resp.status_code == 200, resp.text
    assert f"/admin/workers/{wid}/pause" in resp.text
    assert f'hx-delete="/admin/workers/{wid}' in resp.text


@pytest.mark.asyncio
async def test_member_worker_detail_hides_control_buttons(
    authed_admin_client: AsyncClient, member_client: AsyncClient
) -> None:
    wid = await _register(authed_admin_client)
    await _set_status(wid, "online")
    resp = await member_client.get(f"/admin/workers/{wid}", headers={"HX-Request": "false"})
    assert resp.status_code == 200, resp.text
    assert 'id="worker-cache-block"' in resp.text
    for action in ("/pause", "/resume", "/clear-cache"):
        assert f"/admin/workers/{wid}{action}" not in resp.text
    assert f'hx-delete="/admin/workers/{wid}' not in resp.text


@pytest.mark.asyncio
async def test_owner_worker_detail_shows_control_buttons(
    authed_admin_client: AsyncClient,
) -> None:
    wid = await _register(authed_admin_client)
    await _set_status(wid, "online")
    resp = await authed_admin_client.get(f"/admin/workers/{wid}", headers={"HX-Request": "false"})
    assert resp.status_code == 200, resp.text
    assert f"/admin/workers/{wid}/pause" in resp.text
    assert f"/admin/workers/{wid}/clear-cache" in resp.text
    assert f'hx-delete="/admin/workers/{wid}' in resp.text
