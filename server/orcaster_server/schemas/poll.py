"""Pydantic schemas for POST /v1/workers/{worker_id}/poll.

A single poll combines heartbeat and claim.

There is no `worker_commands` table. The response does NOT include
`commands: list[...]`. Workers receive push commands as regular tasks with
`delivery_mode='push'` and an optional signature.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class PollRequest(BaseModel):
    """Poll body. Combines heartbeat + claim.

    `status` indicates availability: `idle` allows claiming open tasks;
    `busy` only receives targeted push tasks (delivery_mode='push' to the worker).

    `lease_token` travels when the worker continues with a live execution: the
    server uses it as proof of ownership to validate that the execution is still
    current and to refresh the lease implicitly. The renewal is implemented in the
    handler: a `busy` poll with `lease_token` renews the `lease_expires_at` of the
    worker's own execution (`claimed`/`running`) and persists
    `current_execution_progress`. See `api/poll.py::_SQL_POLL_RENEW_LEASE`.
    """

    status: Literal["idle", "busy"]
    lease_token: UUID | None = None
    current_task_id: int | None = None
    current_execution_progress: int | None = Field(default=None, ge=0, le=100)
    # Default 120 aligned with the worker (ORCH_LEASE_SECONDS): it is the lease
    # the server would write if a poll omitted the field. The current worker
    # always sends it; keeping it at 120 is defense in depth.
    lease_seconds: int = Field(default=120, ge=1, le=3600)
    name: str | None = Field(default=None, max_length=128)
    ram_mb: int | None = Field(default=None, ge=0)
    ram_used_mb: int | None = Field(default=None, ge=0)
    disk_mb: int | None = Field(default=None, ge=0)
    disk_used_mb: int | None = Field(default=None, ge=0)
    # Free disk fraction (free/total of the cache volume, in bytes) for the
    # server-side disk gate. Additive: default None. An older worker does not send
    # it -> NULL in the column -> it is never gated (fail-open, preserves the
    # previous behaviour and bidirectional compatibility). It does NOT carry a
    # strict validator that raises 422: the poll is a hot path (see the lenient
    # `version` normalizer below). A missing/garbage/out-of-[0,1] value is mapped
    # to None = "unknown" = do not gate.
    free_disk_fraction: float | None = None
    cpu_pct: int | None = Field(default=None, ge=0, le=100)
    # Worker version. It travels in the poll and not only in `register` because
    # `register` runs only once per identity: a worker that is upgraded keeps its
    # `state.json` and never registers again, so without this the column would stay
    # frozen at the enrollment version. Optional: an older worker that does not
    # send it must not overwrite the stored one.
    version: str | None = None
    # Capabilities declared by the worker, same contract as `register` (flat map
    # name -> boolean|number, `orcaster_server/eligibility.py`). They travel in the
    # poll for the same reason as `version`: `register` runs only once per identity,
    # and re-enrolling to change them creates a new UUID. Absent (None) = "leave the
    # stored one alone": an older worker does not send it. Type `Any` on purpose:
    # the CONTENT is validated in the handler, which ignores an invalid value
    # instead of answering 422 (see `worker_poll`, step 2).
    capabilities: Any = None
    cache_flush_completed: bool = False  # the worker finished a requested cleanup

    @field_validator("version", mode="before")
    @classmethod
    def _normalize_version(cls, v: object) -> object:
        """Normalizes instead of rejecting: the poll is a hot path.

        `version` is cosmetic data (a display column in the dashboard), and the
        poll is the only thing that keeps the worker alive. A 422 here is not seen
        as an "invalid field": the worker retries with multiplicative backoff
        against a body that will never change, stops claiming tasks and stays
        offline forever because of a badly written string in the `.env`. That is
        why `register` (one-shot, loud failure) does validate length and the poll
        does not.

        Empty/blank is treated as absent so as not to overwrite the stored version
        with `""` (a bare `ORCH_VERSION=` in the `.env`), and a long value is
        truncated to the 64 chars that `register` validates.
        """
        if not isinstance(v, str):
            return v
        v = v.strip()
        return v[:64] if v else None

    @field_validator("free_disk_fraction", mode="before")
    @classmethod
    def _normalize_free_disk_fraction(cls, v: object) -> object:
        """Normalizes instead of rejecting: the poll is a hot path (same criterion
        as `version`).

        `free_disk_fraction` feeds the disk gate, not a flow that should fail
        loudly: a 422 here would take the worker out of service over a malformed
        value, when the correct semantics of "cannot measure" is NULL = do not gate
        (fail-open). Therefore anything that is not a finite number within
        [0.0, 1.0] is mapped to None: absent, non-numeric, NaN/inf, or out of range.
        A valid value passes through unchanged.
        """
        if v is None:
            return None
        # bool is a subclass of int: `float(True)`/`float(False)` would give 1.0/0.0,
        # and a JSON `false` would gate the worker (0.0 < threshold). A boolean is
        # not a disk measurement: it is garbage -> None.
        if isinstance(v, bool):
            return None
        try:
            f = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        # NaN/inf and out of [0,1] -> None (fail-open, do not gate).
        if f != f or f in (float("inf"), float("-inf")):
            return None
        if f < 0.0 or f > 1.0:
            return None
        return f


class PollTaskPayload(BaseModel):
    """Task delivered to the worker.

    `created_at` travels in the payload because the client needs it to
    reconstruct the canonical signing message (6 fields, lex order:
    command, created_at, idempotency_key, payload, task_id, worker_id).

    `signature` is standard base64 (not url-safe) over the Ed25519 bytes.
    `worker_id` is the effective recipient of the delivery (always the
    authenticated worker). `target_worker_id` is only set when `delivery_mode='push'`.
    """

    task_id: int
    command: str | None = None  # 'run_batch' for work tasks; reserved name for control ops
    payload: dict = Field(default_factory=dict)
    created_at: datetime
    delivery_mode: Literal["claim", "push"]
    signed: bool = False
    signature: str | None = None  # base64 when signed=true
    idempotency_key: UUID | None = None
    worker_id: UUID  # effective recipient (the worker that polls)
    target_worker_id: UUID | None = None  # set only if delivery_mode='push'
    lease_token: UUID
    lease_expires_at: datetime
    execution_timeout_seconds: int | None = None  # outer timeout; NULL = no limit
    # Workspace secrets declared in `payload.task_env`, decrypted in memory.
    # Sibling of `payload`: outside the signature and the DB. `{}` without
    # `task_env` or without a configured store. `repr=False` so that no log of the
    # model shows them.
    secrets: dict[str, str] = Field(default_factory=dict, repr=False)
    # Names in `secrets` whose value came from a row of the worker itself and not
    # from the workspace one. Names only, sorted; outside the signature like
    # `secrets`. An older worker ignores it (it reads key by key).
    worker_scoped_secrets: list[str] = Field(default_factory=list)


class PollCancelSignal(BaseModel):
    """Cancellation signal for an execution the worker had alive."""

    task_id: int
    lease_token: UUID


class PollResponse(BaseModel):
    """Unified poll response.

    `task` is singular (`PollTaskPayload | None`): each poll delivers at most one
    task. `cancel_signals` can carry N (a worker can have several cancelled
    executions pending notification).

    `next_poll_after_seconds` lets the server modulate the cadence
    (server-driven backoff): by default 30s idle, 5s busy, 1s with a task.
    """

    task: PollTaskPayload | None = None
    cancel_signals: list[PollCancelSignal] = Field(default_factory=list)
    next_poll_after_seconds: int = 30
    clear_cache: bool = False  # control signal: the worker must clear its disk cache
    # Reason why the server is not delivering new tasks to this workspace
    # ("quota" | "disk"); None if there is space. Informational for the
    # worker/dashboard; the worker does not need to act. The Literal documents the
    # real domain (mirrors StorageStatus.paused_reason) and enumerates it in OpenAPI.
    storage_paused: Literal["quota", "disk"] | None = None
    # The server applied the disk gate to this worker (its reported
    # `free_disk_fraction` is below the central threshold). Informational for the
    # worker/dashboard; it does not change the claim contract (the gate already
    # closes steps 4-6 of the server-side pick). None/False = not gated (includes
    # a NULL fraction = unknown).
    disk_gated: bool = False
