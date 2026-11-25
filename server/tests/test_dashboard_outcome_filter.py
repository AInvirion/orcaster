"""Outcome filter in the tasks dashboard.

`outcome` is a dimension additional to status: the terminal nuance declared
by the task (see `orcaster_server.task_outcomes`). This test seeds tasks directly
through SQL (status + outcome), modelled on the harness of
`test_dashboard_variant_filter.py`, and covers: CSV filter by `outcome`, 400
validation of malformed outcomes, the inline badge in the row and the dynamic
chips that the full page renders from the outcomes present.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import example_payload
from tests.test_dashboard import _create_task, _exec_sql

_HX = {"HX-Request": "true"}


async def _list(client: AsyncClient, **params) -> str:
    r = await client.get("/admin/tasks", headers=_HX, params=params)
    assert r.status_code == 200, r.text
    return r.text


async def _seed(
    client: AsyncClient,
    db_url: str,
    *,
    status: str,
    outcome: str | None,
    command: str,
) -> int:
    tid = await _create_task(client, command=command)
    await _exec_sql(
        db_url,
        "UPDATE tasks SET status = CAST(:s AS task_status), outcome = :oc WHERE id = :id",
        {"s": status, "oc": outcome, "id": tid},
    )
    return tid


@pytest.mark.asyncio
async def test_partial_filters_by_outcome(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    tid_plain = await _seed(
        authed_admin_client,
        migrated_db_url,
        status="completed",
        outcome=None,
        command="echo plain",
    )
    tid_named = await _seed(
        authed_admin_client,
        migrated_db_url,
        status="completed",
        outcome="empty_result",
        command="echo named",
    )
    tid_failed = await _seed(
        authed_admin_client,
        migrated_db_url,
        status="failed",
        outcome=None,
        command="echo boom",
    )

    body = await _list(authed_admin_client, outcome="empty_result")
    assert f"task-row-{tid_named}" in body
    assert f"task-row-{tid_plain}" not in body
    assert f"task-row-{tid_failed}" not in body


# The retired status alias in the `status` CSV is covered, with and without
# the plugin, in `tests/test_legacy_status_alias.py`.


@pytest.mark.asyncio
async def test_outcome_other_filters_by_null(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """`other` is the conventional token (failures.py/aggregate_outcomes) for
    'no outcome' (NULL) — NOT a literal outcome name (reserved, never
    stored). The WHERE must translate it to `t.outcome IS NULL`, not to a literal
    match against the column (which would always give 0 rows)."""
    tid_plain = await _seed(
        authed_admin_client,
        migrated_db_url,
        status="completed",
        outcome=None,
        command="echo other-plain",
    )
    tid_named = await _seed(
        authed_admin_client,
        migrated_db_url,
        status="completed",
        outcome="empty_result",
        command="echo other-named",
    )

    body = await _list(authed_admin_client, outcome="other")
    assert f"task-row-{tid_plain}" in body
    assert f"task-row-{tid_named}" not in body


@pytest.mark.asyncio
async def test_outcome_mixed_includes_named_and_null(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Mixed CSV `empty_result,other`: matches both — the named outcome
    AND the tasks without outcome — without collapsing to 0 rows."""
    tid_plain = await _seed(
        authed_admin_client,
        migrated_db_url,
        status="completed",
        outcome=None,
        command="echo mix-plain",
    )
    tid_named = await _seed(
        authed_admin_client,
        migrated_db_url,
        status="completed",
        outcome="empty_result",
        command="echo mix-named",
    )
    tid_other_named = await _seed(
        authed_admin_client,
        migrated_db_url,
        status="completed",
        outcome="timeout",
        command="echo mix-timeout",
    )

    body = await _list(authed_admin_client, outcome="empty_result,other")
    assert f"task-row-{tid_plain}" in body
    assert f"task-row-{tid_named}" in body
    assert f"task-row-{tid_other_named}" not in body


@pytest.mark.asyncio
async def test_outcome_invalid_gives_400(authed_admin_client: AsyncClient) -> None:
    r = await authed_admin_client.get("/admin/tasks", headers=_HX, params={"outcome": "Not-Valid"})
    assert r.status_code == 400, r.text
    assert "Not-Valid" in r.text


@pytest.mark.asyncio
async def test_list_row_shows_outcome(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    await _seed(
        authed_admin_client,
        migrated_db_url,
        status="completed",
        outcome="empty_result",
        command="echo row-badge",
    )
    body = await _list(authed_admin_client)
    assert "badge-outcome" in body
    assert "empty_result" in body


@pytest.mark.asyncio
async def test_dynamic_chips_on_page(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    await _seed(
        authed_admin_client,
        migrated_db_url,
        status="completed",
        outcome="empty_result",
        command="echo chip",
    )
    r = await authed_admin_client.get("/admin/")
    assert r.status_code == 200, r.text
    assert 'data-outcome="empty_result"' in r.text


@pytest.mark.asyncio
async def test_chips_ordered_by_frequency(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Outcome chips are sorted by descending count.

    The DOM order is the priority order of the visual clamp: the chips that
    fall outside the visible lines are the last ones. Sorting by frequency
    (not alphabetically) guarantees that what remains behind `+N more` is the
    long tail, not whatever starts with `z`.
    """
    for i in range(3):
        await _seed(
            authed_admin_client,
            migrated_db_url,
            status="completed",
            outcome="zz_frequent",
            command=f"echo freq {i}",
        )
    await _seed(
        authed_admin_client,
        migrated_db_url,
        status="completed",
        outcome="aa_rare",
        command="echo rare",
    )

    r = await authed_admin_client.get("/admin/")
    assert r.status_code == 200, r.text
    pos_frequent = r.text.index('data-outcome="zz_frequent"')
    pos_rare = r.text.index('data-outcome="aa_rare"')
    assert pos_frequent < pos_rare


@pytest.mark.asyncio
async def test_clamp_disclosure_starts_hidden(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The `N more` of the rail is progressive enhancement.

    The clamp is applied by app.js measuring real lines; the server only emits the
    button, `hidden` and with no count. Without JS there is no clipping and all
    chips are visible — never filters hidden without an affordance to reveal them.
    """
    await _seed(
        authed_admin_client,
        migrated_db_url,
        status="completed",
        outcome="empty_result",
        command="echo clamp",
    )
    r = await authed_admin_client.get("/admin/")
    assert r.status_code == 200, r.text
    assert 'id="chip-overflow-toggle"' in r.text
    assert 'aria-controls="status-chips-wrap"' in r.text
    # `hidden` in the served markup: the button only appears if the JS clips.
    marker = r.text.index('id="chip-overflow-toggle"')
    tag = r.text[marker : r.text.index(">", marker)]
    assert " hidden" in tag
    # It is NOT a `.status-chip`: that class is swept by the status
    # querySelectorAll in app.js and the disclosure would leak into the filter CSV.
    classes = tag.split('class="', 1)[1].split('"', 1)[0].split()
    assert "status-chip" not in classes


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_chips_respect_variant_lens(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The dynamic chips follow the page variant lens.

    An outcome present only in an old version must not render a chip under
    the default `current` lens (clicking it would filter to 0 rows); with
    `variant=all` it does appear.
    """
    tid_old = await _create_task(
        authed_admin_client, commands=example_payload(version="0.7.1")["commands"]
    )
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET status = CAST(:s AS task_status), outcome = :oc WHERE id = :id",
        {"s": "completed", "oc": "stale_only", "id": tid_old},
    )
    # The kind has a newer version without outcomes: the current lens
    # resolves to the latest and leaves the old one archived.
    await _create_task(authed_admin_client, commands=example_payload(version="0.7.5")["commands"])

    r_current = await authed_admin_client.get("/admin/")
    assert r_current.status_code == 200, r_current.text
    assert 'data-outcome="stale_only"' not in r_current.text

    r_all = await authed_admin_client.get("/admin/", params={"variant": "all"})
    assert r_all.status_code == 200, r_all.text
    assert 'data-outcome="stale_only"' in r_all.text
