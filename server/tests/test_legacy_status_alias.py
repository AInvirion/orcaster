"""Retired status aliases and outcome closures through a plugin's profile.

A plugin can declare in its outcomes profile a *retired status alias*
(`add_legacy_status_alias`): a status token that no longer exists in the enum and that
saved URLs still use. The core ships none; without a plugin, the token
gets the same response as any unknown status on the route that receives it.
This file covers the three routes that read the registry, each with and without alias:

- `GET /api/v1/insights/outcomes` and the drill-down `/api/v1/insights/signature/tasks`
  (`_parse_terminal_statuses`): without alias, 422.
- `GET /admin/outcomes[/partial]` (`_outcomes_context`): without alias, 400.
- The HTMX partial of `GET /admin/tasks`: without alias, 400.

The cases with alias use the `example` test plugin from `tests/conftest.py`
(`completed_empty` -> `completed` + `empty_result`).

The second part keeps the invariants of the `completed` closure with outcome
produced by the exit-code outcomes profile: no auto-retry, precedence in
fan-out, delete/cancel/poll and per-tool profile scope. The tasks that
exercise the profile carry the `example-tool` payload and request the `example_plugin`
fixture; without it they would close plain `failed` and the test would pass by the wrong path.
"""

import pytest
from httpx import AsyncClient

from orcaster_server.error_signatures import signature_hash
from tests.conftest import (
    EXAMPLE_LEGACY_OUTCOME,
    EXAMPLE_LEGACY_STATUS,
    example_payload,
    mint_pat_via_app,
)
from tests.test_admin_failures import _register as _register_failures_worker
from tests.test_admin_outcomes_page import _seed_terminal_task as _seed_admin_terminal
from tests.test_api_v1_insights_outcomes import _pairs, _seed_terminal_task, _seed_worker
from tests.test_api_v1_tasks_read import _bearer
from tests.test_auto_retry import _claim_lease, _complete, _create_task_with_policy, _exec_sql
from tests.test_dashboard import _create_task as _create_dashboard_task
from tests.test_tasks_api import _insert_task, _poll, _register, _row, _rows

# Real retired status token: no core plugin declares it.
_RETIRED_STATUS = "completed_no_fingerprint"

_V1_UNKNOWN = "unknown terminal status value(s): {token}. valid: completed, failed, cancelled"

_HX = {"HX-Request": "true"}


# ---------------------------------------------------------------------------
# GET /api/v1/insights/outcomes
# ---------------------------------------------------------------------------


async def _seed_v1_completed(
    client: AsyncClient, token: str, worker: str, outcome: str | None
) -> int:
    task_id, _ = await _seed_terminal_task(
        client, token, worker, status="completed", outcome=outcome, exit_code=0, stderr_lines=[]
    )
    return task_id


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_v1_outcomes_alias_equals_status_and_outcome(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    await _seed_v1_completed(app_client, token, worker, None)
    await _seed_v1_completed(app_client, token, worker, EXAMPLE_LEGACY_OUTCOME)
    await _seed_v1_completed(app_client, token, worker, "other_name")

    legacy = await app_client.get(
        f"/api/v1/insights/outcomes?status={EXAMPLE_LEGACY_STATUS}", headers=_bearer(token)
    )
    equivalent = await app_client.get(
        f"/api/v1/insights/outcomes?status=completed&outcome={EXAMPLE_LEGACY_OUTCOME}",
        headers=_bearer(token),
    )
    assert legacy.status_code == 200, legacy.text
    assert equivalent.status_code == 200, equivalent.text
    assert legacy.json()["total_tasks"] == equivalent.json()["total_tasks"] == 1
    assert (
        _pairs(legacy.json())
        == _pairs(equivalent.json())
        == {("completed", EXAMPLE_LEGACY_OUTCOME)}
    )
    assert legacy.json()["filters"]["status"] == ["completed"]
    assert legacy.json()["filters"]["outcome"] == EXAMPLE_LEGACY_OUTCOME


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_v1_outcomes_explicit_outcome_wins_over_alias(app_client: AsyncClient) -> None:
    """The alias sets `outcome` only if the request does not carry one."""
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    await _seed_v1_completed(app_client, token, worker, EXAMPLE_LEGACY_OUTCOME)
    await _seed_v1_completed(app_client, token, worker, "other_name")

    resp = await app_client.get(
        f"/api/v1/insights/outcomes?status={EXAMPLE_LEGACY_STATUS}&outcome=other_name",
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_tasks"] == 1
    assert _pairs(body) == {("completed", "other_name")}
    assert body["filters"]["outcome"] == "other_name"


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_v1_outcomes_alias_outcome_applies_to_whole_response(
    app_client: AsyncClient,
) -> None:
    """Combined with another status in the CSV, the alias outcome applies to the
    WHOLE response, also to the other statuses of the filter."""
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    await _seed_v1_completed(app_client, token, worker, EXAMPLE_LEGACY_OUTCOME)
    await _seed_v1_completed(app_client, token, worker, None)
    await _seed_terminal_task(
        app_client, token, worker, status="failed", exit_code=1, stderr_lines=["boom: plain"]
    )
    await _seed_terminal_task(
        app_client,
        token,
        worker,
        status="failed",
        outcome=EXAMPLE_LEGACY_OUTCOME,
        exit_code=3,
        stderr_lines=["boom: named"],
    )

    resp = await app_client.get(
        f"/api/v1/insights/outcomes?status={EXAMPLE_LEGACY_STATUS},failed",
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body["filters"]["status"]) == {"completed", "failed"}
    assert body["filters"]["outcome"] == EXAMPLE_LEGACY_OUTCOME
    assert body["total_tasks"] == 2
    assert _pairs(body) == {
        ("completed", EXAMPLE_LEGACY_OUTCOME),
        ("failed", EXAMPLE_LEGACY_OUTCOME),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status_token", [EXAMPLE_LEGACY_STATUS, _RETIRED_STATUS])
async def test_v1_outcomes_alias_token_without_plugin_is_422(
    app_client: AsyncClient, status_token: str
) -> None:
    """Without a plugin there is no alias: the token is just another unknown status."""
    token, _ = await mint_pat_via_app()
    resp = await app_client.get(
        f"/api/v1/insights/outcomes?status={status_token}", headers=_bearer(token)
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["message"] == _V1_UNKNOWN.format(token=status_token)


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_v1_outcomes_undeclared_retired_token_with_plugin_is_422(
    app_client: AsyncClient,
) -> None:
    """A plugin only translates the tokens it declares: the rest is still 422."""
    token, _ = await mint_pat_via_app()
    resp = await app_client.get(
        f"/api/v1/insights/outcomes?status={_RETIRED_STATUS}", headers=_bearer(token)
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["message"] == _V1_UNKNOWN.format(token=_RETIRED_STATUS)


# ---------------------------------------------------------------------------
# GET /api/v1/insights/signature/tasks (drill-down, same parsing as /outcomes)
# ---------------------------------------------------------------------------

_BOOM = "runtimeerror: boom at step <n>"


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_v1_signature_tasks_alias_filters_status_and_outcome(
    app_client: AsyncClient,
) -> None:
    token, _ = await mint_pat_via_app()
    worker = await _seed_worker()
    seeds = [
        ("completed", EXAMPLE_LEGACY_OUTCOME),
        ("completed", None),
        ("failed", None),
    ]
    ids = {}
    for n, (status, outcome) in enumerate(seeds):
        ids[(status, outcome)], _ = await _seed_terminal_task(
            app_client,
            token,
            worker,
            status=status,
            outcome=outcome,
            exit_code=1,
            stderr_lines=[f"RuntimeError: boom at step {n}"],
        )

    resp = await app_client.get(
        f"/api/v1/insights/signature/tasks?signature_hash={signature_hash(_BOOM)}"
        f"&status={EXAMPLE_LEGACY_STATUS}",
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_ids"] == [ids[("completed", EXAMPLE_LEGACY_OUTCOME)]]
    assert body["filters"]["status"] == ["completed"]
    assert body["filters"]["outcome"] == EXAMPLE_LEGACY_OUTCOME


@pytest.mark.asyncio
async def test_v1_signature_tasks_alias_token_without_plugin_is_422(
    app_client: AsyncClient,
) -> None:
    token, _ = await mint_pat_via_app()
    resp = await app_client.get(
        f"/api/v1/insights/signature/tasks?signature_hash={signature_hash(_BOOM)}"
        f"&status={EXAMPLE_LEGACY_STATUS}",
        headers=_bearer(token),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["message"] == _V1_UNKNOWN.format(token=EXAMPLE_LEGACY_STATUS)


# ---------------------------------------------------------------------------
# GET /admin/outcomes[/partial]
# ---------------------------------------------------------------------------


def _chip(status: str, outcome: str, count: int) -> str:
    return (
        f'<span class="failures-chip-label">{status} · {outcome}</span>'
        f'<span class="failures-chip-count">{count}</span>'
    )


async def _seed_admin_completed(
    client: AsyncClient, db_url: str, worker_id: str, outcome: str | None
) -> None:
    await _seed_admin_terminal(client, db_url, worker_id, status="completed", outcome=outcome)


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_admin_outcomes_alias_filters_status_and_outcome(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The alias is equivalent to `status=completed&outcome=empty_result`, not to a
    literal status."""
    worker_id = await _register_failures_worker(authed_admin_client, hostname="alias-outcomes")
    await _seed_admin_completed(
        authed_admin_client, migrated_db_url, worker_id, EXAMPLE_LEGACY_OUTCOME
    )
    await _seed_admin_completed(authed_admin_client, migrated_db_url, worker_id, None)

    resp = await authed_admin_client.get(
        f"/admin/outcomes/partial?range=all&status={EXAMPLE_LEGACY_STATUS}"
    )
    assert resp.status_code == 200, resp.text
    # Only the completed task with the alias outcome enters the effective filter.
    assert '<span class="failures-total">1</span>' in resp.text
    assert _chip("completed", EXAMPLE_LEGACY_OUTCOME, 1) in resp.text

    # The full page reflects the resolved alias: the `completed` radio is
    # checked, not `failed` nor a non-existent value in the presets.
    page = await authed_admin_client.get(
        f"/admin/outcomes?range=all&status={EXAMPLE_LEGACY_STATUS}"
    )
    assert page.status_code == 200, page.text
    assert '<input type="radio" name="status" value="completed" checked' in page.text


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_admin_outcomes_explicit_outcome_wins_over_alias(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id = await _register_failures_worker(
        authed_admin_client, hostname="alias-outcomes-explicit"
    )
    await _seed_admin_completed(
        authed_admin_client, migrated_db_url, worker_id, EXAMPLE_LEGACY_OUTCOME
    )
    await _seed_admin_completed(authed_admin_client, migrated_db_url, worker_id, "other_name")

    resp = await authed_admin_client.get(
        f"/admin/outcomes/partial?range=all&status={EXAMPLE_LEGACY_STATUS}&outcome=other_name"
    )
    assert resp.status_code == 200, resp.text
    assert '<span class="failures-total">1</span>' in resp.text
    assert _chip("completed", "other_name", 1) in resp.text
    assert f"completed · {EXAMPLE_LEGACY_OUTCOME}" not in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/admin/outcomes", "/admin/outcomes/partial"])
@pytest.mark.parametrize("status_token", [EXAMPLE_LEGACY_STATUS, _RETIRED_STATUS])
async def test_admin_outcomes_alias_token_without_plugin_is_400(
    authed_admin_client: AsyncClient, path: str, status_token: str
) -> None:
    resp = await authed_admin_client.get(f"{path}?range=all&status={status_token}")
    assert resp.status_code == 400, resp.text
    assert f"unknown terminal status '{status_token}'" in resp.text


# ---------------------------------------------------------------------------
# GET /admin/tasks (HTMX partial of the dashboard)
# ---------------------------------------------------------------------------


async def _seed_dashboard(
    client: AsyncClient, db_url: str, *, status: str, outcome: str | None, command: str
) -> int:
    task_id = await _create_dashboard_task(client, command=command)
    await _exec_sql(
        db_url,
        "UPDATE tasks SET status = CAST(:s AS task_status), outcome = :oc WHERE id = :id",
        {"s": status, "oc": outcome, "id": task_id},
    )
    return task_id


async def _seed_dashboard_rows(client: AsyncClient, db_url: str) -> dict[str, int]:
    return {
        "alias": await _seed_dashboard(
            client, db_url, status="completed", outcome=EXAMPLE_LEGACY_OUTCOME, command="echo a"
        ),
        "named": await _seed_dashboard(
            client, db_url, status="completed", outcome="other_name", command="echo n"
        ),
        "plain": await _seed_dashboard(
            client, db_url, status="completed", outcome=None, command="echo p"
        ),
        "failed": await _seed_dashboard(
            client, db_url, status="failed", outcome=EXAMPLE_LEGACY_OUTCOME, command="echo f"
        ),
    }


def _rows_in(body: str, ids: dict[str, int]) -> set[str]:
    return {name for name, tid in ids.items() if f"task-row-{tid}" in body}


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_admin_tasks_alias_filters_status_and_outcome(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    ids = await _seed_dashboard_rows(authed_admin_client, migrated_db_url)

    legacy = await authed_admin_client.get(
        "/admin/tasks", headers=_HX, params={"status": EXAMPLE_LEGACY_STATUS}
    )
    explicit = await authed_admin_client.get(
        "/admin/tasks",
        headers=_HX,
        params={"status": "completed", "outcome": EXAMPLE_LEGACY_OUTCOME},
    )
    assert legacy.status_code == 200, legacy.text
    assert explicit.status_code == 200, explicit.text
    assert _rows_in(legacy.text, ids) == _rows_in(explicit.text, ids) == {"alias"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_admin_tasks_alias_outcome_is_appended_to_explicit_outcomes(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """On the dashboard the alias outcome is ADDED to the request's outcome list;
    it neither replaces it nor yields to it (unlike /outcomes)."""
    ids = await _seed_dashboard_rows(authed_admin_client, migrated_db_url)

    resp = await authed_admin_client.get(
        "/admin/tasks",
        headers=_HX,
        params={"status": EXAMPLE_LEGACY_STATUS, "outcome": "other_name"},
    )
    assert resp.status_code == 200, resp.text
    assert _rows_in(resp.text, ids) == {"alias", "named"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_admin_tasks_alias_combined_with_other_status(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The alias's real status is added to the CSV without duplicating, and the alias
    outcome filters all the statuses of the request."""
    ids = await _seed_dashboard_rows(authed_admin_client, migrated_db_url)

    resp = await authed_admin_client.get(
        "/admin/tasks",
        headers=_HX,
        params={"status": f"completed,{EXAMPLE_LEGACY_STATUS},failed"},
    )
    assert resp.status_code == 200, resp.text
    assert _rows_in(resp.text, ids) == {"alias", "failed"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status_token", [EXAMPLE_LEGACY_STATUS, _RETIRED_STATUS])
async def test_admin_tasks_alias_token_without_plugin_is_400(
    authed_admin_client: AsyncClient, status_token: str
) -> None:
    resp = await authed_admin_client.get(
        "/admin/tasks", headers=_HX, params={"status": status_token}
    )
    assert resp.status_code == 400, resp.text
    assert f"unknown status value(s): {status_token}. valid:" in resp.text


# ---------------------------------------------------------------------------
# Outcomes profile by exit code, end to end through /complete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_exit_7_without_block_on_unrecognised_task_closes_failed(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    """Acceptance case: a task WITHOUT an `outcomes` block that the plugin's extractor
    does not recognize and exits with exit 7 closes `failed` with `outcome` NULL, even if
    the active profile defines exit 7.

    Exit 7 is also curl's "failed to connect to host", the most common curl
    failure in practice. A server-wide profile would label any ad hoc task
    whose first failed command was a curl with the verdict of a tool
    that the task does not use.
    """
    worker_id, key = await _register(app_client)
    task_id = await _insert_task(
        migrated_db_url,
        payload={"commands": ["curl -fsSL https://example.test/f -o f", "./f"]},
        target_count=1,
    )
    body = await _poll(app_client, worker_id, key)

    done = await app_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": key},
        json={"lease_token": body["task"]["lease_token"], "exit_code": 7, "status": "failed"},
    )
    assert done.status_code == 200, done.text

    exec_row = await _row(
        migrated_db_url,
        "SELECT status, outcome, exit_code FROM task_executions WHERE task_id=:tid",
        {"tid": task_id},
    )
    assert exec_row == {"status": "failed", "outcome": None, "exit_code": 7}

    task_row = await _row(
        migrated_db_url,
        "SELECT status, outcome FROM tasks WHERE id=:tid",
        {"tid": task_id},
    )
    assert task_row == {"status": "failed", "outcome": None}


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_exit_7_without_block_on_recognised_task_uses_profile(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    """Positive control of the previous test: the plugin's profile does label a task
    of its tool.

    Without this pair, the NULL `outcome` above would also pass if the profile had
    stopped classifying altogether, the failure mode that is easiest to miss, because
    the absence of a label breaks nothing.
    """
    worker_id, key = await _register(app_client)
    task_id = await _insert_task(
        migrated_db_url, payload=example_payload("example-tool run --input x/y"), target_count=1
    )
    body = await _poll(app_client, worker_id, key)

    done = await app_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": key},
        json={"lease_token": body["task"]["lease_token"], "exit_code": 7, "status": "failed"},
    )
    assert done.status_code == 200, done.text

    exec_row = await _row(
        migrated_db_url,
        "SELECT status, outcome, exit_code FROM task_executions WHERE task_id=:tid",
        {"tid": task_id},
    )
    assert exec_row == {"status": "failed", "outcome": "not_reachable", "exit_code": 7}


@pytest.mark.asyncio
async def test_exit_3_without_plugin_closes_failed_even_for_tool_payload(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    """Core without plugins: no exit code has a default classification. The same
    task that the test profile would close `completed` + `empty_result` closes
    plain `failed`."""
    worker_id, key = await _register(app_client)
    task_id = await _insert_task(migrated_db_url, payload=example_payload(), target_count=1)
    body = await _poll(app_client, worker_id, key)

    done = await app_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": key},
        json={"lease_token": body["task"]["lease_token"], "exit_code": 3, "status": "failed"},
    )
    assert done.status_code == 200, done.text

    task_row = await _row(
        migrated_db_url,
        "SELECT status, outcome FROM tasks WHERE id=:tid",
        {"tid": task_id},
    )
    assert task_row == {"status": "failed", "outcome": None}


@pytest.mark.asyncio
async def test_exit_7_with_own_block_is_labelled_on_unrecognised_task(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    """The scope is only for the DEFAULT: an ad hoc task that declares its own
    `payload.outcomes` block is labeled with its own vocabulary, as always. It is
    the recommended route for any tool without its own plugin."""
    worker_id, key = await _register(app_client)
    task_id = await _insert_task(
        migrated_db_url,
        payload={
            "commands": ["curl -fsSL https://example.test/f -o f"],
            "outcomes": {"exit_codes": {"7": {"name": "no_connection", "closes_as": "failed"}}},
        },
        target_count=1,
    )
    body = await _poll(app_client, worker_id, key)

    done = await app_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": key},
        json={"lease_token": body["task"]["lease_token"], "exit_code": 7, "status": "failed"},
    )
    assert done.status_code == 200, done.text

    task_row = await _row(
        migrated_db_url,
        "SELECT status, outcome FROM tasks WHERE id=:tid",
        {"tid": task_id},
    )
    assert task_row == {"status": "failed", "outcome": "no_connection"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_exit_3_without_block_on_unrecognised_task_is_not_success(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    """Exit 3 of the test profile closes `completed`: it is the costliest case, because
    applied out of scope it would count a failure as a success and raise the `progress_pct`
    of the public status page. A task that the extractor does not
    recognize closes `failed`, like any other exit != 0 that the task did not declare.
    """
    worker_id, key = await _register(app_client)
    task_id = await _insert_task(
        migrated_db_url, payload={"commands": ["./build.sh"]}, target_count=1
    )
    body = await _poll(app_client, worker_id, key)

    done = await app_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": key},
        json={"lease_token": body["task"]["lease_token"], "exit_code": 3, "status": "failed"},
    )
    assert done.status_code == 200, done.text

    task_row = await _row(
        migrated_db_url,
        "SELECT status, outcome FROM tasks WHERE id=:tid",
        {"tid": task_id},
    )
    assert task_row == {"status": "failed", "outcome": None}


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_exit_3_execution_becomes_completed_with_outcome_no_retry(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    """Exit 3 classifies the execution as completed + outcome='empty_result',
    and does not trigger auto-retry (there is no policy on this task anyway)."""
    worker_id, key = await _register(app_client)
    task_id = await _insert_task(migrated_db_url, payload=example_payload(), target_count=1)
    body = await _poll(app_client, worker_id, key)
    token = body["task"]["lease_token"]

    done = await app_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": key},
        json={"lease_token": token, "exit_code": 3, "status": "failed"},
    )
    assert done.status_code == 200, done.text

    exec_row = await _row(
        migrated_db_url,
        "SELECT status, outcome, exit_code FROM task_executions WHERE task_id=:tid",
        {"tid": task_id},
    )
    assert exec_row == {"status": "completed", "outcome": "empty_result", "exit_code": 3}

    # The task closes completed + outcome: the result is expressed as the pair
    # (status, outcome), not as a status of its own in the enum.
    task_row = await _row(
        migrated_db_url,
        "SELECT status, outcome, completed_at FROM tasks WHERE id=:tid",
        {"tid": task_id},
    )
    assert task_row["completed_at"] is not None
    assert task_row["status"] == "completed"
    assert task_row["outcome"] == "empty_result"

    # No retry created: still exactly 1 execution.
    n = await _row(
        migrated_db_url,
        "SELECT count(*) AS n FROM task_executions WHERE task_id=:tid",
        {"tid": task_id},
    )
    assert n["n"] == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_exit_3_not_retried_even_when_listed_as_retryable(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Invariant: exit 3 never reopens the task, even with retryable_exit_codes=[3].

    Guard of `orcaster_server/api/tasks.py::task_complete`: `classify_terminal`
    returns `('completed', 'empty_result')` for exit 3. Unlike a status of its
    own in the enum (a string that would never match
    `AutoRetryPolicy.applies_to`'s `status in ('completed','failed')`), this
    'completed' WOULD match if it were not for the explicit guard that excludes
    any completed-with-outcome closure from the retry matrix. Without that
    guard, this policy (retryable_exit_codes=[3]) would reopen the task.
    """
    worker_id, key = await _register(authed_admin_client)
    task_id = await _create_task_with_policy(
        authed_admin_client,
        auto_retry={"max_attempts": 3, "backoff_seconds": 0, "retryable_exit_codes": [3]},
        payload=example_payload(),
    )
    token = await _claim_lease(authed_admin_client, worker_id, key)
    await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token,
        status="failed",
        exit_code=3,
    )

    row = await _row(
        migrated_db_url,
        "SELECT status, outcome, auto_retry_attempts_used FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    # Not retried: terminal completed+outcome closure and the counter did not move.
    assert row["status"] == "completed"
    assert row["outcome"] == "empty_result"
    assert row["auto_retry_attempts_used"] == 0


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_count_mode_failed_then_exit_3_closes_completed_with_outcome(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """A failed followed by exit-3 with the budget exhausted closes completed. The
    exit-3 is a success with nuance (closes_as=completed): the task closes
    completed + outcome=empty_result even if there is a previous failed in the
    history. DELIBERATE behavior.

    Sequence (target_count=1, same worker via sticky retry):
      1. attempt #1: exit_code=1 (real failure, retryable) -> auto-retry reopens.
      2. attempt #2: exit_code=3 -> completed + outcome='empty_result'.
    """
    worker_id, key = await _register(authed_admin_client)
    task_id = await _create_task_with_policy(
        authed_admin_client,
        auto_retry={"max_attempts": 3, "backoff_seconds": 0, "retryable_exit_codes": [1]},
        payload=example_payload(),
    )

    # Attempt #1: real retryable failure -> auto-retry reopens the task.
    token1 = await _claim_lease(authed_admin_client, worker_id, key)
    status1 = await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token1,
        status="failed",
        exit_code=1,
    )
    assert status1 == 200

    reopened = await _row(
        migrated_db_url,
        "SELECT status FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert reopened["status"] == "queued", "the real failure must have triggered the auto-retry"

    # Attempt #2 (sticky -> same worker): exit 3 -> completed + outcome.
    token2 = await _claim_lease(authed_admin_client, worker_id, key)
    status2 = await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token2,
        status="failed",
        exit_code=3,
    )
    assert status2 == 200

    row = await _row(
        migrated_db_url,
        "SELECT status, outcome FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row["status"] == "completed"
    assert row["outcome"] == "empty_result"

    # Full history: attempt #1 is still 'failed' (it is not rewritten).
    execs = await _rows(
        migrated_db_url,
        "SELECT status, outcome, exit_code FROM task_executions WHERE task_id=:id "
        "ORDER BY attempt_index",
        {"id": task_id},
    )
    assert len(execs) == 2
    assert execs[0] == {"status": "failed", "outcome": None, "exit_code": 1}
    assert execs[1] == {"status": "completed", "outcome": "empty_result", "exit_code": 3}


# ---------------------------------------------------------------------------
# Closure precedence in fan-out (target_count=2, two concurrent workers)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_target2_completed_plus_outcome_closes_completed(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    """1 plain completed + 1 completed with outcome: closes 'completed' (both
    count toward the target) with outcome=NULL (preference for the plain success)."""
    w1, k1 = await _register(app_client, "a")
    w2, k2 = await _register(app_client, "b")
    # target_count=1 so completed=1 reaches target; bump attempt_budget to 2
    # so the 2nd worker can also claim a lease.
    task_id = await _insert_task(migrated_db_url, payload=example_payload(), target_count=1)
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET attempt_budget = 2 WHERE id = :id",
        {"id": task_id},
    )
    # Both workers claim leases before either completes (fan-out pattern).
    body1 = await _poll(app_client, w1, k1)
    body2 = await _poll(app_client, w2, k2)
    # w1 succeeds, w2 reports exit 3 (outcome).
    resp1 = await app_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k1},
        json={"lease_token": body1["task"]["lease_token"], "exit_code": 0, "status": "completed"},
    )
    assert resp1.status_code == 200, resp1.text
    resp2 = await app_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k2},
        json={"lease_token": body2["task"]["lease_token"], "exit_code": 3, "status": "failed"},
    )
    assert resp2.status_code == 200, resp2.text

    row = await _row(
        migrated_db_url,
        "SELECT status, outcome FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row["status"] == "completed"  # real completed >= target wins
    assert row["outcome"] is None  # preference for the plain success


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_target2_failed_plus_outcome_closes_failed(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    """1 real failed + 1 outcome with budget exhausted: task closes as 'failed'.

    target_count=2 requires TWO completed to close 'completed'; with only one
    (the outcome one) and the budget exhausted, it still closes 'failed': the
    outcome does not change this precedence (behavior IDENTICAL to a plain
    failure; only the representation of the winning execution differs)."""
    w1, k1 = await _register(app_client, "a")
    w2, k2 = await _register(app_client, "b")
    task_id = await _insert_task(migrated_db_url, payload=example_payload(), target_count=2)
    # Both workers claim leases first (fan-out pattern).
    body1 = await _poll(app_client, w1, k1)
    body2 = await _poll(app_client, w2, k2)
    # w1 fails with exit 1 (real failure), w2 exits with 3 (outcome).
    resp1 = await app_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k1},
        json={"lease_token": body1["task"]["lease_token"], "exit_code": 1, "status": "failed"},
    )
    assert resp1.status_code == 200, resp1.text
    resp2 = await app_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k2},
        json={"lease_token": body2["task"]["lease_token"], "exit_code": 3, "status": "failed"},
    )
    assert resp2.status_code == 200, resp2.text

    row = await _row(
        migrated_db_url,
        "SELECT status FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    # completed=1 (only w2, with outcome) < target_count=2 -> falls through to
    # terminal_non_expired(2) >= attempt_budget(2) -> failed.
    assert row["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_target2_all_outcome_closes_completed(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    """All the executions exit 3 (outcome): closes 'completed' + outcome
    (a real 'completed', consistent with the precedence rules)."""
    w1, k1 = await _register(app_client, "a")
    w2, k2 = await _register(app_client, "b")
    task_id = await _insert_task(migrated_db_url, payload=example_payload(), target_count=2)
    # Both workers claim leases first (fan-out pattern).
    body1 = await _poll(app_client, w1, k1)
    body2 = await _poll(app_client, w2, k2)
    # Both exit with code 3 (outcome).
    resp1 = await app_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k1},
        json={"lease_token": body1["task"]["lease_token"], "exit_code": 3, "status": "failed"},
    )
    assert resp1.status_code == 200, resp1.text
    resp2 = await app_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k2},
        json={"lease_token": body2["task"]["lease_token"], "exit_code": 3, "status": "failed"},
    )
    assert resp2.status_code == 200, resp2.text

    row = await _row(
        migrated_db_url,
        "SELECT status, outcome FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    # completed=2 (both, with outcome) >= target_count=2 -> completed.
    assert row["status"] == "completed"
    assert row["outcome"] == "empty_result"


# ---------------------------------------------------------------------------
# Regressions of terminal invariants (delete / cancel / poll guards)
# ---------------------------------------------------------------------------
#
# Helper: create a completed+outcome task via the normal poll+complete flow.
# exit_code=3 + status="failed" -> server reclassifies to completed+outcome.


async def _make_outcome_closed_task(client: AsyncClient, db_url: str) -> tuple[int, str, str]:
    """Register a worker, create a task, poll it, complete with exit_code=3.

    Returns (task_id, worker_id, api_key).
    The task row will be status='completed', outcome='empty_result' after this call.
    """
    worker_id, key = await _register(client)
    task_id = await _insert_task(db_url, payload=example_payload(), target_count=1)
    body = await _poll(client, worker_id, key)
    token = body["task"]["lease_token"]
    done = await client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": key},
        json={"lease_token": token, "exit_code": 3, "status": "failed"},
    )
    assert done.status_code == 200, done.text
    return task_id, worker_id, key


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_outcome_task_is_deletable_via_admin(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """DELETE /admin/tasks/{id} must work on a completed+outcome task
    (it is a normal 'completed', already covered by `_TERMINAL_TASK_STATES`)."""
    task_id, _wid, _key = await _make_outcome_closed_task(authed_admin_client, migrated_db_url)

    resp = await authed_admin_client.delete(f"/admin/tasks/{task_id}")
    assert resp.status_code == 204, resp.text

    count = await _row(
        migrated_db_url,
        "SELECT count(*) AS n FROM tasks WHERE id = :id",
        {"id": task_id},
    )
    assert count["n"] == 0, "outcome-closed task row must be deleted; phantom-delete detected"


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_outcome_task_cancel_is_noop(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Cancelling a completed+outcome task must be a no-op (409), not flip it."""
    task_id, _wid, _key = await _make_outcome_closed_task(authed_admin_client, migrated_db_url)

    before = await _row(
        migrated_db_url,
        "SELECT status, outcome, completed_at FROM tasks WHERE id = :id",
        {"id": task_id},
    )
    assert before["status"] == "completed"
    assert before["outcome"] == "empty_result"

    resp = await authed_admin_client.post(f"/admin/tasks/{task_id}/cancel", json={})
    # A terminal task must be rejected (the handler raises 409 when RETURNING is empty).
    assert resp.status_code == 409, (
        f"expected 409 for cancelling a terminal outcome-closed task, "
        f"got {resp.status_code}: {resp.text}"
    )

    after = await _row(
        migrated_db_url,
        "SELECT status, outcome, completed_at FROM tasks WHERE id = :id",
        {"id": task_id},
    )
    assert after["status"] == "completed", "outcome-closed task must not be flipped to cancelled"
    assert after["outcome"] == "empty_result"
    assert after["completed_at"] == before["completed_at"], "completed_at must be unchanged"


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_poll_does_not_reclaim_outcome_task(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """poll must not hand out claims for a task already closed completed+outcome.

    Approach: after closing the task, register a second fresh worker and have
    it poll. The poll must return no task (no_task_available) because the
    only task in the workspace is already in a terminal state.
    """
    task_id, _wid, _key = await _make_outcome_closed_task(authed_admin_client, migrated_db_url)

    # Sanity: task is indeed completed+outcome.
    row = await _row(
        migrated_db_url,
        "SELECT status, outcome FROM tasks WHERE id = :id",
        {"id": task_id},
    )
    assert row["status"] == "completed"
    assert row["outcome"] == "empty_result"

    # Register a fresh second worker and poll — must get nothing.
    worker2_id, key2 = await _register(authed_admin_client, "w2-poll-guard")
    body = await _poll(authed_admin_client, worker2_id, key2)
    assert body.get("task") is None, (
        f"poll guard failed: second worker received task {body.get('task')} "
        "for an outcome-closed task"
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_idempotent_recreate_of_outcome_task_does_not_500(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Re-POST with the idempotency_key of a task already closed completed+outcome
    returns the existing task, not a 500 (TaskStatus still covers the full enum;
    the outcome does not take part in the response schema)."""
    import uuid

    worker_id, key = await _register(authed_admin_client)
    idem = str(uuid.uuid4())
    payload = example_payload("example-tool run --input x/y")

    first = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "payload": payload, "idempotency_key": idem},
    )
    assert first.status_code == 200, first.text
    task_id = int(first.json()["task_id"])

    # Bring the task to completed+outcome: poll + complete with exit_code=3.
    body = await _poll(authed_admin_client, worker_id, key)
    token = body["task"]["lease_token"]
    done = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": key},
        json={"lease_token": token, "exit_code": 3, "status": "failed"},
    )
    assert done.status_code == 200, done.text

    sanity = await _row(
        migrated_db_url, "SELECT status, outcome FROM tasks WHERE id=:id", {"id": task_id}
    )
    assert sanity["status"] == "completed"
    assert sanity["outcome"] == "empty_result"

    # Idempotent replay: same key + same payload → existing task, NOT 500.
    replay = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "payload": payload, "idempotency_key": idem},
    )
    assert replay.status_code == 200, replay.text
    rbody = replay.json()
    assert rbody["task_id"] == task_id
    assert rbody["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_task_detail_surfaces_outcome_execution_in_summary(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The task-detail summary must count the execution with outcome
    (`_aggregates`'s generic `outcomes` bucket)."""
    task_id, _wid, _key = await _make_outcome_closed_task(authed_admin_client, migrated_db_url)

    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    assert resp.status_code == 200, resp.text
    assert 'class="stat-label">empty_result</span>' in resp.text, (
        "task-detail summary must surface the outcome execution, not drop it silently"
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_task_detail_treats_outcome_task_as_terminal(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The task-detail must treat a completed+outcome task as terminal:
    active Retry and Delete button (not Cancel)."""
    task_id, _wid, _key = await _make_outcome_closed_task(authed_admin_client, migrated_db_url)

    resp = await authed_admin_client.get(f"/admin/tasks/{task_id}")
    assert resp.status_code == 200, resp.text
    body = resp.text

    assert "data-retry" in body
    assert 'aria-disabled="true"' not in body, (
        "outcome-closed task is terminal: Retry must not be disabled"
    )
    assert "hx-delete=" in body, "terminal outcome-closed task must offer Delete"
    assert "/cancel" not in body, "terminal outcome-closed task must not offer Cancel (would 409)"


# ---------------------------------------------------------------------------
# The outcome of the 'failed' closure of an all_available task must come from
# the CURRENT epoch, not from any failed execution of an earlier epoch already
# superseded by a retry (the subquery must be scoped to snapshot_epoch, unlike
# the one of the 'completed' closure).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_available_failed_close_outcome_is_epoch_scoped(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Reproduces the bug by forcing `finished_at` of epoch 0 into the future: without
    the epoch scope, the subquery's `ORDER BY finished_at DESC` would pick that
    old row over the real failed of epoch 1 (current), even though it
    no longer represents the task's current state after the retry."""
    worker_id, key = await _register(authed_admin_client)
    outcomes_payload = {
        "outcomes": {
            "exit_codes": {
                "9": {"name": "epoch0_reason", "closes_as": "failed"},
                "8": {"name": "epoch1_reason", "closes_as": "failed"},
            }
        }
    }
    create = await authed_admin_client.post(
        "/admin/tasks", json={"target": "all-available", "payload": outcomes_payload}
    )
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    # Epoch 0: fails with outcome 'epoch0_reason' (only online worker -> closes).
    token0 = await _claim_lease(authed_admin_client, worker_id, key)
    status0 = await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token0,
        status="failed",
        exit_code=9,
    )
    assert status0 == 200

    row0 = await _row(
        migrated_db_url, "SELECT status, outcome FROM tasks WHERE id=:id", {"id": task_id}
    )
    assert row0["status"] == "failed"
    assert row0["outcome"] == "epoch0_reason"

    # Move the finished_at of epoch 0 into the future (forces the bug if the
    # subquery is not scoped to snapshot_epoch).
    await _exec_sql(
        migrated_db_url,
        "UPDATE task_executions SET finished_at = now() + interval '1 hour' "
        "WHERE task_id = :id AND snapshot_epoch = 0",
        {"id": task_id},
    )

    # Retry: bumps snapshot_epoch and re-snapshots against the (only)
    # online worker. The bump opens epoch 1.
    retry = await authed_admin_client.post(f"/admin/tasks/{task_id}/retry", json={})
    assert retry.status_code == 200, retry.text

    # Epoch 1: fails with a DIFFERENT outcome.
    token1 = await _claim_lease(authed_admin_client, worker_id, key)
    status1 = await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token1,
        status="failed",
        exit_code=8,
    )
    assert status1 == 200

    row1 = await _row(
        migrated_db_url,
        "SELECT status, outcome, snapshot_epoch FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row1["status"] == "failed"
    assert row1["snapshot_epoch"] == 1
    # The outcome must reflect the CURRENT epoch (1), not 0 despite having a
    # more recent finished_at (deliberately tampered with above).
    assert row1["outcome"] == "epoch1_reason"
