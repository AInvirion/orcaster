"""The `/admin/secrets` page with per-worker scope.

Covers the form's worker selector (the whole workspace by default, without
deleted workers or workers of another workspace), the Scope column, set and
delete by scope with their flash message and audit, and the worker errors
(nonexistent or deleted) as an inline 400 without the value.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from tests.test_secrets_page import (  # noqa: F401 - `keys_file` is a fixture
    NO_HTMX,
    VALUE,
    _assert_no_value,
    _audit,
    _fetch,
    _post_headers,
    keys_file,
)

W1_VALUE = "ghp_page_worker-a_value_QWERTY"


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


async def _set(client: AsyncClient, name: str, value: str, worker: str = "", **kw):
    return await client.post(
        "/admin/secrets",
        data={"name": name, "value": value, "worker": worker},
        headers=_post_headers(),
        **kw,
    )


async def _scoped_rows() -> list[dict]:
    return await _fetch(
        "SELECT name, worker_id::text AS worker_id FROM task_secrets "
        "ORDER BY name, worker_id NULLS FIRST"
    )


@pytest.mark.asyncio
async def test_form_offers_workspace_default_and_live_workers(
    authed_admin_client: AsyncClient,
    keys_file: Path,  # noqa: F811
) -> None:
    w1 = await _worker("host-a", name="worker-a")
    await _worker("gone-host", name="gone", deleted=True)
    page = await authed_admin_client.get("/admin/secrets", headers=NO_HTMX)
    assert page.status_code == 200
    assert '<select id="secret-worker" name="worker"' in page.text
    assert '<option value="" selected>All workers (workspace-wide)</option>' in page.text
    assert f'<option value="{w1}">worker-a</option>' in page.text
    assert ">gone<" not in page.text


@pytest.mark.asyncio
async def test_set_and_delete_per_worker(
    authed_admin_client: AsyncClient,
    keys_file: Path,  # noqa: F811
) -> None:
    w1 = await _worker("host-a", name="worker-a")
    assert (await _set(authed_admin_client, "GITHUB_TOKENS", VALUE)).status_code == 303
    resp = await _set(
        authed_admin_client, "GITHUB_TOKENS", W1_VALUE, worker=w1, follow_redirects=False
    )
    assert resp.status_code == 303
    _assert_no_value(resp, W1_VALUE)
    assert await _scoped_rows() == [
        {"name": "GITHUB_TOKENS", "worker_id": None},
        {"name": "GITHUB_TOKENS", "worker_id": w1},
    ]

    page = await authed_admin_client.get("/admin/secrets", headers=NO_HTMX)
    assert "Secret GITHUB_TOKENS saved for worker worker-a." in page.text
    assert "<th>Scope</th>" in page.text
    assert '<td class="muted">Workspace</td>' in page.text
    assert "<td>Worker <code>worker-a</code></td>" in page.text
    url = f"/admin/secrets/GITHUB_TOKENS/delete?worker={w1}"
    assert f'data-confirm-url="{url}"' in page.text
    assert 'data-confirm-title="Delete secret GITHUB_TOKENS for worker worker-a?"' in page.text
    _assert_no_value(page, VALUE, W1_VALUE)

    audit = await _audit("set_task_secret")
    assert [a["payload"] for a in audit] == [
        {"name": "GITHUB_TOKENS", "worker_id": None, "scope": "workspace"},
        {"name": "GITHUB_TOKENS", "worker_id": w1, "scope": "worker"},
    ]

    resp = await authed_admin_client.post(url, headers=_post_headers(), follow_redirects=False)
    assert resp.status_code == 303
    assert await _scoped_rows() == [{"name": "GITHUB_TOKENS", "worker_id": None}]
    after = await authed_admin_client.get("/admin/secrets", headers=NO_HTMX)
    assert "Secret GITHUB_TOKENS deleted for worker worker-a." in after.text
    assert [a["payload"] for a in await _audit("delete_task_secret")] == [
        {"name": "GITHUB_TOKENS", "worker_id": w1, "scope": "worker"},
    ]
    again = await authed_admin_client.post(url, headers=_post_headers(), follow_redirects=False)
    assert again.status_code == 404


@pytest.mark.asyncio
async def test_set_with_unknown_or_deleted_worker_is_400_inline(
    authed_admin_client: AsyncClient,
    keys_file: Path,  # noqa: F811
) -> None:
    gone = await _worker("gone-host", name="gone", deleted=True)
    for ref, msg in ((str(uuid.uuid4()), "not found in this workspace"), (gone, "is deleted")):
        resp = await _set(authed_admin_client, "GITHUB_TOKENS", W1_VALUE, worker=ref)
        assert resp.status_code == 400
        assert msg in resp.text
        assert 'id="secret-worker-err"' in resp.text
        _assert_no_value(resp, W1_VALUE)
    assert await _scoped_rows() == []


@pytest.mark.asyncio
async def test_delete_with_unknown_worker_is_404(
    authed_admin_client: AsyncClient,
    keys_file: Path,  # noqa: F811
) -> None:
    resp = await authed_admin_client.post(
        f"/admin/secrets/GH/delete?worker={uuid.uuid4()}",
        headers=_post_headers(),
        follow_redirects=False,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_padded_worker_reference_is_rejected(
    authed_admin_client: AsyncClient,
    keys_file: Path,  # noqa: F811
) -> None:
    """The form does not trim `worker`. "worker-a " and "worker-a" are different
    workers; with whitespace at the edges (or only whitespace) it is an inline
    400 and nothing is written. The UUID works for the worker whose name carries
    spaces."""
    padded = await _worker("host-a", name="worker-a ")
    exact = await _worker("host-b", name="worker-a")
    for ref in ("worker-a ", " worker-a", "   ", f"{exact} "):
        resp = await _set(authed_admin_client, "GH", W1_VALUE, worker=ref)
        assert resp.status_code == 400, ref
        assert 'id="secret-worker-err"' in resp.text
        assert "leading or trailing whitespace" in resp.text
        _assert_no_value(resp, W1_VALUE)
    assert await _scoped_rows() == []

    assert (await _set(authed_admin_client, "GH", W1_VALUE, worker="worker-a")).status_code == 303
    assert (await _set(authed_admin_client, "GH", W1_VALUE, worker=padded)).status_code == 303
    assert sorted(r["worker_id"] for r in await _scoped_rows()) == sorted([exact, padded])

    resp = await authed_admin_client.post(
        "/admin/secrets/GH/delete?worker=worker-a%20",
        headers=_post_headers(),
        follow_redirects=False,
    )
    assert resp.status_code == 422
    assert len(await _scoped_rows()) == 2


@pytest.mark.asyncio
async def test_delete_with_empty_worker_is_rejected(
    authed_admin_client: AsyncClient,
    keys_file: Path,  # noqa: F811
) -> None:
    """An empty `?worker=` is not "no worker": it does not delete the workspace secret."""
    assert (await _set(authed_admin_client, "GH", VALUE)).status_code == 303
    for ref in ("", "%20"):
        resp = await authed_admin_client.post(
            f"/admin/secrets/GH/delete?worker={ref}",
            headers=_post_headers(),
            follow_redirects=False,
        )
        assert resp.status_code == 422, ref
    assert await _scoped_rows() == [{"name": "GH", "worker_id": None}]


@pytest.mark.asyncio
async def test_set_worker_deleted_between_lookups_is_400_inline(
    authed_admin_client: AsyncClient,
    keys_file: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the worker is deleted between the route's lookup and `set_secret`'s,
    the form shows the worker error, not a 500."""
    from orcaster_server.task_secrets import service

    wid = await _worker("host-a", name="worker-a")

    real = service.resolve_worker
    calls: list[str] = []

    # The first lookup (the route's) is real; the second (`set_secret`'s) sees
    # the worker already deleted.
    async def second_fails(*a, **kw):
        calls.append(kw["ref"])
        if len(calls) > 1:
            raise service.WorkerDeleted(wid)
        return await real(*a, **kw)

    monkeypatch.setattr(service, "resolve_worker", second_fails)
    resp = await _set(authed_admin_client, "GH", W1_VALUE, worker=wid)
    assert len(calls) == 2
    assert resp.status_code == 400
    assert "is deleted" in resp.text
    assert 'id="secret-worker-err"' in resp.text
    _assert_no_value(resp, W1_VALUE)
    assert await _scoped_rows() == []


@pytest.mark.asyncio
async def test_delete_per_worker_without_keys_works(
    authed_admin_client: AsyncClient,
    keys_file: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting from the dashboard with `?worker=` does not require keys."""
    from orcaster_server.config import settings
    from orcaster_server.task_secrets import keyring

    w1 = await _worker("host-a", name="worker-a")
    assert (
        await _set(authed_admin_client, "GITHUB_TOKENS", W1_VALUE, worker=w1)
    ).status_code == 303
    monkeypatch.setattr(settings, "task_secrets_keys_path", None)
    keyring.reset_keyring_cache()

    url = f"/admin/secrets/GITHUB_TOKENS/delete?worker={w1}"
    resp = await authed_admin_client.post(url, headers=_post_headers(), follow_redirects=False)
    assert resp.status_code == 303, resp.text
    assert await _scoped_rows() == []
