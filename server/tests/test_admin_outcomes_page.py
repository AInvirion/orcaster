"""Tests for the outcomes dashboard view: rename of /failures to /outcomes +
terminal status filters (failed/completed/cancelled/all) and outcome filters.
`/admin/failures[/partial]` remains as a 308 redirect preserving the query
string (saved/shared URLs must not break)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import EXAMPLE_KIND
from tests.test_admin_failures import _create_task, _register


def _example_pip_line(version: str) -> str:
    """Install line that the test plugin's extractor recognizes."""
    return f"uv pip install {EXAMPLE_KIND}=={version}"


async def _seed_terminal_task(
    client: AsyncClient,
    db_url: str,
    worker_id: str,
    *,
    status: str,
    outcome: str | None = None,
    exit_code: int | None = None,
    stderr_lines: list[str] | None = None,
    command: str = "run_batch",
    completed_offset: timedelta = timedelta(0),
) -> tuple[int, str]:
    """Seed a terminal task (failed/completed/cancelled) with an optional
    `outcome`, modeled on `_seed_failed_task` but generalized to any terminal
    status."""
    task_id = await _create_task(client, command=command)
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE tasks SET status=:status, outcome=:outcome, "
                    "completed_at = now() - make_interval(secs => :off_secs), "
                    "command=:cmd WHERE id=:t"
                ),
                {
                    "status": status,
                    "outcome": outcome,
                    "off_secs": completed_offset.total_seconds(),
                    "cmd": command,
                    "t": task_id,
                },
            )
            execution_id = str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO task_executions "
                            "(task_id, worker_id, status, attempt_index, exit_code, "
                            " started_at, finished_at) "
                            "VALUES (:t, :w, :status, 1, :ec, now(), now()) "
                            "RETURNING id::text"
                        ),
                        {"t": task_id, "w": worker_id, "status": status, "ec": exit_code},
                    )
                ).scalar_one()
            )
            seq = 1
            for line in stderr_lines or []:
                await conn.execute(
                    text(
                        "INSERT INTO output_events "
                        "(execution_id, seq, ts, stream, text) "
                        "VALUES (CAST(:e AS uuid), :seq, now(), 'stderr', :tx)"
                    ),
                    {"e": execution_id, "seq": seq, "tx": line + "\n"},
                )
                seq += 1
    finally:
        await engine.dispose()
    return task_id, execution_id


@pytest.mark.asyncio
async def test_failures_redirects_308_with_query(authed_admin_client: AsyncClient) -> None:
    r = await authed_admin_client.get("/admin/failures?range=24h&limit=50", follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == "/admin/outcomes?range=24h&limit=50"


@pytest.mark.asyncio
async def test_failures_partial_redirects_308_with_query(
    authed_admin_client: AsyncClient,
) -> None:
    r = await authed_admin_client.get(
        "/admin/failures/partial?range=all&variant=all", follow_redirects=False
    )
    assert r.status_code == 308
    assert r.headers["location"] == "/admin/outcomes/partial?range=all&variant=all"


@pytest.mark.asyncio
async def test_outcomes_default_is_failed(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id = await _register(authed_admin_client, hostname="outcomes-default")
    await _seed_terminal_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        status="failed",
        exit_code=1,
        stderr_lines=["BoomError: seed failure"],
    )
    await _seed_terminal_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        status="completed",
        outcome="empty_result",
    )
    resp = await authed_admin_client.get("/admin/outcomes?range=all")
    assert resp.status_code == 200
    body = resp.text
    # the 'failed' radio comes checked by default
    assert '<input type="radio" name="status" value="failed" checked' in body
    # only the failed task's signature appears; the completed task's outcome is
    # not counted in the (status, outcome) summary (the outcomes <select> still
    # lists "empty_result" as an available option, a separate filter)
    assert "boomerror: seed failure" in body.lower()
    assert "completed · empty_result" not in body


@pytest.mark.asyncio
async def test_outcomes_status_completed_shows_outcome(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id = await _register(authed_admin_client, hostname="outcomes-completed")
    await _seed_terminal_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        status="completed",
        outcome="empty_result",
    )
    resp = await authed_admin_client.get(
        "/admin/outcomes/partial?range=all&status=completed&outcome=empty_result"
    )
    assert resp.status_code == 200
    assert "empty_result" in resp.text


# The retired status alias in `status` is covered, with and without a plugin,
# in `tests/test_legacy_status_alias.py`.


@pytest.mark.asyncio
async def test_outcomes_status_all_adds_terminal_states(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id = await _register(authed_admin_client, hostname="outcomes-all")
    await _seed_terminal_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        status="failed",
        exit_code=1,
        stderr_lines=["AllError: seed"],
    )
    await _seed_terminal_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        status="completed",
        outcome="empty_result",
    )
    await _seed_terminal_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        status="cancelled",
    )
    resp = await authed_admin_client.get("/admin/outcomes/partial?range=all&status=all")
    assert resp.status_code == 200
    assert '<span class="failures-total">3</span>' in resp.text


# === Follow-up — tz note only in the shell, never in the partial ===============
# outcomes.html (shell) includes _tz_note.html with no data gate; _outcomes.html
# (the partial behind hx-get="/admin/outcomes/partial", refreshed by
# "change, submit") must not include it. If someone moved the include into the
# partial, the server-side "UTC" fallback would overwrite the local text again on
# every filter/submit.


@pytest.mark.asyncio
async def test_outcomes_page_includes_tz_note(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.get("/admin/outcomes")
    assert resp.status_code == 200
    assert "js-tz-note" in resp.text


@pytest.mark.asyncio
async def test_outcomes_partial_omits_tz_note(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.get("/admin/outcomes/partial")
    assert resp.status_code == 200
    assert "js-tz-note" not in resp.text


@pytest.mark.asyncio
async def test_outcomes_form_coordinates_and_locks_controls(
    authed_admin_client: AsyncClient,
) -> None:
    """Each `change` fires a GET to the partial; without coordination, rapid
    clicks stack aggregations on the server (aborting the XHR does not cancel
    the query in Postgres) and a dead request leaves the previous filter's data
    under the new controls. The form must declare hx-sync (the last change
    wins) and hx-disabled-elt (controls blocked in flight); the Refresh button
    shares the form's sync scope."""
    resp = await authed_admin_client.get("/admin/outcomes")
    assert resp.status_code == 200
    body = resp.text
    assert 'hx-sync="this:replace"' in body
    # Plain selectors, not `find`: `find X` resolves only the FIRST descendant
    # (querySelector) and there are several preset radios.
    assert (
        'hx-disabled-elt="#failures-filter input, #failures-filter select, '
        '#failures-filter button"' in body
    )
    # The form is in its own hx-indicator: .htmx-request on the form is the CSS
    # hook that dims the presets (the radio input is invisible and the disabled
    # state is not visible on its own).
    assert 'hx-indicator="this, #failures-loading"' in body
    assert 'hx-sync="#failures-filter:replace"' in body


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_outcomes_select_lists_variants_outside_the_window(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The Version <select> offers ALL the workspace's variants (same source as
    the dashboard), not only those with terminal tasks within the current
    window/status. Previously, with the defaults (failed + today), a version
    that only failed before today was not selectable."""
    worker_id = await _register(authed_admin_client, hostname="outcomes-vocab")
    # Old variant: failed 2 days ago (outside the default `today` window).
    await _seed_terminal_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        status="failed",
        exit_code=1,
        stderr_lines=["OldError: archived version"],
        command=_example_pip_line("0.7.1"),
        completed_offset=timedelta(days=2),
    )
    # New variant: failed today (the only one visible under the old behavior).
    await _seed_terminal_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        status="failed",
        exit_code=1,
        stderr_lines=["NewError: current version"],
        command=_example_pip_line("0.7.5"),
    )
    resp = await authed_admin_client.get("/admin/outcomes")  # defaults: today+failed
    assert resp.status_code == 200
    body = resp.text
    assert 'value="example-tool~0.7.5"' in body
    # The archived variant MUST be an option even though it did not fail today.
    assert 'value="example-tool~0.7.1"' in body


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_outcomes_lens_current_keeps_resolving_over_terminal_tasks(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The default `current` lens is still resolved over the variants present
    AMONG THE TERMINAL TASKS of the current status/window, NOT over the whole
    workspace: if today only an old version is failing, the default page must
    show it (not be empty because the global latest did not fail)."""
    worker_id = await _register(authed_admin_client, hostname="outcomes-current")
    # The ONLY failed task today is from the old version.
    await _seed_terminal_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        status="failed",
        exit_code=1,
        stderr_lines=["BurningError: old version on fire"],
        command=_example_pip_line("0.7.1"),
    )
    # The newest version exists in the workspace but has NO failed task.
    await _seed_terminal_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        status="completed",
        outcome="extracted",
        command=_example_pip_line("0.7.5"),
    )
    resp = await authed_admin_client.get("/admin/outcomes")  # defaults: today+failed+current
    assert resp.status_code == 200
    assert "burningerror: old version on fire" in resp.text.lower()
    # 0.7.5 is still an option of the select (workspace-wide vocabulary), even
    # though the STATUS axis (completed, not failed) excludes it from the current lens.
    assert 'value="example-tool~0.7.5"' in resp.text


def _variant_select_html(body: str) -> str:
    """Extract the Version <select> from the page HTML.

    The HTMX swap of `#failures-block` does NOT re-render this control (it lives
    in `outcomes.html`, outside the hx-target), so EVERYTHING it shows must be
    invariant to the filters or the user sees data from the initial render while
    the table has already changed. The tests compare the whole block across radios
    to verify that invariant in one go.
    """
    start = body.index('<select name="variant"')
    end = body.index("</select>", start)
    return body[start:end]


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_outcomes_select_is_invariant_to_the_status_radio(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The `· N` and the `(current)` hint do NOT depend on the status radio: the
    count is the all-time TERMINAL tasks of that version (all three statuses
    together) and the hint is the latest WITH terminal activity.

    The select is not re-rendered on the HTMX swap, so any data that followed
    the radio would go stale as soon as the operator changes status: the table
    would show `completed` while the select kept the `failed` numbers. The whole
    block is compared across two radios to pin this down."""
    worker_id = await _register(authed_admin_client, hostname="outcomes-counts")
    for stderr in ("CountError: one", "CountError: two"):
        await _seed_terminal_task(
            authed_admin_client,
            migrated_db_url,
            worker_id,
            status="failed",
            exit_code=1,
            stderr_lines=[stderr],
            command=_example_pip_line("0.7.1"),
        )
    await _seed_terminal_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        status="completed",
        outcome="extracted",
        command=_example_pip_line("0.7.5"),
    )
    resp = await authed_admin_client.get("/admin/outcomes")  # defaults: today+failed
    assert resp.status_code == 200
    select = _variant_select_html(resp.text)
    # Count = all-time terminal tasks of the version, whatever the radio.
    assert ">0.7.1 &middot; 2</option>" in select
    # Hint on the latest WITH terminal activity, even if its only task is not failed.
    assert ">0.7.5 (current) &middot; 1</option>" in select
    # Changing the radio does not move a single character of the control.
    resp2 = await authed_admin_client.get("/admin/outcomes?status=completed")
    assert resp2.status_code == 200
    assert _variant_select_html(resp2.text) == select
    resp3 = await authed_admin_client.get("/admin/outcomes?status=all")
    assert resp3.status_code == 200
    assert _variant_select_html(resp3.text) == select


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_outcomes_select_is_invariant_to_the_window(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """It does not depend on the time window either: the count is all-time and the
    hint comes from the same set. A different `range` rescales the table, not
    the control (which the HTMX swap does not redraw).

    The only terminal task INSIDE the default window is the old version, so a
    hint tied to the lens would mark `0.7.1 (current)` with `today` and jump to
    `0.7.5` with `range=all`. The invariant is precisely that it does not jump."""
    worker_id = await _register(authed_admin_client, hostname="outcomes-window")
    await _seed_terminal_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        status="failed",
        exit_code=1,
        stderr_lines=["WindowError: today"],
        command=_example_pip_line("0.7.1"),
    )
    await _seed_terminal_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        status="failed",
        exit_code=1,
        stderr_lines=["WindowError: two days ago"],
        command=_example_pip_line("0.7.5"),
        completed_offset=timedelta(days=2),
    )
    today = await authed_admin_client.get("/admin/outcomes")  # default window: today
    all_time = await authed_admin_client.get("/admin/outcomes?range=all")
    assert today.status_code == 200 and all_time.status_code == 200
    select = _variant_select_html(today.text)
    assert ">0.7.1 &middot; 1</option>" in select
    # Outside the window: it still counts and is still the catalog's current.
    assert ">0.7.5 (current) &middot; 1</option>" in select
    assert _variant_select_html(all_time.text) == select


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_outcomes_hint_current_ignores_versions_without_terminal_tasks(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The `(current)` hint comes from the variants WITH at least one terminal
    task, not from the bare vocabulary: a freshly queued version (all its tasks
    in flight) appears as an option with `· 0`, but does not get the
    `(current)` — the outcomes page only talks about tasks that have already
    finished."""
    worker_id = await _register(authed_admin_client, hostname="outcomes-queued")
    await _seed_terminal_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        status="failed",
        exit_code=1,
        stderr_lines=["QueuedError: terminal one"],
        command=_example_pip_line("0.7.5"),
    )
    # Newest version, but without a single terminal task yet.
    await _create_task(authed_admin_client, command=_example_pip_line("0.8.0"))
    resp = await authed_admin_client.get("/admin/outcomes")
    assert resp.status_code == 200
    select = _variant_select_html(resp.text)
    assert ">0.8.0 &middot; 0</option>" in select  # selectable...
    assert ">0.8.0 (current)" not in select  # ...but it is not the current
    assert ">0.7.5 (current) &middot; 1</option>" in select
