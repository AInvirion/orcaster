"""Contract tests: public `/api/v1` API, children (forks) of a parent.

`GET /api/v1/tasks/{id}/children` (cursor-paginated list, id desc, same pattern as
`GET /api/v1/tasks`) and the derived `child_count` field in `TaskDetail`. The
parent->children relation is derived by query from `tasks.parent_task_id` (no migration, no
new column, direct children only). Inherits the multi-tenant isolation of
`WorkspaceContext`: missing / cross-tenant parent -> 404.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.conftest import mint_pat_via_app


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create_workspace(name: str, slug: str) -> str:
    from orcaster_server import db as app_db
    from orcaster_server.workspaces.service import create_workspace

    sm = app_db.get_sessionmaker()
    assert sm is not None
    async with sm() as s:
        ws_id = await create_workspace(s, name=name, slug=slug)
        await s.commit()
        return ws_id


async def _create_task(client: AsyncClient, token: str, **body) -> int:
    resp = await client.post("/api/v1/tasks", headers=_bearer(token), json=body or {"payload": {}})
    assert resp.status_code == 201, resp.text
    return resp.json()["task_id"]


async def _fork_task(client: AsyncClient, token: str, parent_id: int, **body) -> int:
    resp = await client.post(
        f"/api/v1/tasks/{parent_id}/fork", headers=_bearer(token), json=body or {}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["task_id"]


# ---------------------------------------------------------------------------
# GET /tasks/{id}/children: list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_children_lists_forks(app_client: AsyncClient) -> None:
    """A parent with N forks returns the N children with correct id/status."""
    token, _ = await mint_pat_via_app()
    parent_id = await _create_task(app_client, token)
    child_ids = [await _fork_task(app_client, token, parent_id) for _ in range(3)]

    resp = await app_client.get(f"/api/v1/tasks/{parent_id}/children", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [t["id"] for t in body["tasks"]] == sorted(child_ids, reverse=True)
    assert all(t["parent_task_id"] == parent_id for t in body["tasks"])
    assert all(t["status"] == "queued" for t in body["tasks"])
    assert body["next_cursor"] is None


@pytest.mark.asyncio
async def test_children_empty(app_client: AsyncClient) -> None:
    """A parent without forks -> empty list and null next_cursor."""
    token, _ = await mint_pat_via_app()
    parent_id = await _create_task(app_client, token)

    resp = await app_client.get(f"/api/v1/tasks/{parent_id}/children", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"tasks": [], "next_cursor": None}


@pytest.mark.asyncio
async def test_children_cursor_pagination(app_client: AsyncClient) -> None:
    """`limit` + `cursor` paginate the children by id desc, without repeating or skipping."""
    token, _ = await mint_pat_via_app()
    parent_id = await _create_task(app_client, token)
    child_ids = [await _fork_task(app_client, token, parent_id) for _ in range(3)]
    ids_desc = sorted(child_ids, reverse=True)

    page1 = await app_client.get(
        f"/api/v1/tasks/{parent_id}/children", headers=_bearer(token), params={"limit": 2}
    )
    assert page1.status_code == 200, page1.text
    body1 = page1.json()
    assert [t["id"] for t in body1["tasks"]] == ids_desc[:2]
    assert body1["next_cursor"] == ids_desc[1]

    page2 = await app_client.get(
        f"/api/v1/tasks/{parent_id}/children",
        headers=_bearer(token),
        params={"limit": 2, "cursor": body1["next_cursor"]},
    )
    assert page2.status_code == 200, page2.text
    body2 = page2.json()
    assert [t["id"] for t in body2["tasks"]] == ids_desc[2:]
    assert body2["next_cursor"] is None


@pytest.mark.asyncio
async def test_children_parent_not_found_404(app_client: AsyncClient) -> None:
    """Missing parent -> 404."""
    token, _ = await mint_pat_via_app()
    resp = await app_client.get("/api/v1/tasks/999999/children", headers=_bearer(token))
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_children_cross_tenant_parent_404(app_client: AsyncClient) -> None:
    """Parent in another workspace -> 404 (without leaking existence)."""
    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    token_b, _ = await mint_pat_via_app(workspace_id=ws_b, email=None, label="b")
    theirs = await _create_task(app_client, token_b)

    token, _ = await mint_pat_via_app()
    resp = await app_client.get(f"/api/v1/tasks/{theirs}/children", headers=_bearer(token))
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# child_count in TaskDetail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detail_child_count_zero_without_forks(app_client: AsyncClient) -> None:
    """`child_count` = 0 when the task has no forks."""
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)

    resp = await app_client.get(f"/api/v1/tasks/{task_id}", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["child_count"] == 0


@pytest.mark.asyncio
async def test_detail_child_count_excludes_cross_tenant_children(app_client: AsyncClient) -> None:
    """`child_count` does not count children from another workspace that point to the same parent.

    Defense in depth: the detail subselect filters by
    `workspace_id`. Inserts via direct SQL a task in workspace B whose
    `parent_task_id` points to the task in workspace A, and checks that A still
    sees `child_count == 0` (the cross-tenant row does not inflate the count).
    """
    from sqlalchemy import text

    from orcaster_server import db as app_db

    token, _ = await mint_pat_via_app()
    parent_id = await _create_task(app_client, token)

    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    sm = app_db.get_sessionmaker()
    assert sm is not None
    async with sm() as s:
        await s.execute(
            text(
                "INSERT INTO tasks (workspace_id, parent_task_id, signature) "
                "VALUES (:ws, :parent, '\\x00'::bytea)"
            ),
            {"ws": ws_b, "parent": parent_id},
        )
        await s.commit()

    resp = await app_client.get(f"/api/v1/tasks/{parent_id}", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["child_count"] == 0


@pytest.mark.asyncio
async def test_detail_child_count_direct_children_only(app_client: AsyncClient) -> None:
    """`child_count` counts only direct children (one level); a grandchild does not inflate
    the grandparent."""
    token, _ = await mint_pat_via_app()
    grandparent = await _create_task(app_client, token)
    child1 = await _fork_task(app_client, token, grandparent)
    await _fork_task(app_client, token, grandparent)
    # Grandchild: fork of the first child, must NOT count for the grandparent.
    await _fork_task(app_client, token, child1)

    resp_gp = await app_client.get(f"/api/v1/tasks/{grandparent}", headers=_bearer(token))
    assert resp_gp.status_code == 200, resp_gp.text
    assert resp_gp.json()["child_count"] == 2

    resp_child = await app_client.get(f"/api/v1/tasks/{child1}", headers=_bearer(token))
    assert resp_child.status_code == 200, resp_child.text
    assert resp_child.json()["child_count"] == 1
