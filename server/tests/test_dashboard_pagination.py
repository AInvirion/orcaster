"""Dashboard pagination tests (backend LIMIT/OFFSET)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.test_dashboard import _create_task


@pytest.mark.asyncio
async def test_page_size_default_50(authed_admin_client: AsyncClient) -> None:
    """Without page_size, returns 50 rows when there are more than 50 tasks."""
    for i in range(55):
        await _create_task(authed_admin_client, command=f"cmd-default-{i}")
    resp = await authed_admin_client.get("/admin/tasks", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text
    assert body.count('class="task-row"') == 50


@pytest.mark.asyncio
async def test_page_size_whitelist_25(authed_admin_client: AsyncClient) -> None:
    """page_size=25 returns exactly 25 rows when there are more than 25."""
    for i in range(30):
        await _create_task(authed_admin_client, command=f"cmd-ps25-{i}")
    resp = await authed_admin_client.get(
        "/admin/tasks?page_size=25", headers={"HX-Request": "true"}
    )
    assert resp.status_code == 200
    assert resp.text.count('class="task-row"') == 25


@pytest.mark.asyncio
async def test_page_size_100(authed_admin_client: AsyncClient) -> None:
    """page_size=100 returns exactly 100 rows when there are more than 100."""
    for i in range(105):
        await _create_task(authed_admin_client, command=f"cmd-ps100-{i}")
    resp = await authed_admin_client.get(
        "/admin/tasks?page_size=100", headers={"HX-Request": "true"}
    )
    assert resp.status_code == 200
    assert resp.text.count('class="task-row"') == 100


@pytest.mark.asyncio
async def test_page_size_invalid_falls_back_to_50(authed_admin_client: AsyncClient) -> None:
    """page_size outside the whitelist {25, 50, 100} → fallback to 50."""
    for i in range(55):
        await _create_task(authed_admin_client, command=f"cmd-invalid-ps-{i}")
    resp = await authed_admin_client.get(
        "/admin/tasks?page_size=999", headers={"HX-Request": "true"}
    )
    assert resp.status_code == 200
    assert resp.text.count('class="task-row"') == 50


@pytest.mark.asyncio
async def test_page2_no_overlap_with_page1(authed_admin_client: AsyncClient) -> None:
    """page=2 returns the second block without overlapping page=1."""
    for i in range(60):
        await _create_task(authed_admin_client, command=f"overlap-cmd-{i:03d}")

    resp1 = await authed_admin_client.get(
        "/admin/tasks?page=1&page_size=25&sort=created_at&dir=asc",
        headers={"HX-Request": "true"},
    )
    resp2 = await authed_admin_client.get(
        "/admin/tasks?page=2&page_size=25&sort=created_at&dir=asc",
        headers={"HX-Request": "true"},
    )
    assert resp1.status_code == 200
    assert resp2.status_code == 200

    import re

    ids1 = set(re.findall(r'id="task-row-(\d+)"', resp1.text))
    ids2 = set(re.findall(r'id="task-row-(\d+)"', resp2.text))
    assert len(ids1) == 25
    assert len(ids2) == 25
    assert ids1.isdisjoint(ids2), f"Solapamiento detectado: {ids1 & ids2}"


@pytest.mark.asyncio
async def test_page_excess_clamps_to_last_page(authed_admin_client: AsyncClient) -> None:
    """An excessive page (> total_pages) returns the last page and the hidden OOB."""
    for i in range(30):
        await _create_task(authed_admin_client, command=f"clamp-cmd-{i}")
    resp = await authed_admin_client.get(
        "/admin/tasks?page=999&page_size=25", headers={"HX-Request": "true"}
    )
    assert resp.status_code == 200
    body = resp.text
    assert body.count('class="task-row"') == 5
    assert 'hx-swap-oob="true"' in body
    assert 'id="filter-page-val"' in body
    assert 'value="2"' in body


@pytest.mark.asyncio
async def test_total_pages_without_filters(authed_admin_client: AsyncClient) -> None:
    """Correct total_pages with no active filters."""
    for i in range(55):
        await _create_task(authed_admin_client, command=f"tp-nofilt-{i}")
    resp = await authed_admin_client.get(
        "/admin/tasks?page_size=25", headers={"HX-Request": "true"}
    )
    assert resp.status_code == 200
    assert "Page 1 of 3" in resp.text


@pytest.mark.asyncio
async def test_total_pages_with_filters(authed_admin_client: AsyncClient) -> None:
    """Correct total_pages with active filters (uses filtered_count)."""
    for i in range(30):
        await _create_task(authed_admin_client, command=f"tp-filt-match-{i}")
    for i in range(30):
        await _create_task(authed_admin_client, command=f"other-cmd-{i}")
    resp = await authed_admin_client.get(
        "/admin/tasks?q=tp-filt-match&page_size=25", headers={"HX-Request": "true"}
    )
    assert resp.status_code == 200
    assert "Page 1 of 2" in resp.text


@pytest.mark.asyncio
async def test_pagination_footer_rendered(authed_admin_client: AsyncClient) -> None:
    """The pagination footer appears in the partial HTML."""
    for i in range(5):
        await _create_task(authed_admin_client, command=f"footer-smoke-{i}")
    resp = await authed_admin_client.get("/admin/tasks", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text
    assert 'class="pagination-footer"' in body
    # Decorative arrows in aria-hidden spans; the button text is "Prev"/"Next".
    assert '<span aria-hidden="true">←</span>Prev' in body
    assert 'Next<span aria-hidden="true">→</span>' in body
    assert "Page 1 of 1" in body
    assert 'class="btn-secondary pagination-prev"' in body
    # With 1 page, Prev and Next must be disabled (native disabled, without
    # redundant aria-disabled).
    assert body.count("disabled") >= 2
    # The live region was removed from the indicator (noise under the 3 s polling).
    assert 'class="pagination-indicator"' in body
    assert "aria-live" not in body.split('class="pagination-indicator"')[1][:80]
