"""Tests for the `Cache-Control: no-store` middleware on the admin plane.

The `/admin/*` plane is cookie-authenticated and serves sensitive per-workspace
content; the global middleware (`orcaster_server/main.py:_admin_cache_control`) sets
`no-store` on every `/admin/*` response that does not declare its own `Cache-Control`,
without touching routes outside `/admin`.
"""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_admin_html_endpoint_sets_no_store(authed_admin_client: AsyncClient) -> None:
    """An HTML endpoint of the admin plane declares `Cache-Control: no-store`."""
    resp = await authed_admin_client.get("/admin/tasks")
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_non_admin_path_not_touched(app_client: AsyncClient) -> None:
    """The middleware does NOT inject `no-store` outside `/admin` (e.g. /healthz)."""
    resp = await app_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") != "no-store"
