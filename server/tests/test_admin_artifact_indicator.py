"""Topbar artifact indicator: GET /admin/artifact-indicator.

Counts unique artifacts = COUNT(DISTINCT basename(rel_path)) whose rel_path ends
with the configured suffix, in the active workspace. The default suffix is empty
(indicator off); these tests turn it on with `_indicator_on`.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_admin_enforcement import _create_workspace
from tests.test_dashboard import _register, _seed_completed_execution
from tests.test_dashboard_artifacts import _insert_artifact


@pytest.fixture(autouse=True)
def _indicator_on(monkeypatch) -> None:
    """The default suffix is empty (off): the tests turn it on."""
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifact_indicator_suffix", ".report.json")


async def _seed_signature_raw(
    db_url: str, *, workspace_id: str, worker_id: str, rel_path: str
) -> None:
    """Insert a completed task + a completed execution + an artifact at `rel_path`,
    under `workspace_id`. The count only looks at tasks.workspace_id, so the worker
    may belong to any workspace (FK satisfied)."""
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            task_id = (
                await conn.execute(
                    text(
                        "INSERT INTO tasks (workspace_id, command, payload, target_mode, "
                        "target_count, priority, status, run_at, signed, signature) "
                        "VALUES (:w, 'echo fp', '{}'::jsonb, 'count', 1, 0, 'completed', "
                        "now(), true, decode(repeat('00', 64), 'hex')) RETURNING id"
                    ),
                    {"w": workspace_id},
                )
            ).scalar_one()
            eid = (
                await conn.execute(
                    text(
                        "INSERT INTO task_executions (task_id, worker_id, status, exit_code, "
                        "finished_at) VALUES (:t, :wk, 'completed', 0, now()) RETURNING id"
                    ),
                    {"t": task_id, "wk": worker_id},
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO task_artifacts (execution_id, rel_path, size_bytes, sha256, "
                    "content_type) VALUES (:e, :p, 10, 'sha', 'application/json')"
                ),
                {"e": eid, "p": rel_path},
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_artifact_indicator_counts_distinct_models(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """N distinct signed artifacts => the pill shows N."""
    eid = await _seed_completed_execution(authed_admin_client, migrated_db_url, "fp-count")
    for name in ("a.report.json", "b.report.json", "c.report.json"):
        await _insert_artifact(migrated_db_url, eid, name, "sha", "application/json", 10)

    r = await authed_admin_client.get("/admin/artifact-indicator", headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    assert "Artifacts" in r.text
    assert 'tele-num">3<' in r.text


@pytest.mark.asyncio
async def test_artifact_indicator_dedups_by_basename(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The same signed artifact in 2 executions/paths counts as 1."""
    eid1 = await _seed_completed_execution(authed_admin_client, migrated_db_url, "fp-dedup-1")
    eid2 = await _seed_completed_execution(authed_admin_client, migrated_db_url, "fp-dedup-2")
    await _insert_artifact(
        migrated_db_url, eid1, "out/qwen.report.json", "s1", "application/json", 10
    )
    await _insert_artifact(
        migrated_db_url, eid2, "other/qwen.report.json", "s2", "application/json", 10
    )

    r = await authed_admin_client.get("/admin/artifact-indicator", headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    assert 'tele-num">1<' in r.text


@pytest.mark.asyncio
async def test_artifact_indicator_filters_by_suffix(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Artifacts not ending in .report.json do not inflate the count."""
    eid = await _seed_completed_execution(authed_admin_client, migrated_db_url, "fp-suffix")
    await _insert_artifact(
        migrated_db_url, eid, "model-x.report.json", "s1", "application/json", 10
    )
    await _insert_artifact(migrated_db_url, eid, "results.json", "s2", "application/json", 10)

    r = await authed_admin_client.get("/admin/artifact-indicator", headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    assert 'tele-num">1<' in r.text


@pytest.mark.asyncio
async def test_artifact_indicator_isolated_by_workspace(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Signatures from another workspace are not counted in the active workspace."""
    # Signature in the active workspace (Default) -> counts.
    eid = await _seed_completed_execution(authed_admin_client, migrated_db_url, "fp-iso-a")
    await _insert_artifact(
        migrated_db_url, eid, "model-a.report.json", "sa", "application/json", 10
    )

    # Signature in ANOTHER workspace -> must NOT count.
    ws_b = await _create_workspace("WS_FP_B", f"ws-fp-b-{uuid.uuid4().hex[:8]}")
    worker_id = await _register(authed_admin_client, hostname="fp-iso-borrow")
    await _seed_signature_raw(
        migrated_db_url, workspace_id=ws_b, worker_id=worker_id, rel_path="model-b.report.json"
    )

    r = await authed_admin_client.get("/admin/artifact-indicator", headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    assert 'tele-num">1<' in r.text


@pytest.mark.asyncio
async def test_artifact_indicator_non_htmx_redirects(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Typed directly (no HX-Request) -> 303 to /admin/."""
    r = await authed_admin_client.get(
        "/admin/artifact-indicator", headers={"HX-Request": ""}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/"


@pytest.mark.asyncio
async def test_artifact_indicator_feature_off_empty_body(
    authed_admin_client: AsyncClient, migrated_db_url: str, monkeypatch
) -> None:
    """With an empty ARTIFACT_INDICATOR_SUFFIX the endpoint returns an empty body (pill hidden)."""
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifact_indicator_suffix", "")
    eid = await _seed_completed_execution(authed_admin_client, migrated_db_url, "fp-off")
    await _insert_artifact(migrated_db_url, eid, "model.report.json", "s1", "application/json", 10)

    r = await authed_admin_client.get("/admin/artifact-indicator", headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    assert r.text.strip() == ""
    assert "Artifacts" not in r.text


@pytest.mark.asyncio
async def test_topbar_includes_artifact_indicator(
    authed_admin_client: AsyncClient,
) -> None:
    """The dashboard (base.html) includes the Artifacts <li> with HTMX polling."""
    r = await authed_admin_client.get("/admin/")
    assert r.status_code == 200
    body = r.text
    assert 'id="artifact-indicator"' in body
    assert 'hx-get="/admin/artifact-indicator"' in body
    assert 'hx-trigger="load, every 30s"' in body


@pytest.mark.asyncio
async def test_topbar_omits_artifact_indicator_when_feature_off(
    authed_admin_client: AsyncClient, monkeypatch
) -> None:
    """With an empty ARTIFACT_INDICATOR_SUFFIX base.html does NOT render the pill
    <li>: without the element there is no HTMX polling every 30s of
    /admin/artifact-indicator."""
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifact_indicator_suffix", "")
    r = await authed_admin_client.get("/admin/")
    assert r.status_code == 200
    body = r.text
    assert 'id="artifact-indicator"' not in body
    assert 'hx-get="/admin/artifact-indicator"' not in body


@pytest.mark.asyncio
async def test_artifact_indicator_no_status_shows_only_produced(
    authed_admin_client: AsyncClient, migrated_db_url: str, monkeypatch, tmp_path
) -> None:
    """Without a valid status (path points to a missing file) => only produced."""
    from orcaster_server.config import settings as app_settings

    eid = await _seed_completed_execution(authed_admin_client, migrated_db_url, "fp-nost")
    await _insert_artifact(migrated_db_url, eid, "m2.report.json", "s1", "application/json", 10)
    monkeypatch.setattr(
        app_settings, "artifact_indicator_sync_status_path", str(tmp_path / "absent.json")
    )

    r = await authed_admin_client.get("/admin/artifact-indicator", headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    assert 'tele-num">1<' in r.text
    assert "uploaded" not in r.text.lower()


@pytest.mark.asyncio
async def test_artifact_indicator_empty_status_path_shows_only_produced(
    authed_admin_client: AsyncClient, migrated_db_url: str, monkeypatch
) -> None:
    """Empty setting (uploaded feature off) => only produced, no 500."""
    from orcaster_server.config import settings as app_settings

    eid = await _seed_completed_execution(authed_admin_client, migrated_db_url, "fp-emptyp")
    await _insert_artifact(migrated_db_url, eid, "m3.report.json", "s1", "application/json", 10)
    monkeypatch.setattr(app_settings, "artifact_indicator_sync_status_path", "")

    r = await authed_admin_client.get("/admin/artifact-indicator", headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    assert 'tele-num">1<' in r.text
    assert "uploaded" not in r.text.lower()


@pytest.mark.asyncio
async def test_artifact_indicator_reads_uploaded_from_status(
    authed_admin_client: AsyncClient, migrated_db_url: str, monkeypatch, tmp_path
) -> None:
    """With a valid sync_status.json the endpoint exposes uploaded + freshness."""
    import json

    from orcaster_server.config import settings as app_settings

    eid = await _seed_completed_execution(authed_admin_client, migrated_db_url, "fp-up")
    await _insert_artifact(migrated_db_url, eid, "m.report.json", "s1", "application/json", 10)
    status = tmp_path / "sync_status.json"
    status.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "uploaded_total": 5923,
                "by_source": {"primary": 5923},
                "commit_sha": "6a72bcd",
                "pushed_at": "2026-06-26T15:30:00Z",
            }
        )
    )
    monkeypatch.setattr(app_settings, "artifact_indicator_sync_status_path", str(status))

    r = await authed_admin_client.get("/admin/artifact-indicator", headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    assert 'tele-num">1<' in r.text  # produced
    assert "5,923" in r.text  # uploaded, thousands-formatted
    assert "6a72bcd" in r.text  # commit sha in tooltip
    assert "2026-06-26T15:30:00" in r.text  # pushed_at ISO in <time datetime=...>


@pytest.mark.asyncio
async def test_topbar_not_rendered_with_default_suffix(
    authed_admin_client: AsyncClient, monkeypatch
) -> None:
    """With the Settings default (empty suffix) the pill is not rendered."""
    from orcaster_server.config import Settings
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(
        app_settings,
        "artifact_indicator_suffix",
        Settings(_env_file=None).artifact_indicator_suffix,
    )
    r = await authed_admin_client.get("/admin/")
    assert r.status_code == 200
    assert 'id="artifact-indicator"' not in r.text
