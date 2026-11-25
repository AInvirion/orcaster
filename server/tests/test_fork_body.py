"""Unit tests for `_build_fork_body` (admin_ui).

Rebuilds the POST `/admin/tasks` body for the "Fork" button. These tests
focus on `target_mode` taking precedence over targeting: an `all_available`
task must fork preserving that intent even if it has already run on one or
more workers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from orcaster_server.api.admin_ui import _build_fork_body


def _task(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "target_mode": "count",
        "target_count": 1,
        "target_worker_pool": None,
        "target_worker_id": None,
        "payload": {"foo": "bar"},
        "priority": 0,
        "claim_timeout_seconds": None,
        "delivery_mode": "claim",
    }
    base.update(over)
    return base


def _attempt(worker_id: str) -> dict[str, Any]:
    return {
        "worker_id": worker_id,
        "started_at": datetime(2026, 5, 26, 12, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 5, 26, 12, 1, tzinfo=UTC),
    }


def test_fork_open_all_available_with_attempts_preserves_intent() -> None:
    """An open `all-available` task that already ran on several workers used
    to fork to `worker:<last>`, losing the intent. It must now fork to
    `all-available`."""
    task = _task(target_mode="all_available", target_worker_pool=None)
    attempts = [_attempt("11111111-1111-1111-1111-111111111111")]
    body = _build_fork_body(task, attempts)
    assert body["target"] == "all-available"


def test_fork_restricted_all_available_preserves_pool() -> None:
    """`all:<ids>` (restricted pool) forks as `all:<ids>`."""
    pool = ["aaaa1111-1111-1111-1111-111111111111", "bbbb2222-2222-2222-2222-222222222222"]
    task = _task(target_mode="all_available", target_worker_pool=pool)
    body = _build_fork_body(task, [])
    assert body["target"] == "all:" + ",".join(pool)


def test_fork_count_with_last_worker_pins_to_worker() -> None:
    """count without a pool and with a known last worker → `worker:<id>` (unchanged)."""
    wid = "cccc3333-3333-3333-3333-333333333333"
    task = _task(target_mode="count", target_count=1)
    body = _build_fork_body(task, [_attempt(wid)])
    assert body["target"] == f"worker:{wid}"


def test_fork_count_no_attempts_uses_count() -> None:
    """count without previous executions → `<N>`."""
    task = _task(target_mode="count", target_count=3)
    body = _build_fork_body(task, [])
    assert body["target"] == "3"
