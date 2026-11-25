"""`/api/v1/secrets` API with worker scope.

`?worker=` on PUT and DELETE (UUID or visible name), `worker_id`/`worker_name`
in the responses and the explicit errors: `worker_not_found` (404, also
for a worker in another workspace), `worker_ambiguous` (409), `worker_deleted`
(409, only when setting) and `invalid_worker` (422, empty `worker=`).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from orcaster_server.config import settings
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from tests.conftest import mint_pat_via_app
from tests.test_api_v1_secrets import (  # noqa: F401 - `keys_file` is a fixture
    _h,
    keys_file,
)


async def _worker(
    hostname: str,
    *,
    name: str | None = None,
    workspace_id: str = DEFAULT_WORKSPACE_ID,
    deleted: bool = False,
) -> str:
    from orcaster_server import db as app_db

    sm = app_db.get_sessionmaker()
    assert sm is not None
    async with sm() as s:
        wid = (
            await s.execute(
                text(
                    "INSERT INTO workers "
                    "(workspace_id, api_key_hash, hostname, name, os, arch, deleted_at) "
                    "VALUES (:ws, :h, :host, :name, 'linux', 'x86_64', "
                    "CASE WHEN :deleted THEN now() ELSE NULL END) RETURNING id"
                ),
                {
                    "ws": workspace_id,
                    "h": uuid.uuid4().hex,
                    "host": hostname,
                    "name": name,
                    "deleted": deleted,
                },
            )
        ).scalar_one()
        await s.commit()
    return str(wid)


@pytest.mark.asyncio
async def test_put_list_delete_per_worker(
    app_client: AsyncClient,
    keys_file: Path,  # noqa: F811
) -> None:
    token, _ = await mint_pat_via_app(role="owner", email="owner@example.com")
    w1 = await _worker("host-a", name="worker-a")
    w2 = await _worker("worker-b")

    wide = await app_client.put(
        "/api/v1/secrets/GITHUB_TOKENS", json={"value": "wide-v"}, headers=_h(token)
    )
    assert wide.status_code == 200 and wide.json()["worker_id"] is None
    by_name = await app_client.put(
        "/api/v1/secrets/GITHUB_TOKENS?worker=worker-a", json={"value": "v1"}, headers=_h(token)
    )
    assert by_name.status_code == 200, by_name.text
    assert (by_name.json()["worker_id"], by_name.json()["worker_name"]) == (w1, "worker-a")
    by_id = await app_client.put(
        f"/api/v1/secrets/GITHUB_TOKENS?worker={w2}", json={"value": "v2"}, headers=_h(token)
    )
    assert by_id.status_code == 200, by_id.text
    assert (by_id.json()["worker_id"], by_id.json()["worker_name"]) == (w2, "worker-b")

    items = (await app_client.get("/api/v1/secrets", headers=_h(token))).json()["items"]
    assert [(i["name"], i["worker_id"], i["worker_name"]) for i in items] == [
        ("GITHUB_TOKENS", None, None),
        ("GITHUB_TOKENS", w1, "worker-a"),
        ("GITHUB_TOKENS", w2, "worker-b"),
    ]

    # DELETE with worker deletes only that row; without worker, only the workspace one.
    assert (
        await app_client.delete("/api/v1/secrets/GITHUB_TOKENS?worker=worker-a", headers=_h(token))
    ).status_code == 204
    again = await app_client.delete(
        "/api/v1/secrets/GITHUB_TOKENS?worker=worker-a", headers=_h(token)
    )
    assert again.status_code == 404 and again.json()["error"] == "not_found"
    assert (
        await app_client.delete("/api/v1/secrets/GITHUB_TOKENS", headers=_h(token))
    ).status_code == 204
    items = (await app_client.get("/api/v1/secrets", headers=_h(token))).json()["items"]
    assert [i["worker_id"] for i in items] == [w2]


@pytest.mark.asyncio
async def test_worker_errors_are_explicit(
    app_client: AsyncClient,
    keys_file: Path,  # noqa: F811
) -> None:
    from orcaster_server import db as app_db

    token, _ = await mint_pat_via_app(role="owner")
    await _worker("twin-a", name="twin")
    await _worker("twin-b", name="twin")
    gone = await _worker("gone-host", name="gone", deleted=True)
    sm = app_db.get_sessionmaker()
    assert sm is not None
    async with sm() as s:
        other_ws = str(
            (
                await s.execute(
                    text(
                        "INSERT INTO workspaces (name, slug) VALUES ('Other', 'other') RETURNING id"
                    )
                )
            ).scalar_one()
        )
        await s.commit()
    foreign = await _worker("foreign", workspace_id=other_ws)

    async def put(ref: str):
        return await app_client.put(
            f"/api/v1/secrets/GH?worker={ref}", json={"value": "v"}, headers=_h(token)
        )

    for ref in ("nope", foreign, str(uuid.uuid4())):
        resp = await put(ref)
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"] == "worker_not_found"
        assert "not found in this workspace" in resp.json()["message"]
    resp = await put("twin")
    assert resp.status_code == 409 and resp.json()["error"] == "worker_ambiguous"
    resp = await put(gone)
    assert resp.status_code == 409 and resp.json()["error"] == "worker_deleted"
    resp = await put("")
    assert resp.status_code == 422 and resp.json()["error"] == "invalid_worker"
    resp = await app_client.delete("/api/v1/secrets/GH?worker=", headers=_h(token))
    assert resp.status_code == 422 and resp.json()["error"] == "invalid_worker"
    resp = await app_client.delete("/api/v1/secrets/GH?worker=nope", headers=_h(token))
    assert resp.status_code == 404 and resp.json()["error"] == "worker_not_found"
    items = (await app_client.get("/api/v1/secrets", headers=_h(token))).json()["items"]
    assert items == []


@pytest.mark.asyncio
async def test_delete_cleans_up_deleted_worker_row(
    app_client: AsyncClient,
    keys_file: Path,  # noqa: F811
) -> None:
    token, _ = await mint_pat_via_app(role="owner")
    wid = await _worker("old-host", name="old")
    assert (
        await app_client.put(
            "/api/v1/secrets/GH?worker=old", json={"value": "v"}, headers=_h(token)
        )
    ).status_code == 200
    from orcaster_server import db as app_db

    sm = app_db.get_sessionmaker()
    assert sm is not None
    async with sm() as s:
        await s.execute(text("UPDATE workers SET deleted_at = now() WHERE id = :id"), {"id": wid})
        await s.commit()
    resp = await app_client.delete("/api/v1/secrets/GH?worker=old", headers=_h(token))
    assert resp.status_code == 204, resp.text


@pytest.mark.asyncio
async def test_member_cannot_write_worker_scope(
    app_client: AsyncClient,
    keys_file: Path,  # noqa: F811
) -> None:
    member, _ = await mint_pat_via_app(role="member", email="m@example.com")
    await _worker("worker-a")
    resp = await app_client.put(
        "/api/v1/secrets/GH?worker=worker-a", json={"value": "v"}, headers=_h(member)
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_padded_worker_reference_is_rejected_not_stripped(
    app_client: AsyncClient,
    keys_file: Path,  # noqa: F811
) -> None:
    """`?worker=` is not trimmed. Worker names are not trimmed when
    registering, so "worker-a " and "worker-a" are different workers: a
    reference with spaces at the edges is 422 `invalid_worker` and neither writes
    nor deletes anything; the exact name resolves to its worker and the UUID works for the
    worker whose name has spaces."""
    token, _ = await mint_pat_via_app(role="owner")
    padded = await _worker("host-a", name="worker-a ")
    exact = await _worker("host-b", name="worker-a")

    for ref in ("worker-a%20", "%20worker-a", "%20", f"{exact}%20"):
        resp = await app_client.put(
            f"/api/v1/secrets/GH?worker={ref}", json={"value": "v"}, headers=_h(token)
        )
        assert resp.status_code == 422, (ref, resp.text)
        assert resp.json()["error"] == "invalid_worker"
    items = (await app_client.get("/api/v1/secrets", headers=_h(token))).json()["items"]
    assert items == []

    by_name = await app_client.put(
        "/api/v1/secrets/GH?worker=worker-a", json={"value": "v-exact"}, headers=_h(token)
    )
    assert by_name.status_code == 200, by_name.text
    assert by_name.json()["worker_id"] == exact
    by_id = await app_client.put(
        f"/api/v1/secrets/GH?worker={padded}", json={"value": "v-padded"}, headers=_h(token)
    )
    assert by_id.status_code == 200, by_id.text
    assert (by_id.json()["worker_id"], by_id.json()["worker_name"]) == (padded, "worker-a ")

    resp = await app_client.delete("/api/v1/secrets/GH?worker=worker-a%20", headers=_h(token))
    assert resp.status_code == 422 and resp.json()["error"] == "invalid_worker"
    items = (await app_client.get("/api/v1/secrets", headers=_h(token))).json()["items"]
    assert sorted(i["worker_id"] for i in items) == sorted([exact, padded])


@pytest.mark.asyncio
async def test_worker_deleted_between_lookups_is_409(
    app_client: AsyncClient,
    keys_file: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`set_secret` resolves the worker again; if it was deleted (soft or hard)
    between the two lookups, the response is the explicit error and not a 500."""
    from orcaster_server.task_secrets import service

    token, _ = await mint_pat_via_app(role="owner")
    wid = await _worker("host-a", name="worker-a")

    real = service.resolve_worker
    for exc, status_code, code in (
        (service.WorkerDeleted(wid), 409, "worker_deleted"),
        (service.WorkerNotFound(wid), 404, "worker_not_found"),
    ):
        calls: list[str] = []

        # The first lookup (the route's) is real; the second (`set_secret`'s)
        # sees the worker already deleted.
        async def second_fails(*a, _exc=exc, _calls=calls, **kw):
            _calls.append(kw["ref"])
            if len(_calls) > 1:
                raise _exc
            return await real(*a, **kw)

        monkeypatch.setattr(service, "resolve_worker", second_fails)
        resp = await app_client.put(
            "/api/v1/secrets/GH?worker=worker-a", json={"value": "v"}, headers=_h(token)
        )
        assert len(calls) == 2
        assert resp.status_code == status_code, resp.text
        assert resp.json()["error"] == code
    items = (await app_client.get("/api/v1/secrets", headers=_h(token))).json()["items"]
    assert items == []


@pytest.mark.asyncio
async def test_delete_per_worker_without_keys_is_204(
    app_client: AsyncClient,
    keys_file: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELETE with `?worker=` does not require the keys file either."""
    from orcaster_server.task_secrets import keyring

    token, _ = await mint_pat_via_app(role="owner", email="owner@example.com")
    await _worker("host-a", name="worker-a")
    put = await app_client.put(
        "/api/v1/secrets/GITHUB_TOKENS?worker=worker-a", json={"value": "v1"}, headers=_h(token)
    )
    assert put.status_code == 200, put.text

    monkeypatch.setattr(settings, "task_secrets_keys_path", None)
    keyring.reset_keyring_cache()
    resp = await app_client.delete(
        "/api/v1/secrets/GITHUB_TOKENS?worker=worker-a", headers=_h(token)
    )
    assert resp.status_code == 204, resp.text
    items = (await app_client.get("/api/v1/secrets", headers=_h(token))).json()["items"]
    assert items == []
