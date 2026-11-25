"""Tests for the GET /api/v1/insights/failures endpoint."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import DEFAULT_WORKSPACE_ID, mint_pat_via_app
from tests.test_api_v1_tasks_read import _bearer, _db_url


async def _seed_worker(workspace_id: str = DEFAULT_WORKSPACE_ID) -> str:
    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            return str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO workers (api_key_hash, hostname, os, arch, workspace_id) "
                            "VALUES (:h, 'host', 'linux', 'amd64', :ws) RETURNING id::text"
                        ),
                        {"h": uuid.uuid4().hex, "ws": workspace_id},
                    )
                ).scalar_one()
            )
    finally:
        await engine.dispose()


async def _seed_failed_task(
    client: AsyncClient,
    token: str,
    worker_id: str,
    *,
    exit_code: int | None,
    stderr_lines: list[str],
    stdout_lines: list[str] | None = None,
    command_line: str | None = None,
    exec_status: str = "failed",
    command: str = "run_batch",
    payload: dict | None = None,
) -> tuple[int, str]:
    """Create a task via the API, mark it failed and seed its execution + output.

    Returns (task_id, execution_id).
    """
    resp = await client.post(
        "/api/v1/tasks", headers=_bearer(token), json={"payload": payload or {}}
    )
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["task_id"]

    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE tasks SET status='failed', completed_at=now(), "
                    "command=:cmd, payload=CAST(:pl AS jsonb) WHERE id=:t"
                ),
                {"cmd": command, "pl": _json(payload or {}), "t": task_id},
            )
            execution_id = str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO task_executions "
                            "(task_id, worker_id, status, attempt_index, exit_code, "
                            " started_at, finished_at) "
                            "VALUES (:t, :w, :s, 1, :ec, now(), now()) RETURNING id::text"
                        ),
                        {"t": task_id, "w": worker_id, "s": exec_status, "ec": exit_code},
                    )
                ).scalar_one()
            )
            seq = 1
            for stream, lines in (
                ("command", [command_line] if command_line else []),
                ("stdout", stdout_lines or []),
                ("stderr", stderr_lines),
            ):
                for line in lines:
                    await conn.execute(
                        text(
                            "INSERT INTO output_events (execution_id, seq, ts, stream, text) "
                            "VALUES (CAST(:e AS uuid), :seq, now(), :st, :tx)"
                        ),
                        {"e": execution_id, "seq": seq, "st": stream, "tx": line + "\n"},
                    )
                    seq += 1
    finally:
        await engine.dispose()
    return task_id, execution_id


def _json(d: dict) -> str:
    import json

    return json.dumps(d)


@pytest.mark.asyncio
async def test_failures_empty(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    resp = await app_client.get("/api/v1/insights/failures", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_failed_tasks"] == 0
    assert body["by_exit_code"] == []
    assert body["by_worker"] == []
    assert body["by_signature"] == []


@pytest.mark.asyncio
async def test_failures_single_task_signature_and_example(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    task_id, exec_id = await _seed_failed_task(
        app_client,
        token,
        worker,
        exit_code=1,
        stderr_lines=["Traceback ...", "RuntimeError: CUDA out of memory. Tried 2.50 GiB"],
        stdout_lines=["loading model"],
        command_line="$ example-tool run --model org/foo",
        payload={"repo": "org/foo", "ref": "abc123"},
    )
    resp = await app_client.get("/api/v1/insights/failures", headers=_bearer(token))
    body = resp.json()
    assert body["total_failed_tasks"] == 1
    assert body["by_exit_code"] == [{"exit_code": 1, "count": 1}]
    assert body["by_worker"] == [{"worker_id": worker, "count": 1}]
    assert len(body["by_signature"]) == 1
    grp = body["by_signature"][0]
    assert grp["signature"] == "runtimeerror: cuda out of memory. tried <n> gib"
    from orcaster_server.error_signatures import signature_hash

    assert grp["signature_hash"] == signature_hash(grp["signature"])
    assert grp["count"] == 1
    assert grp["sample_task_ids"] == [task_id]
    ex = grp["example"]
    assert ex["execution_id"] == exec_id
    assert ex["exit_code"] == 1
    assert ex["command_line"] == "example-tool run --model org/foo"  # "$ " stripped
    assert ex["payload"] == {"repo": "org/foo", "ref": "abc123"}
    assert "CUDA out of memory" in ex["stderr_tail"]
    assert "loading model" in ex["stdout_tail"]
    assert ex["duration_seconds"] is not None


@pytest.mark.asyncio
async def test_failures_groups_same_signature_diff_numbers(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    await _seed_failed_task(
        app_client,
        token,
        worker,
        exit_code=1,
        stderr_lines=["RuntimeError: failed at step 12 of 99"],
    )
    await _seed_failed_task(
        app_client,
        token,
        worker,
        exit_code=1,
        stderr_lines=["RuntimeError: failed at step 7 of 42"],
    )
    body = (await app_client.get("/api/v1/insights/failures", headers=_bearer(token))).json()
    assert body["total_failed_tasks"] == 2
    assert len(body["by_signature"]) == 1
    assert body["by_signature"][0]["count"] == 2
    assert body["by_signature"][0]["signature"] == "runtimeerror: failed at step <n> of <n>"


@pytest.mark.asyncio
async def test_failures_null_exit_and_no_stderr_fallback(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    await _seed_failed_task(
        app_client, token, worker, exit_code=None, stderr_lines=[], exec_status="expired"
    )
    body = (await app_client.get("/api/v1/insights/failures", headers=_bearer(token))).json()
    assert body["total_failed_tasks"] == 1
    assert body["by_exit_code"] == [{"exit_code": None, "count": 1}]
    assert body["by_signature"][0]["signature"] == "no-stderr:exit=null"


@pytest.mark.asyncio
async def test_failures_representative_execution_ignores_completed(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    # Failed task with one completed and one failed execution: the representative one is
    # the failed one.
    task_id, exec_id = await _seed_failed_task(
        app_client,
        token,
        worker,
        exit_code=2,
        stderr_lines=["boom: real failure"],
    )
    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO task_executions "
                    "(task_id, worker_id, status, attempt_index, exit_code, finished_at) "
                    "VALUES (:t, :w, 'completed', 2, 0, now())"
                ),
                {"t": task_id, "w": worker},
            )
    finally:
        await engine.dispose()
    body = (await app_client.get("/api/v1/insights/failures", headers=_bearer(token))).json()
    grp = body["by_signature"][0]
    assert grp["signature"] == "boom: real failure"
    assert grp["example"]["exit_code"] == 2
    assert grp["example"]["execution_id"] == exec_id


@pytest.mark.asyncio
async def test_failures_signature_uses_last_nonempty_across_events(app_client: AsyncClient) -> None:
    # The worker writes stderr in chunks; the last event may be whitespace.
    # The signature must come from the last NON-empty line of the whole stderr, not from the
    # last event.
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    await _seed_failed_task(
        app_client,
        token,
        worker,
        exit_code=1,
        stderr_lines=["RuntimeError: real boom", "   "],
    )
    body = (await app_client.get("/api/v1/insights/failures", headers=_bearer(token))).json()
    assert body["by_signature"][0]["signature"] == "runtimeerror: real boom"
    assert body["total_failed_tasks"] == 1


@pytest.mark.asyncio
async def test_failures_signature_reconstructs_line_split_across_chunks(
    app_client: AsyncClient,
) -> None:
    # The worker truncates each event to 64 KiB, so an error line may
    # be split across two consecutive events (no "\n" in the first). The
    # signature must rebuild the whole line (string_agg with separator '').
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    task_id, exec_id = await _seed_failed_task(
        app_client, token, worker, exit_code=1, stderr_lines=[]
    )
    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            for seq, chunk in ((1, "RuntimeError: CUDA out of "), (2, "memory\n")):
                await conn.execute(
                    text(
                        "INSERT INTO output_events (execution_id, seq, ts, stream, text) "
                        "VALUES (CAST(:e AS uuid), :seq, now(), 'stderr', :tx)"
                    ),
                    {"e": exec_id, "seq": seq, "tx": chunk},
                )
    finally:
        await engine.dispose()
    body = (await app_client.get("/api/v1/insights/failures", headers=_bearer(token))).json()
    assert body["by_signature"][0]["signature"] == "runtimeerror: cuda out of memory"


@pytest.mark.asyncio
async def test_failures_no_execution_null_example_fallback(app_client: AsyncClient) -> None:
    """Failed task without a terminal execution -> empty by_worker; by_signature uses fallback."""
    token, _ = await mint_pat_via_app()
    # Create the task via the API and then mark it failed without inserting any execution.
    resp = await app_client.post("/api/v1/tasks", headers=_bearer(token), json={"payload": {}})
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["task_id"]

    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET status='failed', completed_at=now() WHERE id=:t"),
                {"t": task_id},
            )
    finally:
        await engine.dispose()

    body = (await app_client.get("/api/v1/insights/failures", headers=_bearer(token))).json()
    assert body["total_failed_tasks"] == 1
    # Without a representative execution: by_worker does not include this task (worker_id is None).
    assert body["by_worker"] == []
    assert len(body["by_signature"]) == 1
    grp = body["by_signature"][0]
    assert grp["signature"] == "no-stderr:exit=null"
    ex = grp["example"]
    assert ex["execution_id"] is None
    assert ex["exit_code"] is None


@pytest.mark.asyncio
async def test_failures_limit_truncates_signatures_only(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    for i in range(3):
        await _seed_failed_task(
            app_client,
            token,
            worker,
            exit_code=i,
            stderr_lines=[f"distinct error type alpha{chr(97 + i)}"],
        )
    body = (
        await app_client.get("/api/v1/insights/failures?limit=2", headers=_bearer(token))
    ).json()
    assert body["total_failed_tasks"] == 3  # not truncated
    assert len(body["by_exit_code"]) == 3  # not truncated
    assert len(body["by_signature"]) == 2  # truncated to limit
    assert body["filters"]["limit"] == 2


@pytest.mark.asyncio
async def test_failures_since_param_returns_200_not_500(app_client: AsyncClient) -> None:
    """Regression: CAST(:since AS timestamptz) failed when _as_utc returned str.

    With the bug, GET /api/v1/insights/failures?since=<iso> responded HTTP 500
    because asyncpg does not accept a str as a parameter for a CAST to timestamptz.
    The fix makes _as_utc always return datetime | None.
    """
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    await _seed_failed_task(
        app_client,
        token,
        worker,
        exit_code=1,
        stderr_lines=["RuntimeError: regression since param"],
    )
    # Pass a non-null `since`: this exercises CAST(:since AS timestamptz) in
    # _SQL_FAILED_TASKS. Before the fix, _as_utc returned a str (isoformat), and
    # asyncpg raised InvalidTextRepresentation -> HTTP 500. Now it must be 200.
    since = "2020-01-01T00:00:00Z"
    resp = await app_client.get(f"/api/v1/insights/failures?since={since}", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "total_failed_tasks" in body
    assert body["total_failed_tasks"] >= 1


@pytest.mark.asyncio
async def test_failures_tail_keeps_only_last_lines_across_many_events(
    app_client: AsyncClient,
) -> None:
    # The tail is built in SQL over the last events: with many lines spread over
    # separate events, stdout_tail must return only the last ones (<= _TAIL_MAX_LINES),
    # not the first. Regression test for the trimming that used to materialize the whole blob.
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    _, _exec_id = await _seed_failed_task(
        app_client,
        token,
        worker,
        exit_code=1,
        stderr_lines=["RuntimeError: boom"],
        stdout_lines=[f"line{i}" for i in range(60)],
    )
    body = (await app_client.get("/api/v1/insights/failures", headers=_bearer(token))).json()
    tail = body["by_signature"][0]["example"]["stdout_tail"]
    lines = tail.splitlines()
    assert len(lines) <= 20  # _TAIL_MAX_LINES
    assert "line59" in lines  # the last line is always present
    assert "line0" not in lines  # old lines are discarded
