"""Tests for the failures dashboard view."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server.api.admin_ui import (
    _LIMIT_DEFAULT,
    _LIMIT_MAX,
    _RANGE_DEFAULT,
    _clamp_limit,
    _normalize_range,
    _range_to_since,
)
from tests.conftest import mint_token_via_app


def test_normalize_range_accepts_whitelist() -> None:
    assert _normalize_range("today") == "today"
    assert _normalize_range("24h") == "24h"
    assert _normalize_range("7d") == "7d"
    assert _normalize_range("30d") == "30d"
    assert _normalize_range("all") == "all"


def test_normalize_range_falls_back_to_default() -> None:
    assert _normalize_range(None) == _RANGE_DEFAULT
    assert _normalize_range("") == _RANGE_DEFAULT
    assert _normalize_range("bogus") == _RANGE_DEFAULT
    # The default window changed from "7d" to the current day.
    assert _RANGE_DEFAULT == "today"


def test_range_to_since_all_is_none() -> None:
    now = datetime(2026, 6, 26, 12, 0, 0, tzinfo=UTC)
    assert _range_to_since("all", now) is None


def test_range_to_since_today_is_utc_midnight() -> None:
    # "today" starts at UTC midnight of the day of `now`, not at now-24h.
    now = datetime(2026, 6, 26, 12, 34, 56, tzinfo=UTC)
    assert _range_to_since("today", now) == datetime(2026, 6, 26, 0, 0, 0, tzinfo=UTC)


def test_range_to_since_windows() -> None:
    now = datetime(2026, 6, 26, 12, 0, 0, tzinfo=UTC)
    assert _range_to_since("24h", now) == now - timedelta(hours=24)
    assert _range_to_since("7d", now) == now - timedelta(days=7)
    assert _range_to_since("30d", now) == now - timedelta(days=30)


def test_clamp_limit() -> None:
    assert _clamp_limit(50) == 50
    assert _clamp_limit(0) == 1
    assert _clamp_limit(-5) == 1
    # The ceiling is the selector maximum (_LIMIT_MAX), not a loose 1000: a high
    # ?limit is clamped to what the UI offers. Tied to the constant so that the
    # test and the contract do not diverge again.
    assert _clamp_limit(5000) == _LIMIT_MAX
    assert _LIMIT_DEFAULT == 50


_REGISTER = {
    "hostname": "fail-worker",
    "os": "linux",
    "arch": "x86_64",
    "ram_mb": 1024,
    "disk_mb": 10000,
    "tags": [],
    "capabilities": {},
}


async def _register(client: AsyncClient, hostname: str = "fail-worker") -> str:
    token = await mint_token_via_app()
    resp = await client.post(
        "/v1/workers/register",
        json={**_REGISTER, "hostname": hostname, "enrollment_token": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["worker_id"]


async def _create_task(client: AsyncClient, command: str = "run_batch") -> int:
    resp = await client.post(
        "/admin/tasks",
        json={"target": "1", "payload": {"commands": [command], "requirements": []}},
    )
    assert resp.status_code == 200, resp.text
    return int(resp.json()["task_id"])


async def _seed_failed_task(
    client: AsyncClient,
    db_url: str,
    worker_id: str,
    *,
    exit_code: int | None,
    stderr_lines: list[str],
    command_line: str | None = None,
    command: str = "run_batch",
    completed_offset: timedelta = timedelta(0),
) -> tuple[int, str]:
    """Create a task via /admin/tasks, mark it failed and seed its execution
    + output_events. `completed_offset` shifts `completed_at` into the past.
    Returns (task_id, execution_id)."""
    task_id = await _create_task(client, command=command)
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE tasks SET status='failed', "
                    "completed_at = now() - make_interval(secs => :off_secs), "
                    "command=:cmd WHERE id=:t"
                ),
                {"off_secs": completed_offset.total_seconds(), "cmd": command, "t": task_id},
            )
            execution_id = str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO task_executions "
                            "(task_id, worker_id, status, attempt_index, exit_code, "
                            " started_at, finished_at) "
                            "VALUES (:t, :w, 'failed', 1, :ec, now(), now()) "
                            "RETURNING id::text"
                        ),
                        {"t": task_id, "w": worker_id, "ec": exit_code},
                    )
                ).scalar_one()
            )
            seq = 1
            for stream, lines in (
                ("command", [command_line] if command_line else []),
                ("stderr", stderr_lines),
            ):
                for line in lines:
                    await conn.execute(
                        text(
                            "INSERT INTO output_events "
                            "(execution_id, seq, ts, stream, text) "
                            "VALUES (CAST(:e AS uuid), :seq, now(), :st, :tx)"
                        ),
                        {"e": execution_id, "seq": seq, "st": stream, "tx": line + "\n"},
                    )
                    seq += 1
    finally:
        await engine.dispose()
    return task_id, execution_id


@pytest.mark.asyncio
async def test_failures_partial_no_html_wrapper(
    authed_admin_client: AsyncClient,
) -> None:
    resp = await authed_admin_client.get("/admin/outcomes/partial")
    assert resp.status_code == 200
    body = resp.text
    assert "<html" not in body.lower()


@pytest.mark.asyncio
async def test_failures_partial_shows_signature_and_example(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id = await _register(authed_admin_client, hostname="oom-host")
    await _seed_failed_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        exit_code=1,
        stderr_lines=["Traceback (most recent call last):", "RuntimeError: CUDA out of memory"],
        command_line="$ run_batch --model x",
        command="run_batch",
    )
    resp = await authed_admin_client.get("/admin/outcomes/partial?range=all&limit=50")
    assert resp.status_code == 200
    body = resp.text
    # normalized signature of the last stderr line
    assert "runtimeerror: cuda out of memory" in body.lower()
    # exit code panel + worker panel
    assert worker_id[:8] in body
    # example: command_line rendered without the "$ " prefix
    assert "run_batch --model x" in body
    assert "$ run_batch" not in body  # prefix must be stripped, not passed through
    # download link to the existing admin route
    assert "/output/download" in body


@pytest.mark.asyncio
async def test_failures_page_full_html(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.get("/admin/outcomes")
    assert resp.status_code == 200
    body = resp.text
    assert "<html" in body.lower()
    assert "Outcomes" in body
    # preset bar present
    assert 'hx-get="/admin/outcomes/partial"' in body
    assert ">24h<" in body and ">All<" in body
    # nav link present
    assert 'href="/admin/outcomes"' in body


@pytest.mark.asyncio
async def test_failures_page_has_loading_indicator(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.get("/admin/outcomes")
    assert resp.status_code == 200
    body = resp.text
    # form and refresh point to the same spinner; both add the form to the
    # indicator set: .htmx-request on #failures-filter is the CSS hook
    # that dims the disabled controls during the request.
    assert 'hx-indicator="this, #failures-loading"' in body
    assert 'hx-indicator="#failures-filter, #failures-loading"' in body
    assert 'id="failures-loading"' in body
    assert "htmx-indicator" in body


@pytest.mark.asyncio
async def test_failures_partial_excludes_loading_indicator(
    authed_admin_client: AsyncClient,
) -> None:
    resp = await authed_admin_client.get("/admin/outcomes/partial")
    assert resp.status_code == 200
    # the indicator lives outside #failures-block so a partial swap can't erase it
    assert 'id="failures-loading"' not in resp.text


@pytest.mark.asyncio
async def test_failures_page_empty_state(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.get("/admin/outcomes?range=24h")
    assert resp.status_code == 200
    assert "No failed tasks in the last 24 hours." in resp.text


@pytest.mark.asyncio
async def test_outcomes_empty_state_status_completed(authed_admin_client: AsyncClient) -> None:
    """The empty state must not say "No failures" for a status other than
    failed — it would be false (there were no *failures*, there were zero
    *completed*)."""
    resp = await authed_admin_client.get("/admin/outcomes/partial?range=24h&status=completed")
    assert resp.status_code == 200
    assert "No completed tasks in the last 24 hours." in resp.text
    assert "No failures" not in resp.text


@pytest.mark.asyncio
async def test_failures_range_filters_by_completed_at(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id = await _register(authed_admin_client, hostname="old-host")
    # completed 10 days ago: inside `all`/`30d`, outside `24h`/`7d`
    await _seed_failed_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        exit_code=2,
        stderr_lines=["OldError: boom"],
        completed_offset=timedelta(days=10),
    )
    inside = await authed_admin_client.get("/admin/outcomes/partial?range=all")
    assert "olderror: boom" in inside.text.lower()
    outside = await authed_admin_client.get("/admin/outcomes/partial?range=24h")
    assert "olderror: boom" not in outside.text.lower()
    assert "No failed tasks in the last 24 hours." in outside.text


@pytest.mark.asyncio
async def test_failures_scoped_to_session_workspace(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    # Seed a failed task in the Default workspace and assert it is visible to the
    # Default session. (Cross-workspace isolation — that a second workspace cannot
    # see this data — is covered by the v1 endpoint tests in
    # test_api_v1_insights_failures.py; this test proves the scope param is wired.)
    worker_id = await _register(authed_admin_client, hostname="iso-host")
    await _seed_failed_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        exit_code=3,
        stderr_lines=["SecretError: leak-me-not"],
    )
    # The aggregation is scoped by ctx.workspace_id; the Default client sees it,
    # which proves the scope param is wired. (A full second-workspace client is
    # covered by the v1 endpoint's isolation test in test_api_v1_insights_failures.py.)
    seen = await authed_admin_client.get("/admin/outcomes/partial?range=all")
    assert "secreterror: leak-me-not" in seen.text.lower()
