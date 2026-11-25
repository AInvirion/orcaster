"""Seeding helper for the artifacts endpoint tests.

`_running_execution` reproduces the real flow already used by the
output/complete tests (see `tests/test_tasks_api.py`): a worker enrolls and
obtains an API key, a `queued` task is inserted, the worker claims it via
`POST /v1/workers/{id}/poll` (which returns `lease_token` and leaves the
execution in `claimed` state, one of the two states the artifacts endpoint
accepts), and finally the `execution_id` is resolved via SQL from
`(task_id, lease_token)`.

It reuses the constants/SQL from `test_tasks_api.py` so it does not diverge
from the canonical flow.
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_tasks_api import _insert_task, _poll, _register


async def _running_execution(app_client: AsyncClient, db_url: str) -> tuple[int, str, str, str]:
    """Bring a task to a `claimed` execution owned by a worker.

    Returns `(task_id, worker_api_key, lease_token, execution_id)`. The
    `lease_token` is valid and the execution is left in `claimed` (the endpoint's
    `status IN ('claimed', 'running')` predicate).
    """
    worker_id, key = await _register(app_client)
    task_id = await _insert_task(db_url)
    body = await _poll(app_client, worker_id, key)
    lease_token = body["task"]["lease_token"]

    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            execution_id = (
                await conn.execute(
                    text(
                        "SELECT id::text FROM task_executions "
                        "WHERE task_id = :t AND lease_token = :l"
                    ),
                    {"t": task_id, "l": lease_token},
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    return task_id, key, lease_token, execution_id
