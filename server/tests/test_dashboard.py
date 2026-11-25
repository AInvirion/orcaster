"""Smoke tests for the HTMX dashboard."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server.api.admin_ui import templates
from tests.conftest import mint_token_via_app

REGISTER = {
    "hostname": "dash-worker",
    "os": "linux",
    "arch": "x86_64",
    "ram_mb": 1024,
    "disk_mb": 10000,
    "tags": [],
    "capabilities": {},
}


async def _register(client: AsyncClient, hostname: str = "dash-worker") -> str:
    token = await mint_token_via_app()
    resp = await client.post(
        "/v1/workers/register",
        json={**REGISTER, "hostname": hostname, "enrollment_token": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["worker_id"]


async def _create_task(
    client: AsyncClient,
    command: str = "echo dash",
    commands: list[str] | None = None,
    requirements: list[str] | None = None,
) -> int:
    # Contract: the worker expects
    # payload={"requirements": list[str], "commands": list[str] non-empty}.
    # By default we derive commands from the `command` arg to reflect the flow
    # of the "New task" modal; tests may override by passing commands/requirements.
    payload_commands = commands if commands is not None else [command]
    payload_requirements = requirements if requirements is not None else []
    resp = await client.post(
        "/admin/tasks",
        json={
            "target": "1",
            "payload": {
                "commands": payload_commands,
                "requirements": payload_requirements,
            },
        },
    )
    assert resp.status_code == 200, resp.text
    return int(resp.json()["task_id"])


async def _exec_sql(db_url: str, sql: str, params: dict[str, Any]) -> None:
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params)
    finally:
        await engine.dispose()


async def _row(db_url: str, sql: str, params: dict[str, Any]) -> dict | None:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            r = (await conn.execute(text(sql), params)).mappings().first()
            return dict(r) if r else None
    finally:
        await engine.dispose()


async def _rows(db_url: str, sql: str, params: dict[str, Any]) -> list[dict]:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            r = (await conn.execute(text(sql), params)).mappings().all()
            return [dict(x) for x in r]
    finally:
        await engine.dispose()


def _worker_card_block(body: str, worker_id: str) -> str:
    """Cut out of the deck body the HTML block of the card of ONE specific
    worker (from its `data-worker-id="..."` to the `data-worker-id="..."`
    of the next card, or the end of the body if it is the last one).

    Without this cut, an assertion such as `'data-confirm-tone="warn"' in body`
    would match against ANY card in the render, not necessarily the one of the
    worker under test: it would pass even if the attribute were on the wrong
    card."""
    start = body.index(f'data-worker-id="{worker_id}"')
    next_marker = body.find('data-worker-id="', start + 1)
    end = next_marker if next_marker != -1 else len(body)
    return body[start:end]


@pytest.mark.asyncio
async def test_index_returns_html_with_htmx(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.get("/admin/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "<title>" in body
    # htmx is served locally (vendored), no longer from the unpkg CDN.
    assert "/static/htmx.min.js" in body
    assert "/admin/workers" in body
    assert "/admin/tasks" in body


@pytest.mark.asyncio
async def test_admin_workers_without_htmx_redirects_to_dashboard(
    authed_admin_client: AsyncClient,
) -> None:
    # /admin/workers and /admin/tasks are dashboard partials. If someone
    # types them in or shares them directly (without HX-Request), we redirect to /admin/
    # so as not to serve orphan fragments without nav.
    resp = await authed_admin_client.get(
        "/admin/workers", headers={"HX-Request": ""}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/"


@pytest.mark.asyncio
async def test_admin_tasks_without_htmx_redirects_preserving_filters(
    authed_admin_client: AsyncClient,
) -> None:
    resp = await authed_admin_client.get(
        "/admin/tasks?status=failed&q=pip",
        headers={"HX-Request": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # The params are preserved so the dashboard initializes the chips.
    assert resp.headers["location"].startswith("/admin/?")
    assert "status=failed" in resp.headers["location"]
    assert "q=pip" in resp.headers["location"]


@pytest.mark.asyncio
async def test_workers_partial_no_html_wrapper(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.get("/admin/workers", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text
    assert "<html" not in body.lower()
    # The partial is a deck of cards (<ul class="worker-deck-list">), not a
    # table. We also accept the "no workers" case where only the empty
    # message appears.
    assert "worker-deck-list" in body or "worker-deck-empty" in body


@pytest.mark.asyncio
async def test_tasks_partial_no_html_wrapper(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.get("/admin/tasks", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text
    assert "<html" not in body.lower()
    assert "<table" in body


@pytest.mark.asyncio
async def test_workers_partial_shows_registered_worker(authed_admin_client: AsyncClient) -> None:
    await _register(authed_admin_client, hostname="visible-host")
    resp = await authed_admin_client.get("/admin/workers")
    assert resp.status_code == 200
    assert "visible-host" in resp.text


@pytest.mark.asyncio
async def test_workers_json_lists_registered_workers(authed_admin_client: AsyncClient) -> None:
    """`/admin/workers.json` feeds the selector of the "New task" modal with
    the workers available for the `worker(s)` and `all-subset` modes."""
    worker_id = await _register(authed_admin_client, hostname="picker-host")
    resp = await authed_admin_client.get("/admin/workers.json")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    items = resp.json()
    assert isinstance(items, list)
    match = next((w for w in items if w["id"] == worker_id), None)
    assert match is not None
    assert match["hostname"] == "picker-host"
    assert match["status"] in ("online", "paused", "offline")
    # `version` must be present; `null` if the worker did not report it.
    # `REGISTER` carries no version, so this fresh worker exposes `null`.
    assert "version" in match
    assert match["version"] is None
    # Without a reported version it does not reach the task secret floor (fails closed).
    assert match["task_secrets_supported"] is False
    # No leak of heavy fields (ram/disk are not needed here).
    assert set(match.keys()) == {
        "id",
        "hostname",
        "name",
        "status",
        "version",
        "task_secrets_supported",
    }


@pytest.mark.asyncio
async def test_workers_json_exposes_reported_version(
    authed_admin_client: AsyncClient,
) -> None:
    """`/admin/workers.json` must expose the version reported by the worker,
    so a client can read it without scraping the "About" HTML."""
    token = await mint_token_via_app()
    resp = await authed_admin_client.post(
        "/v1/workers/register",
        json={
            **REGISTER,
            "hostname": "versioned-host",
            "version": "0.23.0",
            "enrollment_token": token,
        },
    )
    assert resp.status_code == 200, resp.text
    worker_id = resp.json()["worker_id"]

    resp = await authed_admin_client.get("/admin/workers.json")
    assert resp.status_code == 200
    match = next((w for w in resp.json() if w["id"] == worker_id), None)
    assert match is not None
    assert match["version"] == "0.23.0"
    # This version is below the task secret floor.
    assert match["task_secrets_supported"] is False


@pytest.mark.asyncio
async def test_workers_json_task_secrets_supported_at_floor(
    authed_admin_client: AsyncClient,
) -> None:
    """A worker at the version floor (with a local suffix) receives
    tasks that use stored secrets and `workers.json` says so."""
    token = await mint_token_via_app()
    resp = await authed_admin_client.post(
        "/v1/workers/register",
        json={
            **REGISTER,
            "hostname": "secrets-host",
            "version": "0.47.0+patched",
            "enrollment_token": token,
        },
    )
    assert resp.status_code == 200, resp.text
    worker_id = resp.json()["worker_id"]

    resp = await authed_admin_client.get("/admin/workers.json")
    assert resp.status_code == 200
    match = next((w for w in resp.json() if w["id"] == worker_id), None)
    assert match is not None
    assert match["task_secrets_supported"] is True


@pytest.mark.asyncio
async def test_dashboard_modal_exposes_target_worker_modes(
    authed_admin_client: AsyncClient,
) -> None:
    """The "New task" modal must offer the 4 targeting modes so that
    the operator can direct tasks to specific workers (syntax `worker:`,
    `workers:`, `all:`)."""
    resp = await authed_admin_client.get("/admin/")
    body = resp.text
    assert 'value="count"' in body
    assert 'value="all-available"' in body
    assert 'value="worker"' in body
    assert 'value="all-subset"' in body
    assert 'id="target-workers-row"' in body
    assert 'id="nt-workers-picker"' in body


@pytest.mark.asyncio
async def test_dashboard_modal_exposes_isolation_inputs(
    authed_admin_client: AsyncClient,
) -> None:
    """The "New task" modal must expose the isolation toggle and the inputs for
    `task_env`/`egress_allowlist`, so an operator can create
    tasks with isolation from the UI without falling back to the API/CLI."""
    resp = await authed_admin_client.get("/admin/")
    body = resp.text
    assert 'id="nt-isolation-enabled"' in body
    assert 'id="nt-task-env"' in body
    assert 'id="nt-egress-allowlist"' in body
    # The sub-fields start hidden (revealed by the toggle via JS). This is
    # checked piecewise so as not to couple the test to attribute order:
    # we locate the div by its id and check that its class list carries
    # `isolation-fields` and `hidden`, without requiring order or the absence of others.
    assert 'id="nt-isolation-fields"' in body
    iso_div = re.search(r'<div\b[^>]*\bid="nt-isolation-fields"[^>]*>', body)
    assert iso_div is not None
    iso_classes = re.search(r'\bclass="([^"]*)"', iso_div.group(0))
    assert iso_classes is not None
    class_tokens = iso_classes.group(1).split()
    assert "isolation-fields" in class_tokens
    assert "hidden" in class_tokens


@pytest.mark.asyncio
async def test_workers_partial_drops_capabilities_column(
    authed_admin_client: AsyncClient,
) -> None:
    """Capabilities was always empty and was dropped. The deck shows
    RAM and Disk as independent labels inside each card."""
    await _register(authed_admin_client, hostname="caps-host")
    resp = await authed_admin_client.get("/admin/workers")
    body = resp.text
    assert "Capabilities" not in body
    assert "key(s)" not in body
    assert "RAM" in body
    assert "Disk" in body


@pytest.mark.asyncio
async def test_workers_partial_shows_ram_and_disk_usage(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """RAM/Disk are shown in human format (MB/GB) with a usage bar."""
    worker_id = await _register(authed_admin_client, hostname="usage-host")
    # We force known metrics via SQL (register only sends totals).
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET ram_mb = 2048, ram_used_mb = 512, "
        "disk_mb = 10240, disk_used_mb = 2048 WHERE id = :id",
        {"id": worker_id},
    )
    resp = await authed_admin_client.get("/admin/workers")
    body = resp.text
    # 512 MB / 2 GB → "512 MB / 2.0 GB"
    assert "512 MB" in body
    assert "2.0 GB" in body
    # 2048 MB / 10240 MB → "2.0 GB / 10 GB"
    assert "10 GB" in body
    # Usage bar (CSS classes) present — new markup.
    assert "worker-bar-fill" in body
    # 25% RAM and 20% Disk → both in the "low" band.
    assert "load-low" in body


@pytest.mark.asyncio
async def test_workers_partial_renders_name_as_badge(
    authed_admin_client: AsyncClient,
) -> None:
    token = await mint_token_via_app()
    resp = await authed_admin_client.post(
        "/v1/workers/register",
        json={
            **REGISTER,
            "hostname": "named-host",
            "name": "alpha-worker",
            "enrollment_token": token,
        },
    )
    assert resp.status_code == 200
    resp = await authed_admin_client.get("/admin/workers")
    body = resp.text
    # The new design shows `w.name or w.hostname` as the main title
    # in the card link; the hostname is only reachable on the
    # detail page. The name "alpha-worker" must appear as visible text.
    assert "alpha-worker" in body


@pytest.mark.asyncio
async def test_workers_partial_renders_relative_last_seen(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """last_seen appears as relative time, not as raw ISO."""
    worker_id = await _register(authed_admin_client, hostname="time-host")
    # We force a heartbeat 5 minutes ago. Wide margin so that the
    # latency between the UPDATE and the GET does not drop the bucket to "4m ago".
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET last_heartbeat_at = now() - interval '5 minutes' WHERE id = :id",
        {"id": worker_id},
    )
    resp = await authed_admin_client.get("/admin/workers")
    body = resp.text
    # Relative present. We accept 4m/5m ago: there is clock skew between the postgres
    # of the fixture (container) and the test process, and depending on which clock
    # produces the delta it is common to lose ~30-60s.
    # After the polish pass: the relative is the visible content of the <time>,
    # and the absolute lives in `title=` (ISO 8601) — before, we showed both.
    import re

    match = re.search(r"\(?(\d+)m ago\)?", body)
    assert match is not None, body
    minutes = int(match.group(1))
    assert 3 <= minutes <= 6, f"expected 4-5m ago bucket, got {minutes}m"
    # The <time> still exposes datetime= in ISO for a11y/machines, and the
    # native tooltip (`title=`) shows the absolute UTC.
    assert "<time " in body
    assert 'datetime="' in body
    assert 'title="UTC · ' in body, body
    # The UTC ISO appears as an attribute (datetime= and title=), not as visible
    # text.
    assert re.search(r'datetime="\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', body), body


@pytest.mark.asyncio
async def test_tasks_partial_shows_created_task(authed_admin_client: AsyncClient) -> None:
    await _create_task(authed_admin_client, command="echo unique-cmd-xyz")
    resp = await authed_admin_client.get("/admin/tasks")
    assert resp.status_code == 200
    assert "unique-cmd-xyz" in resp.text


@pytest.mark.asyncio
async def test_task_detail_ok(authed_admin_client: AsyncClient) -> None:
    task_id = await _create_task(authed_admin_client, command="echo detail")
    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    assert resp.status_code == 200
    body = resp.text
    assert f"#{task_id}" in body
    assert "queued" in body  # status
    # Aggregates: target_count is always rendered as the denominator of the
    # progress (e.g. "0 / 1 done"). The "target_count" label itself no longer
    # appears as an independent column after the distill.
    assert "Executions" in body
    assert "echo detail" in body


@pytest.mark.asyncio
async def test_task_detail_count_mode_shows_same_worker_checkbox(
    authed_admin_client: AsyncClient,
) -> None:
    """In `count` mode the detail exposes the "Retry on the
    same worker" opt-in (unchecked by default) next to the Retry button."""
    task_id = await _create_task(authed_admin_client, command="echo cnt")
    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    assert resp.status_code == 200
    body = resp.text
    assert "Retry on the same worker" in body
    # The input exists and is unchecked by default (no `checked` attribute),
    # robust against attribute reordering.
    m = re.search(r'<input[^>]*id="retry-same-worker"[^>]*>', body)
    assert m is not None, "checkbox retry-same-worker missing"
    assert "checked" not in m.group(0)


@pytest.mark.asyncio
async def test_task_detail_all_available_hides_same_worker_checkbox(
    authed_admin_client: AsyncClient,
) -> None:
    """In `all_available` mode retry ignores `force_same_worker`;
    the detail hides the checkbox and shows an explanatory note in its place."""
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "all-available", "payload": {"commands": ["echo aa"]}},
    )
    assert resp.status_code == 200, resp.text
    task_id = int(resp.json()["task_id"])
    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="retry-same-worker"' not in body
    assert 'id="retry-same-worker-note"' in body


@pytest.mark.asyncio
async def test_task_detail_404(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.get("/admin/tasks/9999999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_task_detail_without_children_shows_zero_forks(
    authed_admin_client: AsyncClient,
) -> None:
    # A parent without children does not break the view and shows "Forks (0)".
    task_id = await _create_task(authed_admin_client, command="echo lonely")
    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    assert resp.status_code == 200
    body = resp.text
    assert "Forks (0)" in body


@pytest.mark.asyncio
async def test_task_detail_lists_direct_fork_children(
    authed_admin_client: AsyncClient,
) -> None:
    # The parent detail shows its direct children (forks)
    # with the count and a link to the detail of each child.
    parent_id = await _create_task(authed_admin_client, command="echo parent")
    resp = await authed_admin_client.post(f"/admin/tasks/{parent_id}/fork", json={"target": "1"})
    assert resp.status_code == 200, resp.text
    child_id = int(resp.json()["task_id"])

    resp = await authed_admin_client.get(f"/admin/tasks/{parent_id}")
    assert resp.status_code == 200
    body = resp.text
    assert "Forks (1)" in body
    # Each row links to the child detail.
    assert f'/admin/tasks/{child_id}"' in body
    assert f"#{child_id}" in body


@pytest.mark.asyncio
async def test_worker_detail_ok(authed_admin_client: AsyncClient) -> None:
    worker_id = await _register(authed_admin_client, hostname="lone-worker")
    resp = await authed_admin_client.get(f"/admin/workers/{worker_id}")
    assert resp.status_code == 200
    assert "lone-worker" in resp.text
    assert worker_id in resp.text


@pytest.mark.asyncio
async def test_worker_detail_404(authed_admin_client: AsyncClient) -> None:
    bogus = "00000000-0000-0000-0000-000000000000"
    resp = await authed_admin_client.get(f"/admin/workers/{bogus}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_worker_detail_cache_block_not_pending(
    authed_admin_client: AsyncClient,
) -> None:
    """Without a prior clear-cache: the block appears in the 'Cache not cleaned yet' state."""
    worker_id = await _register(authed_admin_client, hostname="cache-worker-idle")
    resp = await authed_admin_client.get(f"/admin/workers/{worker_id}")
    assert resp.status_code == 200
    assert "worker-cache-block" in resp.text
    assert "Cache not cleaned yet" in resp.text


@pytest.mark.asyncio
async def test_worker_detail_cache_block_pending_after_clear(
    authed_admin_client: AsyncClient,
) -> None:
    """After POST clear-cache the block shows 'Cleaning cache' and the button is disabled."""
    worker_id = await _register(authed_admin_client, hostname="cache-worker-pending")
    clear = await authed_admin_client.post(f"/admin/workers/{worker_id}/clear-cache")
    assert clear.status_code == 200
    resp = await authed_admin_client.get(f"/admin/workers/{worker_id}")
    assert resp.status_code == 200
    assert "worker-cache-block" in resp.text
    assert "Cleaning cache" in resp.text
    # `disabled` must be INSIDE the cache block, on the Clear cache button
    # (not anywhere in the HTML): we isolate the block segment.
    block = resp.text.split("worker-cache-block", 1)[1].split("Clear cache", 1)[0]
    assert "disabled" in block


@pytest.mark.asyncio
async def test_worker_detail_cache_block_timed_out_after_ttl(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """A pending request older than the TTL (old worker that
    never acks) is derived as 'timed out': the block stops showing 'Cleaning
    cache' and the Clear cache button is re-enabled (not disabled)."""
    worker_id = await _register(authed_admin_client, hostname="cache-worker-stale")
    # requested_at in the past, beyond the TTL (10 min); no ack (done_at NULL).
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET cache_flush_requested_at = now() - interval '11 minutes',"
        " cache_flush_done_at = NULL WHERE id = :id",
        {"id": worker_id},
    )
    resp = await authed_admin_client.get(f"/admin/workers/{worker_id}")
    assert resp.status_code == 200
    assert "worker-cache-block" in resp.text
    assert "Cache flush timed out" in resp.text
    assert "Cleaning cache" not in resp.text
    block = resp.text.split("worker-cache-block", 1)[1].split("Clear cache", 1)[0]
    assert "disabled" not in block


@pytest.mark.asyncio
async def test_worker_detail_cache_block_pending_within_ttl(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Control: a recent request (within the TTL) is still pending: it shows
    'Cleaning cache' and the button is disabled."""
    worker_id = await _register(authed_admin_client, hostname="cache-worker-fresh")
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET cache_flush_requested_at = now() - interval '2 minutes',"
        " cache_flush_done_at = NULL WHERE id = :id",
        {"id": worker_id},
    )
    resp = await authed_admin_client.get(f"/admin/workers/{worker_id}")
    assert resp.status_code == 200
    assert "Cleaning cache" in resp.text
    assert "Cache flush timed out" not in resp.text
    block = resp.text.split("worker-cache-block", 1)[1].split("Clear cache", 1)[0]
    assert "disabled" in block


@pytest.mark.asyncio
async def test_worker_detail_cache_block_last_cleaned(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Regression of the `not pending_raw` guard: a completed flush
    (done_at >= requested_at) shows 'Last cleaned' and re-enables the button. This
    is the path that protects the guard change from `not cache_flush_pending` to
    `not pending_raw`: it covers non-null done_at and >= req_at."""
    worker_id = await _register(authed_admin_client, hostname="cache-worker-cleaned")
    # old requested_at and later done_at (>= req_at): completed flush.
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET cache_flush_requested_at = now() - interval '5 minutes',"
        " cache_flush_done_at = now() - interval '1 minute' WHERE id = :id",
        {"id": worker_id},
    )
    resp = await authed_admin_client.get(f"/admin/workers/{worker_id}")
    assert resp.status_code == 200
    assert "worker-cache-block" in resp.text
    assert "Last cleaned" in resp.text
    assert "Cleaning cache" not in resp.text
    assert "Cache flush timed out" not in resp.text
    block = resp.text.split("worker-cache-block", 1)[1].split("Clear cache", 1)[0]
    assert "disabled" not in block


async def _insert_output_events(
    db_url: str, execution_id: str, events: list[tuple[int, str, str]]
) -> None:
    """Insert events (seq, stream, text) into output_events for an execution."""
    for seq, stream, txt in events:
        await _exec_sql(
            db_url,
            "INSERT INTO output_events (execution_id, seq, ts, stream, text) "
            "VALUES (CAST(:eid AS uuid), :seq, now(), :stream, :txt)",
            {"eid": execution_id, "seq": seq, "stream": stream, "txt": txt},
        )


async def _insert_output_events_bulk(
    db_url: str, execution_id: str, events: list[tuple[int, str, str]]
) -> None:
    """executemany version of _insert_output_events (a single engine/tx) to
    seed thousands of events without opening an engine per row."""
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO output_events (execution_id, seq, ts, stream, text) "
                    "VALUES (CAST(:eid AS uuid), :seq, now(), :stream, :txt)"
                ),
                [
                    {"eid": execution_id, "seq": seq, "stream": stream, "txt": txt}
                    for seq, stream, txt in events
                ],
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_execution_output_shows_events(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The output is rendered from output_events, interleaved by
    stream. The stdout/stderr columns are no longer rendered."""
    worker_id = await _register(authed_admin_client, hostname="out-worker")
    task_id = await _create_task(authed_admin_client, command="echo out")
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO task_executions (task_id, worker_id, status, exit_code, finished_at) "
        "VALUES (:tid, :wid, 'completed', 0, now())",
        {"tid": task_id, "wid": worker_id},
    )
    row = await _row(
        migrated_db_url,
        "SELECT id::text AS id FROM task_executions WHERE task_id=:tid AND worker_id=:wid",
        {"tid": task_id, "wid": worker_id},
    )
    assert row is not None
    await _insert_output_events(
        migrated_db_url,
        row["id"],
        [(1, "stdout", "STDOUT-MARKER"), (2, "stderr", "STDERR-MARKER")],
    )
    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}/executions/{worker_id}/output")
    assert resp.status_code == 200
    body = resp.text
    assert "STDOUT-MARKER" in body
    assert "STDERR-MARKER" in body
    # The stream is distinguished by CSS class.
    assert "out-stderr" in body


@pytest.mark.asyncio
async def test_execution_output_404(authed_admin_client: AsyncClient) -> None:
    task_id = await _create_task(authed_admin_client, command="echo nope")
    bogus = "00000000-0000-0000-0000-000000000000"
    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}/executions/{bogus}/output")
    assert resp.status_code == 404


# --- Endpoint by UUID -------------------------------------------------------
#
# Each attempt has a stable UUID. The UI links here
# from the attempts table so that the output of historical attempts of the
# same worker stays accessible (the legacy endpoint always resolved to the
# last attempt and lost the earlier ones).


@pytest.mark.asyncio
async def test_execution_output_by_uuid_returns_events(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id = await _register(authed_admin_client, hostname="uuid-out-worker")
    task_id = await _create_task(authed_admin_client, command="echo uuid-out")
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO task_executions (task_id, worker_id, status, exit_code, finished_at) "
        "VALUES (:tid, :wid, 'completed', 0, now())",
        {"tid": task_id, "wid": worker_id},
    )
    row = await _row(
        migrated_db_url,
        "SELECT id::text AS id FROM task_executions WHERE task_id = :tid AND worker_id = :wid",
        {"tid": task_id, "wid": worker_id},
    )
    assert row is not None
    exec_id = row["id"]
    await _insert_output_events(
        migrated_db_url, exec_id, [(1, "stdout", "UUID-STDOUT"), (2, "stderr", "UUID-STDERR")]
    )
    resp = await authed_admin_client.get(f"/admin/executions/{exec_id}/output")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "UUID-STDOUT" in body
    assert "UUID-STDERR" in body


@pytest.mark.asyncio
async def test_execution_output_by_uuid_serves_historical_attempts(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """If there are N attempts of the same worker, each UUID must serve ITS output."""
    worker_id = await _register(authed_admin_client, hostname="uuid-hist-worker")
    task_id = await _create_task(authed_admin_client, command="echo hist")
    # Attempt 1 (historical, terminal) and attempt 2 (latest). The active partial unique
    # index allows multiple terminal rows per (task_id, worker_id).
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO task_executions (task_id, worker_id, status, "
        "exit_code, attempt_index, finished_at) "
        "VALUES (:tid, :wid, 'failed', 1, 1, now())",
        {"tid": task_id, "wid": worker_id},
    )
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO task_executions (task_id, worker_id, status, "
        "exit_code, attempt_index, finished_at) "
        "VALUES (:tid, :wid, 'completed', 0, 2, now())",
        {"tid": task_id, "wid": worker_id},
    )
    rows = await _rows(
        migrated_db_url,
        "SELECT id::text AS id, attempt_index FROM task_executions "
        "WHERE task_id = :tid AND worker_id = :wid ORDER BY attempt_index ASC",
        {"tid": task_id, "wid": worker_id},
    )
    assert len(rows) == 2
    old_id = rows[0]["id"]
    new_id = rows[1]["id"]
    await _insert_output_events(migrated_db_url, old_id, [(1, "stdout", "OLD-STDOUT")])
    await _insert_output_events(migrated_db_url, new_id, [(1, "stdout", "NEW-STDOUT")])

    resp_old = await authed_admin_client.get(f"/admin/executions/{old_id}/output")
    assert resp_old.status_code == 200
    assert "OLD-STDOUT" in resp_old.text
    assert "NEW-STDOUT" not in resp_old.text

    resp_new = await authed_admin_client.get(f"/admin/executions/{new_id}/output")
    assert resp_new.status_code == 200
    assert "NEW-STDOUT" in resp_new.text
    assert "OLD-STDOUT" not in resp_new.text


@pytest.mark.asyncio
async def test_execution_output_by_uuid_404_unknown_uuid(
    authed_admin_client: AsyncClient,
) -> None:
    bogus = "00000000-0000-0000-0000-000000000000"
    resp = await authed_admin_client.get(f"/admin/executions/{bogus}/output")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_execution_output_by_uuid_404_malformed_uuid(
    authed_admin_client: AsyncClient,
) -> None:
    # Malformed UUID: the endpoint validates before touching the DB and returns 404
    # (not a 500 from a postgres parse error).
    resp = await authed_admin_client.get("/admin/executions/not-a-uuid/output")
    assert resp.status_code == 404


# --- On-demand download of the full output -----------------------------------
#
# GET /admin/executions/{id}/output/download generates the artifact on the fly
# from output_events (no persisted materialization): it paginates by
# seq cursor and gzips while streaming. httpx decompresses automatically by
# Content-Encoding, so resp.text/resp.content arrive decompressed.


async def _seed_completed_execution(client: AsyncClient, db_url: str, hostname: str) -> str:
    """Register worker + task + completed execution; return the execution UUID."""
    worker_id = await _register(client, hostname=hostname)
    task_id = await _create_task(client, command="echo dl")
    await _exec_sql(
        db_url,
        "INSERT INTO task_executions (task_id, worker_id, status, exit_code, finished_at) "
        "VALUES (:tid, :wid, 'completed', 0, now())",
        {"tid": task_id, "wid": worker_id},
    )
    row = await _row(
        db_url,
        "SELECT id::text AS id FROM task_executions WHERE task_id=:tid AND worker_id=:wid",
        {"tid": task_id, "wid": worker_id},
    )
    assert row is not None
    return row["id"]


@pytest.mark.asyncio
async def test_output_download_text(authed_admin_client: AsyncClient, migrated_db_url: str) -> None:
    """format=text: raw concatenation of the `text` values in seq order, with
    stdout/stderr interleaved, without prefixes or metadata."""
    exec_id = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "dl-text-worker"
    )
    await _insert_output_events(
        migrated_db_url,
        exec_id,
        [(1, "stdout", "one\n"), (2, "stderr", "two\n"), (3, "stdout", "three\n")],
    )
    resp = await authed_admin_client.get(f"/admin/executions/{exec_id}/output/download")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-encoding"] == "gzip"
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["content-disposition"] == f'attachment; filename="output-{exec_id}.log"'
    # The admin plane declares `no-store` across the board, also
    # on the output download (sensitive content per workspace).
    assert resp.headers["cache-control"] == "no-store"
    assert resp.text == "one\ntwo\nthree\n"


@pytest.mark.asyncio
async def test_output_download_jsonl(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """format=jsonl: one JSON line per event with seq/ts/stream/text."""
    exec_id = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "dl-jsonl-worker"
    )
    await _insert_output_events(
        migrated_db_url,
        exec_id,
        [(1, "stdout", "one\n"), (2, "stderr", "two\n")],
    )
    resp = await authed_admin_client.get(
        f"/admin/executions/{exec_id}/output/download?format=jsonl"
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-encoding"] == "gzip"
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    assert resp.headers["content-disposition"] == f'attachment; filename="output-{exec_id}.jsonl"'
    events = [json.loads(line) for line in resp.text.splitlines()]
    assert [(e["seq"], e["stream"], e["text"]) for e in events] == [
        (1, "stdout", "one\n"),
        (2, "stderr", "two\n"),
    ]
    # ts present and ISO-8601 parseable in every event.
    assert all(e["ts"] for e in events)


@pytest.mark.asyncio
async def test_output_download_paginates_without_loss_or_dup(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """More events than one batch: the seq cursor paginates without duplicating, losing
    or reordering events. `n` is derived from `_DOWNLOAD_BATCH_SIZE` so the
    test keeps crossing the batch boundary if the constant changes."""
    from orcaster_server.output_log import _DOWNLOAD_BATCH_SIZE

    exec_id = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "dl-page-worker"
    )
    n = _DOWNLOAD_BATCH_SIZE + 50
    await _insert_output_events_bulk(
        migrated_db_url,
        exec_id,
        [(i, "stdout", f"L{i}\n") for i in range(1, n + 1)],
    )
    resp = await authed_admin_client.get(f"/admin/executions/{exec_id}/output/download")
    assert resp.status_code == 200
    assert resp.text == "".join(f"L{i}\n" for i in range(1, n + 1))


@pytest.mark.asyncio
async def test_output_download_snapshot_excludes_seq_above_max(
    authed_admin_client: AsyncClient,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot contract: the generator pins `MAX(seq)` at the start
    and filters `WHERE seq <= :max_seq`. Events with `seq > max_seq` are
    EXCLUDED from the artifact even if the execution is still alive and the worker is
    still emitting.

    Since all the seeding happens before the GET, the real `MAX(seq)` would cover everything;
    to exercise the filter we pin the snapshot at a known value (3) via
    monkeypatch of `_SQL_OUTPUT_MAX_SEQ`, and we seed events on both sides of
    that cut. Events with `seq <= 3` must appear; those with `seq > 3` (surplus
    emitted "after" the snapshot) must NOT.
    """
    import orcaster_server.output_log as output_log

    exec_id = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "dl-snapshot-worker"
    )
    pinned_max_seq = 3
    # Batch 1 (inside the snapshot, seq <= max_seq) and batch 2 (live surplus,
    # seq > max_seq). We seed both before the GET: only the first must come out.
    await _insert_output_events(
        migrated_db_url,
        exec_id,
        [(1, "stdout", "snap-1\n"), (2, "stdout", "snap-2\n"), (3, "stdout", "snap-3\n")],
    )
    await _insert_output_events(
        migrated_db_url,
        exec_id,
        [(4, "stdout", "LIVE-OVERFLOW-4\n"), (5, "stdout", "LIVE-OVERFLOW-5\n")],
    )
    # Pins the snapshot at `pinned_max_seq` regardless of the existing rows
    # (the generator reads this MAX(seq) at start).
    monkeypatch.setattr(
        output_log,
        "_SQL_OUTPUT_MAX_SEQ",
        text(f"SELECT {pinned_max_seq}"),
    )

    resp = await authed_admin_client.get(f"/admin/executions/{exec_id}/output/download")
    assert resp.status_code == 200, resp.text
    # Only the snapshot events (seq <= 3); the surplus (seq > 3) is excluded.
    assert resp.text == "snap-1\nsnap-2\nsnap-3\n"
    assert "LIVE-OVERFLOW-4" not in resp.text
    assert "LIVE-OVERFLOW-5" not in resp.text


@pytest.mark.asyncio
async def test_output_download_invalid_format_422(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    exec_id = await _seed_completed_execution(authed_admin_client, migrated_db_url, "dl-422-worker")
    resp = await authed_admin_client.get(f"/admin/executions/{exec_id}/output/download?format=xml")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_output_download_unknown_execution_404(
    authed_admin_client: AsyncClient,
) -> None:
    bogus = "00000000-0000-0000-0000-000000000000"
    resp = await authed_admin_client.get(f"/admin/executions/{bogus}/output/download")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_output_download_malformed_uuid_404(
    authed_admin_client: AsyncClient,
) -> None:
    # Malformed UUID: validated before touching the DB (same pattern as the viewer).
    resp = await authed_admin_client.get("/admin/executions/not-a-uuid/output/download")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_output_download_requires_session(app_client: AsyncClient) -> None:
    # No session → 401 (app_client sends HX-Request: true; the host handler
    # only redirects 303 on non-HTMX HTML navigation).
    bogus = "00000000-0000-0000-0000-000000000000"
    resp = await app_client.get(f"/admin/executions/{bogus}/output/download")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_output_download_no_events_empty_file(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Execution without events → 200 with an empty file (not an error)."""
    exec_id = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "dl-empty-worker"
    )
    resp = await authed_admin_client.get(f"/admin/executions/{exec_id}/output/download")
    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "gzip"
    assert resp.content == b""


@pytest.mark.asyncio
async def test_output_download_urn_uuid_404(authed_admin_client: AsyncClient) -> None:
    """`urn:uuid:<hex>` passes Python's `UUID()` but NOT the Postgres CAST:
    without normalizing the path param, the endpoint returned 500 (DataError)."""
    bogus = "urn:uuid:00000000-0000-0000-0000-000000000000"
    resp = await authed_admin_client.get(f"/admin/executions/{bogus}/output/download")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_execution_output_by_uuid_urn_uuid_404(
    authed_admin_client: AsyncClient,
) -> None:
    """Same `urn:uuid:` vector against the viewer by UUID → 404, not 500."""
    bogus = "urn:uuid:00000000-0000-0000-0000-000000000000"
    resp = await authed_admin_client.get(f"/admin/executions/{bogus}/output")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_output_download_cross_workspace_404(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """A Default admin cannot download the output of an execution of another
    workspace → 404, without leaking existence (workspace isolation)."""
    import uuid as uuid_mod

    from tests.test_admin_enforcement import (
        _create_workspace,
        _make_authed_client_for_workspace,
    )

    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid_mod.uuid4().hex[:8]}")
    client_b = await _make_authed_client_for_workspace(ws_b)
    try:
        # Worker + task + execution with events, all in workspace B.
        token = await mint_token_via_app(workspace_id=ws_b)
        reg = await client_b.post(
            "/v1/workers/register",
            json={**REGISTER, "hostname": "dl-ws-b-worker", "enrollment_token": token},
        )
        assert reg.status_code == 200, reg.text
        worker_id = reg.json()["worker_id"]
        task_id = await _create_task(client_b, command="echo dl-b")
        await _exec_sql(
            migrated_db_url,
            "INSERT INTO task_executions (task_id, worker_id, status, exit_code, finished_at) "
            "VALUES (:tid, :wid, 'completed', 0, now())",
            {"tid": task_id, "wid": worker_id},
        )
        row = await _row(
            migrated_db_url,
            "SELECT id::text AS id FROM task_executions WHERE task_id=:tid AND worker_id=:wid",
            {"tid": task_id, "wid": worker_id},
        )
        assert row is not None
        exec_id = row["id"]
        await _insert_output_events(migrated_db_url, exec_id, [(1, "stdout", "secret\n")])
    finally:
        await client_b.aclose()

    # B can download it (seed sanity)…
    client_b2 = await _make_authed_client_for_workspace(ws_b)
    try:
        ok = await client_b2.get(f"/admin/executions/{exec_id}/output/download")
        assert ok.status_code == 200
    finally:
        await client_b2.aclose()

    # …but A (Default) gets 404.
    resp = await authed_admin_client.get(f"/admin/executions/{exec_id}/output/download")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_execution_output_page_has_download_links(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The output view offers the 'Download log' button and the 'JSONL' link
    (always visible, also with the execution alive). UI in English."""
    exec_id = await _seed_completed_execution(
        authed_admin_client, migrated_db_url, "dl-links-worker"
    )
    resp = await authed_admin_client.get(f"/admin/executions/{exec_id}/output")
    assert resp.status_code == 200
    assert "Download log" in resp.text
    assert f"/admin/executions/{exec_id}/output/download?format=jsonl" in resp.text


def test_aggregates_outcome_only_in_terminal_states() -> None:
    """The `outcomes` breakdown of the task detail only counts terminal
    executions — a residual outcome on an active execution (inconsistent
    data) must not inflate the badge (display-only)."""
    from orcaster_server.api.admin_ui import _aggregates

    executions = [
        {"status": "completed", "outcome": "no_result"},
        {"status": "running", "outcome": "no_result"},
    ]
    agg = _aggregates(executions, 2)
    assert agg["done"] == 1
    assert agg["running"] == 1
    assert agg["outcomes"] == {"no_result": 1}


@pytest.mark.asyncio
async def test_task_detail_aggregates(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    # Create 4 workers and 4 executions with different states.
    w1 = await _register(authed_admin_client, hostname="agg-w1")
    w2 = await _register(authed_admin_client, hostname="agg-w2")
    w3 = await _register(authed_admin_client, hostname="agg-w3")
    w4 = await _register(authed_admin_client, hostname="agg-w4")
    task_id = await _create_task(authed_admin_client, command="echo agg")
    for wid, st in (
        (w1, "completed"),
        (w2, "failed"),
        (w3, "running"),
        (w4, "cancelled"),
    ):
        await _exec_sql(
            migrated_db_url,
            "INSERT INTO task_executions (task_id, worker_id, status) "
            "VALUES (:tid, :wid, CAST(:st AS execution_status))",
            {"tid": task_id, "wid": wid, "st": st},
        )
    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    assert resp.status_code == 200
    body = resp.text
    # Each aggregate must appear with a count of 1.
    # I count occurrences of the pattern "<strong>1</strong>" — there must be at least 4
    # (done, failed, running, cancelled).
    assert body.count("<strong>1</strong>") >= 4
    assert "done" in body
    assert "failed" in body
    assert "running" in body
    assert "cancelled" in body


# === Delete worker button in partial + detail ==============================


@pytest.mark.asyncio
async def test_workers_partial_renders_delete_button_force_when_online(
    authed_admin_client: AsyncClient,
) -> None:
    worker_id = await _register(authed_admin_client, hostname="online-host")
    resp = await authed_admin_client.get("/admin/workers")
    assert resp.status_code == 200
    body = resp.text
    # A freshly registered worker is online → button with force=true.
    assert f'hx-delete="/admin/workers/{worker_id}?force=true"' in body
    assert "Delete (force)" in body
    # The warning now lives in the dialog's data-confirm-message (not in native
    # hx-confirm). The system dialog replaces window.confirm() — see
    # initConfirmDialog in app.js.
    assert 'data-confirm-title="Force-delete' in body
    assert "online-host" in body


@pytest.mark.asyncio
async def test_workers_partial_renders_delete_button_no_force_when_offline(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id = await _register(authed_admin_client, hostname="offline-host")
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET status = 'offline' WHERE id = :id",
        {"id": worker_id},
    )
    resp = await authed_admin_client.get("/admin/workers")
    body = resp.text
    # Offline → plain DELETE without force.
    assert f'hx-delete="/admin/workers/{worker_id}"' in body
    assert "?force=true" not in body.split(worker_id, 1)[1].split("</tr>", 1)[0]
    # Label "Delete" without "(force)".
    assert "Delete (force)" not in body
    assert "Delete" in body


@pytest.mark.asyncio
async def test_worker_detail_renders_delete_button_with_redirect(
    authed_admin_client: AsyncClient,
) -> None:
    worker_id = await _register(authed_admin_client, hostname="detail-host")
    resp = await authed_admin_client.get(f"/admin/workers/{worker_id}")
    body = resp.text
    assert f'hx-delete="/admin/workers/{worker_id}?force=true"' in body
    # Post-action redirect to /admin/.
    assert 'data-after-request="dashboard"' in body


# === Tri-state pause/resume control in the worker detail ===================


@pytest.mark.asyncio
async def test_worker_detail_online_offers_pause(authed_admin_client: AsyncClient) -> None:
    """An online worker offers an active Pause, with a custom dialog and a reason textarea."""
    worker_id = await _register(authed_admin_client, hostname="pausable-host")
    resp = await authed_admin_client.get(f"/admin/workers/{worker_id}")
    body = resp.text
    assert f'hx-post="/admin/workers/{worker_id}/pause"' in body
    assert f'hx-post="/admin/workers/{worker_id}/resume"' not in body
    # Custom dialog (not native confirm()): it is the only one with a textarea.
    assert 'data-confirm-title="Pause worker pausable-host?"' in body
    assert 'data-confirm-reason="Reason (optional)"' in body
    # Warn tone, not danger: the action is reversible.
    assert 'data-confirm-tone="warn"' in body
    # The message explains the soft drain, which is the non-obvious part.
    assert "keep running until they finish" in body
    # The button is resynchronized after a successful pause via its own reload, not
    # tied to the 3s poller (it would lose the action if the reason dialog
    # is open during a poll).
    assert 'data-after-request="reload"' in body


@pytest.mark.asyncio
async def test_worker_detail_paused_offers_resume(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id = await _register(authed_admin_client, hostname="paused-host")
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET status = 'paused' WHERE id = :id",
        {"id": worker_id},
    )
    resp = await authed_admin_client.get(f"/admin/workers/{worker_id}")
    body = resp.text
    assert f'hx-post="/admin/workers/{worker_id}/resume"' in body
    assert f'hx-post="/admin/workers/{worker_id}/pause"' not in body
    assert 'data-confirm-title="Resume worker paused-host?"' in body
    # Same resynchronization mechanism as Pause: own reload after success.
    assert 'data-after-request="reload"' in body


@pytest.mark.asyncio
async def test_worker_detail_offline_pause_disabled(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Offline offers neither Pause nor Resume: the POST would give 409. A disabled
    button remains with the reason in an accessible info-tip, not in the
    title: a <button disabled> never receives focus, so a title alone would be
    reachable only by mouse hover."""
    worker_id = await _register(authed_admin_client, hostname="offline-pause-host")
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET status = 'offline' WHERE id = :id",
        {"id": worker_id},
    )
    resp = await authed_admin_client.get(f"/admin/workers/{worker_id}")
    body = resp.text
    assert f'hx-post="/admin/workers/{worker_id}/pause"' not in body
    assert f'hx-post="/admin/workers/{worker_id}/resume"' not in body
    block = body.split('class="worker-detail-actions"', 1)[1].split("Delete worker", 1)[0]
    assert "disabled" in block
    assert "Only an online worker can be paused" in block
    # The reason lives in a keyboard-reachable role="tooltip" (focusable icon
    # with aria-describedby), not only in an unreachable title.
    assert 'role="tooltip" id="worker-pause-tip-bubble"' in block
    assert 'aria-describedby="worker-pause-tip-bubble"' in block


@pytest.mark.asyncio
async def test_delete_worker_via_dashboard_removes_row(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """E2E of the dashboard → delete flow: the partial shows the row, DELETE
    returns 204, the next listing does not show the worker."""
    worker_id = await _register(authed_admin_client, hostname="doomed-host")
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET status = 'offline' WHERE id = :id",
        {"id": worker_id},
    )

    before = await authed_admin_client.get("/admin/workers")
    assert worker_id in before.text

    delete = await authed_admin_client.delete(f"/admin/workers/{worker_id}")
    assert delete.status_code == 204

    after = await authed_admin_client.get("/admin/workers")
    assert worker_id not in after.text


# === Pause/resume and stale suffix in the workers deck =====================


@pytest.mark.asyncio
async def test_workers_deck_kebab_offers_pause_when_online(
    authed_admin_client: AsyncClient,
) -> None:
    worker_id = await _register(authed_admin_client, hostname="deck-online-host")
    resp = await authed_admin_client.get("/admin/workers")
    card = _worker_card_block(resp.text, worker_id)
    assert f'hx-post="/admin/workers/{worker_id}/pause"' in card
    assert f'hx-post="/admin/workers/{worker_id}/resume"' not in card
    # Scoped to the card of the worker under test, not to the whole body: otherwise
    # they would match against the tone/reason/trigger of another card of the deck.
    assert 'data-confirm-reason="Reason (optional)"' in card
    assert 'data-confirm-tone="warn"' in card
    # hx-swap="none" + immediate refresh: the endpoint returns JSON, so
    # the Delete swap (outerHTML on .worker-card) would paint the raw JSON.
    assert 'data-after-request="refresh-workers"' in card


@pytest.mark.asyncio
async def test_workers_deck_kebab_offers_resume_when_paused(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id = await _register(authed_admin_client, hostname="deck-paused-host")
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET status = 'paused' WHERE id = :id",
        {"id": worker_id},
    )
    resp = await authed_admin_client.get("/admin/workers")
    body = resp.text
    assert f'hx-post="/admin/workers/{worker_id}/resume"' in body
    assert f'hx-post="/admin/workers/{worker_id}/pause"' not in body


@pytest.mark.asyncio
async def test_workers_deck_kebab_omits_pause_when_offline(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Offline offers neither Pause nor Resume in the kebab: the POST would give 409.

    An online sibling is registered in the SAME render: without it, this test
    would pass even if the Pause item did not exist (it would only verify
    absence, not discriminate by state). With the sibling, we assert by id
    that the online one DOES offer it and the offline one does NOT."""
    online_id = await _register(authed_admin_client, hostname="deck-online-sibling")
    offline_id = await _register(authed_admin_client, hostname="deck-offline-host")
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET status = 'offline' WHERE id = :id",
        {"id": offline_id},
    )
    resp = await authed_admin_client.get("/admin/workers")
    body = resp.text
    online_card = _worker_card_block(body, online_id)
    offline_card = _worker_card_block(body, offline_id)
    assert f'hx-post="/admin/workers/{online_id}/pause"' in online_card
    assert f'hx-post="/admin/workers/{offline_id}/pause"' not in offline_card
    assert f'hx-post="/admin/workers/{offline_id}/resume"' not in offline_card
    # The Delete is still there for the offline one.
    assert f'hx-delete="/admin/workers/{offline_id}"' in offline_card


@pytest.mark.asyncio
async def test_workers_deck_pause_item_precedes_delete(
    authed_admin_client: AsyncClient,
) -> None:
    """Pause goes above Delete, to separate it from the destructive zone."""
    worker_id = await _register(authed_admin_client, hostname="deck-order-host")
    body = (await authed_admin_client.get("/admin/workers")).text
    assert body.index(f'hx-post="/admin/workers/{worker_id}/pause"') < body.index(
        f'hx-delete="/admin/workers/{worker_id}?force=true"'
    )


@pytest.mark.asyncio
async def test_workers_deck_badge_marks_paused_stale(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """A paused and powered-off worker stays `paused` with an old heartbeat
    indefinitely. Without the suffix, the operator reads the card as capacity
    available to resume when the machine is off."""
    worker_id = await _register(authed_admin_client, hostname="stale-paused-host")
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET status = 'paused', "
        "last_heartbeat_at = now() - interval '30 minutes' WHERE id = :id",
        {"id": worker_id},
    )
    body = (await authed_admin_client.get("/admin/workers")).text
    assert "paused (stale)" in body


@pytest.mark.asyncio
async def test_workers_deck_badge_paused_fresh_without_stale(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """A paused worker with a fresh heartbeat carries no suffix, even though ANOTHER
    paused and stale worker does carry it in the SAME render: without the stale
    sibling, this test would pass even if the suffix did not exist at
    all (it would only verify its global absence). With the sibling,
    we assert by id that the suffix appears for one and not for the other."""
    stale_id = await _register(authed_admin_client, hostname="stale-paused-sibling")
    fresh_id = await _register(authed_admin_client, hostname="fresh-paused-host")
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET status = 'paused', "
        "last_heartbeat_at = now() - interval '30 minutes' WHERE id = :id",
        {"id": stale_id},
    )
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET status = 'paused', last_heartbeat_at = now() WHERE id = :id",
        {"id": fresh_id},
    )
    body = (await authed_admin_client.get("/admin/workers")).text
    stale_card = _worker_card_block(body, stale_id)
    fresh_card = _worker_card_block(body, fresh_id)
    assert "paused (stale)" in stale_card
    assert "paused (stale)" not in fresh_card
    # It appears exactly once in the whole render: only for the stale one.
    assert body.count("paused (stale)") == 1
    # The normal badge of the fresh one still appears.
    assert 'class="badge badge-paused"' in fresh_card


def test_workers_deck_template_paused_without_heartbeat_does_not_crash() -> None:
    """Guard `heartbeat_age_seconds is not none` in `_workers_deck.html`: if
    it were dropped, a `paused` worker with `heartbeat_age_seconds` `None` would break
    the comparison `None > heartbeat_timeout_seconds` with `TypeError` during
    render. The failure mode is not an ugly card: it is `/admin/workers`
    returning 500 and the ENTIRE deck pane blank, because the exception
    happens in a template that serves all the workers of the render, not only
    the one that triggers the condition.

    This combination cannot be forced via DB + `GET /admin/workers`:
    `workers.last_heartbeat_at` is `NOT NULL DEFAULT now()` since
    migration 0001_init (`orcaster_server/models.py:174-178`), so no real row
    ever arrives with that field null, and `_heartbeat_age_seconds` only returns
    `None` for that case. Forcing it would require an `ALTER TABLE ... DROP NOT
    NULL` against the test DB schema, which is session-wide
    (`migrated_db_url`) and shared among all the tests in the file: a
    schema change there would leak into the rest of the suite. The template is rendered
    directly (same pattern as `test_ts_macro.py`), without going through the
    DB, to exercise exactly the combination the guard protects."""
    tmpl = templates.env.get_template("_workers_deck.html")
    worker = {
        "id": "11111111-1111-1111-1111-111111111111",
        "hostname": "never-heartbeat-host",
        "name": None,
        "os": "linux",
        "arch": "x86_64",
        "status": "paused",
        "ram_mb": None,
        "ram_used_mb": None,
        "disk_mb": None,
        "disk_used_mb": None,
        "cpu_pct": None,
        "active_count": 0,
        "active_task_id": None,
        "last_heartbeat_at": None,
        "heartbeat_age_seconds": None,
    }
    html = tmpl.render(workers=[worker], common_platform=None, heartbeat_timeout_seconds=120)
    assert "paused (stale)" not in html
    assert 'class="badge badge-paused"' in html


@pytest.mark.asyncio
async def test_dashboard_workers_pane_accepts_refresh_trigger(
    authed_admin_client: AsyncClient,
) -> None:
    """Without `refresh` in the pane hx-trigger, the kebab htmx.trigger does
    nothing and the deck takes up to 3s to reflect the change."""
    resp = await authed_admin_client.get("/admin/", headers={"HX-Request": "false"})
    assert resp.status_code == 200
    assert 'hx-trigger="load, every 3s, refresh"' in resp.text


# === Clickable row (the col-actions column was removed) ====================


@pytest.mark.asyncio
async def test_tasks_partial_row_is_clickable_no_inline_actions(
    authed_admin_client: AsyncClient,
) -> None:
    """The whole row navigates to the detail; there is no actions cell.

    The col-actions column was removed: the inline Cancel duplicated the bulk-bar and the
    detail, and in mostly terminal fleets it left an empty column. The
    per-row affordance is the row itself (tr.task-row with data-href that
    initRowNav in app.js consumes); the .task-id-link remains the keyboard-accessible
    entry point.
    """
    task_id = await _create_task(authed_admin_client, command="long-running")
    resp = await authed_admin_client.get("/admin/tasks")
    body = resp.text
    row_marker = f'id="task-row-{task_id}"'
    assert row_marker in body
    row_start = body.index(row_marker)
    row_end = body.index("</tr>", row_start)
    row_html = body[row_start:row_end]
    # The row carries the navigation affordance…
    assert 'class="task-row"' in row_html
    assert f'data-href="/admin/tasks/{task_id}"' in row_html
    # …and the keyboard-accessible link is still present.
    assert f'href="/admin/tasks/{task_id}"' in row_html
    # No inline actions: neither Cancel (lives in the bulk-bar and detail) nor kebab.
    assert "col-actions" not in body
    assert "btn-row-action" not in row_html
    assert f"/admin/tasks/{task_id}/cancel" not in body
    assert "row-actions" not in row_html


@pytest.mark.asyncio
async def test_tasks_partial_no_view_details_duplicate(
    authed_admin_client: AsyncClient,
) -> None:
    """The table does not show 'View details' (the mono #id link is the entry to the detail)."""
    await _create_task(authed_admin_client)
    resp = await authed_admin_client.get("/admin/tasks")
    assert "View details" not in resp.text


@pytest.mark.asyncio
async def test_task_detail_renders_cancel_button_with_reload(
    authed_admin_client: AsyncClient,
) -> None:
    task_id = await _create_task(authed_admin_client)
    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    body = resp.text
    assert f'hx-post="/admin/tasks/{task_id}/cancel"' in body
    assert 'data-after-request="reload"' in body


@pytest.mark.asyncio
async def test_cancel_via_hx_prompt_header_persists_reason(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """HTMX sends the `hx-prompt` reason in the `HX-Prompt` header.
    The endpoint must read it as a fallback when there is no body."""
    task_id = await _create_task(authed_admin_client, command="cancellable")

    resp = await authed_admin_client.post(
        f"/admin/tasks/{task_id}/cancel",
        headers={"HX-Prompt": "operator decided to stop"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "cancelled"}

    trow = await _row(
        migrated_db_url,
        "SELECT status, cancelled_reason FROM tasks WHERE id = :id",
        {"id": task_id},
    )
    assert trow is not None
    assert trow["status"] == "cancelled"
    assert trow["cancelled_reason"] == "operator decided to stop"

    # The audit log also receives the reason from the header.
    arows = await _rows(
        migrated_db_url,
        "SELECT reason FROM admin_actions WHERE target_id = :tid",
        {"tid": str(task_id)},
    )
    assert len(arows) == 1
    assert arows[0]["reason"] == "operator decided to stop"


@pytest.mark.asyncio
async def test_cancel_via_hx_prompt_empty_keeps_reason_null(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """If the user accepted the prompt() without typing, HX-Prompt arrives as
    an empty string. It must be treated as no reason."""
    task_id = await _create_task(authed_admin_client)

    resp = await authed_admin_client.post(
        f"/admin/tasks/{task_id}/cancel",
        headers={"HX-Prompt": ""},
    )
    assert resp.status_code == 200

    trow = await _row(
        migrated_db_url,
        "SELECT cancelled_reason FROM tasks WHERE id = :id",
        {"id": task_id},
    )
    assert trow is not None
    assert trow["cancelled_reason"] is None


@pytest.mark.asyncio
async def test_cancel_body_reason_takes_precedence_over_header(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Compat with API clients: if the body carries `reason`, it wins over HX-Prompt."""
    task_id = await _create_task(authed_admin_client)

    resp = await authed_admin_client.post(
        f"/admin/tasks/{task_id}/cancel",
        json={"reason": "from body"},
        headers={"HX-Prompt": "from header"},
    )
    assert resp.status_code == 200

    trow = await _row(
        migrated_db_url,
        "SELECT cancelled_reason FROM tasks WHERE id = :id",
        {"id": task_id},
    )
    assert trow is not None
    assert trow["cancelled_reason"] == "from body"


@pytest.mark.asyncio
async def test_cancel_via_hx_prompt_header_percent_decoded(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Same contract as worker pause/resume: the dialog sends `HX-Prompt`
    percent-encoded (setRequestHeader throws on characters outside
    Latin-1) and the server decodes it. Plain text without `%` passes intact."""
    task_id = await _create_task(authed_admin_client, command="cancellable")

    resp = await authed_admin_client.post(
        f"/admin/tasks/{task_id}/cancel",
        headers={"HX-Prompt": "resultados%20err%C3%A1ticos%20%E2%80%94%20revisar"},
    )
    assert resp.status_code == 200

    trow = await _row(
        migrated_db_url,
        "SELECT cancelled_reason FROM tasks WHERE id = :id",
        {"id": task_id},
    )
    assert trow is not None
    assert trow["cancelled_reason"] == "resultados erráticos — revisar"


# === Row actions by state ==================================================


@pytest.mark.asyncio
async def test_tasks_partial_has_no_inline_row_actions(
    authed_admin_client: AsyncClient,
) -> None:
    """The list carries no inline actions in any state: Cancel/Delete
    live in the bulk-bar and in the detail; the row navigates to the detail."""
    task_id = await _create_task(authed_admin_client, command="still-queued")
    resp = await authed_admin_client.get("/admin/tasks")
    body = resp.text
    assert f'hx-post="/admin/tasks/{task_id}/cancel"' not in body
    assert f'hx-delete="/admin/tasks/{task_id}"' not in body


@pytest.mark.asyncio
async def test_task_detail_renders_delete_button_when_terminal(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    task_id = await _create_task(authed_admin_client)
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET status = CAST(:s AS task_status) WHERE id = :id",
        {"s": "failed", "id": task_id},
    )
    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    body = resp.text
    assert f'hx-delete="/admin/tasks/{task_id}"' in body
    assert 'data-after-request="dashboard"' in body
    # The cancel button must not appear in detail-terminal.
    assert "/cancel" not in body


@pytest.mark.asyncio
async def test_delete_task_via_dashboard_removes_row(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """E2E: terminal → DELETE 204 → next GET does not show the task."""
    task_id = await _create_task(authed_admin_client)
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET status = CAST(:s AS task_status) WHERE id = :id",
        {"s": "completed", "id": task_id},
    )

    before = await authed_admin_client.get("/admin/tasks")
    assert f"#{task_id}" in before.text

    delete = await authed_admin_client.delete(f"/admin/tasks/{task_id}")
    assert delete.status_code == 204

    after = await authed_admin_client.get("/admin/tasks")
    # The task must no longer appear in the listing.
    assert f">#{task_id}<" not in after.text


# === Audit log view ========================================================


@pytest.mark.asyncio
async def test_admin_actions_page_empty(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.get("/admin/actions")
    assert resp.status_code == 200
    body = resp.text
    assert "<html" in body.lower()
    assert "Admin actions audit log" in body
    assert "No admin actions recorded yet." in body
    # Filter form present.
    assert 'name="action"' in body
    assert 'name="target_kind"' in body
    assert 'hx-get="/admin/actions/partial"' in body


@pytest.mark.asyncio
async def test_admin_actions_partial_no_html_wrapper(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.get("/admin/actions/partial")
    assert resp.status_code == 200
    body = resp.text
    assert "<html" not in body.lower()
    assert "<table" in body


@pytest.mark.asyncio
async def test_admin_actions_page_shows_recorded_actions(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    # Generate the 3 distinct actions.
    worker_id = await _register(authed_admin_client, hostname="audit-host")
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET status = 'offline' WHERE id = :id",
        {"id": worker_id},
    )
    del_resp = await authed_admin_client.delete(f"/admin/workers/{worker_id}")
    assert del_resp.status_code == 204

    t_cancel = await _create_task(authed_admin_client, command="will-be-cancelled")
    cancel_resp = await authed_admin_client.post(
        f"/admin/tasks/{t_cancel}/cancel",
        json={"reason": "audit smoke"},
    )
    assert cancel_resp.status_code == 200

    t_delete = await _create_task(authed_admin_client, command="will-be-deleted")
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET status = CAST(:s AS task_status) WHERE id = :id",
        {"s": "completed", "id": t_delete},
    )
    del_t = await authed_admin_client.delete(f"/admin/tasks/{t_delete}")
    assert del_t.status_code == 204

    resp = await authed_admin_client.get("/admin/actions")
    body = resp.text
    # Each action produces a badge with its name.
    assert "delete_worker" in body
    assert "cancel_task" in body
    assert "delete_task" in body
    # Reason persists and is rendered.
    assert "audit smoke" in body
    # Worker prefix visible.
    assert worker_id[:8] in body
    # Task links present.
    assert f'href="/admin/tasks/{t_cancel}"' in body
    assert f'href="/admin/tasks/{t_delete}"' in body


@pytest.mark.asyncio
async def test_admin_actions_filter_by_action(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    # One cancel + one completed delete.
    t1 = await _create_task(authed_admin_client, command="t1")
    await authed_admin_client.post(f"/admin/tasks/{t1}/cancel", json={"reason": "r1"})

    t2 = await _create_task(authed_admin_client, command="t2")
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET status = CAST(:s AS task_status) WHERE id = :id",
        {"s": "completed", "id": t2},
    )
    await authed_admin_client.delete(f"/admin/tasks/{t2}")

    resp = await authed_admin_client.get("/admin/actions/partial?action=cancel_task")
    body = resp.text
    assert "cancel_task" in body
    assert "delete_task" not in body
    # Only the t1 row appears.
    assert f"task #{t1}" in body
    assert f"task #{t2}" not in body


@pytest.mark.asyncio
async def test_admin_actions_filter_by_target_kind(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    # One delete_worker + one cancel_task.
    worker_id = await _register(authed_admin_client, hostname="filter-host")
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET status = 'offline' WHERE id = :id",
        {"id": worker_id},
    )
    await authed_admin_client.delete(f"/admin/workers/{worker_id}")

    t = await _create_task(authed_admin_client, command="cancel-me")
    await authed_admin_client.post(f"/admin/tasks/{t}/cancel", json={})

    resp = await authed_admin_client.get("/admin/actions/partial?target_kind=worker")
    body = resp.text
    assert "delete_worker" in body
    assert "cancel_task" not in body
    assert worker_id[:8] in body


@pytest.mark.asyncio
async def test_admin_actions_rejects_invalid_filter_values(
    authed_admin_client: AsyncClient,
) -> None:
    """An unknown value is treated as no filter (not 400) → keeps
    the UI usable if someone types a URL with garbage."""
    t = await _create_task(authed_admin_client)
    await authed_admin_client.post(f"/admin/tasks/{t}/cancel", json={})

    resp = await authed_admin_client.get("/admin/actions/partial?action=bogus_value")
    assert resp.status_code == 200
    # The filter is ignored → the cancel_task still appears.
    assert "cancel_task" in resp.text


@pytest.mark.asyncio
async def test_admin_actions_limit_clamped(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.get("/admin/actions/partial?limit=99999")
    assert resp.status_code == 200
    # No error: the SQL receives min(limit, 500). We do not verify the exact count
    # because we do not insert enough rows; it is enough that it does not break.


@pytest.mark.asyncio
async def test_dashboard_links_to_admin_actions(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.get("/admin/")
    assert "/admin/actions" in resp.text


# === Bulk UI: checkboxes + action bar ======================


@pytest.mark.asyncio
async def test_tasks_partial_renders_bulk_checkboxes(
    authed_admin_client: AsyncClient,
) -> None:
    task_id = await _create_task(authed_admin_client, command="bulk-cbx-target")
    resp = await authed_admin_client.get("/admin/tasks")
    body = resp.text
    # Checkbox per row with the task id.
    assert f'class="bulk-task-cbx" value="{task_id}"' in body
    # "Select all" checkbox in the header.
    assert 'class="bulk-select-all"' in body


@pytest.mark.asyncio
async def test_dashboard_renders_bulk_action_bar(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.get("/admin/")
    body = resp.text
    assert 'id="bulk-bar"' in body
    assert 'id="bulk-retry-btn"' in body
    assert 'id="bulk-cancel-btn"' in body
    assert 'id="bulk-delete-btn"' in body
    # The wiring of the bulk buttons is done in app.js (addEventListener), not
    # inline in the template — it is enough to verify the script is loaded.
    assert "/static/app.js" in body
    # Refresh trigger of the panel to clean up after a bulk.
    assert 'id="tasks-panel"' in body
    # Automatic polling is paused during interaction: interval
    # without an eval filter or `every`: the 5s tick lives in app.js (initTasksPolling),
    # which fires `refresh` only when the panel is idle.
    assert 'hx-trigger="load, refresh"' in body
    assert "dashTasksIdle(" not in body


@pytest.mark.asyncio
async def test_tasks_table_renders_runs_progress_bar(authed_admin_client: AsyncClient) -> None:
    """The Workers column carries the fraction + micro progress-bar when
    target_count > 1. When target_count == 1 (the modal default) the
    column becomes a single `pending`/`done` state and the bar is suppressed —
    that is why we force a high target here."""
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={
            "target": "3",
            "payload": {
                "commands": ["runs-progress-smoke"],
                "requirements": [],
            },
        },
    )
    assert resp.status_code == 200, resp.text
    resp = await authed_admin_client.get("/admin/tasks")
    body = resp.text
    assert 'class="runs-progress"' in body
    # At rest (done=0, target>0) the tier is 'low'.
    assert "runs-progress-fill--low" in body
    # The bar carries the value in data-pct (app.js passes it to --pct via CSSOM).
    assert "data-pct=" in body
    assert 'style="--pct' not in body


@pytest.mark.asyncio
async def test_admin_actions_filter_accepts_bulk_values(
    authed_admin_client: AsyncClient,
) -> None:
    """The audit view select now accepts filtering by the two bulk actions."""
    a = await _create_task(authed_admin_client, command="bulk-audit-a")
    b = await _create_task(authed_admin_client, command="bulk-audit-b")
    await authed_admin_client.post(
        "/admin/tasks/bulk/cancel",
        json={"task_ids": [a, b], "reason": "bulk-audit"},
    )
    resp = await authed_admin_client.get("/admin/actions/partial?action=bulk_cancel_task")
    body = resp.text
    assert resp.status_code == 200
    assert "bulk_cancel_task" in body
    # The singular cancel_task must NOT appear (strict filter).
    # The two bulk actions must reference the two tasks.
    assert str(a) in body and str(b) in body


# --- Dynamic sort + counts in the /admin/tasks partial ----------------------
#
# Covers the backend contract of the partial endpoint:
# - whitelisted sort -> 200
# - sort outside the whitelist -> 400
# - invalid dir -> silent fallback (200, without breaking the polling)
# - counts present in the template context
#
# Note: the `_tasks_table.html` template does not render sort/dir/counts
# (the UI layer). To validate the context without touching the HTML,
# we use a temporary patch on `templates.TemplateResponse` that captures the
# context dictionary passed to the render.


@pytest.mark.asyncio
async def test_tasks_partial_default_sort_returns_200(authed_admin_client: AsyncClient) -> None:
    """Without explicit params, the partial keeps working (defaults
    created_at/desc). Verifies that the extended signature does not break the current
    dashboard polling."""
    await _create_task(authed_admin_client, command="sort-default")
    resp = await authed_admin_client.get("/admin/tasks", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "<table" in resp.text


@pytest.mark.asyncio
async def test_tasks_partial_valid_sort_columns(authed_admin_client: AsyncClient) -> None:
    """Each whitelisted column returns 200 with any valid dir."""
    await _create_task(authed_admin_client, command="sort-valid")
    for col in ("created_at", "status", "done", "target_count"):
        for direction in ("asc", "desc"):
            resp = await authed_admin_client.get(
                f"/admin/tasks?sort={col}&dir={direction}",
                headers={"HX-Request": "true"},
            )
            assert resp.status_code == 200, (col, direction, resp.text)


@pytest.mark.asyncio
async def test_tasks_partial_invalid_sort_returns_400(
    authed_admin_client: AsyncClient,
) -> None:
    """A sort outside the whitelist must be rejected explicitly to prevent
    a client from trying to inject SQL via the query param."""
    resp = await authed_admin_client.get(
        "/admin/tasks?sort=t.id;DROP",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 400
    assert "sort" in resp.text.lower()


@pytest.mark.asyncio
async def test_tasks_partial_invalid_dir_silent_fallback(
    authed_admin_client: AsyncClient,
) -> None:
    """An invalid dir = noise (old link / typo). We must not break the
    3s polling; we only fall back to 'desc'."""
    await _create_task(authed_admin_client, command="dir-fallback")
    resp = await authed_admin_client.get(
        "/admin/tasks?sort=created_at&dir=sideways",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_tasks_partial_context_includes_counts_and_sort(
    authed_admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The template receives `total_count`, `filtered_count`, `sort`, `dir`.
    The template does not use them yet, so we intercept the
    context instead of asserting on the rendered HTML."""
    from orcaster_server.api import admin_ui

    await _create_task(authed_admin_client, command="ctx-a")
    await _create_task(authed_admin_client, command="ctx-b")

    captured: dict[str, Any] = {}
    original = admin_ui.templates.TemplateResponse

    def _spy(request, name, context, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "_tasks_table.html":
            captured.update(context)
        return original(request, name, context, *args, **kwargs)

    monkeypatch.setattr(admin_ui.templates, "TemplateResponse", _spy)

    resp = await authed_admin_client.get(
        "/admin/tasks?sort=status&dir=asc",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert captured["sort"] == "status"
    assert captured["dir"] == "asc"
    assert captured["total_count"] >= 2
    # Without active filters, filtered_count == total_count.
    assert captured["filtered_count"] == captured["total_count"]


@pytest.mark.asyncio
async def test_tasks_partial_filtered_count_differs_with_filters(
    authed_admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With active filters, filtered_count reflects the subset, total
    is still the whole universe of the `tasks` table."""
    from orcaster_server.api import admin_ui

    # We ensure at least 2 tasks in the DB; the `q` filter must discard
    # at least one.
    await _create_task(authed_admin_client, command="counts-keep")
    await _create_task(authed_admin_client, command="counts-skip")

    captured: dict[str, Any] = {}
    original = admin_ui.templates.TemplateResponse

    def _spy(request, name, context, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "_tasks_table.html":
            captured.update(context)
        return original(request, name, context, *args, **kwargs)

    monkeypatch.setattr(admin_ui.templates, "TemplateResponse", _spy)

    resp = await authed_admin_client.get(
        "/admin/tasks?q=counts-keep",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert captured["active_filters"] is True
    assert captured["filtered_count"] <= captured["total_count"]
    assert captured["total_count"] >= 2


@pytest.mark.asyncio
async def test_tasks_partial_non_htmx_redirect_preserves_sort(
    authed_admin_client: AsyncClient,
) -> None:
    """The non-htmx redirect includes sort/dir when they differ from the defaults
    so the dashboard preserves the view when sharing the link."""
    resp = await authed_admin_client.get(
        "/admin/tasks?sort=status&dir=asc",
        headers={"HX-Request": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert "sort=status" in loc
    assert "dir=asc" in loc


# --- Inline failure signal (exit_code + stderr-tail) on failed rows ---------
#
# The `failed` badge alone does not tell the operator what happened. We add a
# line under the badge with `exit N` (mono, danger-fg color) and the stderr
# tail (muted, truncated with title). It only appears for status='failed';
# other rows stay identical to avoid inflating density.


async def _seed_failed_execution(
    db_url: str, task_id: int, worker_id: str, exit_code: int = 75
) -> str:
    """Insert a failed execution WITHOUT touching stdout/stderr (deprecated
    columns that /complete no longer writes) and return its id."""
    execution_id = str(uuid.uuid4())
    await _exec_sql(
        db_url,
        "INSERT INTO task_executions (id, task_id, worker_id, status, "
        "exit_code, attempt_index, finished_at) "
        "VALUES (:eid, :tid, :wid, 'failed', :code, 1, now())",
        {"eid": execution_id, "tid": task_id, "wid": worker_id, "code": exit_code},
    )
    return execution_id


async def _seed_output_event(
    db_url: str, execution_id: str, seq: int, stream: str, text_chunk: str
) -> None:
    await _exec_sql(
        db_url,
        "INSERT INTO output_events (execution_id, seq, ts, stream, text) "
        "VALUES (:eid, :seq, now(), :stream, :text)",
        {"eid": execution_id, "seq": seq, "stream": stream, "text": text_chunk},
    )


@pytest.mark.asyncio
async def test_tasks_partial_renders_failure_signal_for_failed_row(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """A failed task with exit_code and stderr must show the inline chip
    `exit N` and the stderr tail. The stderr is seeded via `output_events`,
    which is where the real output lives (the stdout/stderr columns of
    task_executions were dropped in migration 0049)."""
    worker_id = await _register(authed_admin_client, hostname="fail-signal-host")
    task_id = await _create_task(authed_admin_client, command="will-fail")
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET status = CAST(:s AS task_status) WHERE id = :id",
        {"s": "failed", "id": task_id},
    )
    execution_id = await _seed_failed_execution(migrated_db_url, task_id, worker_id)
    await _seed_output_event(
        migrated_db_url,
        execution_id,
        1,
        "stderr",
        "Traceback (most recent call last):\nRuntimeError: pip install boom\n",
    )
    resp = await authed_admin_client.get("/admin/tasks", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text
    row_marker = f'id="task-row-{task_id}"'
    row_start = body.index(row_marker)
    row_end = body.index("</tr>", row_start)
    row_html = body[row_start:row_end]
    # Badge still present.
    assert "badge-failed" in row_html
    # Inline chip with the exit code in mono.
    assert "fail-inline" in row_html
    assert "exit 75" in row_html
    # stderr-tail visible and title with the content (rtrim'd).
    assert "fail-stderr" in row_html
    assert "RuntimeError: pip install boom" in row_html


@pytest.mark.asyncio
async def test_tasks_partial_stderr_tail_recomposes_chunks_and_caps_at_200(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The tail is rebuilt by concatenating the stderr events in `seq` order
    (the worker chunks may split a line), ignores the stdout events,
    rtrims the trailing whitespace and keeps only the last
    200 chars."""
    worker_id = await _register(authed_admin_client, hostname="fail-chunks-host")
    task_id = await _create_task(authed_admin_client, command="will-fail-chunked")
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET status = CAST(:s AS task_status) WHERE id = :id",
        {"s": "failed", "id": task_id},
    )
    execution_id = await _seed_failed_execution(migrated_db_url, task_id, worker_id)
    # Initial filler that pushes the final message beyond 200 chars.
    filler = "x" * 300
    await _seed_output_event(
        migrated_db_url, execution_id, 1, "stdout", "NOT-IN-TAIL progress line\n"
    )
    await _seed_output_event(
        migrated_db_url, execution_id, 2, "stderr", filler + "\nRuntimeError: pip ins"
    )
    await _seed_output_event(migrated_db_url, execution_id, 3, "stderr", "tall boom\n   \n")
    resp = await authed_admin_client.get("/admin/tasks", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text
    row_marker = f'id="task-row-{task_id}"'
    row_start = body.index(row_marker)
    row_end = body.index("</tr>", row_start)
    row_html = body[row_start:row_end]
    # The line split across chunks is reassembled in seq order.
    assert "RuntimeError: pip install boom" in row_html
    # stdout does not contaminate the stderr tail.
    assert "NOT-IN-TAIL" not in row_html
    # Cap at 200: the full rtrim'd text measures 300 + 1 + 30 chars, so
    # the start of the filler falls outside and the tail starts inside the filler.
    full_rtrimmed = filler + "\nRuntimeError: pip install boom"
    expected_tail = full_rtrimmed[-200:]
    assert expected_tail in row_html
    assert ("x" * 201) not in row_html


@pytest.mark.asyncio
async def test_tasks_partial_stderr_tail_not_computed_for_non_failed_rows(
    authed_admin_client: AsyncClient,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The template only shows the tail when status == 'failed', so the
    SQL must not pay for the subquery on output_events for the other rows
    (the listing is polled every 5s with ~50 rows, mostly non-failed): the
    contract is that `last_stderr_tail` arrives NULL even if stderr events
    exist. It is asserted on the context because the HTML does not distinguish
    "NULL" from "computed but hidden"."""
    from orcaster_server.api import admin_ui

    worker_id = await _register(authed_admin_client, hostname="ok-with-stderr-host")
    task_id = await _create_task(authed_admin_client, command="ok-noisy-stderr")
    execution_id = str(uuid.uuid4())
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO task_executions (id, task_id, worker_id, status, "
        "exit_code, attempt_index, finished_at) "
        "VALUES (:eid, :tid, :wid, 'completed', 0, 1, now())",
        {"eid": execution_id, "tid": task_id, "wid": worker_id},
    )
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET status = CAST(:s AS task_status) WHERE id = :id",
        {"s": "completed", "id": task_id},
    )
    await _seed_output_event(
        migrated_db_url, execution_id, 1, "stderr", "WARNING: deprecated flag\n"
    )

    captured: dict[str, Any] = {}
    original = admin_ui.templates.TemplateResponse

    def _spy(request, name, context, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "_tasks_table.html":
            captured.update(context)
        return original(request, name, context, *args, **kwargs)

    monkeypatch.setattr(admin_ui.templates, "TemplateResponse", _spy)

    resp = await authed_admin_client.get("/admin/tasks", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    row = next(r for r in captured["tasks"] if r["id"] == task_id)
    assert row["last_stderr_tail"] is None


@pytest.mark.asyncio
async def test_tasks_partial_failed_without_stderr_events_shows_exit_only(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """A failed execution without stderr events shows the `exit N` chip
    but not the stderr span (tail NULL, not an empty string)."""
    worker_id = await _register(authed_admin_client, hostname="fail-no-stderr-host")
    task_id = await _create_task(authed_admin_client, command="will-fail-silent")
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET status = CAST(:s AS task_status) WHERE id = :id",
        {"s": "failed", "id": task_id},
    )
    await _seed_failed_execution(migrated_db_url, task_id, worker_id, exit_code=137)
    resp = await authed_admin_client.get("/admin/tasks", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text
    row_marker = f'id="task-row-{task_id}"'
    row_start = body.index(row_marker)
    row_end = body.index("</tr>", row_start)
    row_html = body[row_start:row_end]
    assert "exit 137" in row_html
    assert "fail-stderr" not in row_html


@pytest.mark.asyncio
async def test_tasks_partial_no_failure_signal_for_non_failed_rows(
    authed_admin_client: AsyncClient,
) -> None:
    """A queued/completed/cancelled task must not carry the `fail-inline`
    line. The signal is exclusive to the failed state."""
    task_id = await _create_task(authed_admin_client, command="still-queued-no-signal")
    resp = await authed_admin_client.get("/admin/tasks", headers={"HX-Request": "true"})
    body = resp.text
    row_marker = f'id="task-row-{task_id}"'
    row_start = body.index(row_marker)
    row_end = body.index("</tr>", row_start)
    row_html = body[row_start:row_end]
    assert "fail-inline" not in row_html
    assert "exit " not in row_html


@pytest.mark.asyncio
async def test_tasks_partial_failure_signal_falls_back_when_no_data(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """A failed task without executions (edge case: marked failed by the
    retry-exhausted logic without a recorded attempt) falls back to the plain badge
    — it does not break the render."""
    task_id = await _create_task(authed_admin_client, command="failed-without-exec")
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET status = CAST(:s AS task_status) WHERE id = :id",
        {"s": "failed", "id": task_id},
    )
    resp = await authed_admin_client.get("/admin/tasks", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text
    row_marker = f'id="task-row-{task_id}"'
    row_start = body.index(row_marker)
    row_end = body.index("</tr>", row_start)
    row_html = body[row_start:row_end]
    assert "badge-failed" in row_html
    # Without exit_code or stderr: the inline line is not rendered.
    assert "fail-inline" not in row_html


# --- Heading hierarchy (a11y, WCAG 2.2 SC 1.3.1) ----------------------------
#
# The dashboard must have a single <h1>, panels in <h2> and nothing that jumps
# directly from h1 to h3. The modals (<dialog>) live inside the same
# document outline; their titles must respect the hierarchy (h2, not h3).


@pytest.mark.asyncio
async def test_dashboard_heading_hierarchy_no_skip(authed_admin_client: AsyncClient) -> None:
    """A single h1, at least one h2, and no h3 in /admin/ (all the visible
    headings fall in h1/h2 now that the modals rise to h2)."""
    import re

    resp = await authed_admin_client.get("/admin/")
    assert resp.status_code == 200
    body = resp.text

    # A single h1, content = "Tasks".
    h1s = re.findall(r"<h1[^>]*>(.*?)</h1>", body, flags=re.DOTALL)
    assert len(h1s) == 1, f"expected exactly 1 h1, got {len(h1s)}"
    assert "Tasks" in h1s[0]

    # h2s present: "Workers" (panel) + "New task" (modal).
    h2s = re.findall(r"<h2[^>]*>(.*?)</h2>", body, flags=re.DOTALL)
    h2_texts = [re.sub(r"<[^>]+>", "", t).strip() for t in h2s]
    assert any("Workers" in t for t in h2_texts), h2_texts
    assert any("New task" in t for t in h2_texts), h2_texts

    # There must be no h3 in the dashboard outline (the h1→h3 skip was the bug).
    assert "<h3" not in body, "h3 present in /admin/ — heading skip detected"


# --- Active filter chips ----------------------------------------------------
#
# Dismissible chips above the table when filters are applied. Each chip
# is a link to the same endpoint with ITS param removed (preserving the rest).


@pytest.mark.asyncio
async def test_tasks_partial_no_chips_without_filters(
    authed_admin_client: AsyncClient,
) -> None:
    """Without active filters: the chips wrap must NOT be rendered. Avoids an
    empty slot above the table."""
    await _create_task(authed_admin_client, command="no-chips")
    resp = await authed_admin_client.get("/admin/tasks", headers={"HX-Request": "true"})
    body = resp.text
    assert "active-filters" not in body
    assert "active-filter-chip" not in body


@pytest.mark.asyncio
async def test_tasks_partial_renders_chip_per_status_token(
    authed_admin_client: AsyncClient,
) -> None:
    """Combining status=failed,cancelled + q must produce 3 chips: two for
    status (one per token) and one for q."""
    await _create_task(authed_admin_client, command="chips-target")
    resp = await authed_admin_client.get(
        "/admin/tasks?status=failed,cancelled&q=run_batch",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "active-filters" in body
    # Three chips: one per token + one for q.
    assert body.count('class="active-filter-chip"') == 3
    # Labels and values visible.
    assert ">failed<" in body
    assert ">cancelled<" in body
    assert 'class="active-filter-chip-val">"run_batch"<' in body


@pytest.mark.asyncio
async def test_tasks_partial_chip_dismiss_removes_only_its_param(
    authed_admin_client: AsyncClient,
) -> None:
    """The href/hx-get of each chip must remove ITS param and preserve the
    rest. We combine status=failed,cancelled + q and verify each chip."""
    await _create_task(authed_admin_client, command="dismiss-target")
    resp = await authed_admin_client.get(
        "/admin/tasks?status=failed,cancelled&q=needle",
        headers={"HX-Request": "true"},
    )
    body = resp.text

    # The chip that removes 'failed' must leave status=cancelled and q=needle.
    import re

    pattern = (
        r'<a class="active-filter-chip"[^>]*'
        r'hx-get="([^"]+)"[^>]*'
        r'aria-label="Remove filter ([^"]+)"'
    )
    chips = re.findall(pattern, body)
    assert len(chips) == 3, chips
    chips_by_label = {label: href for href, label in chips}

    failed_href = chips_by_label["status failed"]
    assert "status=cancelled" in failed_href
    assert "status=failed" not in failed_href
    assert "q=needle" in failed_href

    cancelled_href = chips_by_label["status cancelled"]
    assert "status=failed" in cancelled_href
    assert "status=cancelled" not in cancelled_href
    assert "q=needle" in cancelled_href

    q_href = chips_by_label["q needle"]
    # Removes q, preserves both statuses (csv).
    assert "q=needle" not in q_href
    assert "status=failed%2Ccancelled" in q_href or "status=failed,cancelled" in q_href


@pytest.mark.asyncio
async def test_tasks_partial_chip_when_only_q_active(
    authed_admin_client: AsyncClient,
) -> None:
    """Only q active: a single chip whose link has no query params (clears)."""
    await _create_task(authed_admin_client, command="only-q-target")
    resp = await authed_admin_client.get(
        "/admin/tasks?q=only-q",
        headers={"HX-Request": "true"},
    )
    body = resp.text
    assert body.count('class="active-filter-chip"') == 1
    # The href must be /admin/tasks without params (a single chip removes everything).
    assert 'hx-get="/admin/tasks"' in body


# --- Tests of ?partial=1 and live HTMX polling -----------------------------


@pytest.mark.asyncio
async def test_output_partial_polls_while_live(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The ?partial=1 fragment includes the HTMX trigger while the execution
    is not terminal, and omits it when the execution is in a terminal state."""
    worker_id = await _register(authed_admin_client, hostname="partial-poll-worker")
    task_id = await _create_task(authed_admin_client, command="echo partial")

    # Insert an execution in 'running' (non-terminal) state.
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO task_executions (task_id, worker_id, status) "
        "VALUES (:tid, :wid, CAST('running' AS execution_status))",
        {"tid": task_id, "wid": worker_id},
    )

    # Get the UUID of the execution just created.
    row = await _row(
        migrated_db_url,
        "SELECT id::text AS id FROM task_executions WHERE task_id = :tid AND worker_id = :wid",
        {"tid": task_id, "wid": worker_id},
    )
    assert row is not None
    execution_id = row["id"]

    # The partial fragment must include the HTMX trigger because it is 'running'.
    r = await authed_admin_client.get(f"/admin/executions/{execution_id}/output?partial=1")
    assert r.status_code == 200
    assert 'hx-trigger="every 2s"' in r.text
    assert "live" in r.text

    # Update to terminal state 'completed' → the trigger must not appear.
    await _exec_sql(
        migrated_db_url,
        "UPDATE task_executions SET status = CAST('completed' AS execution_status), "
        "exit_code = 0, finished_at = now() WHERE id = CAST(:eid AS uuid)",
        {"eid": execution_id},
    )

    r2 = await authed_admin_client.get(f"/admin/executions/{execution_id}/output?partial=1")
    assert r2.status_code == 200
    assert "hx-trigger" not in r2.text


@pytest.mark.asyncio
async def test_output_partial_legacy_route_polls_while_live(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """?partial=1 fragment of the legacy endpoint by task_id+worker_id.

    Includes the HTMX trigger while the execution is not terminal and omits it
    after completion.
    """
    worker_id = await _register(authed_admin_client, hostname="legacy-partial-worker")
    task_id = await _create_task(authed_admin_client, command="echo legacy-partial")

    await _exec_sql(
        migrated_db_url,
        "INSERT INTO task_executions (task_id, worker_id, status) "
        "VALUES (:tid, :wid, CAST('running' AS execution_status))",
        {"tid": task_id, "wid": worker_id},
    )

    r = await authed_admin_client.get(
        f"/admin/tasks/{task_id}/executions/{worker_id}/output?partial=1"
    )
    assert r.status_code == 200
    assert 'hx-trigger="every 2s"' in r.text
    assert "live" in r.text

    await _exec_sql(
        migrated_db_url,
        "UPDATE task_executions SET status = CAST('completed' AS execution_status), "
        "exit_code = 0, finished_at = now() "
        "WHERE task_id = :tid AND worker_id = :wid",
        {"tid": task_id, "wid": worker_id},
    )

    r2 = await authed_admin_client.get(
        f"/admin/tasks/{task_id}/executions/{worker_id}/output?partial=1"
    )
    assert r2.status_code == 200
    assert "hx-trigger" not in r2.text


@pytest.mark.asyncio
async def test_output_partial_incremental_by_seq(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Incremental polling by `after=N` brings only events with seq > N and the
    poller advances the cursor in its poll_url."""
    worker_id = await _register(authed_admin_client, hostname="partial-incr-worker")
    task_id = await _create_task(authed_admin_client, command="echo incr")
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO task_executions (task_id, worker_id, status) "
        "VALUES (:tid, :wid, CAST('running' AS execution_status))",
        {"tid": task_id, "wid": worker_id},
    )
    row = await _row(
        migrated_db_url,
        "SELECT id::text AS id FROM task_executions WHERE task_id = :tid AND worker_id = :wid",
        {"tid": task_id, "wid": worker_id},
    )
    assert row is not None
    execution_id = row["id"]
    await _insert_output_events(
        migrated_db_url,
        execution_id,
        [(1, "stdout", "EVENT-ONE"), (2, "stdout", "EVENT-TWO"), (3, "stderr", "EVENT-THREE")],
    )

    # after=1 → only events with seq 2 and 3.
    r = await authed_admin_client.get(f"/admin/executions/{execution_id}/output?partial=1&after=1")
    assert r.status_code == 200
    assert "EVENT-ONE" not in r.text
    assert "EVENT-TWO" in r.text
    assert "EVENT-THREE" in r.text
    # The poller advances the cursor to the last seq seen (3).
    assert "after=3" in r.text


@pytest.mark.asyncio
async def test_output_paginated_with_catchup_poller(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Pagination: the first load brings at most one page of
    events; if more remain, an immediate catch-up poller is emitted
    (`load delay:100ms`) EVEN IF the execution is terminal. The last page
    of a terminal one no longer emits a poller."""
    from orcaster_server.api.admin_ui import _OUTPUT_EVENTS_PAGE_SIZE as page

    worker_id = await _register(authed_admin_client, hostname="paged-worker")
    task_id = await _create_task(authed_admin_client, command="echo paged")
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO task_executions (task_id, worker_id, status, exit_code, finished_at) "
        "VALUES (:tid, :wid, 'completed', 0, now())",
        {"tid": task_id, "wid": worker_id},
    )
    row = await _row(
        migrated_db_url,
        "SELECT id::text AS id FROM task_executions WHERE task_id = :tid AND worker_id = :wid",
        {"tid": task_id, "wid": worker_id},
    )
    assert row is not None
    execution_id = row["id"]
    # page + 2 events in bulk (one by one would be hundreds of round-trips).
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO output_events (execution_id, seq, ts, stream, text) "
        "SELECT CAST(:eid AS uuid), g, now(), 'stdout', 'PAGED-EV-' || g "
        "FROM generate_series(1, :n) AS g",
        {"eid": execution_id, "n": page + 2},
    )

    # First load: only the first page + immediate catch-up poller.
    r = await authed_admin_client.get(f"/admin/executions/{execution_id}/output")
    assert r.status_code == 200
    assert f"PAGED-EV-{page}" in r.text
    assert f"PAGED-EV-{page + 1}" not in r.text
    assert 'hx-trigger="load delay:100ms"' in r.text
    assert f"after={page}" in r.text

    # Catch-up page: brings the rest and, with none left (terminal),
    # emits no new poller.
    r2 = await authed_admin_client.get(
        f"/admin/executions/{execution_id}/output?partial=1&after={page}"
    )
    assert r2.status_code == 200
    assert f"PAGED-EV-{page + 1}" in r2.text
    assert f"PAGED-EV-{page + 2}" in r2.text
    assert "hx-trigger" not in r2.text


@pytest.mark.asyncio
async def test_output_partial_empty_poll_appends_no_whitespace(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """UI regression: the log grew with blank lines on every heartbeat
    because the poll fragment emitted Jinja whitespace as text nodes
    (the container is white-space: pre-wrap). Three paths covered:
    1. Empty live poll: starts directly at the poller.
    2. Live poll with events: starts directly at the first chunk.
    3. Terminal poll without backlog (live=false, has_more=false): starts directly
       at the OOB statusline — the {%- with %} introduces no stray text nodes."""
    worker_id = await _register(authed_admin_client, hostname="ws-clean-worker")
    task_id = await _create_task(authed_admin_client, command="echo ws-clean")
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO task_executions (task_id, worker_id, status) "
        "VALUES (:tid, :wid, CAST('running' AS execution_status))",
        {"tid": task_id, "wid": worker_id},
    )
    row = await _row(
        migrated_db_url,
        "SELECT id::text AS id FROM task_executions WHERE task_id = :tid AND worker_id = :wid",
        {"tid": task_id, "wid": worker_id},
    )
    assert row is not None
    execution_id = row["id"]

    # Empty poll (no events): the fragment starts directly at the poller.
    r = await authed_admin_client.get(f"/admin/executions/{execution_id}/output?partial=1")
    assert r.status_code == 200
    assert r.text.startswith('<span id="exec-output-poller"')
    assert '</span><p id="exec-output-status"' in r.text
    # The fragment ends right at the close of the OOB statusline: a trailing
    # newline would land in the log as a text node (HTMX extracts the OOB and
    # swaps the rest, whitespace included).
    assert r.text.endswith("</p>")

    # Poll with events: the fragment starts directly at the first chunk.
    await _insert_output_events(migrated_db_url, execution_id, [(1, "stdout", "WS-CHUNK-A")])
    r2 = await authed_admin_client.get(f"/admin/executions/{execution_id}/output?partial=1")
    assert r2.status_code == 200
    assert r2.text.startswith('<span class="out-chunk out-stdout"')
    assert r2.text.endswith("</p>")

    # Terminal-without-poller path: after completing the execution, a poll with the cursor
    # already up to date (after=1, seq=1 already consumed) emits no poller or text nodes —
    # the {%- with %} of the OOB statusline is the only thing between the (empty) lines and
    # the statusline, and it must start without visible whitespace.
    await _exec_sql(
        migrated_db_url,
        "UPDATE task_executions SET status = CAST('completed' AS execution_status), "
        "exit_code = 0, finished_at = now() WHERE id = CAST(:eid AS uuid)",
        {"eid": execution_id},
    )
    r3 = await authed_admin_client.get(f"/admin/executions/{execution_id}/output?partial=1&after=1")
    assert r3.status_code == 200
    # No new events + terminal → the fragment starts directly at the statusline,
    # without a poller or text nodes between the {%- endfor -%} and the {%- with %}.
    assert r3.text.startswith('<p id="exec-output-status"')
    assert r3.text.endswith("</p>")
    # Terminal without backlog → no new poller is emitted.
    assert "exec-output-poller" not in r3.text


@pytest.mark.asyncio
async def test_output_full_page_log_div_has_no_stray_text(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The first load must not leave text nodes inside the log div either:
    the poller sits flush against the opening and closing tags."""
    worker_id = await _register(authed_admin_client, hostname="ws-clean-full-worker")
    task_id = await _create_task(authed_admin_client, command="echo ws-clean-full")
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO task_executions (task_id, worker_id, status) "
        "VALUES (:tid, :wid, CAST('running' AS execution_status))",
        {"tid": task_id, "wid": worker_id},
    )
    row = await _row(
        migrated_db_url,
        "SELECT id::text AS id FROM task_executions WHERE task_id = :tid AND worker_id = :wid",
        {"tid": task_id, "wid": worker_id},
    )
    assert row is not None
    execution_id = row["id"]

    r = await authed_admin_client.get(f"/admin/executions/{execution_id}/output")
    assert r.status_code == 200
    # Empty log + running: the poller is the only child of the div, without whitespace.
    assert 'class="output-log"><span id="exec-output-poller"' in r.text
    assert "</span></div>" in r.text


@pytest.mark.asyncio
async def test_output_poller_live_class(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The poller carries the `poller-live` class only while the execution is
    alive (the CSS uses it for the skeleton/cursor). The catch-up poller of
    a terminal execution with backlog does NOT carry it."""
    from orcaster_server.api.admin_ui import _OUTPUT_EVENTS_PAGE_SIZE as page

    worker_id = await _register(authed_admin_client, hostname="poller-live-worker")
    task_id = await _create_task(authed_admin_client, command="echo poller-live")
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO task_executions (task_id, worker_id, status) "
        "VALUES (:tid, :wid, CAST('running' AS execution_status))",
        {"tid": task_id, "wid": worker_id},
    )
    row = await _row(
        migrated_db_url,
        "SELECT id::text AS id FROM task_executions WHERE task_id = :tid AND worker_id = :wid",
        {"tid": task_id, "wid": worker_id},
    )
    assert row is not None
    execution_id = row["id"]

    # Running → poller with the poller-live class (full page and fragment).
    r_full = await authed_admin_client.get(f"/admin/executions/{execution_id}/output")
    assert r_full.status_code == 200
    assert '<span id="exec-output-poller" class="poller-live"' in r_full.text
    r_part = await authed_admin_client.get(f"/admin/executions/{execution_id}/output?partial=1")
    assert r_part.status_code == 200
    assert '<span id="exec-output-poller" class="poller-live"' in r_part.text

    # Terminal with backlog → catch-up poller present but without the class.
    await _exec_sql(
        migrated_db_url,
        "UPDATE task_executions SET status = CAST('completed' AS execution_status), "
        "exit_code = 0, finished_at = now() WHERE id = CAST(:eid AS uuid)",
        {"eid": execution_id},
    )
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO output_events (execution_id, seq, ts, stream, text) "
        "SELECT CAST(:eid AS uuid), g, now(), 'stdout', 'PL-EV-' || g "
        "FROM generate_series(1, :n) AS g",
        {"eid": execution_id, "n": page + 1},
    )
    r_catchup = await authed_admin_client.get(f"/admin/executions/{execution_id}/output?partial=1")
    assert r_catchup.status_code == 200
    assert 'hx-trigger="load delay:100ms"' in r_catchup.text
    assert "poller-live" not in r_catchup.text


# --- Payload viewer: Jinja filters ------------------------------------------


def test_payload_commands_returns_list():
    from orcaster_server.api.admin_ui import _payload_commands

    payload = {"commands": ["echo a", "echo b"], "requirements": []}
    assert _payload_commands(payload) == ["echo a", "echo b"]


def test_payload_commands_missing_key_returns_none():
    from orcaster_server.api.admin_ui import _payload_commands

    assert _payload_commands({"type": "control"}) is None


def test_payload_commands_not_a_list_returns_none():
    from orcaster_server.api.admin_ui import _payload_commands

    assert _payload_commands({"commands": "echo a"}) is None


def test_payload_commands_non_str_element_returns_none():
    from orcaster_server.api.admin_ui import _payload_commands

    assert _payload_commands({"commands": ["echo a", 3]}) is None


def test_payload_commands_empty_list_returns_none():
    from orcaster_server.api.admin_ui import _payload_commands

    assert _payload_commands({"commands": []}) is None


def test_payload_commands_empty_dict_returns_none():
    from orcaster_server.api.admin_ui import _payload_commands

    assert _payload_commands({}) is None


def test_payload_commands_none_returns_none():
    from orcaster_server.api.admin_ui import _payload_commands

    assert _payload_commands(None) is None


def test_payload_commands_non_dict_returns_none():
    from orcaster_server.api.admin_ui import _payload_commands

    assert _payload_commands("not a dict") is None


def test_payload_json_pretty_and_sorted():
    from orcaster_server.api.admin_ui import _payload_json

    payload = {"requirements": [], "commands": ["echo a"]}
    out = _payload_json(payload)
    # sort_keys=True → "commands" appears before "requirements"
    assert out.index('"commands"') < out.index('"requirements"')
    # indent=2 → there is a two-space indentation
    assert "\n  " in out
    # exact round-trip
    assert json.loads(out) == payload


def test_payload_json_unicode_not_escaped():
    from orcaster_server.api.admin_ui import _payload_json

    out = _payload_json({"commands": ["echo ñ 中"]})
    assert "ñ" in out
    assert "中" in out
    assert "\\u" not in out


def test_payload_json_preserves_embedded_quotes():
    from orcaster_server.api.admin_ui import _payload_json

    out = _payload_json({"commands": ['echo "hi"']})
    assert json.loads(out) == {"commands": ['echo "hi"']}


def test_payload_json_none_returns_braces():
    from orcaster_server.api.admin_ui import _payload_json

    assert _payload_json(None) == "{}"


def test_payload_json_empty_dict_returns_braces():
    from orcaster_server.api.admin_ui import _payload_json

    assert _payload_json({}) == "{}"


# --- Payload viewer: admin_task_detail handler render ----------------------


@pytest.mark.asyncio
async def test_task_detail_renders_payload_disclosure(authed_admin_client: AsyncClient) -> None:
    task_id = await _create_task(authed_admin_client, commands=["echo one", "echo two"])
    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    assert resp.status_code == 200
    body = resp.text
    assert 'class="payload-disclosure"' in body
    assert "COMMANDS · 2" in body


@pytest.mark.asyncio
async def test_task_detail_payload_disclosure_lists_each_command(
    authed_admin_client: AsyncClient,
) -> None:
    task_id = await _create_task(
        authed_admin_client, commands=["echo one", "echo two", "echo three"]
    )
    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    body = resp.text
    assert body.count('class="payload-cmd-item"') == 3


@pytest.mark.asyncio
async def test_task_detail_copy_button_has_full_json(authed_admin_client: AsyncClient) -> None:
    import html as _html
    import re as _re

    task_id = await _create_task(authed_admin_client, commands=["echo one"])
    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    body = resp.text
    m = _re.search(r'data-copy="([^"]*)"', body)
    assert m is not None
    parsed = json.loads(_html.unescape(m.group(1)))
    assert "commands" in parsed
    assert parsed["commands"] == ["echo one"]


@pytest.mark.asyncio
async def test_task_detail_summary_keeps_args_pretty(authed_admin_client: AsyncClient) -> None:
    task_id = await _create_task(authed_admin_client, commands=["echo summary-text", "echo b"])
    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    body = resp.text
    # args_pretty: first command + "(+1 more)" (regression of the existing chip)
    assert "echo summary-text" in body
    assert "(+1 more)" in body


@pytest.mark.asyncio
async def test_task_detail_payload_without_commands_shows_payload_block(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    task_id = await _create_task(authed_admin_client, command="echo seed")
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET payload = CAST(:p AS jsonb) WHERE id = :id",
        {"p": json.dumps({"type": "control", "action": "pause"}), "id": task_id},
    )
    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    body = resp.text
    assert "PAYLOAD" in body
    assert "COMMANDS ·" not in body


@pytest.mark.asyncio
async def test_task_detail_empty_payload_shows_no_structured_payload(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    task_id = await _create_task(authed_admin_client, command="echo seed")
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET payload = CAST(:p AS jsonb) WHERE id = :id",
        {"p": json.dumps({}), "id": task_id},
    )
    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    body = resp.text
    assert "No structured payload" in body


@pytest.mark.asyncio
async def test_task_detail_polling_oob_excludes_payload_meta(
    authed_admin_client: AsyncClient,
) -> None:
    task_id = await _create_task(authed_admin_client, commands=["echo one"])
    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    body = resp.text
    assert 'hx-select-oob="#task-status-badge,#task-detail-attempts,#task-detail-actions"' in body


@pytest.mark.asyncio
async def test_task_detail_payload_escapes_html_in_commands(
    authed_admin_client: AsyncClient,
) -> None:
    # The payload is controlled by whoever creates the task: a command with HTML must not be
    # injected as markup either in the visible text or in the data-copy attribute.
    evil = '<img src=x onerror="alert(1)">'
    task_id = await _create_task(authed_admin_client, commands=[evil])
    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    assert resp.status_code == 200
    body = resp.text
    # The raw tag never appears unescaped...
    assert "<img src=x" not in body
    # ...and it does appear escaped (Jinja autoescape).
    assert "&lt;img src=x" in body


# --- RUNTIME column --------------------------------------------------------
#
# The RUNTIME column replaces the Created column in the tasks table.
# It shows 3 states: not started (no execution or no started_at), running
# (started_at present, finished_at absent), terminal (both present).
# It is based on last_started_at / last_finished_at of the most recent execution
# (by assigned_at DESC).


async def _insert_exec(
    db_url: str,
    task_id: int,
    worker_id: str,
    status: str,
    *,
    started: bool,
    finished: bool,
    assigned_offset_seconds: int = 0,
    attempt_index: int = 1,
) -> None:
    """Insert a row into task_executions with controlled timestamps.

    `assigned_offset_seconds` lets us force which one is the 'most recent'
    (greatest assigned_at) deterministically.
    """
    await _exec_sql(
        db_url,
        """
        INSERT INTO task_executions
            (task_id, worker_id, status, attempt_index,
             assigned_at, started_at, finished_at, exit_code)
        VALUES (:tid, :wid, CAST(:s AS execution_status), :ai,
                now() + make_interval(secs => :off),
                CASE WHEN :started THEN now() + make_interval(secs => :off) ELSE NULL END,
                CASE WHEN :finished THEN now() + make_interval(secs => :off) ELSE NULL END,
                CASE WHEN :finished THEN 0 ELSE NULL END)
        """,
        {
            "tid": task_id,
            "wid": worker_id,
            "s": status,
            "ai": attempt_index,
            "off": assigned_offset_seconds,
            "started": started,
            "finished": finished,
        },
    )


def _row_html(body: str, task_id: int) -> str:
    """Extract the <tr> of a task from the table HTML."""
    marker = f'id="task-row-{task_id}"'
    start = body.index(marker)
    end = body.index("</tr>", start)
    return body[start:end]


@pytest.mark.asyncio
async def test_runtime_column_header_replaces_created(
    authed_admin_client: AsyncClient,
) -> None:
    """The header shows Runtime and no longer the Created sort button."""
    await _create_task(authed_admin_client)
    body = (await authed_admin_client.get("/admin/tasks")).text
    assert 'class="col-runtime"' in body
    assert ">Runtime<" in body
    # The Created sort button was removed (it was the only th with that label).
    assert 'data-sort-col="created_at"' not in body


@pytest.mark.asyncio
async def test_runtime_column_queued_shows_not_started(
    authed_admin_client: AsyncClient,
) -> None:
    """A task without an execution (queued) shows 'not started'."""
    task_id = await _create_task(authed_admin_client)
    body = (await authed_admin_client.get("/admin/tasks")).text
    row = _row_html(body, task_id)
    assert 'data-runtime-state="none"' in row
    assert "not started" in row
    assert "js-runtime-ts" not in row


@pytest.mark.asyncio
async def test_runtime_column_running_shows_start_and_progress(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Running: a start <time> + progress marker, no end."""
    task_id = await _create_task(authed_admin_client)
    worker_id = await _register(authed_admin_client, hostname="rt-running")
    await _insert_exec(
        migrated_db_url,
        task_id,
        worker_id,
        "running",
        started=True,
        finished=False,
    )
    body = (await authed_admin_client.get("/admin/tasks")).text
    row = _row_html(body, task_id)
    assert 'data-runtime-state="running"' in row
    assert 'class="js-ts runtime-start"' in row
    assert "runtime-running" in row
    assert "runtime-end" not in row


@pytest.mark.asyncio
async def test_runtime_column_terminal_shows_range(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Terminal: two <time> (start and end) and the range arrow."""
    task_id = await _create_task(authed_admin_client)
    worker_id = await _register(authed_admin_client, hostname="rt-terminal")
    await _insert_exec(
        migrated_db_url,
        task_id,
        worker_id,
        "completed",
        started=True,
        finished=True,
    )
    body = (await authed_admin_client.get("/admin/tasks")).text
    row = _row_html(body, task_id)
    assert 'data-runtime-state="terminal"' in row
    assert 'class="js-ts runtime-start"' in row
    assert 'class="js-ts runtime-end"' in row
    assert "runtime-arrow" in row


@pytest.mark.asyncio
async def test_runtime_column_finished_without_start_is_not_started(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Edge case: finished_at present but started_at NULL → not started."""
    task_id = await _create_task(authed_admin_client)
    worker_id = await _register(authed_admin_client, hostname="rt-edge")
    await _insert_exec(
        migrated_db_url,
        task_id,
        worker_id,
        "expired",
        started=False,
        finished=True,
    )
    body = (await authed_admin_client.get("/admin/tasks")).text
    row = _row_html(body, task_id)
    assert 'data-runtime-state="none"' in row
    assert "not started" in row


@pytest.mark.asyncio
async def test_runtime_column_uses_most_recent_execution(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """With several executions, RUNTIME reflects the one with the greatest assigned_at.

    The old one is finished; the new one is running. If the
    column took the oldest it would show 'terminal'; it must show 'running'.
    """
    task_id = await _create_task(authed_admin_client)
    worker_id = await _register(authed_admin_client, hostname="rt-recent")
    await _insert_exec(
        migrated_db_url,
        task_id,
        worker_id,
        "failed",
        started=True,
        finished=True,
        assigned_offset_seconds=0,
        attempt_index=1,
    )
    await _insert_exec(
        migrated_db_url,
        task_id,
        worker_id,
        "running",
        started=True,
        finished=False,
        assigned_offset_seconds=60,
        attempt_index=2,
    )
    body = (await authed_admin_client.get("/admin/tasks")).text
    row = _row_html(body, task_id)
    assert 'data-runtime-state="running"' in row


@pytest.mark.asyncio
async def test_failure_signal_uses_most_recent_by_assigned_at(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The exit_code signal reflects the execution with the greatest assigned_at,
    NOT the one with the greatest finished_at.

    Attempt 1: old assigned_at, RECENT finished_at, exit_code=11.
    Attempt 2: RECENT assigned_at, old finished_at, exit_code=22.
    It must render "exit 22" (the most recent by assigned_at).
    """
    worker_id = await _register(authed_admin_client, hostname="fail-signal-order")
    task_id = await _create_task(authed_admin_client, command="will-fail-order")
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET status = CAST(:s AS task_status) WHERE id = :id",
        {"s": "failed", "id": task_id},
    )
    # Attempt 1: old assigned_at (offset -120s) BUT RECENT finished_at
    # (offset +120s). exit_code=11. If the query used finished_at DESC, this one
    # would win — which the test verifies does NOT happen.
    await _exec_sql(
        migrated_db_url,
        """
        INSERT INTO task_executions
            (task_id, worker_id, status, attempt_index,
             assigned_at, started_at, finished_at, exit_code)
        VALUES (:tid, :wid, CAST('failed' AS execution_status), 1,
                now() + make_interval(secs => -120),
                now() + make_interval(secs => -120),
                now() + make_interval(secs =>  120),
                11)
        """,
        {"tid": task_id, "wid": worker_id},
    )
    # Attempt 2: RECENT assigned_at (offset +60s) but old finished_at
    # (offset -60s). exit_code=22. The query orders by assigned_at DESC, so
    # this one must win.
    await _exec_sql(
        migrated_db_url,
        """
        INSERT INTO task_executions
            (task_id, worker_id, status, attempt_index,
             assigned_at, started_at, finished_at, exit_code)
        VALUES (:tid, :wid, CAST('failed' AS execution_status), 2,
                now() + make_interval(secs =>  60),
                now() + make_interval(secs =>  60),
                now() + make_interval(secs => -60),
                22)
        """,
        {"tid": task_id, "wid": worker_id},
    )
    resp = await authed_admin_client.get("/admin/tasks", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    row = _row_html(resp.text, task_id)
    assert "fail-inline" in row
    assert "exit 22" in row
    assert "exit 11" not in row


@pytest.mark.asyncio
async def test_runtime_column_fallback_is_compact_format(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The visible fallback (without JS) of the RUNTIME column uses the compact format
    "Mmm DD HH:MM", without year or seconds.

    Verifies that:
    - The text of the <time> elements does NOT contain a four-digit year (\\d{4}-\\d{2}-\\d{2}).
    - The text DOES match the compact pattern [A-Z][a-z]{2} \\d{2} \\d{2}:\\d{2}.
    - The title= attribute keeps the full UTC ISO (contains the year).
    """
    import re

    task_id = await _create_task(authed_admin_client)
    worker_id = await _register(authed_admin_client, hostname="rt-compact")
    await _insert_exec(
        migrated_db_url,
        task_id,
        worker_id,
        "completed",
        started=True,
        finished=True,
    )
    body = (await authed_admin_client.get("/admin/tasks")).text
    row = _row_html(body, task_id)

    # Extract the visible content of the <time> elements of RUNTIME.
    time_texts = re.findall(r'class="js-ts runtime-(?:start|end)"[^>]*>([^<]+)<', row)
    assert time_texts, "No runtime <time> elements found in the row"

    for ts_text in time_texts:
        # The fallback must NOT contain a long ISO date with a year.
        assert not re.search(r"\d{4}-\d{2}-\d{2}", ts_text), (
            f"El fallback visible contiene formato largo: {ts_text!r}"
        )
        # The fallback MUST match "Mmm DD HH:MM".
        assert re.match(r"[A-Z][a-z]{2} \d{2} \d{2}:\d{2}", ts_text), (
            f"The visible fallback does not have the compact format: {ts_text!r}"
        )

    # The title= of runtime-start must keep the full UTC (contains the year).
    title_match = re.search(r'class="js-ts runtime-start"[^>]*title="([^"]+)"', row)
    assert title_match, "The title attribute was not found in runtime-start"
    assert re.search(r"\d{4}-\d{2}-\d{2}", title_match.group(1)), (
        "The runtime-start title does not contain the full UTC ISO"
    )


@pytest.mark.asyncio
async def test_tasks_partial_exposes_fork_lineage(
    authed_admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The list exposes parent_task_id and parent_params_changed per row.

    - task without a parent → parent_task_id None
    - identical fork (same target as the parent) → parent_params_changed False
    - modified fork (different target) → parent_params_changed True
    """
    from orcaster_server.api import admin_ui

    # Create the parent with an empty payload so that the identical fork (same empty
    # payload and same target=1) does not differ in any field compared by
    # _params_changed.
    resp = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert resp.status_code == 200, resp.text
    parent_id = int(resp.json()["task_id"])
    resp = await authed_admin_client.post(f"/admin/tasks/{parent_id}/fork", json={"target": "1"})
    assert resp.status_code == 200, resp.text
    identical_id = int(resp.json()["task_id"])
    resp = await authed_admin_client.post(f"/admin/tasks/{parent_id}/fork", json={"target": "3"})
    assert resp.status_code == 200, resp.text
    modified_id = int(resp.json()["task_id"])

    captured: dict = {}
    original = admin_ui.templates.TemplateResponse

    def _spy(request, name, context, *args, **kwargs):
        if name == "_tasks_table.html":
            captured.update(context)
        return original(request, name, context, *args, **kwargs)

    monkeypatch.setattr(admin_ui.templates, "TemplateResponse", _spy)
    resp = await authed_admin_client.get("/admin/tasks", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "tasks" in captured, (
        "the TemplateResponse spy did not capture _tasks_table.html — "
        "check the HX-Request header and the template name"
    )
    by_id = {t["id"]: t for t in captured["tasks"]}
    assert by_id[parent_id]["parent_task_id"] is None
    assert by_id[parent_id]["parent_params_changed"] is None
    assert by_id[identical_id]["parent_task_id"] == parent_id
    assert by_id[identical_id]["parent_params_changed"] is False
    assert by_id[modified_id]["parent_task_id"] == parent_id
    assert by_id[modified_id]["parent_params_changed"] is True


@pytest.mark.asyncio
async def test_tasks_partial_renders_fork_parent_link(
    authed_admin_client: AsyncClient,
) -> None:
    """Forked row → link to the parent; modified fork → 'modified' marker;
    task without a parent → no fork sub-line."""
    # Create the parent with an empty payload so that the identical fork (same empty
    # payload and same target=1) does not differ in any field compared by
    # _params_changed.
    resp = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert resp.status_code == 200, resp.text
    parent_id = int(resp.json()["task_id"])

    resp = await authed_admin_client.post(f"/admin/tasks/{parent_id}/fork", json={"target": "1"})
    assert resp.status_code == 200, resp.text
    identical_id = int(resp.json()["task_id"])

    resp = await authed_admin_client.post(f"/admin/tasks/{parent_id}/fork", json={"target": "3"})
    assert resp.status_code == 200, resp.text
    modified_id = int(resp.json()["task_id"])

    body = (await authed_admin_client.get("/admin/tasks", headers={"HX-Request": "true"})).text

    # Identical fork: link to the parent inside .fork-inline, without a "modified" marker.
    identical_row = _row_html(body, identical_id)
    assert "fork-inline" in identical_row
    assert f'href="/admin/tasks/{parent_id}"' in identical_row
    assert "modified" not in identical_row

    # Modified fork: link + "modified" marker.
    modified_row = _row_html(body, modified_id)
    assert "fork-inline" in modified_row
    assert f'href="/admin/tasks/{parent_id}"' in modified_row
    assert "modified" in modified_row

    # Task without a parent: it does not render the fork sub-line.
    parent_row = _row_html(body, parent_id)
    assert "fork-inline" not in parent_row


@pytest.mark.asyncio
async def test_workers_deck_query_executes_with_new_columns(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Verifies that _SQL_LIST_WORKERS includes cpu_pct and active_task_id without
    breaking the endpoint. Exercises the new subselects with real data: a
    worker with cpu_pct set and a running execution.

    The markup (that "73" and "CPU" appear in the HTML) is covered by the tests of
    the worker card further below.
    """
    worker_id = await _register(authed_admin_client, hostname="cpu-query-host")

    # We set cpu_pct to 73 so the subselect does not return NULL.
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET cpu_pct = 73 WHERE id = :id",
        {"id": worker_id},
    )

    # We create a task so we can reference it in the execution.
    task_id = await _create_task(authed_admin_client, command="echo query-test")

    # We insert a running execution for the worker: it exercises the subselect
    # of active_task_id. NOT NULL columns without a default: task_id, worker_id,
    # attempt_index (server_default=1), snapshot_epoch (server_default=0),
    # status (server_default='assigned'), assigned_at (server_default=now()),
    # last_heartbeat_at (server_default=now()), attempts (server_default=1).
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO task_executions (id, task_id, worker_id, attempt_index, "
        "snapshot_epoch, status) VALUES (gen_random_uuid(), :task_id, "
        "CAST(:worker_id AS uuid), 0, 0, 'running')",
        {"task_id": task_id, "worker_id": worker_id},
    )

    # The endpoint must answer 200: the new subselects run without error.
    resp = await authed_admin_client.get("/admin/workers")
    assert resp.status_code == 200


def test_load_tier_thresholds() -> None:
    from orcaster_server.api.admin_ui import _load_tier

    assert _load_tier(None) == "none"
    assert _load_tier(0) == "low"
    assert _load_tier(50) == "low"
    assert _load_tier(51) == "mid"
    assert _load_tier(80) == "mid"
    assert _load_tier(81) == "high"
    assert _load_tier(100) == "high"


# === Worker card (Disk ring + RAM/CPU bars + task link) ========================


@pytest.mark.asyncio
async def test_workers_card_uses_name_over_hostname(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id = await _register(authed_admin_client, hostname="raw-host")
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET name = 'pretty-name' WHERE id = :id",
        {"id": worker_id},
    )
    resp = await authed_admin_client.get("/admin/workers")
    body = resp.text
    assert "pretty-name" in body
    assert "raw-host" not in body


@pytest.mark.asyncio
async def test_workers_card_links_active_task(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id = await _register(authed_admin_client, hostname="busy-host")
    task_id = await _create_task(authed_admin_client)
    # We force an active execution of this task on this worker.
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO task_executions (id, task_id, worker_id, attempt_index, "
        "snapshot_epoch, status) VALUES (gen_random_uuid(), :t, :w, 0, 0, 'running')",
        {"t": task_id, "w": worker_id},
    )
    resp = await authed_admin_client.get("/admin/workers")
    body = resp.text
    assert f"/admin/tasks/{task_id}" in body


@pytest.mark.asyncio
async def test_workers_card_keeps_stable_id(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id = await _register(authed_admin_client, hostname="morph-host")
    resp = await authed_admin_client.get("/admin/workers")
    assert f'id="worker-card-{worker_id}"' in resp.text


@pytest.mark.asyncio
async def test_workers_card_idle_when_no_active_task(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    await _register(authed_admin_client, hostname="idle-host")
    resp = await authed_admin_client.get("/admin/workers")
    assert "idle" in resp.text.lower()


@pytest.mark.asyncio
async def test_workers_card_renders_cpu_pct(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id = await _register(authed_admin_client, hostname="cpu-render-host")
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET cpu_pct = 73 WHERE id = :id",
        {"id": worker_id},
    )
    resp = await authed_admin_client.get("/admin/workers")
    body = resp.text
    assert resp.status_code == 200
    assert "73%" in body
    assert "CPU" in body


@pytest.mark.asyncio
async def test_workers_card_shows_dash_when_cpu_unknown(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    # The freshly registered worker has cpu_pct=NULL by default.
    await _register(authed_admin_client, hostname="cpu-null-host")
    resp = await authed_admin_client.get("/admin/workers")
    body = resp.text
    assert resp.status_code == 200
    assert "CPU" in body
    # When cpu_pct is None the template emits micro_bar("CPU", 0, "none", "—", muted=true),
    # which produces class="worker-bar load-none". We check the load-none class inside
    # a worker-bar because the "—" also appears in the empty disk ring;
    # load-none in a worker-bar is exclusive to a metric with no data (CPU or RAM without data).
    assert "worker-bar load-none" in body


@pytest.mark.asyncio
async def test_workers_card_shows_version(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    # The worker version lives in its own component. The deck shows it
    # per card (`v{version}`) to compare the fleet at a glance against the server one.
    worker_id = await _register(authed_admin_client, hostname="ver-host")
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET version = '9.9.9' WHERE id = :id",
        {"id": worker_id},
    )
    resp = await authed_admin_client.get("/admin/workers")
    body = resp.text
    assert resp.status_code == 200
    assert "v9.9.9" in body
    # 9.9.9 reaches the task secret floor; no warning.
    assert "No task secrets" not in body


@pytest.mark.asyncio
async def test_workers_card_flags_worker_below_task_secrets_floor(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    # Below the floor, the server does not hand it tasks that use
    # stored secrets; the card warns about it next to the version.
    worker_id = await _register(authed_admin_client, hostname="old-host")
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET version = '0.46.0' WHERE id = :id",
        {"id": worker_id},
    )
    resp = await authed_admin_client.get("/admin/workers")
    body = resp.text
    assert resp.status_code == 200
    assert "v0.46.0" in body
    assert "No task secrets" in body
    assert "Below 0.47.0: tasks that use stored secrets are not delivered to this worker" in body


@pytest.mark.asyncio
async def test_workers_card_shows_muted_fallback_when_version_null(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    # A freshly registered worker without a reported version (REGISTER does not carry it): the
    # card shows the muted fallback, mirroring the worker detail.
    await _register(authed_admin_client, hostname="no-ver-host")
    resp = await authed_admin_client.get("/admin/workers")
    body = resp.text
    assert resp.status_code == 200
    assert "no version reported" in body


@pytest.mark.asyncio
async def test_task_detail_shows_outcome_badge(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Task detail shows an outcome badge when present."""
    task_id = await _create_task(authed_admin_client, command="echo outcome")

    # Update the task to completed with outcome='no_result' via SQL
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET status = 'completed', outcome = 'no_result' WHERE id = :id",
        {"id": task_id},
    )

    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    assert resp.status_code == 200
    body = resp.text
    assert f"#{task_id}" in body
    assert "badge-outcome" in body
    assert "no_result" in body


# === tz note inside #executions-block (hx-preserve) ==========================
# The note used to live outside #executions-block, so the first execution
# arriving by polling (hx-select="#executions-block") never showed it
# until the full page was reloaded. It now lives inside the block with
# hx-preserve, only when there are executions.


@pytest.mark.asyncio
async def test_task_detail_with_executions_renders_tz_note_inside_block(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id = await _register(authed_admin_client, hostname="tz-note-worker")
    task_id = await _create_task(authed_admin_client, command="echo tz")
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO task_executions (task_id, worker_id, status) "
        "VALUES (:tid, :wid, CAST(:st AS execution_status))",
        {"tid": task_id, "wid": worker_id, "st": "completed"},
    )
    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="tz-note-executions"' in body
    assert 'hx-preserve="true"' in body
    # The note must stay INSIDE #executions-block: after the </header>
    # that closes executions-head, and before the table <figure>. If it ended up
    # outside the block again, this order would break.
    idx_block = body.index('id="executions-block"')
    idx_header_close = body.index("</header>", idx_block)
    idx_note = body.index("js-tz-note", idx_block)
    idx_figure = body.index("<figure", idx_block)
    assert idx_header_close < idx_note < idx_figure


@pytest.mark.asyncio
async def test_task_detail_without_executions_omits_tz_note(
    authed_admin_client: AsyncClient,
) -> None:
    task_id = await _create_task(authed_admin_client, command="echo tz-empty")
    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    assert resp.status_code == 200
    body = resp.text
    assert "No executions yet" in body
    assert "js-tz-note" not in body


@pytest.mark.asyncio
async def test_worker_detail_with_executions_renders_tz_note_inside_block(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id = await _register(authed_admin_client, hostname="tz-note-worker-2")
    task_id = await _create_task(authed_admin_client, command="echo tz-worker")
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO task_executions (task_id, worker_id, status) "
        "VALUES (:tid, :wid, CAST(:st AS execution_status))",
        {"tid": task_id, "wid": worker_id, "st": "completed"},
    )
    resp = await authed_admin_client.get(f"/admin/workers/{worker_id}")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="tz-note-executions"' in body
    assert 'hx-preserve="true"' in body
    idx_block = body.index('id="executions-block"')
    idx_header_close = body.index("</header>", idx_block)
    idx_note = body.index("js-tz-note", idx_block)
    idx_figure = body.index("<figure", idx_block)
    assert idx_header_close < idx_note < idx_figure


@pytest.mark.asyncio
async def test_worker_detail_without_executions_omits_tz_note(
    authed_admin_client: AsyncClient,
) -> None:
    worker_id = await _register(authed_admin_client, hostname="tz-note-worker-empty")
    resp = await authed_admin_client.get(f"/admin/workers/{worker_id}")
    assert resp.status_code == 200
    body = resp.text
    assert "No executions assigned" in body
    assert "js-tz-note" not in body


# === tz note in the Forks section ==========================================
# The Forks section lives outside #executions-block (outside the <div hx-get=...
# hx-trigger="every 3s"> and its hx-select-oob), so it is never re-rendered
# by polling: it needs no id/hx-preserve. These tests anchor that the note
# appears there (gated by fork_children) and that it does not duplicate ids with the one in
# Executions.


@pytest.mark.asyncio
async def test_task_detail_with_fork_children_renders_tz_note_in_forks_section(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id = await _register(authed_admin_client, hostname="tz-note-worker-forks")
    parent_id = await _create_task(authed_admin_client, command="echo parent")
    # Executions also present: this way we verify that the TWO notes (the one in
    # Executions with id/hx-preserve and the one in Forks without id) coexist without clashing.
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO task_executions (task_id, worker_id, status) "
        "VALUES (:tid, :wid, CAST(:st AS execution_status))",
        {"tid": parent_id, "wid": worker_id, "st": "completed"},
    )
    fork_resp = await authed_admin_client.post(
        f"/admin/tasks/{parent_id}/fork", json={"target": "1"}
    )
    assert fork_resp.status_code == 200, fork_resp.text

    resp = await authed_admin_client.get(f"/admin/tasks/{parent_id}")
    assert resp.status_code == 200
    body = resp.text

    # Page with executions AND forks -> TWO js-tz-note notes: one with an id
    # ("tz-note-executions" + hx-preserve) and another without an id, in Forks. If
    # someone added tz_note_id to the Forks one we would have a duplicate id.
    assert body.count("js-tz-note") == 2
    assert body.count('id="tz-note-executions"') == 1

    idx_forks_section = body.index('aria-label="Forks"')
    idx_forks_header_close = body.index("</header>", idx_forks_section)
    idx_forks_figure = body.index("<figure", idx_forks_section)
    idx_note_open = body.index('<p class="tz-note js-tz-note"', idx_forks_header_close)
    idx_note_close = body.index(">", idx_note_open)
    assert idx_forks_header_close < idx_note_open < idx_forks_figure
    # The Forks note carries no id or hx-preserve: the section is outside the
    # block that polls every 3s and does not need to anchor the local text.
    note_tag = body[idx_note_open:idx_note_close]
    assert "id=" not in note_tag
    assert "hx-preserve" not in note_tag


@pytest.mark.asyncio
async def test_task_detail_with_executions_and_no_forks_has_single_tz_note(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """With executions but no fork children, only the Executions note must
    appear (with id + hx-preserve). Forks must not add a second note
    or duplicate the id "tz-note-executions" when there are no children."""
    worker_id = await _register(authed_admin_client, hostname="tz-note-worker-no-forks")
    task_id = await _create_task(authed_admin_client, command="echo tz-no-forks")
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO task_executions (task_id, worker_id, status) "
        "VALUES (:tid, :wid, CAST(:st AS execution_status))",
        {"tid": task_id, "wid": worker_id, "st": "completed"},
    )
    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    assert resp.status_code == 200
    body = resp.text
    assert "Forks (0)" in body
    assert body.count("js-tz-note") == 1
    assert body.count('id="tz-note-executions"') == 1


# === Pins the anti-flicker placement =======================================
# These includes of _tz_note.html were not anchored by any test: if
# someone moved the include from a page shell into the partial that
# polls (or vice versa), the note would flicker again every 3-5s without anything
# detecting it.


@pytest.mark.asyncio
async def test_admin_tasks_partial_omits_tz_note(authed_admin_client: AsyncClient) -> None:
    """_tasks_table.html polls every 5s (hx-trigger="load, every 5s [...],
    refresh"); the note lives in dashboard.html (the shell), outside
    #tasks-panel. If the include migrated to the partial, the "UTC" fallback
    would overwrite the local text on every poll."""
    resp = await authed_admin_client.get("/admin/tasks")
    assert resp.status_code == 200
    assert "js-tz-note" not in resp.text


@pytest.mark.asyncio
async def test_dashboard_shell_includes_tz_note(authed_admin_client: AsyncClient) -> None:
    """Counterpart of the previous test: the shell (dashboard.html) does carry the
    note, and before #tasks-panel (outside the block that polls)."""
    resp = await authed_admin_client.get("/admin/")
    assert resp.status_code == 200
    body = resp.text
    idx_note = body.index("js-tz-note")
    idx_panel = body.index('id="tasks-panel"')
    assert idx_note < idx_panel


@pytest.mark.asyncio
async def test_admin_actions_partial_omits_tz_note(authed_admin_client: AsyncClient) -> None:
    """_admin_actions_table.html polls every 3s (hx-trigger="change, submit,
    every 3s"); the note lives in admin_actions.html (the shell). If the include
    migrated to the partial, it would flicker on every poll/filter."""
    resp = await authed_admin_client.get("/admin/actions/partial")
    assert resp.status_code == 200
    assert "js-tz-note" not in resp.text


@pytest.mark.asyncio
async def test_admin_actions_page_includes_tz_note(authed_admin_client: AsyncClient) -> None:
    """Counterpart: the audit log shell does carry the note."""
    resp = await authed_admin_client.get("/admin/actions")
    assert resp.status_code == 200
    assert "js-tz-note" in resp.text
