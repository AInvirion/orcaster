"""Task filter by artifact in the dashboard endpoint."""

import hashlib

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_dashboard import _seed_completed_execution
from tests.test_dashboard_artifacts import _insert_artifact

_HX = {"HX-Request": "true"}


async def _task_id_of_execution(db_url: str, execution_id: str) -> int:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            return (
                await conn.execute(
                    text("SELECT task_id FROM task_executions WHERE id = CAST(:e AS uuid)"),
                    {"e": execution_id},
                )
            ).scalar_one()
    finally:
        await engine.dispose()


async def _list(client: AsyncClient, **params) -> str:
    r = await client.get("/admin/tasks", headers=_HX, params=params)
    assert r.status_code == 200, r.text
    return r.text


@pytest.mark.asyncio
async def test_with_artifacts_includes_only_producers(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    eid = await _seed_completed_execution(authed_admin_client, migrated_db_url, "art-w1")
    tid_with = await _task_id_of_execution(migrated_db_url, eid)
    await _insert_artifact(migrated_db_url, eid, "results.json", "h1", "application/json", 10)
    # A task without artifacts.
    eid2 = await _seed_completed_execution(authed_admin_client, migrated_db_url, "art-w2")
    tid_without = await _task_id_of_execution(migrated_db_url, eid2)

    body = await _list(authed_admin_client, has_artifacts="yes")
    assert f"task-row-{tid_with}" in body
    assert f"task-row-{tid_without}" not in body


@pytest.mark.asyncio
async def test_without_artifacts_excludes_producers(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    eid = await _seed_completed_execution(authed_admin_client, migrated_db_url, "art-w3")
    tid_with = await _task_id_of_execution(migrated_db_url, eid)
    await _insert_artifact(migrated_db_url, eid, "out.bin", "h2", "application/octet-stream", 4)
    eid2 = await _seed_completed_execution(authed_admin_client, migrated_db_url, "art-w4")
    tid_without = await _task_id_of_execution(migrated_db_url, eid2)

    body = await _list(authed_admin_client, has_artifacts="no")
    assert f"task-row-{tid_without}" in body
    assert f"task-row-{tid_with}" not in body


@pytest.mark.asyncio
async def test_filter_by_content_type(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    eid_json = await _seed_completed_execution(authed_admin_client, migrated_db_url, "art-w5")
    tid_json = await _task_id_of_execution(migrated_db_url, eid_json)
    await _insert_artifact(migrated_db_url, eid_json, "a.json", "h3", "application/json", 1)
    eid_txt = await _seed_completed_execution(authed_admin_client, migrated_db_url, "art-w6")
    tid_txt = await _task_id_of_execution(migrated_db_url, eid_txt)
    await _insert_artifact(migrated_db_url, eid_txt, "a.txt", "h4", "text/plain", 1)

    body = await _list(authed_admin_client, artifact_type="application/json")
    assert f"task-row-{tid_json}" in body
    assert f"task-row-{tid_txt}" not in body


@pytest.mark.asyncio
async def test_filter_by_sha256_exact_and_prefix(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    eid = await _seed_completed_execution(authed_admin_client, migrated_db_url, "art-w7")
    tid = await _task_id_of_execution(migrated_db_url, eid)
    sha = hashlib.sha256(b"payload").hexdigest()
    await _insert_artifact(migrated_db_url, eid, "x.bin", sha, "application/octet-stream", 7)
    eid2 = await _seed_completed_execution(authed_admin_client, migrated_db_url, "art-w8")
    tid2 = await _task_id_of_execution(migrated_db_url, eid2)
    await _insert_artifact(migrated_db_url, eid2, "y.bin", "f" * 64, "application/octet-stream", 7)

    body_exact = await _list(authed_admin_client, artifact_sha256=sha)
    assert f"task-row-{tid}" in body_exact
    assert f"task-row-{tid2}" not in body_exact

    body_prefix = await _list(authed_admin_client, artifact_sha256=sha[:12])
    assert f"task-row-{tid}" in body_prefix
    assert f"task-row-{tid2}" not in body_prefix

    # Uppercase prefix: the server lowercases it before the LIKE (the sha
    # lives in lowercase hex), so it must still match.
    body_upper = await _list(authed_admin_client, artifact_sha256=sha[:12].upper())
    assert f"task-row-{tid}" in body_upper


@pytest.mark.asyncio
async def test_filter_by_path_glob_anchored(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    eid = await _seed_completed_execution(authed_admin_client, migrated_db_url, "art-w9")
    tid = await _task_id_of_execution(migrated_db_url, eid)
    await _insert_artifact(migrated_db_url, eid, "report2.5-7b.json", "h5", "application/json", 1)
    eid2 = await _seed_completed_execution(authed_admin_client, migrated_db_url, "art-w10")
    tid2 = await _task_id_of_execution(migrated_db_url, eid2)
    # In a subfolder: must NOT match the anchored glob `report2.5-*.json`.
    await _insert_artifact(
        migrated_db_url, eid2, "nested/report2.5-7b.json", "h6", "application/json", 1
    )

    body = await _list(authed_admin_client, artifact_path="report2.5-*.json")
    assert f"task-row-{tid}" in body
    assert f"task-row-{tid2}" not in body

    # With a folder prefix, the subfolder one does match.
    body2 = await _list(authed_admin_client, artifact_path="nested/report2.5-*.json")
    assert f"task-row-{tid2}" in body2
    assert f"task-row-{tid}" not in body2


@pytest.mark.asyncio
async def test_match_across_old_attempt(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """An artifact from an old attempt matches (EXISTS over any execution)."""
    eid = await _seed_completed_execution(authed_admin_client, migrated_db_url, "art-w11")
    tid = await _task_id_of_execution(migrated_db_url, eid)
    await _insert_artifact(migrated_db_url, eid, "from-old.json", "h7", "application/json", 1)
    # Second execution (newer attempt) of the SAME task, without artifacts.
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            wid = (
                await conn.execute(
                    text("SELECT worker_id FROM task_executions WHERE id = CAST(:e AS uuid)"),
                    {"e": eid},
                )
            ).scalar_one()
            # Explicit attempt_index=2: migration 0014 enforces
            # UNIQUE (task_id, worker_id, attempt_index) and the default is 1,
            # which would collide with the seed execution (same task + worker).
            await conn.execute(
                text(
                    "INSERT INTO task_executions "
                    "(task_id, worker_id, status, exit_code, finished_at, attempt_index) "
                    "VALUES (:t, :w, 'failed', 1, now(), 2)"
                ),
                {"t": tid, "w": wid},
            )
    finally:
        await engine.dispose()

    body = await _list(authed_admin_client, artifact_path="from-old.json")
    assert f"task-row-{tid}" in body


@pytest.mark.asyncio
async def test_artifact_filter_combines_with_status(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    eid = await _seed_completed_execution(authed_admin_client, migrated_db_url, "art-w12")
    tid = await _task_id_of_execution(migrated_db_url, eid)
    await _insert_artifact(migrated_db_url, eid, "z.json", "h8", "application/json", 1)
    # status=failed must not match this task (its task is queued by default
    # from the seed); the AND between filters excludes it.
    body = await _list(authed_admin_client, has_artifacts="yes", status="failed")
    assert f"task-row-{tid}" not in body


@pytest.mark.asyncio
async def test_no_artifact_filter_unchanged(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Without artifact params (default any) the listing does not change."""
    eid = await _seed_completed_execution(authed_admin_client, migrated_db_url, "art-w13")
    tid = await _task_id_of_execution(migrated_db_url, eid)
    await _insert_artifact(migrated_db_url, eid, "k.json", "h9", "application/json", 1)
    body = await _list(authed_admin_client)
    assert f"task-row-{tid}" in body


@pytest.mark.asyncio
async def test_chip_remove_preserves_artifact_params(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """When rendering with status + artifact, the status chip keeps the
    artifact params in its remove_qs (removal href)."""
    eid = await _seed_completed_execution(authed_admin_client, migrated_db_url, "art-chip")
    await _insert_artifact(migrated_db_url, eid, "c.json", "hc", "application/json", 1)
    r = await authed_admin_client.get(
        "/admin/tasks",
        headers=_HX,
        params={"status": "queued", "artifact_type": "application/json"},
    )
    assert r.status_code == 200, r.text
    # The status chip removes only status; its href keeps the artifact_type.
    assert "artifact_type=application" in r.text


@pytest.mark.asyncio
async def test_non_htmx_redirect_preserves_artifact_params(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """GET without HX-Request redirects to /admin/ preserving the artifact params."""
    r = await authed_admin_client.get(
        "/admin/tasks",
        headers={"HX-Request": "false"},
        params={"has_artifacts": "yes", "artifact_path": "*.json"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "has_artifacts=yes" in loc
    assert "artifact_path=" in loc
