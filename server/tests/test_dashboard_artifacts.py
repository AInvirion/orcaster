"""Admin artifact endpoints: listing, download, zip, scoping."""

import io
import zipfile

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_dashboard import _seed_completed_execution  # noqa: F401


async def _insert_artifact(
    db_url: str, execution_id: str, rel_path: str, sha: str, ctype: str, size: int
) -> None:
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO task_artifacts
                        (execution_id, rel_path, size_bytes, sha256, content_type)
                    VALUES (CAST(:e AS uuid), :p, :s, :h, :c)
                    """
                ),
                {"e": execution_id, "p": rel_path, "s": size, "h": sha, "c": ctype},
            )
    finally:
        await engine.dispose()


async def _mark_collected(
    db_url: str, execution_id: str, rel_path: str, note: str = "backed up to KB"
) -> None:
    """Simulate the marker left by the operational harvesting routine.

    The real write is done by the ops script through direct SQL; the server never
    marks rows. Here it is emulated with the same UPDATE.
    """
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    UPDATE task_artifacts
                       SET collected_at = now(), collected_note = :n
                     WHERE execution_id = CAST(:e AS uuid) AND rel_path = :p
                    """
                ),
                {"e": execution_id, "p": rel_path, "n": note},
            )
    finally:
        await engine.dispose()


async def _artifact_id(db_url: str, execution_id: str, rel_path: str) -> int:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            return (
                await conn.execute(
                    text(
                        "SELECT id FROM task_artifacts "
                        " WHERE execution_id = CAST(:e AS uuid) AND rel_path = :p"
                    ),
                    {"e": execution_id, "p": rel_path},
                )
            ).scalar_one()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_artifacts_list_json(authed_admin_client: AsyncClient, migrated_db_url: str) -> None:
    eid = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "artifacts-list-worker"
    )
    await _insert_artifact(migrated_db_url, eid, "results.json", "abc", "application/json", 12)
    r = await authed_admin_client.get(f"/admin/executions/{eid}/artifacts")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["rel_path"] == "results.json"
    assert items[0]["size_bytes"] == 12
    assert items[0]["content_type"] == "application/json"


@pytest.mark.asyncio
async def test_artifacts_list_cross_workspace_404(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    r = await authed_admin_client.get(
        "/admin/executions/99999999-9999-9999-9999-999999999999/artifacts"
    )
    assert r.status_code == 404


async def _write_bytes(root_env: str, execution_id: str, rel_path: str, data: bytes) -> None:
    from pathlib import Path

    p = Path(root_env) / execution_id / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


@pytest.mark.asyncio
async def test_artifact_download_one(
    authed_admin_client: AsyncClient, migrated_db_url: str, tmp_path, monkeypatch
) -> None:
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifacts_root", tmp_path)
    eid = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "artifact-dl-worker"
    )
    content = b'{"k": 1}'
    import hashlib

    await _insert_artifact(
        migrated_db_url,
        eid,
        "out/r.json",
        hashlib.sha256(content).hexdigest(),
        "application/json",
        len(content),
    )
    await _write_bytes(str(tmp_path), eid, "out/r.json", content)

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            aid = (
                await conn.execute(
                    text("SELECT id FROM task_artifacts WHERE execution_id = CAST(:e AS uuid)"),
                    {"e": eid},
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    r = await authed_admin_client.get(f"/admin/executions/{eid}/artifacts/{aid}/download")
    assert r.status_code == 200
    assert r.content == content
    cd = r.headers["content-disposition"]
    assert 'filename="r.json"' in cd
    assert "filename*=UTF-8''r.json" in cd
    assert r.headers["content-type"].startswith("application/json")
    assert int(r.headers["content-length"]) == len(content)


@pytest.mark.asyncio
async def test_artifacts_zip_all(
    authed_admin_client: AsyncClient, migrated_db_url: str, tmp_path, monkeypatch
) -> None:
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifacts_root", tmp_path)
    eid = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "artifact-zip-worker"
    )
    import hashlib

    files = {"a.json": b"AAA", "sub/b.json": b"BBBB"}
    for rel, data in files.items():
        await _insert_artifact(
            migrated_db_url,
            eid,
            rel,
            hashlib.sha256(data).hexdigest(),
            "application/json",
            len(data),
        )
        await _write_bytes(str(tmp_path), eid, rel, data)

    r = await authed_admin_client.get(f"/admin/executions/{eid}/artifacts.zip")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/zip")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert set(zf.namelist()) == {"a.json", "sub/b.json"}
    assert zf.read("sub/b.json") == b"BBBB"


@pytest.mark.asyncio
async def test_artifacts_zip_missing_bytes_404(
    authed_admin_client: AsyncClient, migrated_db_url: str, tmp_path, monkeypatch
) -> None:
    # If the metadata exists but the bytes of some artifact are missing on disk, the
    # zip must NOT silently skip it (it would deliver a partial zip with a
    # misleading 200): it must fail explicitly with 404 BEFORE starting to stream,
    # consistent with the individual download.
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifacts_root", tmp_path)
    eid = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "artifact-zip-missing-worker"
    )
    import hashlib

    present, missing = b"AAA", b"BBBB"
    await _insert_artifact(
        migrated_db_url, eid, "a.json", hashlib.sha256(present).hexdigest(), "application/json", 3
    )
    await _write_bytes(str(tmp_path), eid, "a.json", present)
    # "gone.json" has a metadata row but its bytes are never written to disk.
    await _insert_artifact(
        migrated_db_url,
        eid,
        "gone.json",
        hashlib.sha256(missing).hexdigest(),
        "application/json",
        4,
    )

    r = await authed_admin_client.get(f"/admin/executions/{eid}/artifacts.zip")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_artifact_download_sanitizes_filename(
    authed_admin_client: AsyncClient, migrated_db_url: str, tmp_path, monkeypatch
) -> None:
    # A rel_path with a quote (not rejected by validate_rel_path) must not
    # produce a malformed Content-Disposition: the quote is sanitized to "_".
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifacts_root", tmp_path)
    eid = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "artifact-sanitize-worker"
    )
    import hashlib

    rel = 'weird".json'
    content = b"x"
    await _insert_artifact(
        migrated_db_url,
        eid,
        rel,
        hashlib.sha256(content).hexdigest(),
        "application/json",
        len(content),
    )
    await _write_bytes(str(tmp_path), eid, rel, content)

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            aid = (
                await conn.execute(
                    text("SELECT id FROM task_artifacts WHERE execution_id = CAST(:e AS uuid)"),
                    {"e": eid},
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    r = await authed_admin_client.get(f"/admin/executions/{eid}/artifacts/{aid}/download")
    assert r.status_code == 200
    assert r.content == content
    cd = r.headers["content-disposition"]
    # The quote in the ASCII name is sanitized to "_"; filename* carries the percent-encoded
    # original.
    assert 'filename="weird_.json"' in cd
    assert "filename*=UTF-8''" in cd


@pytest.mark.asyncio
async def test_artifact_download_non_ascii_filename(
    authed_admin_client: AsyncClient, migrated_db_url: str, tmp_path, monkeypatch
) -> None:
    # A rel_path with a non-ASCII basename (CJK) must return 200 without UnicodeEncodeError
    # (Starlette encodes headers as latin-1; without RFC 6266 it would raise 500).
    # The Content-Disposition header must contain filename*=UTF-8'' with the
    # percent-encoded name and the ASCII fallback filename= with '?' as substitute.
    from urllib.parse import quote

    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifacts_root", tmp_path)
    eid = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "artifact-utf8-worker"
    )
    import hashlib

    rel = "resultado_あ.json"  # あ — outside the latin-1 range
    content = b"utf8-content"
    await _insert_artifact(
        migrated_db_url,
        eid,
        rel,
        hashlib.sha256(content).hexdigest(),
        "application/json",
        len(content),
    )
    await _write_bytes(str(tmp_path), eid, rel, content)

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            aid = (
                await conn.execute(
                    text("SELECT id FROM task_artifacts WHERE execution_id = CAST(:e AS uuid)"),
                    {"e": eid},
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    r = await authed_admin_client.get(f"/admin/executions/{eid}/artifacts/{aid}/download")
    assert r.status_code == 200
    assert r.content == content
    cd = r.headers["content-disposition"]
    # ASCII fallback filename=: the CJK character is replaced by '?'
    assert 'filename="resultado_?.json"' in cd
    # filename*= UTF-8 percent-encoded
    expected_encoded = quote(rel, safe="")
    assert f"filename*=UTF-8''{expected_encoded}" in cd


@pytest.mark.asyncio
async def test_task_detail_shows_latest_attempt_artifacts(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    eid = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "artifact-view-worker"
    )
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            task_id = (
                await conn.execute(
                    text("SELECT task_id FROM task_executions WHERE id = CAST(:e AS uuid)"),
                    {"e": eid},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    await _insert_artifact(migrated_db_url, eid, "results.json", "sha", "application/json", 10)
    r = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    assert r.status_code == 200
    assert "results.json" in r.text
    assert f"/admin/executions/{eid}/artifacts" in r.text


@pytest.mark.asyncio
async def test_delete_task_removes_artifact_metadata_and_bytes(
    authed_admin_client: AsyncClient, migrated_db_url: str, tmp_path, monkeypatch
) -> None:
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifacts_root", tmp_path)
    eid = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "artifact-del-worker"
    )
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            task_id = (
                await conn.execute(
                    text("SELECT task_id FROM task_executions WHERE id = CAST(:e AS uuid)"),
                    {"e": eid},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    await _insert_artifact(migrated_db_url, eid, "r.json", "sha", "application/json", 1)
    await _write_bytes(str(tmp_path), eid, "r.json", b"x")

    # bulk delete only removes tasks in a terminal state; we leave it 'completed'.
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET status = 'completed' WHERE id = :t"),
                {"t": task_id},
            )
    finally:
        await engine.dispose()

    # Task deletion via the admin bulk delete endpoint.
    r = await authed_admin_client.post("/admin/tasks/bulk/delete", json={"task_ids": [task_id]})
    assert r.status_code in (200, 202)

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            n = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM task_artifacts WHERE execution_id = CAST(:e AS uuid)"
                    ),
                    {"e": eid},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert n == 0
    assert not (tmp_path / eid).exists()


@pytest.mark.asyncio
async def test_single_delete_task_removes_artifact_bytes(
    authed_admin_client: AsyncClient, migrated_db_url: str, tmp_path, monkeypatch
) -> None:
    # Covers the cleanup of the individual delete handler (DELETE /admin/tasks/{id}),
    # twin of the bulk path.
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifacts_root", tmp_path)
    eid = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "artifact-single-del-worker"
    )
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            task_id = (
                await conn.execute(
                    text("SELECT task_id FROM task_executions WHERE id = CAST(:e AS uuid)"),
                    {"e": eid},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    await _insert_artifact(migrated_db_url, eid, "r.json", "sha", "application/json", 1)
    await _write_bytes(str(tmp_path), eid, "r.json", b"x")

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET status = 'completed' WHERE id = :t"),
                {"t": task_id},
            )
    finally:
        await engine.dispose()

    r = await authed_admin_client.delete(f"/admin/tasks/{task_id}")
    assert r.status_code in (200, 202, 204)

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            n = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM task_artifacts WHERE execution_id = CAST(:e AS uuid)"
                    ),
                    {"e": eid},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert n == 0
    assert not (tmp_path / eid).exists()


@pytest.mark.asyncio
async def test_task_detail_empty_state_no_artifacts(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    # Task with an execution but no artifacts → empty state of the partial.
    eid = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "artifact-empty-worker"
    )
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            task_id = (
                await conn.execute(
                    text("SELECT task_id FROM task_executions WHERE id = CAST(:e AS uuid)"),
                    {"e": eid},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    r = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    assert r.status_code == 200
    assert "No artifacts produced by this task." in r.text


# --- Artifact harvesting -----------------------------------------------------
#
# A "harvested" artifact keeps its row (evidence that the task produced the
# file) but no longer has bytes on disk: the operational routine purged them
# because the signature is backed up in the KB. The download must explain the cause
# (410 Gone), not lie with a 404 inconsistency error.


@pytest.mark.asyncio
async def test_artifacts_list_json_expone_collected_at(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    eid = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "artifacts-collected-list-worker"
    )
    await _insert_artifact(migrated_db_url, eid, "live.json", "abc", "application/json", 12)
    await _insert_artifact(migrated_db_url, eid, "gone.json", "def", "application/json", 34)
    await _mark_collected(migrated_db_url, eid, "gone.json")

    r = await authed_admin_client.get(f"/admin/executions/{eid}/artifacts")
    assert r.status_code == 200
    by_path = {i["rel_path"]: i for i in r.json()}
    # The harvested row stays in the listing: the task DID produce the artifact.
    assert set(by_path) == {"live.json", "gone.json"}
    assert by_path["live.json"]["collected_at"] is None
    assert by_path["gone.json"]["collected_at"] is not None
    assert by_path["gone.json"]["collected_at"].startswith("20")


@pytest.mark.asyncio
async def test_artifact_download_collected_410(
    authed_admin_client: AsyncClient, migrated_db_url: str, tmp_path, monkeypatch
) -> None:
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifacts_root", tmp_path)
    eid = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "artifact-collected-dl-worker"
    )
    await _insert_artifact(migrated_db_url, eid, "gone.json", "sha", "application/json", 9)
    await _mark_collected(migrated_db_url, eid, "gone.json")
    aid = await _artifact_id(migrated_db_url, eid, "gone.json")

    r = await authed_admin_client.get(f"/admin/executions/{eid}/artifacts/{aid}/download")
    assert r.status_code == 410, r.text
    detail = r.json()["detail"]
    assert "collected" in detail
    assert "external storage" in detail


@pytest.mark.asyncio
async def test_artifact_download_collected_410_wins_over_present_bytes(
    authed_admin_client: AsyncClient, migrated_db_url: str, tmp_path, monkeypatch
) -> None:
    # The marker is the source of truth of the contract: if the row says harvested,
    # the response is 410 even if residual bytes remain on disk (e.g. a
    # half-finished purge). It avoids serving bytes the quota no longer counts.
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifacts_root", tmp_path)
    eid = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "artifact-collected-residual-worker"
    )
    await _insert_artifact(migrated_db_url, eid, "gone.json", "sha", "application/json", 1)
    await _write_bytes(str(tmp_path), eid, "gone.json", b"x")
    await _mark_collected(migrated_db_url, eid, "gone.json")
    aid = await _artifact_id(migrated_db_url, eid, "gone.json")

    r = await authed_admin_client.get(f"/admin/executions/{eid}/artifacts/{aid}/download")
    assert r.status_code == 410, r.text


@pytest.mark.asyncio
async def test_artifact_download_missing_bytes_stays_404(
    authed_admin_client: AsyncClient, migrated_db_url: str, tmp_path, monkeypatch
) -> None:
    # Without a harvest marker, missing bytes are still a real
    # inconsistency → 404, not 410.
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifacts_root", tmp_path)
    eid = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "artifact-missing-404-worker"
    )
    await _insert_artifact(migrated_db_url, eid, "nobytes.json", "sha", "application/json", 3)
    aid = await _artifact_id(migrated_db_url, eid, "nobytes.json")

    r = await authed_admin_client.get(f"/admin/executions/{eid}/artifacts/{aid}/download")
    assert r.status_code == 404
    assert r.json()["detail"] == "artifact bytes missing"


@pytest.mark.asyncio
async def test_artifacts_zip_excludes_collected(
    authed_admin_client: AsyncClient, migrated_db_url: str, tmp_path, monkeypatch
) -> None:
    import hashlib

    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifacts_root", tmp_path)
    eid = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "artifact-zip-mixed-worker"
    )
    live = b"AAA"
    await _insert_artifact(
        migrated_db_url, eid, "a.json", hashlib.sha256(live).hexdigest(), "application/json", 3
    )
    await _write_bytes(str(tmp_path), eid, "a.json", live)
    # Harvested: row present, bytes purged. Must NOT abort the zip with 404.
    await _insert_artifact(migrated_db_url, eid, "gone.json", "sha", "application/json", 4)
    await _mark_collected(migrated_db_url, eid, "gone.json")

    r = await authed_admin_client.get(f"/admin/executions/{eid}/artifacts.zip")
    assert r.status_code == 200, r.text
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert set(zf.namelist()) == {"a.json"}


@pytest.mark.asyncio
async def test_artifacts_zip_all_collected_410(
    authed_admin_client: AsyncClient, migrated_db_url: str, tmp_path, monkeypatch
) -> None:
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifacts_root", tmp_path)
    eid = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "artifact-zip-collected-worker"
    )
    for rel in ("a.json", "b.json"):
        await _insert_artifact(migrated_db_url, eid, rel, "sha", "application/json", 4)
        await _mark_collected(migrated_db_url, eid, rel)

    r = await authed_admin_client.get(f"/admin/executions/{eid}/artifacts.zip")
    assert r.status_code == 410, r.text
    assert "collected" in r.json()["detail"]


@pytest.mark.asyncio
async def test_artifacts_zip_without_artifacts_is_not_410(
    authed_admin_client: AsyncClient, migrated_db_url: str, tmp_path, monkeypatch
) -> None:
    # Execution without artifacts: the 410 is for "all harvested", not for the
    # empty case (nothing was purged). The existing behaviour is preserved: empty zip.
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifacts_root", tmp_path)
    eid = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "artifact-zip-vacio-worker"
    )
    r = await authed_admin_client.get(f"/admin/executions/{eid}/artifacts.zip")
    assert r.status_code == 200, r.text
    assert zipfile.ZipFile(io.BytesIO(r.content)).namelist() == []


@pytest.mark.asyncio
async def test_task_detail_collected_without_download_link(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    eid = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "artifact-ui-collected-worker"
    )
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            task_id = (
                await conn.execute(
                    text("SELECT task_id FROM task_executions WHERE id = CAST(:e AS uuid)"),
                    {"e": eid},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    await _insert_artifact(migrated_db_url, eid, "gone.json", "sha", "application/json", 10)
    await _mark_collected(migrated_db_url, eid, "gone.json")
    aid = await _artifact_id(migrated_db_url, eid, "gone.json")

    r = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    assert r.status_code == 200
    # The rel_path stays visible (evidence), but without a download link.
    assert "gone.json" in r.text
    assert f"/admin/executions/{eid}/artifacts/{aid}/download" not in r.text
    assert "Collected" in r.text
    # Only artifact and it is harvested → nothing to zip.
    assert "Download all (zip)" not in r.text


@pytest.mark.asyncio
async def test_task_detail_zip_link_with_mix(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    # With at least one non-harvested artifact, the zip button is still offered.
    eid = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "artifact-ui-mixed-worker"
    )
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            task_id = (
                await conn.execute(
                    text("SELECT task_id FROM task_executions WHERE id = CAST(:e AS uuid)"),
                    {"e": eid},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    await _insert_artifact(migrated_db_url, eid, "live.json", "sha", "application/json", 10)
    await _insert_artifact(migrated_db_url, eid, "gone.json", "sha", "application/json", 10)
    await _mark_collected(migrated_db_url, eid, "gone.json")
    live_id = await _artifact_id(migrated_db_url, eid, "live.json")
    gone_id = await _artifact_id(migrated_db_url, eid, "gone.json")

    r = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    assert r.status_code == 200
    assert f"/admin/executions/{eid}/artifacts/{live_id}/download" in r.text
    assert f"/admin/executions/{eid}/artifacts/{gone_id}/download" not in r.text
    assert "Download all (zip)" in r.text
