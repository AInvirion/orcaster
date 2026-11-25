"""Tests for the GET /api/v1/insights/outcomes endpoint.

Generalizes `/insights/failures` to all terminal states (completed,
failed, cancelled) segmented by (status, outcome). The seeding harness is
modelled on `test_api_v1_insights_failures.py`; here the status/
outcome/variant of each seeded task is parametrized.
"""

import json
import uuid
from datetime import UTC, datetime, time, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import DEFAULT_WORKSPACE_ID, EXAMPLE_KIND, mint_pat_via_app
from tests.test_api_v1_tasks_read import _bearer, _db_url


def _json(d: dict) -> str:
    return json.dumps(d)


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


async def _seed_terminal_task(
    client: AsyncClient,
    token: str,
    worker_id: str,
    *,
    status: str,
    outcome: str | None = None,
    exit_code: int | None,
    stderr_lines: list[str],
    stdout_lines: list[str] | None = None,
    command_line: str | None = None,
    exec_status: str | None = None,
    command: str = "run_batch",
    payload: dict | None = None,
    completed_offset: timedelta = timedelta(0),
    completed_at: datetime | None = None,
) -> tuple[int, str]:
    """Create a task via the API, close it with `status` (+ optional `outcome`) and
    seed its representative execution + output.

    `exec_status` by default follows the per-class criterion for the representative
    execution (failed -> failed, completed -> completed, cancelled -> cancelled);
    it is only passed explicitly to force a mismatch (not used here).

    `completed_offset` pushes `completed_at` into the past relative to the
    POSTGRES clock (`now() - offset`), to exercise the `since` time-window
    filter. It suits "N days ago", where the absolute instant does not matter,
    only the distance relative to "now".

    `completed_at` sets `completed_at` to an ABSOLUTE instant (aware UTC),
    supplied by the caller via a bind param, instead of computing it with the
    Postgres clock. Use it when the task must land at a precise point relative
    to the APP clock (an anchor inside the default window, see
    `_safe_today_anchor`; or an instant AFTER the app's "now" to
    simulate a DB running ahead): it prevents app<->DB clock drift from moving
    the task out of the expected window between seeding and the request.

    Passing both (`completed_at` and a non-zero `completed_offset`) is
    ambiguous and raises `ValueError`.
    """
    if completed_at is not None and completed_offset != timedelta(0):
        raise ValueError("pass completed_at or completed_offset, not both")

    resp = await client.post(
        "/api/v1/tasks", headers=_bearer(token), json={"payload": payload or {}}
    )
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["task_id"]

    if exec_status is None:
        exec_status = status

    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            if completed_at is not None:
                await conn.execute(
                    text(
                        "UPDATE tasks SET status=:st, outcome=:oc, "
                        "completed_at = :completed_at, "
                        "command=:cmd, payload=CAST(:pl AS jsonb) WHERE id=:t"
                    ),
                    {
                        "st": status,
                        "oc": outcome,
                        "completed_at": completed_at,
                        "cmd": command,
                        "pl": _json(payload or {}),
                        "t": task_id,
                    },
                )
            else:
                await conn.execute(
                    text(
                        "UPDATE tasks SET status=:st, outcome=:oc, "
                        "completed_at = now() - make_interval(secs => :off_secs), "
                        "command=:cmd, payload=CAST(:pl AS jsonb) WHERE id=:t"
                    ),
                    {
                        "st": status,
                        "oc": outcome,
                        "off_secs": completed_offset.total_seconds(),
                        "cmd": command,
                        "pl": _json(payload or {}),
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


def _example_pip_line(version: str) -> str:
    """Install line that the test plugin's extractor recognizes."""
    return f"uv pip install {EXAMPLE_KIND}=={version}"


def _pairs(body: dict) -> set[tuple[str, str | None]]:
    return {(r["status"], r["outcome"]) for r in body["by_status_outcome"]}


@pytest.mark.asyncio
async def test_outcomes_default_covers_all_terminal_states(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    await _seed_terminal_task(
        app_client, token, worker, status="failed", exit_code=1, stderr_lines=["boom: real"]
    )
    await _seed_terminal_task(
        app_client, token, worker, status="completed", exit_code=0, stderr_lines=[]
    )
    await _seed_terminal_task(
        app_client,
        token,
        worker,
        status="completed",
        outcome="empty_result",
        exit_code=3,
        stderr_lines=[],
    )
    await _seed_terminal_task(
        app_client, token, worker, status="cancelled", exit_code=None, stderr_lines=[]
    )

    resp = await app_client.get("/api/v1/insights/outcomes", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_tasks"] == 4
    assert _pairs(body) == {
        ("failed", None),
        ("completed", None),
        ("completed", "empty_result"),
        ("cancelled", None),
    }
    for row in body["by_status_outcome"]:
        assert row["count"] == 1
    assert set(body["filters"]["status"]) == {"completed", "failed", "cancelled"}


@pytest.mark.asyncio
async def test_outcomes_filter_status_multi(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    await _seed_terminal_task(
        app_client, token, worker, status="failed", exit_code=1, stderr_lines=["boom"]
    )
    await _seed_terminal_task(
        app_client, token, worker, status="cancelled", exit_code=None, stderr_lines=[]
    )
    await _seed_terminal_task(
        app_client, token, worker, status="completed", exit_code=0, stderr_lines=[]
    )

    resp = await app_client.get(
        "/api/v1/insights/outcomes?status=failed,cancelled", headers=_bearer(token)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_tasks"] == 2
    assert ("completed", None) not in _pairs(body)


@pytest.mark.asyncio
async def test_outcomes_filter_outcome_name_and_other(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    await _seed_terminal_task(
        app_client, token, worker, status="completed", exit_code=0, stderr_lines=[]
    )
    await _seed_terminal_task(
        app_client,
        token,
        worker,
        status="completed",
        outcome="empty_result",
        exit_code=3,
        stderr_lines=[],
    )

    resp_named = await app_client.get(
        "/api/v1/insights/outcomes?status=completed&outcome=empty_result",
        headers=_bearer(token),
    )
    assert resp_named.status_code == 200, resp_named.text
    assert resp_named.json()["total_tasks"] == 1

    resp_other = await app_client.get(
        "/api/v1/insights/outcomes?status=completed&outcome=other", headers=_bearer(token)
    )
    assert resp_other.status_code == 200, resp_other.text
    assert resp_other.json()["total_tasks"] == 1
    assert _pairs(resp_other.json()) == {("completed", None)}


# The retired status alias in `status` is covered, with and without a plugin, in
# `tests/test_legacy_status_alias.py`.


@pytest.mark.asyncio
async def test_outcomes_rejects_non_terminal_status(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    resp = await app_client.get("/api/v1/insights/outcomes?status=queued", headers=_bearer(token))
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_outcomes_variant(app_client: AsyncClient) -> None:
    # The variant is derived at creation from `payload.commands`, not
    # from the `command` column that the task adopts when it closes; that is why the
    # pip spec goes in the creation payload.
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    await _seed_terminal_task(
        app_client,
        token,
        worker,
        status="failed",
        exit_code=1,
        stderr_lines=["OldError: from 0.7.0"],
        payload={"commands": [_example_pip_line("0.7.0")], "requirements": []},
    )
    await _seed_terminal_task(
        app_client,
        token,
        worker,
        status="failed",
        exit_code=1,
        stderr_lines=["NewError: from 0.7.5"],
        payload={"commands": [_example_pip_line("0.7.5")], "requirements": []},
    )

    resp_current = await app_client.get(
        "/api/v1/insights/outcomes?status=failed&variant=current", headers=_bearer(token)
    )
    assert resp_current.status_code == 200, resp_current.text
    assert resp_current.json()["total_tasks"] == 1

    resp_all = await app_client.get(
        "/api/v1/insights/outcomes?status=failed&variant=all", headers=_bearer(token)
    )
    assert resp_all.status_code == 200, resp_all.text
    assert resp_all.json()["total_tasks"] == 2

    resp_default = await app_client.get(
        "/api/v1/insights/outcomes?status=failed", headers=_bearer(token)
    )
    assert resp_default.status_code == 200, resp_default.text
    assert resp_default.json()["total_tasks"] == 2


@pytest.mark.asyncio
async def test_failures_wrapper_unchanged(app_client: AsyncClient) -> None:
    """Same multi-state seeding: /insights/failures keeps its shape and semantics."""
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    _, exec_id = await _seed_terminal_task(
        app_client,
        token,
        worker,
        status="failed",
        exit_code=1,
        stderr_lines=["RuntimeError: CUDA out of memory. Tried 2.50 GiB"],
        stdout_lines=["loading model"],
        command_line="$ example-tool run --model org/foo",
        payload={"repo": "org/foo", "ref": "abc123"},
    )
    await _seed_terminal_task(
        app_client, token, worker, status="completed", exit_code=0, stderr_lines=[]
    )
    await _seed_terminal_task(
        app_client,
        token,
        worker,
        status="completed",
        outcome="empty_result",
        exit_code=3,
        stderr_lines=[],
    )
    await _seed_terminal_task(
        app_client, token, worker, status="cancelled", exit_code=None, stderr_lines=[]
    )

    resp = await app_client.get("/api/v1/insights/failures", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_failed_tasks"] == 1
    assert body["by_exit_code"] == [{"exit_code": 1, "count": 1}]
    assert body["by_worker"] == [{"worker_id": worker, "count": 1}]
    assert len(body["by_signature"]) == 1
    grp = body["by_signature"][0]
    assert grp["signature"] == "runtimeerror: cuda out of memory. tried <n> gib"
    ex = grp["example"]
    assert ex["execution_id"] == exec_id
    assert ex["exit_code"] == 1
    assert ex["command_line"] == "example-tool run --model org/foo"
    assert ex["payload"] == {"repo": "org/foo", "ref": "abc123"}
    assert "CUDA out of memory" in ex["stderr_tail"]
    assert "loading model" in ex["stdout_tail"]
    # The wrapper's schema does not change: no by_status_outcome.
    assert "by_status_outcome" not in body


def _safe_today_anchor() -> datetime:
    """An instant "of today" inside the default window, independent of clocks.

    The default window starts at midnight UTC of the current day,
    computed with the APP clock at request time, not with the Postgres
    clock used when seeding. Seeding with `completed_at=now()` (the default
    of `_seed_terminal_task`) is right almost always, but becomes flaky if
    the Postgres VM clock is skewed from the app clock (known clock drift
    of the Podman VM), which moves the comparison edge. Anchoring
    with an absolute `completed_at` from the app clock removes that variable.

    One minute after midnight UTC is enough: the UPPER edge of the
    default window no longer filters (the SQL leaves it open), so it does
    not need to be dodged.
    """
    now = datetime.now(UTC)
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return today_midnight + timedelta(minutes=1)


def _assert_default_window_bounds(filters: dict) -> None:
    """Structural assertions on the window default, without recomputing
    midnight on the client (avoids flakiness from crossing midnight between
    seeding and the request): `since` is not None and its time-of-day == midnight;
    `until` is not None and >= `since`.
    """
    assert filters["since"] is not None
    since = datetime.fromisoformat(filters["since"])
    assert since.time() == time(0, 0, 0)
    assert filters["until"] is not None
    until = datetime.fromisoformat(filters["until"])
    assert until >= since


@pytest.mark.asyncio
async def test_outcomes_default_window_is_today(app_client: AsyncClient) -> None:
    """Without since/until, the default window is the current day.

    The response counts only what closed today and `filters.since`/`filters.until`
    reflect the effective window (midnight UTC -> now), which the orcaster CLI
    reads to display it.
    """
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    # Closed today (inside the default window). Absolute anchor from the app
    # clock (see `_safe_today_anchor`): seeding with Postgres's `now()`
    # is flaky if its clock is skewed from the app clock that computes
    # the lower edge of the default window.
    await _seed_terminal_task(
        app_client,
        token,
        worker,
        status="failed",
        exit_code=1,
        stderr_lines=["today boom"],
        completed_at=_safe_today_anchor(),
    )
    # Closed 3 days ago (outside the default window).
    await _seed_terminal_task(
        app_client,
        token,
        worker,
        status="failed",
        exit_code=2,
        stderr_lines=["old boom"],
        completed_offset=timedelta(days=3),
    )

    resp = await app_client.get("/api/v1/insights/outcomes", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_tasks"] == 1
    _assert_default_window_bounds(body["filters"])


@pytest.mark.asyncio
async def test_outcomes_explicit_since_escapes_to_history(app_client: AsyncClient) -> None:
    """An explicit old `since` disables the default and returns the history."""
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    await _seed_terminal_task(
        app_client, token, worker, status="failed", exit_code=1, stderr_lines=["today boom"]
    )
    await _seed_terminal_task(
        app_client,
        token,
        worker,
        status="failed",
        exit_code=2,
        stderr_lines=["old boom"],
        completed_offset=timedelta(days=3),
    )

    resp = await app_client.get(
        "/api/v1/insights/outcomes",
        headers=_bearer(token),
        params={"since": "2020-01-01T00:00:00Z"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_tasks"] == 2
    assert datetime.fromisoformat(body["filters"]["since"]) == datetime(2020, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_outcomes_until_only_still_bounded_to_today(app_client: AsyncClient) -> None:
    """Only `until` (without `since`) does NOT scan the whole history.

    When only `until` is passed, `since` is injected as midnight UTC of today
    instead of staying unbounded.
    """
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    # Closed today (inside the injected window). Absolute anchor from the app
    # clock, see `_safe_today_anchor`.
    await _seed_terminal_task(
        app_client,
        token,
        worker,
        status="failed",
        exit_code=1,
        stderr_lines=["today boom"],
        completed_at=_safe_today_anchor(),
    )
    # Closed 3 days ago (outside the injected window, although before `until`).
    await _seed_terminal_task(
        app_client,
        token,
        worker,
        status="failed",
        exit_code=2,
        stderr_lines=["old boom"],
        completed_offset=timedelta(days=3),
    )

    until_param = "2030-01-01T00:00:00Z"
    resp = await app_client.get(
        "/api/v1/insights/outcomes",
        headers=_bearer(token),
        params={"until": until_param},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_tasks"] == 1
    assert body["filters"]["since"] is not None
    since = datetime.fromisoformat(body["filters"]["since"])
    assert since.time() == time(0, 0, 0)
    assert datetime.fromisoformat(body["filters"]["until"]) == datetime(2030, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_failures_default_window_is_today(app_client: AsyncClient) -> None:
    """The /insights/failures wrapper applies the same default window."""
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    # Absolute anchor from the app clock inside the default window, see
    # `_safe_today_anchor`.
    await _seed_terminal_task(
        app_client,
        token,
        worker,
        status="failed",
        exit_code=1,
        stderr_lines=["today boom"],
        completed_at=_safe_today_anchor(),
    )
    await _seed_terminal_task(
        app_client,
        token,
        worker,
        status="failed",
        exit_code=2,
        stderr_lines=["old boom"],
        completed_offset=timedelta(days=3),
    )

    resp = await app_client.get("/api/v1/insights/failures", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_failed_tasks"] == 1
    _assert_default_window_bounds(body["filters"])


@pytest.mark.asyncio
async def test_outcomes_default_window_includes_task_closed_after_app_now(
    app_client: AsyncClient,
) -> None:
    """The default upper edge must NOT hide what has just closed.

    `completed_at` is stamped by the POSTGRES clock and the default `until` is
    computed by the APP clock (`datetime.now(UTC)`), so a DB running ahead
    of the app would leave `completed_at > until` for a task closed "right
    now": it would not show up until the next request. Here the skew is simulated
    by seeding the task with a `completed_at` AFTER the app's `now`.

    With the upper edge omitted in the SQL when `until` is the default, the
    task falls inside the window; `filters.until` still echoes the effective
    instant (informational) even though it lies BEFORE the seeded `completed_at`.
    """
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    skewed = datetime.now(UTC) + timedelta(minutes=5)
    await _seed_terminal_task(
        app_client,
        token,
        worker,
        status="failed",
        exit_code=1,
        stderr_lines=["skewed boom"],
        completed_at=skewed,
    )

    resp = await app_client.get("/api/v1/insights/outcomes", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_tasks"] == 1
    _assert_default_window_bounds(body["filters"])
    # The echo is informational: the reported window does not bound the result.
    assert datetime.fromisoformat(body["filters"]["until"]) < skewed


@pytest.mark.asyncio
async def test_failures_default_window_includes_task_closed_after_app_now(
    app_client: AsyncClient,
) -> None:
    """The /insights/failures wrapper inherits the open upper edge."""
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    skewed = datetime.now(UTC) + timedelta(minutes=5)
    await _seed_terminal_task(
        app_client,
        token,
        worker,
        status="failed",
        exit_code=1,
        stderr_lines=["skewed boom"],
        completed_at=skewed,
    )

    resp = await app_client.get("/api/v1/insights/failures", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_failed_tasks"] == 1
    _assert_default_window_bounds(body["filters"])
    assert datetime.fromisoformat(body["filters"]["until"]) < skewed


@pytest.mark.asyncio
async def test_outcomes_explicit_until_still_bounds_upper_edge(
    app_client: AsyncClient,
) -> None:
    """An EXPLICIT `until` is not relaxed: the client's intent prevails.

    Both edges are explicit so the test does not depend on the time of day
    or on the Postgres clock (the seeded `completed_at` values are absolute, from the
    app clock, like the request parameters).
    """
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    now = datetime.now(UTC)
    await _seed_terminal_task(
        app_client,
        token,
        worker,
        status="failed",
        exit_code=1,
        stderr_lines=["inside boom"],
        completed_at=now - timedelta(minutes=30),
    )
    await _seed_terminal_task(
        app_client,
        token,
        worker,
        status="failed",
        exit_code=2,
        stderr_lines=["beyond boom"],
        completed_at=now + timedelta(minutes=5),
    )

    resp = await app_client.get(
        "/api/v1/insights/outcomes",
        headers=_bearer(token),
        params={
            "since": (now - timedelta(hours=1)).isoformat(),
            "until": now.isoformat(),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_tasks"] == 1
    assert {c["exit_code"] for c in body["by_exit_code"]} == {1}
