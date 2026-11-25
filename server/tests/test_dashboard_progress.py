"""Dashboard progress bar over the tasks table.

`compute_progress` is the math shared with the public status page; the
`/admin/tasks` partial emits it out-of-band in `#tasks-progress`. The bar follows
the version lens; status/outcome/search/artifact do not apply. The note always
declares the measured version and the cancelled tasks excluded from the total.
"""

import re

import pytest
from httpx import AsyncClient

from orcaster_server.status.snapshot import compute_progress
from tests.test_dashboard import _create_task, _exec_sql

_HX = {"HX-Request": "true"}
_FILTER_NOTE = "Status, outcome, search and artifact filters do not apply to progress."
_CURRENT_NOTE = "Scope: current version of each tool."
# The tasks from `_create_task` carry no variant: they fall in the `other` bucket.
_CURRENT_OTHER_NOTE = "Scope: current version of each tool, plus unversioned tasks."


async def _progress_block(client: AsyncClient, **params) -> str:
    """Return only the #tasks-progress OOB block of the tasks partial."""
    r = await client.get("/admin/tasks", headers=_HX, params=params)
    assert r.status_code == 200, r.text
    m = re.search(
        r'<div id="tasks-progress" class="tasks-progress" hx-swap-oob="true">(.*?)</div>\s*'
        r'<figure class="table-wrap tasks-table-wrap">',
        r.text,
        re.S,
    )
    assert m, "missing OOB block #tasks-progress"
    return m.group(1)


def _copy(block: str) -> str:
    """Progress heading text, without tags or repeated whitespace."""
    m = re.search(r'<span class="progress-copy">(.*?)</span>', block, re.S)
    assert m, block
    text_ = re.sub(r"<[^>]+>", "", m.group(1)).replace("&middot;", "·")
    return " ".join(text_.split())


def _note(block: str) -> str:
    m = re.search(r'<p class="progress-note progress-scope-note">([^<]*)</p>', block)
    assert m, block
    return m.group(1)


def _pct(block: str) -> str:
    m = re.search(r'<span class="progress-pct">([^<]*)</span>', block)
    assert m, block
    return m.group(1)


# --- compute_progress (unidad) ---


def test_compute_progress_empty() -> None:
    p = compute_progress({})
    assert p.progress_denominator == 0
    assert p.processed_count == 0
    assert p.progress_pct == 0.0
    assert p.processed_pct == 0.0


def test_compute_progress_partial_excludes_cancelled() -> None:
    p = compute_progress({"queued": 2, "completed": 3, "failed": 1, "cancelled": 4})
    assert p.progress_denominator == 6
    assert p.completed_count == 3
    assert p.failed_count == 1
    assert p.processed_count == 4
    assert p.progress_pct == 50.0
    assert p.processed_pct == 66.7


def test_compute_progress_caps_below_100_with_pending_work() -> None:
    p = compute_progress({"completed": 9999, "queued": 1})
    assert p.progress_pct == 99.9
    assert p.processed_pct == 99.9


def test_compute_progress_complete() -> None:
    p = compute_progress({"completed": 2, "failed": 1, "cancelled": 5})
    assert p.processed_pct == 100.0
    assert p.progress_pct == 66.7


# --- Rendering in the dashboard ---


@pytest.mark.asyncio
async def test_dashboard_has_empty_progress_slot(authed_admin_client: AsyncClient) -> None:
    r = await authed_admin_client.get("/admin/")
    assert r.status_code == 200, r.text
    assert '<div id="tasks-progress" class="tasks-progress"></div>' in r.text


@pytest.mark.asyncio
async def test_progress_empty_workspace(authed_admin_client: AsyncClient) -> None:
    block = await _progress_block(authed_admin_client)
    assert "No tasks yet" in block
    assert 'role="progressbar"' not in block
    assert _note(block) == _CURRENT_NOTE


@pytest.mark.asyncio
async def test_progress_partial(authed_admin_client: AsyncClient, migrated_db_url: str) -> None:
    ids = [await _create_task(authed_admin_client) for _ in range(4)]
    await _exec_sql(
        migrated_db_url, "UPDATE tasks SET status = 'completed' WHERE id = :id", {"id": ids[0]}
    )
    await _exec_sql(
        migrated_db_url, "UPDATE tasks SET status = 'failed' WHERE id = :id", {"id": ids[1]}
    )
    await _exec_sql(
        migrated_db_url, "UPDATE tasks SET status = 'cancelled' WHERE id = :id", {"id": ids[2]}
    )
    block = await _progress_block(authed_admin_client)
    # cancelled is left out of the denominator: 2 processed of 3.
    assert _copy(block) == "2 of 3 tasks processed (1 completed · 1 failed)"
    assert _pct(block) == "66.7%"
    assert 'aria-valuenow="66.7"' in block
    assert "All tasks processed" not in block
    # Without filters the note still declares the version, and the cancelled tasks that the
    # subtitle counts but the bar does not.
    assert _note(block) == f"{_CURRENT_OTHER_NOTE} 1 cancelled excluded from the total."
    # stable ids on the segments: htmx settles the style and the transition animates.
    assert 'id="progress-segment-failed"' in block
    assert 'id="progress-segment-completed"' in block


@pytest.mark.asyncio
async def test_progress_complete(authed_admin_client: AsyncClient, migrated_db_url: str) -> None:
    ids = [await _create_task(authed_admin_client) for _ in range(3)]
    await _exec_sql(
        migrated_db_url, "UPDATE tasks SET status = 'completed' WHERE id = ANY(:ids)", {"ids": ids}
    )
    block = await _progress_block(authed_admin_client)
    assert _copy(block) == "3 of 3 tasks processed (3 completed · 0 failed)"
    assert _pct(block) == "100.0%"
    assert "All tasks processed" in block


@pytest.mark.asyncio
async def test_progress_ignores_filters_and_says_so(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    ids = [await _create_task(authed_admin_client) for _ in range(2)]
    await _exec_sql(
        migrated_db_url, "UPDATE tasks SET status = 'completed' WHERE id = :id", {"id": ids[0]}
    )
    for params in ({"status": "queued"}, {"q": "no-match-anywhere"}, {"has_artifacts": "yes"}):
        block = await _progress_block(authed_admin_client, **params)
        # The filter reduces the rows, not the bar: it stays at 1 of 2.
        assert _copy(block) == "1 of 2 tasks processed (1 completed · 0 failed)", params
        assert _FILTER_NOTE in block, params


@pytest.mark.asyncio
async def test_progress_follows_version_lens(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The bar rescales with the version selector (default current = latest
    of each tool); a regression that summed all variants fails here."""
    old = [await _create_task(authed_admin_client) for _ in range(2)]
    new = [await _create_task(authed_admin_client) for _ in range(3)]
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET variant_kind = 'example-tool', variant_label = '0.7.5', "
        "status = 'completed' WHERE id = ANY(:ids)",
        {"ids": old},
    )
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET variant_kind = 'example-tool', variant_label = '0.8.0' "
        "WHERE id = ANY(:ids)",
        {"ids": new},
    )
    await _exec_sql(
        migrated_db_url, "UPDATE tasks SET status = 'failed' WHERE id = :id", {"id": new[0]}
    )

    # Default current: only 0.8.0 (1 failed of 3).
    block = await _progress_block(authed_admin_client)
    assert _copy(block) == "1 of 3 tasks processed (0 completed · 1 failed)"
    assert _note(block) == _CURRENT_NOTE

    block = await _progress_block(authed_admin_client, variant="example-tool~0.7.5")
    assert _copy(block) == "2 of 2 tasks processed (2 completed · 0 failed)"
    assert _note(block) == "Scope: example-tool 0.7.5 only."

    block = await _progress_block(authed_admin_client, variant="all")
    assert _copy(block) == "3 of 5 tasks processed (2 completed · 1 failed)"
    assert _note(block) == "Scope: all versions."

    block = await _progress_block(authed_admin_client, variant="example-tool")
    assert _copy(block) == "3 of 5 tasks processed (2 completed · 1 failed)"
    assert _note(block) == "Scope: all example-tool versions."
