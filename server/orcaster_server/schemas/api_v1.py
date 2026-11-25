"""Pydantic schemas of the public `/api/v1` API.

Stable machine contract, separate from the HTMX dashboard. It covers
`GET /auth/whoami`, the task schemas and the insights and secrets schemas.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orcaster_server.schemas.tasks import (
    AdminCreateTaskRequest,
    AutoRetryPolicy,
    CanonicalUUID,
    validate_target_syntax,
)


class ApiCreateTaskRequest(BaseModel):
    """Public body of `POST /api/v1/tasks`: a subset of `AdminCreateTaskRequest`.

    EXCLUDES from the public contract: `delivery_mode`/`target_worker_id`
    (push is admin control-ops; targeting a worker is done via
    `target="worker:<uuid>"`) and `signed` (deprecated, always signed).
    `extra="forbid"` rejects those fields (and any unknown one) with 422.
    """

    model_config = ConfigDict(extra="forbid")

    payload: dict = Field(default_factory=dict)
    target: str = Field(default="1")
    eligibility: dict = Field(default_factory=dict)
    claim_timeout_seconds: int | None = Field(default=None, ge=1)
    execution_timeout_seconds: int | None = Field(default=None, ge=1)
    run_at: datetime | None = None
    priority: int = Field(default=0, ge=-32768, le=32767)
    idempotency_key: CanonicalUUID | None = None
    auto_retry: AutoRetryPolicy | None = None

    @field_validator("target")
    @classmethod
    def _validate_target(cls, v: str) -> str:
        return validate_target_syntax(v)

    def to_admin_request(self) -> AdminCreateTaskRequest:
        """Maps to the internal `AdminCreateTaskRequest` with the non-public defaults.

        `delivery_mode='claim'`, `target_worker_id=None`, `signed=True` are the
        values the server imposes on everything entering through the public API.
        """
        return AdminCreateTaskRequest(
            payload=self.payload,
            target=self.target,
            eligibility=self.eligibility,
            claim_timeout_seconds=self.claim_timeout_seconds,
            execution_timeout_seconds=self.execution_timeout_seconds,
            run_at=self.run_at,
            priority=self.priority,
            idempotency_key=self.idempotency_key,
            auto_retry=self.auto_retry,
        )


class ApiForkTaskRequest(BaseModel):
    """Public body of `POST /api/v1/tasks/{id}/fork`: **all fields optional**.

    The merge against the parent is done server-side by `merge_fork_params`:
    `payload`/`eligibility` are recursively deep-merged; scalars + `auto_retry`
    override if present. `extra="forbid"` rejects the non-public fields
    (`delivery_mode`/`target_worker_id`/`signed`) just like create -> 422.

    Only the fields actually sent override (detected with `model_fields_set`);
    absent ones are inherited from the parent.
    """

    model_config = ConfigDict(extra="forbid")

    payload: dict | None = None
    target: str | None = None
    eligibility: dict | None = None
    claim_timeout_seconds: int | None = Field(default=None, ge=1)
    execution_timeout_seconds: int | None = Field(default=None, ge=1)
    run_at: datetime | None = None
    priority: int | None = Field(default=None, ge=-32768, le=32767)
    idempotency_key: CanonicalUUID | None = None
    auto_retry: AutoRetryPolicy | None = None

    @field_validator("target")
    @classmethod
    def _validate_target(cls, v: str | None) -> str | None:
        return validate_target_syntax(v) if v is not None else None


class TaskChildRow(BaseModel):
    """Row of `GET /api/v1/tasks/{id}/children`, and common base of the task rows.

    `done`/`total` are derived from the task's executions: `done` = `completed`
    executions; `total` = number of executions (materialized attempts).
    `target_count` = nominal fan-out requested by the operator (success threshold:
    the task closes `completed` with exactly `target_count` `completed`
    executions). The attempts budget, which drifts with each retry, lives
    separately in `tasks.attempt_budget` (not exposed here); retries are
    auditable via `retry_count` and the `auto_retry_attempts_used` field of the
    task detail.

    It is its own class and not an alias of the list row: the children listing
    does NOT emit variant or payload, because its query does not project them and
    emitting them as null would misreport the variant of a fork that does have
    one. `response_model_exclude_unset` cannot achieve this: it is a SERIALIZATION
    flag and does not affect schema generation, so the OpenAPI would publish the
    three fields as properties of a response that never sends them, and a client
    generated from the schema would expect impossible fields. With one class per
    response shape, the schema says exactly what travels.
    """

    id: int
    status: str
    target_mode: str
    target_count: int
    done: int
    total: int
    priority: int
    retry_count: int
    parent_task_id: int | None = None
    created_at: datetime
    completed_at: datetime | None = None
    outcome: str | None = None  # Terminal nuance; NULL = ordinary close.


class TaskRowWithVariant(TaskChildRow):
    """Task row that DOES expose the variant. Base of the list and the detail.

    Task variant: tool and version derived from the payload at ingestion
    (`orcaster_server/task_variant.py`). NULL/NULL = `other` bucket (no tool
    recognized).
    """

    variant_kind: str | None = None
    variant_label: str | None = None


class TaskListRow(TaskRowWithVariant):
    """Row of `GET /api/v1/tasks` WITHOUT `include_payload`: variant, no payload."""


class TaskListRowWithPayload(TaskRowWithVariant):
    """Row of `GET /api/v1/tasks` with `include_payload=true`.

    SIBLING of `TaskListRow`, not a subclass. The reason is the union in
    `TaskListResponse.tasks`: `GET /tasks` is ONE route with a per-request
    `include_payload` flag, so both row shapes coexist in the same response model
    and the list must be typed as a union. If one were a subclass of the other, an
    instance with payload would also be an instance of the parent member, the
    union could pick the parent and pydantic would silently drop `payload` when
    serializing. As siblings, neither is an instance of the other and that
    confusion is impossible by construction. DO NOT collapse the hierarchy into
    `TaskListRowWithPayload(TaskListRow)`: it would look shorter and reopen the bug.

    `payload` is required here: if the route chose this shape it is because it
    projected the column. `TaskDetail` inherits from this class because the detail
    always carries the payload.
    """

    payload: dict


class TaskListResponse(BaseModel):
    # Union with the WITH-payload variant first: it is the most specific shape and
    # the one that must win when the row carries the key. The two members are
    # siblings on purpose (see `TaskListRowWithPayload`), so the match is by exact
    # class and the order is only an extra defense, not the mechanism.
    tasks: list[TaskListRowWithPayload | TaskListRow]
    # Last id of the page when more results remain (full page); null when exhausted.
    next_cursor: int | None = None


class TaskChildListResponse(BaseModel):
    """Response of `GET /api/v1/tasks/{id}/children`: same envelope, own row.

    Wrapper separate from `TaskListResponse` so that the children OpenAPI declares
    `TaskChildRow` and not the list row. The shape of the envelope is identical:
    `tasks` + `next_cursor`.
    """

    tasks: list[TaskChildRow]
    # Last id of the page when more results remain (full page); null when exhausted.
    next_cursor: int | None = None


class TaskExecutionSummary(BaseModel):
    """Execution inside `TaskDetail`. The output is NOT inline (it can be
    large); it is fetched via `GET /v1/tasks/{id}/output`.
    """

    worker_id: str
    status: str
    attempt_index: int
    assigned_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    outcome: str | None = None  # Terminal nuance; NULL = ordinary close.


class TaskDetail(TaskListRowWithPayload):
    """`GET /api/v1/tasks/{id}` = row with variant and payload + full fields + executions.

    It inherits from `TaskListRowWithPayload` because the detail emits exactly
    those fields (variant always, `payload` required) and so it re-declares none.

    `child_count` = number of direct forks (children with `parent_task_id = id`),
    a single level, derived by a correlated subselect (no recursion). 0 if there
    are no forks.
    """

    child_count: int = 0
    eligibility: dict
    claim_timeout_seconds: int | None = None
    execution_timeout_seconds: int | None = None
    run_at: datetime | None = None
    auto_retry_policy: dict | None = None
    last_error: str | None = None
    cancelled_reason: str | None = None
    executions: list[TaskExecutionSummary]


class CancelTaskRequest(BaseModel):
    """Optional body of `POST /api/v1/tasks/{id}/cancel`."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


class RetryTaskRequest(BaseModel):
    """Optional body of `POST /api/v1/tasks/{id}/retry`.

    `force_same_worker=false` (default) retries on any eligible worker; `true`
    (opt-in) re-pins to the worker of the last attempt.
    """

    model_config = ConfigDict(extra="forbid")

    force_same_worker: bool = False


class WhoamiUser(BaseModel):
    id: str
    display_name: str
    email: str | None = None


class WhoamiWorkspace(BaseModel):
    id: str
    slug: str
    name: str


class WhoamiToken(BaseModel):
    label: str | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None


class WhoamiResponse(BaseModel):
    """Identity resolved from the PAT: basis of `orcaster auth status`/`login`."""

    user: WhoamiUser
    workspace: WhoamiWorkspace
    role: str
    token: WhoamiToken


class ExitCodeCount(BaseModel):
    """Count of failed tasks by exit_code of their representative execution."""

    exit_code: int | None
    count: int


class WorkerFailureCount(BaseModel):
    """Count of failed tasks by worker of their representative execution."""

    worker_id: str
    count: int


class FailureExample(BaseModel):
    """Representative task of an error-signature group, with breadcrumbs for LLM diagnosis.

    `command_line` is the actual command executed (last `stream='command'` event);
    `payload` is the task's JSONB (repo/ref/args). `execution_id` allows
    downloading the full log via `GET /api/v1/executions/{id}/output`.
    """

    task_id: int
    execution_id: str | None = None
    exit_code: int | None = None
    worker_id: str | None = None
    command: str | None = None
    command_line: str | None = None
    payload: dict = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    stderr_tail: str = ""
    stdout_tail: str = ""


class FailureSignatureGroup(BaseModel):
    """Group of failed tasks with the same normalized error signature."""

    signature: str
    # Stable hash of `signature`: compact, URL-safe key for the
    # `GET /insights/signature/tasks` drill-down, which returns ALL the task ids of the group.
    signature_hash: str
    count: int
    exit_codes: list[int | None]
    example: FailureExample
    sample_task_ids: list[int]


class FailuresFilters(BaseModel):
    """Echo of the applied filters (the workspace scope is implicit in the PAT)."""

    since: datetime | None = None
    until: datetime | None = None
    limit: int


class FailuresResponse(BaseModel):
    """Response of `GET /api/v1/insights/failures`."""

    generated_at: datetime
    filters: FailuresFilters
    total_failed_tasks: int
    by_exit_code: list[ExitCodeCount]
    by_worker: list[WorkerFailureCount]
    by_signature: list[FailureSignatureGroup]


class SignatureTasksFilters(BaseModel):
    """Echo of the `/insights/signature/tasks` filters.

    It reflects the effective window and terminal subset (same as
    `/insights/outcomes`), plus the `after` pagination cursor.
    """

    since: datetime | None = None
    until: datetime | None = None
    limit: int
    after: int | None = None
    status: list[str]
    outcome: str | None = None
    variant: str = "all"


class SignatureTasksResponse(BaseModel):
    """Response of `GET /api/v1/insights/signature/tasks`.

    Drill-down of one signature: lists ALL the task ids of the group within the
    window, paginated by descending id. `signature` is the normalized text (None
    if requested by hash and there was no match); `signature_hash` always
    travels. `next_after` is the cursor of the next page (None if there are no
    more).
    """

    generated_at: datetime
    filters: SignatureTasksFilters
    signature: str | None = None
    signature_hash: str
    task_ids: list[int]
    next_after: int | None = None


class StatusOutcomeCount(BaseModel):
    """Count of terminal tasks per (status, outcome) pair. outcome=None = plain."""

    status: str
    outcome: str | None
    count: int


class OutcomesFilters(BaseModel):
    """Echo of the /insights/outcomes filters (workspace implicit in the PAT)."""

    since: datetime | None = None
    until: datetime | None = None
    limit: int
    status: list[str]
    outcome: str | None = None
    variant: str = "all"


class OutcomesResponse(BaseModel):
    """Response of `GET /api/v1/insights/outcomes`: generalizes failures to all
    terminal states, segmented by (status, outcome), with the same signature and
    example machinery. `/insights/failures` remains as a stable-contract subset
    (status=failed)."""

    generated_at: datetime
    filters: OutcomesFilters
    total_tasks: int
    by_status_outcome: list[StatusOutcomeCount]
    by_exit_code: list[ExitCodeCount]
    by_worker: list[WorkerFailureCount]
    by_signature: list[FailureSignatureGroup]


# --- Task secrets -----------------------------------------------------


class SecretPutRequest(BaseModel):
    """Body of `PUT /api/v1/secrets/{name}`. The value is write-only.

    `strict`: a number or other type is not coerced to a string. Validation
    messages do not quote the value, and the 422 handler in `main.py` removes the
    `input` from errors under `/api/v1/secrets`.
    """

    model_config = ConfigDict(extra="forbid")

    value: str = Field(strict=True, repr=False)

    @field_validator("value")
    @classmethod
    def _validate_value(cls, v: str) -> str:
        from orcaster_server.task_secrets.service import validate_secret_value

        return validate_secret_value(v)


class SecretItem(BaseModel):
    """Metadata of a secret: never the value or the ciphertext.

    `updated_by` is the author's email (or their display_name if they have no
    email), or null if the user was deleted.

    `worker_id` / `worker_name`: null for a workspace-wide secret; when set, the
    worker that receives it and its visible name (`name`, or `hostname` if it has
    none). Additive fields.
    """

    name: str
    fingerprint: str
    created_at: datetime
    updated_at: datetime
    updated_by: str | None = None
    worker_id: str | None = None
    worker_name: str | None = None


class SecretListResponse(BaseModel):
    """`keys_configured`: whether the server has `TASK_SECRETS_KEYS_PATH`. Listing
    does not decrypt, so the metadata is returned regardless; without a keys file,
    setting and deleting respond 503 `task_secrets_unconfigured`."""

    items: list[SecretItem]
    keys_configured: bool
    # Always true on a server with per-worker scope. An older server does not send
    # it and ignores `?worker=` on PUT and DELETE (FastAPI drops unknown query
    # params), so the CLI checks it before using `--worker` so as not to silently
    # set or delete the workspace secret.
    supports_worker_scope: bool = True
