"""Task secret deliveries refused by the poll, to show to the operator.

When a task declares a stored secret that the server cannot decrypt, or no keys
are configured, the task stays `queued` and the poll does not deliver it. This
module keeps, in process memory, the last time each task was refused, so that
the dashboard can show a notice without consulting the log.

State is per process: with several uvicorn processes each one sees only its own
refusals. Size is bounded; entries older than the window are not returned and
are discarded when the cap is exceeded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

# Window during which a refusal stays visible, in monotonic clock seconds.
REFUSAL_WINDOW_SECONDS = 15 * 60.0
_MAX_ENTRIES = 500

Kind = Literal["keys_missing", "undecryptable"]


@dataclass(frozen=True)
class Refusal:
    """A recent refusal: the task, the worker, the cause and the names (never values)."""

    kind: Kind
    names: tuple[str, ...]
    task_id: int
    worker_id: str


@dataclass
class _Entry:
    workspace_id: str
    at: float
    refusal: Refusal


# Key (workspace, kind, task_id): a task refused in several polls takes a single
# entry, with the time of the last refusal.
_REFUSALS: dict[tuple[str, str, int], _Entry] = {}


def record_refusal(
    *,
    workspace_id: str,
    worker_id: str,
    task_id: int,
    kind: Kind,
    names: list[str] | tuple[str, ...],
) -> None:
    """Record (or refresh) the refusal of `task_id`. Names only, never values."""
    key = (workspace_id, kind, task_id)
    now = time.monotonic()
    # Assigning to an existing key does not move it in the dict, so it is popped
    # first. This keeps insertion order equal to the last refusal of each task,
    # so the eviction below drops the least recently refused.
    _REFUSALS.pop(key, None)
    _REFUSALS[key] = _Entry(
        workspace_id=workspace_id,
        at=now,
        refusal=Refusal(
            kind=kind, names=tuple(sorted(names)), task_id=task_id, worker_id=worker_id
        ),
    )
    if len(_REFUSALS) > _MAX_ENTRIES:
        # Drop the least recently refused: the dict keeps insertion order and
        # the `pop` above leaves each key at the position of its last refusal.
        for old in list(_REFUSALS)[: len(_REFUSALS) - _MAX_ENTRIES]:
            del _REFUSALS[old]


def recent_refusals(workspace_id: str) -> list[Refusal]:
    """Refusals of the workspace within the last `REFUSAL_WINDOW_SECONDS` in this process."""
    now = time.monotonic()
    return [
        e.refusal
        for e in _REFUSALS.values()
        if e.workspace_id == workspace_id and now - e.at < REFUSAL_WINDOW_SECONDS
    ]
