"""Task variant filter/lens in the dashboard.

The `(kind, label)` variant is a LENS: it rescales the listing AND the counts
breakdown. Default = "current" (latest of each tool + `other`); old versions
are archived (outside the default, queryable with `?variant=...`).
Tasks are seeded through the real creation endpoint, which already derives the
variant from the payload, to exercise the whole create→query path.
"""

import re

import pytest
from httpx import AsyncClient

from tests.conftest import EXAMPLE_KIND, example_payload
from tests.test_dashboard import _create_task, _exec_sql

# The core ships no extractors: without the test plugin every task would be
# `other` and the lens would have no versions to separate.
pytestmark = pytest.mark.usefixtures("example_plugin")

_HX = {"HX-Request": "true"}


def _example_commands(version: str) -> list[str]:
    """Commands that the test plugin recognizes as `example-tool` with label `version`."""
    return [*example_payload(version=version)["commands"], "example-tool run"]


async def _list(client: AsyncClient, **params) -> str:
    r = await client.get("/admin/tasks", headers=_HX, params=params)
    assert r.status_code == 200, r.text
    return r.text


def _bucket_count(body: str, status: str) -> int | None:
    """Extract the `status` bucket count from the subtitle, or None if it does not appear."""
    m = re.search(
        rf'subtitle-bucket--{status}"[^>]*>\s*<span class="subtitle-bucket-count">(\d+)',
        body,
        re.S,
    )
    return int(m.group(1)) if m else None


@pytest.mark.asyncio
async def test_default_current_shows_latest_and_other_hides_old(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    tid_latest = await _create_task(authed_admin_client, commands=_example_commands("1.10.0"))
    tid_old = await _create_task(authed_admin_client, commands=_example_commands("1.2.0"))
    tid_other = await _create_task(authed_admin_client, command="echo hello")

    body = await _list(authed_admin_client)  # no param → current
    assert f"task-row-{tid_latest}" in body  # latest of the kind
    assert f"task-row-{tid_other}" in body  # other always in current
    assert f"task-row-{tid_old}" not in body  # old version, archived


@pytest.mark.asyncio
async def test_all_shows_every_version(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    tid_latest = await _create_task(authed_admin_client, commands=_example_commands("1.10.0"))
    tid_old = await _create_task(authed_admin_client, commands=_example_commands("1.2.0"))
    tid_other = await _create_task(authed_admin_client, command="echo hello")

    body = await _list(authed_admin_client, variant="all")
    assert f"task-row-{tid_latest}" in body
    assert f"task-row-{tid_old}" in body
    assert f"task-row-{tid_other}" in body


@pytest.mark.asyncio
async def test_specific_pair_isolates_that_version(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    tid_latest = await _create_task(authed_admin_client, commands=_example_commands("1.10.0"))
    tid_old = await _create_task(authed_admin_client, commands=_example_commands("1.2.0"))
    tid_other = await _create_task(authed_admin_client, command="echo hello")

    body = await _list(authed_admin_client, variant=f"{EXAMPLE_KIND}~1.2.0")
    assert f"task-row-{tid_old}" in body
    assert f"task-row-{tid_latest}" not in body
    assert f"task-row-{tid_other}" not in body


@pytest.mark.asyncio
async def test_other_bucket_isolates_unrecognized(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    tid_latest = await _create_task(authed_admin_client, commands=_example_commands("1.10.0"))
    tid_other = await _create_task(authed_admin_client, command="echo hello")

    body = await _list(authed_admin_client, variant="other")
    assert f"task-row-{tid_other}" in body
    assert f"task-row-{tid_latest}" not in body


@pytest.mark.asyncio
async def test_counts_rescope_to_selected_variant(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The counts breakdown (buckets) follows the variant scope, not the global one."""
    ids_latest = [
        await _create_task(authed_admin_client, commands=_example_commands("1.10.0"))
        for _ in range(2)
    ]
    ids_old = [
        await _create_task(authed_admin_client, commands=_example_commands("1.2.0"))
        for _ in range(3)
    ]
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET status = 'failed' WHERE id = ANY(:ids)",
        {"ids": ids_latest + ids_old},
    )

    body_current = await _list(authed_admin_client)  # default current → only 1.10.0
    assert _bucket_count(body_current, "failed") == 2

    body_all = await _list(authed_admin_client, variant="all")  # todas
    assert _bucket_count(body_all, "failed") == 5

    body_old = await _list(authed_admin_client, variant=f"{EXAMPLE_KIND}~1.2.0")
    assert _bucket_count(body_old, "failed") == 3


@pytest.mark.asyncio
async def test_variant_does_not_collapse_subtitle_to_x_of_y(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Selecting a variant (without status/q) does NOT count as an active filter: the
    subtitle shows the rescaled breakdown, not the collapsed "X of Y" mode."""
    tid = await _create_task(authed_admin_client, commands=_example_commands("1.10.0"))
    await _exec_sql(
        migrated_db_url, "UPDATE tasks SET status = 'failed' WHERE id = :id", {"id": tid}
    )
    body = await _list(authed_admin_client, variant=f"{EXAMPLE_KIND}~1.10.0")
    assert "subtitle-bucket--failed" in body


@pytest.mark.asyncio
async def test_status_chip_preserves_variant(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """When combining variant + status, the status chip keeps the variant in its
    remove_qs (removing it returns to the lens, not to 'all')."""
    tid = await _create_task(authed_admin_client, commands=_example_commands("1.10.0"))
    await _exec_sql(
        migrated_db_url, "UPDATE tasks SET status = 'failed' WHERE id = :id", {"id": tid}
    )
    r = await authed_admin_client.get(
        "/admin/tasks",
        headers=_HX,
        params={"status": "failed", "variant": f"{EXAMPLE_KIND}~1.10.0"},
    )
    assert r.status_code == 200, r.text
    assert f"variant={EXAMPLE_KIND}" in r.text


@pytest.mark.asyncio
async def test_non_htmx_redirect_preserves_variant(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    r = await authed_admin_client.get(
        "/admin/tasks",
        headers={"HX-Request": "false"},
        params={"variant": f"{EXAMPLE_KIND}~1.10.0"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert f"variant={EXAMPLE_KIND}" in r.headers["location"]
