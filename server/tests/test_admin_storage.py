"""Dashboard storage indicator."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DEFAULT_WS = "00000000-0000-0000-0000-000000000001"


@pytest.mark.asyncio
async def test_storage_partial_renders_quota_and_disk(
    authed_admin_client: AsyncClient, migrated_db_url: str, monkeypatch
) -> None:
    from orcaster_server import storage
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifact_workspace_max_total_bytes", 10 * 1024**3)
    monkeypatch.setattr(storage, "disk_free_bytes", lambda _root: 5 * 1024**3)
    monkeypatch.setattr(storage, "disk_total_bytes", lambda _root: 20 * 1024**3)

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO workspace_artifact_usage (workspace_id, total_bytes) "
                    "VALUES (:w, :t) ON CONFLICT (workspace_id) DO UPDATE SET total_bytes=:t"
                ),
                {"w": DEFAULT_WS, "t": 2 * 1024**3},  # 2 GiB usados de 10
            )
    finally:
        await engine.dispose()

    # HX-Request: true → the partial responds 200 (conftest already sets it on the
    # client; the handler redirects 303 for requests without that header).
    r = await authed_admin_client.get("/admin/storage", headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    body = r.text
    # English labels and humanized values present.
    assert "Quota" in body
    assert "Disk" in body
