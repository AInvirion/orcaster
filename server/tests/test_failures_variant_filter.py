"""Variant filter/lens on the failures page.

Same semantics as the dashboard: the variant is a LENS. Default = "current",
but for failures the current set is resolved over the variants present AMONG
THE FAILED TASKS (a failures page whose current lens is empty while an old
version keeps failing would be bad UX). Tasks are seeded with the pip spec as
`command` so that the variant is derived on creation.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import EXAMPLE_KIND
from tests.test_admin_failures import _register, _seed_failed_task

# The core ships no extractors: without the test plugin every task would be
# `other` and the lens would have no versions to separate.
pytestmark = pytest.mark.usefixtures("example_plugin")


def _example_pip_line(version: str) -> str:
    """Command that the test plugin recognizes as `example-tool` with label `version`."""
    return f"uv pip install example-tool=={version}"


async def _mark_completed(db_url: str, task_id: int) -> None:
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET status = 'completed' WHERE id = :t"), {"t": task_id}
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_failures_default_current_hides_old_version(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id = await _register(authed_admin_client, hostname="var-fail-1")
    await _seed_failed_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        exit_code=1,
        stderr_lines=["LatestError: from newest version"],
        command=_example_pip_line("1.10.0"),
    )
    await _seed_failed_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        exit_code=1,
        stderr_lines=["ArchivedError: from old version"],
        command=_example_pip_line("1.2.0"),
    )

    body = (await authed_admin_client.get("/admin/outcomes/partial?range=all")).text
    assert "latesterror: from newest version" in body.lower()
    assert "archivederror: from old version" not in body.lower()


@pytest.mark.asyncio
async def test_failures_all_shows_every_version(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id = await _register(authed_admin_client, hostname="var-fail-2")
    await _seed_failed_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        exit_code=1,
        stderr_lines=["LatestError: from newest version"],
        command=_example_pip_line("1.10.0"),
    )
    await _seed_failed_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        exit_code=1,
        stderr_lines=["ArchivedError: from old version"],
        command=_example_pip_line("1.2.0"),
    )

    body = (await authed_admin_client.get("/admin/outcomes/partial?range=all&variant=all")).text
    assert "latesterror: from newest version" in body.lower()
    assert "archivederror: from old version" in body.lower()


@pytest.mark.asyncio
async def test_failures_specific_pair_isolates_version(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id = await _register(authed_admin_client, hostname="var-fail-3")
    await _seed_failed_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        exit_code=1,
        stderr_lines=["LatestError: from newest version"],
        command=_example_pip_line("1.10.0"),
    )
    await _seed_failed_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        exit_code=1,
        stderr_lines=["ArchivedError: from old version"],
        command=_example_pip_line("1.2.0"),
    )

    body = (
        await authed_admin_client.get(
            "/admin/outcomes/partial",
            params={"range": "all", "variant": f"{EXAMPLE_KIND}~1.2.0"},
        )
    ).text
    assert "archivederror: from old version" in body.lower()
    assert "latesterror: from newest version" not in body.lower()


@pytest.mark.asyncio
async def test_failures_current_scoped_to_failed_tasks(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """`current` in failures = latest AMONG THE FAILED. If the newest version
    only has completed tasks (0 failures) and an old one keeps failing, the current
    lens must show the old one, not be empty."""
    worker_id = await _register(authed_admin_client, hostname="var-fail-4")
    # 1.10.0 exists but WITHOUT failures (completed).
    tid_completed, _ = await _seed_failed_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        exit_code=0,
        stderr_lines=["ignored"],
        command=_example_pip_line("1.10.0"),
    )
    await _mark_completed(migrated_db_url, tid_completed)
    # 1.2.0 does fail.
    await _seed_failed_task(
        authed_admin_client,
        migrated_db_url,
        worker_id,
        exit_code=1,
        stderr_lines=["ArchivedError: from old version"],
        command=_example_pip_line("1.2.0"),
    )

    body = (await authed_admin_client.get("/admin/outcomes/partial?range=all")).text
    assert "archivederror: from old version" in body.lower()
