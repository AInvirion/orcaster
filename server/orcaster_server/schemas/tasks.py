"""Pydantic schemas for `/v1/tasks/*` and
`/admin/tasks` endpoints."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

from orcaster_server.eligibility import validate_eligibility
from orcaster_server.env_names import validate_payload_egress_allowlist, validate_payload_task_env
from orcaster_server.models import TaskStatus
from orcaster_server.task_outcomes import parse_outcomes_block

# --- Sticky auto-retry policy --------------------------------------------


# Exit code convention: 75 = EX_TEMPFAIL (Unix). Reserved for a "transient,
# retry-eligible failure" (e.g. a recoverable rate limit). The default of
# `retryable_exit_codes` includes [75] so that tasks with auto_retry enabled
# retry automatically without needing to extend the list.
_DEFAULT_RETRYABLE_EXIT_CODES: list[int] = [75]


class AutoRetryPolicy(BaseModel):
    """Sticky, server-driven auto-retry policy.

    Persisted in `tasks.auto_retry_policy` (JSONB). NULL = backward compatible
    (no automatic retry; the operator must use the
    `/admin/tasks/{id}/retry` endpoint manually).

    Fields:
    - `max_attempts`: hard budget of automatic retries. 0 = off.
    - `sticky`: if True, the retry is rerouted to the worker of the last attempt
      via `target_worker_pool=[uuid]` (reuse of `ORCASTER_CACHE_DIR`).
    - `retryable_exit_codes`: list of exit codes considered retryable when
      `status='completed'` is received with that code. Default [75].
    - `retryable_cancelled_reasons`: retryable cancel reasons
      (e.g. ["outer_timeout"]). Default empty.
    - `backoff_seconds`: minimum delay between attempts. Reserved: it only
      takes effect if the scheduling schema supports a future `run_at`
      (which it does).
    - `sticky_offline_timeout_seconds`: if the pinned worker has been offline
      longer than this span, the `_relax_sticky_pool` housekeeping clears the
      pool so that any eligible worker can take over.
    """

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=0, ge=0, le=20)
    sticky: bool = True
    retryable_exit_codes: list[int] = Field(
        default_factory=lambda: list(_DEFAULT_RETRYABLE_EXIT_CODES)
    )
    retryable_cancelled_reasons: list[str] = Field(default_factory=list)
    backoff_seconds: int = Field(default=60, ge=0, le=3600)
    sticky_offline_timeout_seconds: int = Field(default=1800, ge=0, le=86400)

    def applies_to(
        self,
        *,
        status: str,
        exit_code: int | None,
        cancelled_reason: str | None,
    ) -> bool:
        """Would this close combination trigger auto-retry?

        - status in ('completed', 'failed') + exit_code in
          `retryable_exit_codes`. The worker reports `failed` when any command
          of the batch exits with a non-zero code (`dispatcher.py` returns
          "error" -> `main.py:_runner` maps it to `status='failed'`); the server
          decides whether that exit code is retryable according to the task's
          policy.
        - status='cancelled' + cancelled_reason in
          `retryable_cancelled_reasons`.
        - any other case (no exit_code, generic failure without a code in the
          list, etc.): no retry.
        """
        if self.max_attempts <= 0:
            return False
        if status in ("completed", "failed"):
            return exit_code is not None and exit_code in self.retryable_exit_codes
        if status == "cancelled":
            return (
                cancelled_reason is not None
                and cancelled_reason in self.retryable_cancelled_reasons
            )
        return False


# There are no claim or heartbeat schemas for tasks: both are covered by the
# unified poll endpoint (see orcaster_server/schemas/poll.py).


# Per-event cap: the worker reads chunks of up to 64 KiB
# (dispatcher `stream.read(65536)`), so a legitimate event never exceeds
# 65536 chars. 2x margin; a larger chunk is a buggy/foreign client -> 422.
OUTPUT_EVENT_TEXT_MAX_CHARS = 128 * 1024


class OutputEventIn(BaseModel):
    """An output event emitted by the worker (full output capture).

    `seq` is monotonic per execution (1-indexed); `ts` is the instant the chunk
    was read on the worker (UTC). The server inserts each event into
    `output_events` idempotently via `UNIQUE(execution_id, seq)`.
    """

    seq: int = Field(ge=1)
    ts: datetime
    stream: Literal["stdout", "stderr", "command"]
    text: str = Field(max_length=OUTPUT_EVENT_TEXT_MAX_CHARS)


class TaskOutputRequest(BaseModel):
    lease_token: UUID
    # The output travels as a batch of events, not as concatenated text
    # (source of truth: `output_events`).
    events: list[OutputEventIn] = Field(default_factory=list)


# No `stdout`/`stderr`: the worker does not send them. An older worker that
# still sends them keeps receiving 200 (Pydantic default: extra="ignore").
class TaskCompleteRequest(BaseModel):
    lease_token: UUID
    exit_code: int | None = None
    # `cancelled`: the worker reports a terminal cancel when it kills a
    # subprocess because of the outer timeout (`execution_timeout_seconds`) or
    # another cooperative reason. `cancelled_reason` documents the reason
    # (e.g. `outer_timeout`) and is persisted in `tasks.cancelled_reason` when
    # the task closes.
    status: Literal["completed", "failed", "cancelled"]
    cancelled_reason: str | None = None

    @model_validator(mode="after")
    def _check_cancelled_reason(self) -> TaskCompleteRequest:
        # `cancelled_reason` only makes sense when status='cancelled'.
        # If the worker sends a reason with a terminal status other than
        # cancelled, we silently ignore it (it is not a 422: it simplifies the
        # contract and avoids breaking workers that decide to attach context in
        # other states).
        if self.status != "cancelled":
            self.cancelled_reason = None
        return self


class TaskCompleteResponse(BaseModel):
    ok: bool = True


# --- Artifacts ------------------------------------------------------------


class TaskArtifactOut(BaseModel):
    """Metadata of a persisted artifact (response of the upload endpoint)."""

    rel_path: str
    size_bytes: int
    sha256: str
    content_type: str


class TaskArtifactsUploadResponse(BaseModel):
    """202 response of POST /v1/tasks/{id}/artifacts: the artifacts of the batch."""

    accepted: list[TaskArtifactOut]


# --- Admin: POST /admin/tasks ---------------------------------------------

_TARGET_COUNT_RE = re.compile(r"^[1-9][0-9]*$")

# Canonical UUID, lower/upper case, 8-4-4-4-12.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _parse_uuid_csv(raw: str, *, prefix: str) -> list[str]:
    """Parses `<u1>,<u2>,...` validating syntax.

    Rules:
    - At least 1 token.
    - No empty tokens (rejects `a,,b`, `,`, `a,` ...).
    - Each token must match the canonical UUID format.

    Returns the normalized (lower-case) UUIDs. Raises `ValueError` with an
    informative message (includes the prefix) so the 422 is readable.
    Validating existence against `workers` is done server-side in the handler
    (not here: Pydantic has no DB).
    """
    if raw == "":
        raise ValueError(f"target '{prefix}' requires at least one worker id")
    tokens = raw.split(",")
    if any(t == "" for t in tokens):
        raise ValueError(
            f"target '{prefix}{raw}' has empty UUID tokens (no leading/trailing/double commas)"
        )
    for t in tokens:
        if not _UUID_RE.match(t):
            raise ValueError(f"target '{prefix}{raw}' contains malformed UUID '{t}'")
    return [t.lower() for t in tokens]


def validate_target_syntax(v: str) -> str:
    """Validates the syntax of `target`, returning it unchanged.

    Shared by `AdminCreateTaskRequest` and `ApiCreateTaskRequest` (API v1) so
    that both contracts reject the same malformed `target` values with 422.
    """
    if v == "all-available":
        return v
    if _TARGET_COUNT_RE.match(v):
        return v
    # Syntax: worker:<id>, workers:<ids>, all:<ids>.
    if v.startswith("worker:"):
        rest = v[len("worker:") :]
        ids = _parse_uuid_csv(rest, prefix="worker:")
        if len(ids) != 1:
            raise ValueError(
                "target 'worker:<id>' requires exactly one UUID; "
                "use 'workers:<id1>,<id2>' for multiple"
            )
        return v
    if v.startswith("workers:"):
        _parse_uuid_csv(v[len("workers:") :], prefix="workers:")
        return v
    if v.startswith("all:"):
        _parse_uuid_csv(v[len("all:") :], prefix="all:")
        return v
    raise ValueError(
        "target must be 'all-available', a positive integer string like "
        "'1'/'5', or 'worker:<uuid>' / 'workers:<u1>,<u2>' / "
        "'all:<u1>,<u2>'"
    )


_CANONICAL_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _require_canonical_uuid(v: object) -> object:
    """BeforeValidator: requires the canonical 8-4-4-4-12 format with hyphens.

    Pydantic accepts non-canonical forms by default (without hyphens, with braces,
    `urn:uuid:`) that the `orcaster` CLI rejects locally. We tighten this so the
    server contract is identical to the CLI's. We only validate when the value
    arrives as a str; an already parsed UUID (internal step, e.g.
    ApiCreateTaskRequest.to_admin_request) passes without reprocessing.
    """
    if isinstance(v, str) and not _CANONICAL_UUID_RE.fullmatch(v):
        raise ValueError(
            "must be a canonical UUID (8-4-4-4-12 hex digits with hyphens, "
            "e.g. 550e8400-e29b-41d4-a716-446655440000)"
        )
    return v


CanonicalUUID = Annotated[UUID, BeforeValidator(_require_canonical_uuid)]


class AdminCreateTaskRequest(BaseModel):
    """Body for `POST /admin/tasks` - work tasks only.

    `command` is not part of the external contract. The server always stores
    'run_batch'. Control ops go via `POST /admin/workers/{id}/control-op`.

    `target` is a string. Accepted syntax:

    - `"N"` (integer >=1)                   -> count, pool=None.
    - `"all-available"`                     -> all_available, pool=None.
    - `"worker:<uuid>"`                     -> count=1, pool=[uuid].
    - `"workers:<u1>,<u2>,...,<un>"`        -> count=n, pool=[u1..un].
    - `"all:<u1>,<u2>,...,<un>"`            -> all_available restricted to the subset.

    `signed` remains as a deprecated field (ignored; always signed).
    """

    model_config = ConfigDict(extra="forbid")

    payload: dict = Field(default_factory=dict)
    target: str = Field(default="1")
    eligibility: dict = Field(default_factory=dict)
    claim_timeout_seconds: int | None = Field(default=None, ge=1)
    execution_timeout_seconds: int | None = Field(default=None, ge=1)
    run_at: datetime | None = None
    priority: int = Field(default=0, ge=-32768, le=32767)
    delivery_mode: Literal["claim", "push"] = "claim"
    target_worker_id: UUID | None = None
    signed: bool = True  # DEPRECATED - ignored; always signed.
    idempotency_key: CanonicalUUID | None = None
    # Sticky, server-driven auto-retry policy. NULL = no automatic retry.
    auto_retry: AutoRetryPolicy | None = None

    @field_validator("target")
    @classmethod
    def _validate_target(cls, v: str) -> str:
        return validate_target_syntax(v)

    @field_validator("eligibility")
    @classmethod
    def _validate_eligibility(cls, v: dict) -> dict:
        """Validates `eligibility.capabilities` (`orcaster_server/eligibility.py`).

        Only that key is interpreted; the rest of `eligibility` remains free-form
        JSON with no reader. A requirement the matcher does not know how to
        evaluate is rejected here with 422 instead of being persisted and silently
        matching nobody. It covers create, API v1 (`to_admin_request`) and fork
        (`merge_fork_params`), which build this same request.
        """
        validate_eligibility(v)  # ValueError -> 422 with an actionable message
        return v

    @field_validator("payload")
    @classmethod
    def _validate_isolation_fields(cls, v: dict) -> dict:
        """Validates the reserved isolation keys if present.

        Tri-state semantics: absent = legacy; present (even []) = strict.
        Only the shape is validated here; enforcement lives in the worker.
        """
        # Only on create: this validator does not run when reading stored rows.
        validate_payload_task_env(v)
        validate_payload_egress_allowlist(v)
        return v

    @field_validator("payload")
    @classmethod
    def _validate_outcomes_block(cls, v: dict) -> dict:
        """Validates the SHAPE of payload.outcomes: exit codes 1-255, non-reserved
        name slug, closes_as in {completed, failed}. The semantics are applied by
        the server in /complete (orcaster_server.task_outcomes.classify_terminal).
        The fork inherits the payload merged by merge_fork_params, which builds
        this same request, so the validation covers create, API v1 and fork.
        """
        parse_outcomes_block(v)  # ValueError -> 422 with an actionable message
        return v

    @model_validator(mode="after")
    def _check_consistency(self) -> AdminCreateTaskRequest:
        # delivery_mode='push' requires target_worker_id (a DB CHECK backs it).
        if self.delivery_mode == "push" and self.target_worker_id is None:
            raise ValueError("delivery_mode='push' requires target_worker_id")
        if self.target_worker_id is not None:
            # target_worker_id is only valid with target_mode='count' + target_count=1
            # (mirror of the DB CHECK constraint). The pool syntax (`workers:`,
            # `all:`) is also rejected here because it introduces its own pool/count.
            if self.target == "all-available":
                raise ValueError("target_worker_id is incompatible with target='all-available'")
            if self.target != "1":
                raise ValueError(
                    "target_worker_id requires target='1' (target_mode='count', target_count=1)"
                )
        return self

    def parsed_target(
        self,
    ) -> tuple[Literal["count", "all_available"], int, list[str] | None]:
        """Returns `(target_mode, target_count, pool)` derived from `target`.

        - `pool=None` for `"N"` and `"all-available"` (no restriction).
        - `pool=[ids]` for `worker:`/`workers:`/`all:` (lower-case list).
        - `target_count` represents the nominal size of the pool (not the
          effective snapshot). For `all:<ids>` it is adjusted after materializing
          the snapshot in the handler (see `server/orcaster_server/api/admin_tasks.py`).
        """
        v = self.target
        if v == "all-available":
            return "all_available", 0, None
        if _TARGET_COUNT_RE.match(v):
            return "count", int(v), None
        if v.startswith("worker:"):
            ids = _parse_uuid_csv(v[len("worker:") :], prefix="worker:")
            return "count", 1, ids
        if v.startswith("workers:"):
            ids = _parse_uuid_csv(v[len("workers:") :], prefix="workers:")
            return "count", len(ids), ids
        if v.startswith("all:"):
            ids = _parse_uuid_csv(v[len("all:") :], prefix="all:")
            # Tentative target_count = |pool|; it is overwritten with the real size
            # of the snapshot in the handler.
            return "all_available", len(ids), ids
        # Unreachable: _validate_target already rejected everything else.
        raise ValueError(f"unreachable: unparseable target '{v}'")


def build_admin_request[T](factory: Callable[[], T]) -> T:
    """Runs `factory` (builds an `AdminCreateTaskRequest`), translating
    `pydantic.ValidationError` to `RequestValidationError`.

    `POST /admin/tasks` receives `AdminCreateTaskRequest` as a route parameter:
    FastAPI validates the body BEFORE entering the handler, so an invalid payload
    (e.g. a malformed `payload.outcomes`) already surfaces as a 422 via the
    `RequestValidationError` that FastAPI raises internally.

    `POST /api/v1/tasks` (`ApiCreateTaskRequest.to_admin_request`) and the public
    API fork (`merge_fork_params`), on the other hand, build the effective
    `AdminCreateTaskRequest` INSIDE the handler, with the incoming body already
    validated. A `pydantic.ValidationError` there has no registered handler in
    `orcaster_server/main.py` (which only covers `StarletteHTTPException` and
    `RequestValidationError`) and would surface as a 500 without this wrapper.
    This helper aligns both call sites with the same uniform 422 that
    `/admin/tasks` already uses.
    """
    try:
        return factory()
    except PydanticValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


class AdminCreateTaskResponse(BaseModel):
    task_id: int
    # `failed` appears when `all:<ids>` yields an empty snapshot (every worker
    # in the subset offline at creation): the task is materialised already
    # closed with cancelled_reason='no_eligible_workers'.
    # An idempotent replay (same idempotency_key) returns the existing task
    # with its CURRENT status, which can be any value of the task_status
    # enum. It is typed with the derived StrEnum (single source) instead of a
    # hardcoded Literal, so a new enum value is covered without touching this
    # schema. The nuances of a close (e.g. `completed` +
    # `outcome='no_result'`) are not statuses of their own, so they add no
    # new value here.
    status: TaskStatus
    target: str
    target_mode: Literal["count", "all_available"]
    target_count: int
    # Fork lineage (migration 0019): id of the parent task when the task was
    # created via POST /admin/tasks/{id}/fork. NULL for normal creation via /tasks.
    parent_task_id: int | None = None
    # Why the `all_available` snapshot came out empty, measured at creation.
    # NULL when there was at least one eligible worker (and in every count-mode,
    # which does not snapshot). One of `dispatch_eligibility.BLOCK_REASONS`:
    # `no_workers_online` and `no_workers_with_capabilities` are the two cases
    # that a single `target_count=0` cannot tell apart. With `all-available` the
    # task stays `dispatching` waiting for an eligible worker; with `all:<ids>`
    # it closes `failed` and the same reason is stored in `last_error`.
    no_eligible_reason: str | None = None


# --- Admin bulk: POST /admin/tasks/bulk/{cancel,delete} --------


class _BulkSkipped(BaseModel):
    id: int
    reason: str


class BulkCancelTasksRequest(BaseModel):
    """`task_ids` must carry between 1 and 500 unique IDs (>0)."""

    task_ids: list[int] = Field(min_length=1, max_length=500)
    reason: str | None = None

    @field_validator("task_ids")
    @classmethod
    def _validate_ids(cls, v: list[int]) -> list[int]:
        if any(tid <= 0 for tid in v):
            raise ValueError("task_ids must be positive integers")
        deduped = list(dict.fromkeys(v))
        if len(deduped) != len(v):
            raise ValueError("task_ids must not contain duplicates")
        return deduped


class BulkCancelTasksResponse(BaseModel):
    cancelled: list[int]
    skipped: list[_BulkSkipped]
    not_found: list[int]


class BulkDeleteTasksRequest(BaseModel):
    task_ids: list[int] = Field(min_length=1, max_length=500)

    @field_validator("task_ids")
    @classmethod
    def _validate_ids(cls, v: list[int]) -> list[int]:
        if any(tid <= 0 for tid in v):
            raise ValueError("task_ids must be positive integers")
        deduped = list(dict.fromkeys(v))
        if len(deduped) != len(v):
            raise ValueError("task_ids must not contain duplicates")
        return deduped


class BulkDeleteTasksResponse(BaseModel):
    deleted: list[int]
    skipped: list[_BulkSkipped]
    not_found: list[int]


class BulkRetryTasksRequest(BaseModel):
    """`task_ids` must carry between 1 and 500 unique IDs (>0).

    No `force_same_worker`: the bulk retry ALWAYS re-enqueues to the general
    queue (`force_same_worker=False`); re-pinning N tasks en masse to workers
    that are down would leave them stuck. The individual retry keeps its flag
    (`AdminRetryTaskRequest`) for one-off debugging.
    """

    task_ids: list[int] = Field(min_length=1, max_length=500)

    @field_validator("task_ids")
    @classmethod
    def _validate_ids(cls, v: list[int]) -> list[int]:
        if any(tid <= 0 for tid in v):
            raise ValueError("task_ids must be positive integers")
        deduped = list(dict.fromkeys(v))
        if len(deduped) != len(v):
            raise ValueError("task_ids must not contain duplicates")
        return deduped


class BulkRetryTasksResponse(BaseModel):
    retried: list[int]
    skipped: list[_BulkSkipped]
    not_found: list[int]


# --- Admin: POST /admin/tasks/{id}/retry (retry-in-place) -------


class AdminRetryTaskRequest(BaseModel):
    """Optional body for `POST /admin/tasks/{id}/retry`.

    `force_same_worker=false` (default) retries on **any eligible worker**: it
    keeps the original targeting and the task returns to the general pool. This
    is the expected behavior when the worker that ran the original attempt is
    down, busy or retired (consistent with the bulk retry).

    `force_same_worker=true` (opt-in) re-pins the retry to the same worker that
    ran the last started attempt (via `delivery_mode='claim'` +
    `target_worker_pool`); useful for debugging and to take advantage of the
    worker's local `ORCASTER_CACHE_DIR`. If there is no previously started
    attempt, the original targeting is respected (no push).
    """

    force_same_worker: bool = False


class AdminRetryTaskResponse(BaseModel):
    # `queued`: count-mode retry. `dispatching`: all_available retry
    # re-snapshotted (1+ workers online). `failed`: `all:<ids>` retry
    # with the pool 100% offline (no_eligible_workers).
    task_id: int
    # Migration 0019: counter of in-place retries after this retry (post-bump).
    retry_count: int
    status: Literal["queued", "dispatching", "failed"]
    attempt_number: int
    target_worker_id: UUID | None = None
    delivery_mode: Literal["claim", "push"]


# --- Control-op: POST /admin/workers/{id}/control-op ---------------------

_RESERVED_COMMANDS: frozenset[str] = frozenset({"__reload_config__"})


class AdminControlOpRequest(BaseModel):
    """Body for `POST /admin/workers/{id}/control-op`.

    Creates a signed push task with command=control_op targeted at the worker.
    Only control_ops listed in RESERVED_COMMANDS are valid.
    """

    control_op: str = Field(min_length=1)
    payload: dict = Field(default_factory=dict)
    idempotency_key: CanonicalUUID | None = None

    @field_validator("payload")
    @classmethod
    def _validate_isolation_fields(cls, v: dict) -> dict:
        """Same shape rules for `payload.task_env` and
        `payload.egress_allowlist` as creating a task."""
        validate_payload_task_env(v)
        validate_payload_egress_allowlist(v)
        return v

    @field_validator("control_op")
    @classmethod
    def _validate_control_op(cls, v: str) -> str:
        if v not in _RESERVED_COMMANDS:
            raise ValueError(
                f"unknown control_op '{v}'; valid values: {sorted(_RESERVED_COMMANDS)}"
            )
        return v


class AdminControlOpResponse(BaseModel):
    task_id: int
    status: Literal["queued"]
    control_op: str
    target_worker_id: UUID
