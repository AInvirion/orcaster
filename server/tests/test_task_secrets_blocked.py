"""In-memory record of refused secret deliveries.

`record_refusal` bounds the record to `_MAX_ENTRIES` and, once exceeded, evicts the
least recently refused entries: refusing an already recorded task again refreshes it
and moves it to the end of the eviction order.
"""

from __future__ import annotations

import pytest

from orcaster_server.task_secrets import blocked
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID

_WS = str(DEFAULT_WORKSPACE_ID)


def _refuse(task_id: int, kind: blocked.Kind = "undecryptable") -> None:
    blocked.record_refusal(
        workspace_id=_WS, worker_id="w1", task_id=task_id, kind=kind, names=["API_TOKEN"]
    )


def _task_ids() -> set[int]:
    return {r.task_id for r in blocked.recent_refusals(_WS)}


def test_eviction_drops_the_least_recently_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(blocked, "_MAX_ENTRIES", 3)
    for task_id in (1, 2, 3):
        _refuse(task_id)
    # Task 1 is refused again: it is the most recent, even though it was inserted first.
    _refuse(1)
    _refuse(4)
    assert _task_ids() == {1, 3, 4}


def test_eviction_keeps_insertion_order_without_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(blocked, "_MAX_ENTRIES", 3)
    for task_id in (1, 2, 3, 4, 5):
        _refuse(task_id)
    assert _task_ids() == {3, 4, 5}


def test_refusing_the_same_task_twice_keeps_one_entry_with_the_latest_data() -> None:
    _refuse(7)
    blocked.record_refusal(
        workspace_id=_WS, worker_id="w2", task_id=7, kind="undecryptable", names=["B", "A"]
    )
    [refusal] = blocked.recent_refusals(_WS)
    assert refusal.worker_id == "w2"
    assert refusal.names == ("A", "B")
