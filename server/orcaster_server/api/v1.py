"""Router of the public `/api/v1` API.

Pure JSON authenticated by PAT Bearer (`require_pat_workspace`). Multi-tenant
isolation is inherited from the `WorkspaceContext`. The uniform rendering of
errors as `{"error","message"}` is applied by the exception handler in
`orcaster_server/main.py` for every path under `/api/v1`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Literal, NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server.api.admin_tasks import (
    _SQL_GET_FORK_SOURCE,
    _create_task,
    admin_retry_task,
    bulk_delete_tasks_core,
    cancel_task_core,
    delete_task_core,
    fork_task_core,
    merge_fork_params,
)
from orcaster_server.db import get_session
from orcaster_server.env_names import validate_secret_name, validate_secret_name_pattern
from orcaster_server.error_signatures import is_valid_signature_hash
from orcaster_server.failures import (
    aggregate_failures,
    aggregate_outcomes,
    list_signature_task_ids,
    start_of_utc_day,
    terminal_variant_counts,
)
from orcaster_server.models import TaskStatus
from orcaster_server.output_log import resolve_execution_in_workspace, stream_output_gzip
from orcaster_server.pats.deps import PatContext, require_pat_workspace
from orcaster_server.schemas.api_v1 import (
    ApiCreateTaskRequest,
    ApiForkTaskRequest,
    CancelTaskRequest,
    FailuresResponse,
    OutcomesResponse,
    RetryTaskRequest,
    SecretItem,
    SecretListResponse,
    SecretPutRequest,
    SignatureTasksResponse,
    TaskChildListResponse,
    TaskChildRow,
    TaskDetail,
    TaskExecutionSummary,
    TaskListResponse,
    TaskListRow,
    TaskListRowWithPayload,
    WhoamiResponse,
    WhoamiToken,
    WhoamiUser,
    WhoamiWorkspace,
)
from orcaster_server.schemas.tasks import (
    AdminCreateTaskResponse,
    AdminRetryTaskRequest,
    AdminRetryTaskResponse,
    BulkDeleteTasksRequest,
    BulkDeleteTasksResponse,
    build_admin_request,
)
from orcaster_server.task_outcomes import legacy_status_aliases
from orcaster_server.task_secrets import keyring as task_secrets_keyring
from orcaster_server.task_secrets import service as task_secrets
from orcaster_server.task_variant import (
    VARIANT_TOKEN_MAX_LEN,
    VARIANT_WHERE_SQL,
    VariantFilter,
    known_variant_kinds,
    resolve_variant_filter,
    variant_sql_binds,
)
from orcaster_server.workspaces.deps import require_owner
from orcaster_server.workspaces.models import Workspace

router = APIRouter(prefix="/api/v1", tags=["api-v1"])


# --- Read SQL (paginated, no N+1) ------------------------------------------
#
# `done`/`total` are resolved with a single LEFT JOIN + GROUP BY over the
# executions of the page: `done` = completed executions; `total` = number of
# executions. The cursor is `id < :cursor` ordered by id DESC (stable under
# concurrent inserts). The status filter accepts a list (comma-joined).
#
# The variant filter is built with the SAME clause as the admin UI and the
# failures aggregation (`VARIANT_WHERE_SQL`, single source). `{payload_col}` is
# the opt-in projection of `include_payload`: it is resolved here, in two fixed
# statements compiled once, so that nothing from the request enters the SQL
# construction.
_SQL_LIST_TASKS_HEAD = """
    SELECT t.id, t.status::text AS status, t.target_mode, t.target_count,
           t.priority, t.retry_count, t.parent_task_id, t.created_at, t.completed_at,
           t.outcome, t.variant_kind, t.variant_label,{payload_col}
           COALESCE(SUM(CASE WHEN e.status = 'completed' THEN 1 ELSE 0 END), 0) AS done,
           COUNT(e.task_id) AS total
      FROM tasks t
      LEFT JOIN task_executions e ON e.task_id = t.id
     WHERE t.workspace_id = :workspace_id
       AND (CAST(:cursor AS bigint) IS NULL OR t.id < CAST(:cursor AS bigint))
       AND (CAST(:status_filter AS text) IS NULL
            OR t.status::text = ANY(string_to_array(CAST(:status_filter AS text), ',')))
"""

# `GROUP BY t.id` covers the extra columns by functional dependency on the PK.
_SQL_LIST_TASKS_TAIL = """
     GROUP BY t.id
     ORDER BY t.id DESC
     LIMIT :limit
"""


def _list_tasks_sql(payload_col: str) -> TextClause:
    return text(
        _SQL_LIST_TASKS_HEAD.format(payload_col=payload_col)
        + VARIANT_WHERE_SQL
        + _SQL_LIST_TASKS_TAIL
    )


_SQL_LIST_TASKS = _list_tasks_sql("")
_SQL_LIST_TASKS_WITH_PAYLOAD = _list_tasks_sql(" t.payload,")

# Fields EMITTED by each response shape, DERIVED from the public model that
# declares them. The direction of the dependency is deliberate: the public
# model is the source of truth for the JSON shape, and the SQL projection has
# to feed it. If the set of keys came from `dict(row)`, that is, from the
# SELECT, a refactor that dropped a column from the projection would make the
# key silently disappear from the API, without a single error.
#
# With the tuple derived from the model that is inverted: the row is built with
# `{f: row[f] for f in FIELDS}`, so a column missing from the projection blows
# up while serving the request (`NoSuchColumnError`, which subclasses
# `KeyError` and NAMES the missing column) instead of trimming the response.
#
# Having two tuples and not one expresses the difference between the two
# public contracts, and that difference is NOT an oversight: the children query
# does not project `variant_kind`/`variant_label` or `payload`, and emitting
# them as null would lie about the variant of a fork that does have one (see
# the docstring of `list_task_children`). The common fields no longer need to
# be repeated by hand: the class hierarchy in
# `orcaster_server/schemas/api_v1.py` shares them by inheritance, so the two
# shapes cannot diverge on a common field through a bad copy.
#
# `model_fields` is accessed on the CLASS, not on an instance (instance access
# is deprecated in recent pydantic).
# Fed by `_SQL_LIST_TASKS` (= `_SQL_LIST_TASKS_HEAD` without `payload_col`).
_LIST_TASK_FIELDS: tuple[str, ...] = tuple(TaskListRow.model_fields)

_SQL_GET_TASK_DETAIL = text(
    """
    SELECT t.id, t.status::text AS status, t.target_mode, t.target_count,
           t.priority, t.retry_count, t.parent_task_id, t.created_at, t.completed_at,
           t.outcome, t.variant_kind, t.variant_label,
           t.payload, t.eligibility, t.claim_timeout_seconds,
           t.execution_timeout_seconds, t.run_at, t.auto_retry_policy, t.last_error,
           t.cancelled_reason,
           COALESCE(SUM(CASE WHEN e.status = 'completed' THEN 1 ELSE 0 END), 0) AS done,
           COUNT(e.task_id) AS total,
           (SELECT count(*) FROM tasks c
             WHERE c.parent_task_id = t.id AND c.workspace_id = t.workspace_id) AS child_count
      FROM tasks t
      LEFT JOIN task_executions e ON e.task_id = t.id
     WHERE t.id = :id AND t.workspace_id = :workspace_id
     GROUP BY t.id
    """
)

# Children (direct forks) of a parent: same shape as `_SQL_LIST_TASKS`, but
# filtered by `parent_task_id` (parent→children relation derived by query,
# no recursion, a single level). No status filter. The parent is validated
# separately (existence + workspace) before listing.
_SQL_LIST_CHILDREN = text(
    """
    SELECT t.id, t.status::text AS status, t.target_mode, t.target_count,
           t.priority, t.retry_count, t.parent_task_id, t.created_at, t.completed_at,
           t.outcome,
           COALESCE(SUM(CASE WHEN e.status = 'completed' THEN 1 ELSE 0 END), 0) AS done,
           COUNT(e.task_id) AS total
      FROM tasks t
      LEFT JOIN task_executions e ON e.task_id = t.id
     WHERE t.parent_task_id = :id
       AND t.workspace_id = :workspace_id
       AND (CAST(:cursor AS bigint) IS NULL OR t.id < CAST(:cursor AS bigint))
     GROUP BY t.id
     ORDER BY t.id DESC
     LIMIT :limit
    """
)

# Fields emitted by the children listing, fed by `_SQL_LIST_CHILDREN` (same
# criterion as `_LIST_TASK_FIELDS`, see the long comment above).
_LIST_CHILDREN_FIELDS: tuple[str, ...] = tuple(TaskChildRow.model_fields)

_SQL_TASK_EXECUTIONS = text(
    """
    SELECT worker_id::text AS worker_id, status::text AS status, attempt_index,
           assigned_at, started_at, finished_at, exit_code, outcome
      FROM task_executions
     WHERE task_id = :id
     ORDER BY attempt_index DESC, assigned_at DESC NULLS LAST
    """
)


_SQL_TASK_BELONGS = text("SELECT id FROM tasks WHERE id = :id AND workspace_id = :ws")

_SQL_LATEST_EXECUTION = text(
    "SELECT id::text AS id FROM task_executions "
    "WHERE task_id = :id ORDER BY attempt_index DESC, id DESC LIMIT 1"
)

_SQL_EXECUTION_BY_ATTEMPT = text(
    "SELECT id::text AS id FROM task_executions "
    "WHERE task_id = :id AND attempt_index = :attempt ORDER BY id DESC LIMIT 1"
)


@router.get("/auth/whoami", response_model=WhoamiResponse)
async def whoami(
    ctx: PatContext = Depends(require_pat_workspace),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> WhoamiResponse:
    """Return the identity resolved by the PAT: user, workspace, role and token.

    It does not mutate (except the best-effort `last_used_at` already done by
    `require_pat_workspace`).
    """
    workspace = await db.get(Workspace, ctx.workspace_id)
    if workspace is None:  # pragma: no cover - the FK guarantees the row
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found")
    return WhoamiResponse(
        user=WhoamiUser(
            id=ctx.user.id,
            display_name=ctx.user.display_name,
            email=ctx.user.email,
        ),
        workspace=WhoamiWorkspace(id=workspace.id, slug=workspace.slug, name=workspace.name),
        role=ctx.role,
        token=WhoamiToken(
            label=ctx.token_label,
            last_used_at=ctx.token_last_used_at,
            expires_at=ctx.token_expires_at,
        ),
    )


@router.post(
    "/tasks",
    response_model=AdminCreateTaskResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_task(
    body: ApiCreateTaskRequest,
    ctx: PatContext = Depends(require_pat_workspace),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> AdminCreateTaskResponse:
    """Create a task in the PAT's workspace, reusing `_create_task`.

    The public body is mapped to the internal `AdminCreateTaskRequest` (with the
    non-public defaults: claim/no-push/signed). Multi-tenant isolation is given
    by `ctx.workspace_id`.
    """
    admin_request = build_admin_request(body.to_admin_request)
    resp = await _create_task(db, admin_request, ctx.workspace_id, parent_task_id=None)
    await db.commit()
    return resp


def _coerce_dict(value: object, *, field: str) -> dict:
    """Normalize a JSONB column to `dict`. NULL is normalized to `{}`.

    The driver may deliver the JSONB already decoded or as text; both forms are
    accepted. Anything else (an array or a scalar stored in the column) is
    rejected EXPLICITLY instead of letting it through:

    - Silently returning `{}` would be worse than failing. An automated consumer
      cannot tell that `{}` from a legitimately empty payload, and with
      `include_payload` the response feeds re-queue decisions: a task with a
      corrupt payload would read as a task with no data.
    - Without the guard the error would still occur, but as a pydantic
      `ValidationError` when building the model, without saying which row or
      which field it came from.

    The order of the branches matters: an already decoded value that is NOT a
    dict (a list) falls straight to the `raise`, instead of entering
    `json.loads` and blowing up with a `TypeError` that explains nothing.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str | bytes | bytearray):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
        value = decoded
    raise ValueError(f"{field} is not a JSON object (got {type(value).__name__})")


def _coerce_task_json(value: object, *, task_id: int, field: str) -> dict:
    """`_coerce_dict` with the row identified, so the 500 is actionable.

    Translates the failure into an HTTP 500 that NAMES the task and the field.
    The operator knows which row to fix or delete without hunting for the
    traceback, and the client sees why the page cannot be served.
    `JSONDecodeError` inherits from `ValueError`, so corrupt text goes through
    the same branch.
    """
    try:
        return _coerce_dict(value, field=field)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"task {task_id}: {exc}") from exc


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(
    ctx: PatContext = Depends(require_pat_workspace),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    status_filter: Annotated[list[TaskStatus] | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[int | None, Query(ge=1)] = None,
    variant: Annotated[str, Query(max_length=VARIANT_TOKEN_MAX_LEN)] = "all",
    include_payload: Annotated[bool, Query()] = False,
) -> TaskListResponse:
    """List the tasks of the PAT's workspace, paginated by cursor id desc.

    `done`/`total` come from a single GROUP BY (no N+1). `next_cursor` = id of
    the last row when the page is full (there is more); null when exhausted.

    `variant`: `all` (default) / `other` / `<kind>` / `<kind>~<label>`. The
    absence of a filter means `all` through BOTH doors it can enter by: the
    default of the query param is the literal `"all"`, and the explicit empty
    token (`?variant=`) is resolved by `resolve_variant_filter` with
    `default_mode="all"`. If the list fell into `current` mode it would
    silently hide the non-latest versions of each kind, and an automated
    consumer would read that trimmed page as "there are no more tasks". The
    `current` token is REJECTED with 422: resolving it requires the set of
    present variants, and a source without terminal semantics does exist:
    `_SQL_COUNT_TASKS_BY_VARIANT_STATUS` (`admin_ui.py`) is a GROUP BY without
    a status filter that already resolves `current` for the admin UI. The 422
    is not due to impossibility but to cost: that aggregate is private to the
    admin plane, is full-table and has no cursor, and paying for it on every
    page of a paginated, machine-facing endpoint (which an automated sweep
    walks in bulk) is a cost that was evaluated and deliberately discarded.

    The asymmetry this leaves is made explicit here so it is not re-discussed.
    `/insights/outcomes?variant=current` DOES resolve, because the domain of
    that endpoint is already terminal-only and its own source
    (`terminal_variant_counts`) has exactly that semantics, with no extra cost
    to pay. `/tasks?variant=current` does NOT resolve, because its domain is
    not bounded to terminal tasks and the only source that would serve it is
    the one that was decided not to pay for.

    The `kind` is validated against `known_variant_kinds()`: a kind that no
    loaded plugin registered is rejected with 422 instead of returning an empty
    page. Without the guard, a typo (`example_tool~1.2.3` with an underscore)
    gave HTTP 200 and `tasks: []`, indistinguishable from "there are no tasks
    in that variant"; an automation reads it as an empty queue and re-queues
    the whole inventory. The check costs no query: the catalogue of kinds is
    defined by the code. The LABEL stays free and is NOT validated (it comes
    from the payload, which is external data); a typo in the label still gives
    an empty page, which is correct because that variant does not actually
    exist in the table.

    `include_payload`: opt-in projection of the `payload` in each row. It is not
    a predicate (it does not touch the query plan); without the flag the key is
    not even emitted, so the existing contract does not change. Which of the
    two shapes is sent is decided by the row CLASS (`TaskListRowWithPayload` or
    `TaskListRow`), not by a serialization flag: both are declared in the
    response model as a union, so the OpenAPI describes the two real shapes
    instead of publishing a `payload` that can never arrive without the flag. A
    row whose stored `payload` is not a JSON object cuts the page with a 500
    that names the task (`_coerce_task_json`); see there why it is not degraded
    to `{}`.

    The `max_length` of the param is `VARIANT_TOKEN_MAX_LEN`, the same limit
    with which ingestion CLAMPS what it persists (`clamp_variant`): a longer
    token cannot correspond to any stored variant, so rejecting it does not
    hide rows and avoids binding an arbitrary string from a request not
    authenticated by the UI.
    """
    token = variant.strip()
    if token == "current":
        raise HTTPException(
            status_code=422,
            detail=(
                "variant=current is not supported in the task list; "
                "use all, other, <kind> or <kind>~<label>"
            ),
        )
    # `present` is empty on purpose: only `current` mode consumes it, and that
    # is unreachable here (rejected above). The empty token is covered by
    # `default_mode`, declared at the caller instead of being normalized by hand
    # before the call.
    vf = resolve_variant_filter(token, (), default_mode="all")
    known_kinds = known_variant_kinds()
    if vf.mode in ("kind", "pair") and vf.kind not in known_kinds:
        # It also covers the token that starts with the separator (`~1.2.3`),
        # which splits into an empty kind.
        # Without plugins the catalogue is empty: the message says so instead of
        # ending in an empty list that looks like a format error.
        catalogue = ", ".join(sorted(known_kinds)) or "none (no plugin registers a variant)"
        raise HTTPException(
            status_code=422,
            detail=f"unknown variant kind {vf.kind!r}; known kinds: {catalogue}",
        )
    sql = _SQL_LIST_TASKS_WITH_PAYLOAD if include_payload else _SQL_LIST_TASKS
    rows = (
        (
            await db.execute(
                sql,
                {
                    "workspace_id": ctx.workspace_id,
                    "cursor": cursor,
                    "status_filter": ",".join(s.value for s in status_filter)
                    if status_filter
                    else None,
                    "limit": limit,
                    # Binds ALWAYS present, in every mode: asyncpg requires every
                    # placeholder in the SQL to be bound (see the docstring of
                    # `variant_sql_binds`).
                    **variant_sql_binds(vf),
                },
            )
        )
        .mappings()
        .all()
    )
    tasks: list[TaskListRowWithPayload | TaskListRow]
    if include_payload:
        # Same criterion as the detail: the driver may return the JSON as
        # text, and NULL is normalized to `{}`. The branch is decided once: the
        # flag is invariant for the page, not for the row. A payload that is not
        # a JSON object cuts the page with a 500 that names the task (see
        # `_coerce_task_json`), instead of an opaque ValidationError.
        #
        # The `payload` is read ONCE per row: the comprehension iterates over
        # `_LIST_TASK_FIELDS`, which does NOT include it, and the key is added by
        # the `|` with the already coerced value. Also reading it in the
        # comprehension fetched the raw JSON of up to 200 rows per page only to
        # throw it away afterwards.
        tasks = [
            TaskListRowWithPayload(
                **{f: r[f] for f in _LIST_TASK_FIELDS}
                | {"payload": _coerce_task_json(r["payload"], task_id=r["id"], field="payload")}
            )
            for r in rows
        ]
    else:
        tasks = [TaskListRow(**{f: r[f] for f in _LIST_TASK_FIELDS}) for r in rows]
    next_cursor = tasks[-1].id if len(tasks) == limit else None
    return TaskListResponse(tasks=tasks, next_cursor=next_cursor)


@router.post("/tasks/bulk/delete", response_model=BulkDeleteTasksResponse)
async def bulk_delete_tasks(
    body: BulkDeleteTasksRequest,
    ctx: PatContext = Depends(require_pat_workspace),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> BulkDeleteTasksResponse:
    """Bulk hard-delete of terminal tasks (reuses `bulk_delete_tasks_core`).

    Best-effort per id (HTTP 200): `deleted` / `skipped` (`not terminal: <st>`) /
    `not_found`. Declared BEFORE the parametric `/tasks/{task_id}` routes so
    that `bulk` is not captured by `{task_id}`.
    """
    return await bulk_delete_tasks_core(db, task_ids=body.task_ids, workspace_id=ctx.workspace_id)


@router.get("/tasks/{task_id}", response_model=TaskDetail)
async def get_task(
    task_id: int,
    ctx: PatContext = Depends(require_pat_workspace),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> TaskDetail:
    """Detail of a task of the PAT's workspace. 404 if missing / cross-tenant.

    The executions' stdout/stderr are NOT inline (they can be large); they are
    left for a future dedicated output endpoint.
    """
    row = (
        (await db.execute(_SQL_GET_TASK_DETAIL, {"id": task_id, "workspace_id": ctx.workspace_id}))
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    exec_rows = (await db.execute(_SQL_TASK_EXECUTIONS, {"id": task_id})).mappings().all()
    data = dict(row)
    data["payload"] = _coerce_task_json(data.get("payload"), task_id=task_id, field="payload")
    data["eligibility"] = _coerce_task_json(
        data.get("eligibility"), task_id=task_id, field="eligibility"
    )
    auto_retry = data.get("auto_retry_policy")
    data["auto_retry_policy"] = (
        _coerce_task_json(auto_retry, task_id=task_id, field="auto_retry_policy")
        if auto_retry is not None
        else None
    )
    return TaskDetail(
        **data,
        executions=[TaskExecutionSummary(**dict(e)) for e in exec_rows],
    )


@router.get("/tasks/{task_id}/children", response_model=TaskChildListResponse)
async def list_task_children(
    task_id: int,
    ctx: PatContext = Depends(require_pat_workspace),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[int | None, Query(ge=1)] = None,
) -> TaskChildListResponse:
    """List the children (direct forks) of a task, paginated by cursor id desc.

    The parent→children relation is derived by query from
    `tasks.parent_task_id` (no recursion: a single level). No status filter.
    Same pagination pattern as `GET /tasks`: `next_cursor` = id of the last row
    when the page is full, null when exhausted. Missing / cross-tenant parent →
    404 (same as `get_task`).

    This response does NOT emit variant or payload, and that is deliberate:
    emitting them as null would lie about the variant of a fork that does have
    one, and this query does not project them. The omission used to be achieved
    with `response_model_exclude_unset`, which is a serialization flag and does
    not touch the schema: the OpenAPI of this route published
    `variant_kind`/`variant_label`/`payload` as properties that could never
    arrive. Now the route has its own declared shape (`TaskChildListResponse`
    over `TaskChildRow`), so the schema and the wire say the same thing.

    Which fields are requested from the row is decided by
    `_LIST_CHILDREN_FIELDS`, derived from `TaskChildRow` itself and not from
    the SELECT: a column that drops out of `_SQL_LIST_CHILDREN` blows up with
    `NoSuchColumnError` (a `KeyError` subclass, and it names the column)
    instead of deleting a key from the public contract without anyone noticing.
    """
    belongs = (await db.execute(_SQL_TASK_BELONGS, {"id": task_id, "ws": ctx.workspace_id})).first()
    if belongs is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")

    rows = (
        (
            await db.execute(
                _SQL_LIST_CHILDREN,
                {
                    "id": task_id,
                    "workspace_id": ctx.workspace_id,
                    "cursor": cursor,
                    "limit": limit,
                },
            )
        )
        .mappings()
        .all()
    )
    tasks = [TaskChildRow(**{f: r[f] for f in _LIST_CHILDREN_FIELDS}) for r in rows]
    next_cursor = tasks[-1].id if len(tasks) == limit else None
    return TaskChildListResponse(tasks=tasks, next_cursor=next_cursor)


@router.get("/tasks/{task_id}/output")
async def get_task_output(
    task_id: int,
    ctx: PatContext = Depends(require_pat_workspace),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    format: Literal["text", "jsonl"] = "text",
    attempt: int | None = Query(default=None, ge=1),  # noqa: B008
) -> StreamingResponse:
    """Output (stdout/stderr log) of an execution of the task.

    Without `attempt` it serves the one with the highest `attempt_index`;
    `attempt` (1-based) picks another. `format=text` (default) = raw log;
    `jsonl` = one event per line. Missing or cross-tenant task → 404. Task with
    no executions or execution with no events → 200 with an empty body. PAT
    auth, like the rest of `/api/v1`. gzip in transit (`Content-Encoding:
    gzip`).
    """
    belongs = (await db.execute(_SQL_TASK_BELONGS, {"id": task_id, "ws": ctx.workspace_id})).first()
    if belongs is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")

    if attempt is None:
        exec_id = (await db.execute(_SQL_LATEST_EXECUTION, {"id": task_id})).scalar_one_or_none()
        # No executions yet: empty output (200), consistent with "empty is valid".
        # Null UUID: no real execution has it → MAX(seq) gives 0 → empty stream.
        if exec_id is None:
            exec_id = "00000000-0000-0000-0000-000000000000"
    else:
        exec_id = (
            await db.execute(_SQL_EXECUTION_BY_ATTEMPT, {"id": task_id, "attempt": attempt})
        ).scalar_one_or_none()
        # explicit nonexistent attempt → 404 (unlike "no executions").
        if exec_id is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="attempt not found")

    # Release the request's connection now: the dependency teardown only runs
    # after the whole response is sent, and the builder uses its own short
    # sessions. Without this, a large/slow download holds an idle pool
    # connection for the whole stream (same pattern as the admin output
    # download handler).
    await db.rollback()
    return stream_output_gzip(exec_id, format)


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: int,
    body: CancelTaskRequest | None = None,
    ctx: PatContext = Depends(require_pat_workspace),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, object]:
    """Cancel a task of the PAT's workspace (reuses `cancel_task_core`). 404/409."""
    reason = body.reason if body is not None else None
    await cancel_task_core(db, task_id=task_id, workspace_id=ctx.workspace_id, reason=reason)
    return {"task_id": task_id, "status": "cancelled"}


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: int,
    ctx: PatContext = Depends(require_pat_workspace),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Hard-delete of a terminal task (reuses `delete_task_core`).

    Terminal → 204 with no body. Not terminal → 409. Missing / cross-tenant →
    404 (the core raises the corresponding `HTTPException`, same as cancel).
    """
    await delete_task_core(db, task_id=task_id, workspace_id=ctx.workspace_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/tasks/{task_id}/retry", response_model=AdminRetryTaskResponse)
async def retry_task(
    task_id: int,
    body: RetryTaskRequest | None = None,
    ctx: PatContext = Depends(require_pat_workspace),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> AdminRetryTaskResponse:
    """In-place retry of a terminal task (reuses `admin_retry_task`). 404/409."""
    force = body.force_same_worker if body is not None else False
    return await admin_retry_task(task_id, db, ctx, AdminRetryTaskRequest(force_same_worker=force))


@router.post(
    "/tasks/{task_id}/fork",
    response_model=AdminCreateTaskResponse,
    status_code=status.HTTP_201_CREATED,
)
async def fork_task(
    task_id: int,
    body: ApiForkTaskRequest | None = None,
    ctx: PatContext = Depends(require_pat_workspace),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> AdminCreateTaskResponse:
    """Fork a task with a server-side deep-merge: inherits from the parent what
    is not specified, deep-merges payload/eligibility, overrides scalars.
    404 if the parent does not exist / is cross-tenant.
    """
    override = body.model_dump(exclude_unset=True) if body is not None else {}
    parent = (
        (await db.execute(_SQL_GET_FORK_SOURCE, {"id": task_id, "workspace_id": ctx.workspace_id}))
        .mappings()
        .first()
    )
    if parent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="parent task not found")
    effective = build_admin_request(lambda: merge_fork_params(dict(parent), override))
    return await fork_task_core(db, task_id=task_id, workspace_id=ctx.workspace_id, body=effective)


@router.get("/executions/{execution_id}/output")
async def get_execution_output(
    execution_id: str,
    ctx: PatContext = Depends(require_pat_workspace),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    format: Annotated[Literal["jsonl", "text"], Query()] = "jsonl",
) -> StreamingResponse:
    """Full log (stdout+stderr+command) of an execution, gzip, for drill-down.

    Reuses the streamer of the admin download but with Bearer auth and scoped
    to the PAT's workspace. `jsonl` (default) = one JSON event per line;
    `text` = raw concatenation in `seq` order.
    """
    execution_id = await resolve_execution_in_workspace(
        db, workspace_id=ctx.workspace_id, execution_id=execution_id
    )
    # Release the request connection: the gzip generator uses its own short sessions.
    await db.rollback()
    return stream_output_gzip(execution_id, format)


class InsightsWindow(NamedTuple):
    """Resolved time window of an insights request.

    Separates what is REPORTED from what is FILTERED in SQL, because the
    default upper bound is not a filter (see `_default_window`):

    - `since`: effective lower bound; goes to the SQL and is echoed in
      `filters.since`.
    - `until`: effective upper instant; **only** echoed in `filters.until`.
    - `sql_until`: upper bound that goes to the SQL; `None` = no upper bound.
    """

    since: datetime
    until: datetime
    sql_until: datetime | None


def _default_window(
    since: datetime | None, until: datetime | None, *, now: datetime
) -> InsightsWindow:
    """Default time window of the insights endpoints.

    Both bounds are bounded by default INDEPENDENTLY:
    - `since` absent → UTC midnight of the current day.
    - `until` absent → `now` (the current UTC instant, "up to the latest data
      of the current day").

    Passing an explicit `since` and/or `until` respects the client's intent
    without touching that bound. For the full history it is enough to pass an
    explicit old `since`: `until` will still default to `now`, giving
    [old_since, now].

    The default `until` is NOT translated into a SQL filter (`sql_until` stays
    `None`): `completed_at` is stamped by the POSTGRES clock and this `now` is
    the APP clock, so a skew with an ahead-running DB would leave
    `completed_at > until` for a task closed "right now", which would vanish
    from the default window until the next request. Omitting the upper bound
    removes the skew by construction and costs no performance: the time index
    (`tasks_terminal_completed_idx`) bounds by `since`, and "now" never
    discards historical rows. The effective instant is still echoed in
    `filters.until` to report the window. An explicit `until` does reach the
    SQL: there the bound is the client's intent, not a default.
    """
    effective_since = since if since is not None else start_of_utc_day(now)
    effective_until = until if until is not None else now
    return InsightsWindow(since=effective_since, until=effective_until, sql_until=until)


@router.get("/insights/failures", response_model=FailuresResponse)
async def get_failures_insights(
    ctx: PatContext = Depends(require_pat_workspace),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    since: Annotated[
        datetime | None,
        Query(
            description=(
                "Restricts to tasks closed (completed_at) on or after the date. "
                "If the timezone offset is omitted, UTC is assumed. If this "
                "parameter is omitted, it defaults to UTC midnight of the current "
                "day, independently of whether `until` was passed; for the full "
                "history pass an explicit old `since`."
            )
        ),
    ] = None,
    until: Annotated[
        datetime | None,
        Query(
            description=(
                "Restricts to tasks closed (completed_at) on or before the date. "
                "If the timezone offset is omitted, UTC is assumed. If this "
                "parameter is omitted NO upper bound is applied (so the server "
                "clock cannot hide a just-closed task); `filters.until` echoes "
                "the instant at which the request was resolved, informational."
            )
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> FailuresResponse:
    """Aggregate the errors of the workspace's failed tasks (live, retroactive).

    `since`/`until` filter `completed_at`; a date without a timezone offset is
    interpreted as UTC (see `aggregate_failures`). Each absent bound defaults
    independently: `since` to UTC midnight of the current day, `until` to now;
    for the full history pass an explicit old `since`.
    `filters.since`/`filters.until` of the response reflect the EFFECTIVE
    window. The default `until` is informational: it is not applied as a filter
    (see `_default_window`), so that a task closed "right now" cannot fall out
    through skew between the app clock and the Postgres one.
    """
    window = _default_window(since, until, now=datetime.now(UTC))
    return await aggregate_failures(
        db,
        workspace_id=ctx.workspace_id,
        since=window.since,
        until=window.sql_until,
        reported_until=window.until,
        limit=limit,
    )


_TERMINAL_TASK_STATUS_TOKENS = ("completed", "failed", "cancelled")


def _parse_terminal_statuses(status_: str, outcome: str) -> tuple[tuple[str, ...], str]:
    """CSV of terminal statuses (+ legacy profile alias) → `(statuses, outcome_token)`.

    Single source for parsing `status`/`outcome` of `/insights/outcomes` and of
    the `/insights/signature/tasks` drill-down, so their filter does not
    diverge. Empty → all terminal statuses. A retired status token that the
    outcomes profile declares as an alias (`legacy_status_aliases`) is removed
    from the CSV, adds its real status if missing, and sets `outcome` ONLY if
    the request does not carry one; note: that outcome applies to the WHOLE
    response. If the CSV carries several aliases, the first one sets the
    outcome, in CSV order. Without a plugin there are no aliases and the token
    is just another unknown status. Raises 422 on unknown tokens.
    """
    tokens = [t.strip() for t in status_.split(",") if t.strip()]
    outcome_token = outcome.strip()
    aliases = legacy_status_aliases()
    for alias_token in [t for t in tokens if t in aliases]:
        alias = aliases[alias_token]
        tokens = [t for t in tokens if t != alias_token]
        if alias.status not in tokens:
            tokens.append(alias.status)
        outcome_token = outcome_token or alias.outcome
    bad = [t for t in tokens if t not in _TERMINAL_TASK_STATUS_TOKENS]
    if bad:
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown terminal status value(s): {', '.join(sorted(set(bad)))}. "
                f"valid: {', '.join(_TERMINAL_TASK_STATUS_TOKENS)}"
            ),
        )
    return (tuple(tokens) if tokens else _TERMINAL_TASK_STATUS_TOKENS), outcome_token


async def _resolve_variant_lens(
    db: AsyncSession,
    *,
    workspace_id: str,
    variant: str,
    statuses: tuple[str, ...],
    window: InsightsWindow,
) -> VariantFilter | None:
    """Resolve the version LENS of an insights request.

    `all` (or an empty token) → None (no filter), avoiding the count query. Any
    other token queries the variants present in the subset/window and passes
    them to `resolve_variant_filter`. Single source for `/insights/outcomes`
    and the drill-down. The upper bound that goes to the SQL is
    `window.sql_until` (None when `until` is the default).
    """
    token = variant.strip()
    if not token or token == "all":
        return None
    present = await terminal_variant_counts(
        db,
        workspace_id=workspace_id,
        statuses=statuses,
        since=window.since,
        until=window.sql_until,
    )
    # `default_mode="all"`: if the empty token ever got here, the observable
    # behaviour is still no filter (it does not mutate into the `current` lens).
    return resolve_variant_filter(token, present.keys(), default_mode="all")


@router.get("/insights/outcomes", response_model=OutcomesResponse)
async def get_outcomes_insights(
    ctx: PatContext = Depends(require_pat_workspace),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    since: Annotated[
        datetime | None,
        Query(
            description=(
                "Restricts to tasks closed (completed_at) on or after the date. "
                "If the timezone offset is omitted, UTC is assumed. If this "
                "parameter is omitted, it defaults to UTC midnight of the current "
                "day, independently of whether `until` was passed; for the full "
                "history pass an explicit old `since`."
            )
        ),
    ] = None,
    until: Annotated[
        datetime | None,
        Query(
            description=(
                "Restricts to tasks closed (completed_at) on or before the date. "
                "If the timezone offset is omitted, UTC is assumed. If this "
                "parameter is omitted NO upper bound is applied (so the server "
                "clock cannot hide a just-closed task); `filters.until` echoes "
                "the instant at which the request was resolved, informational."
            )
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    status_: Annotated[str, Query(alias="status")] = "",
    outcome: Annotated[str, Query()] = "",
    # `max_length` = the SAME limit with which ingestion clamps what it persists
    # (`clamp_variant`), as in `GET /tasks`: a longer token cannot correspond to
    # any stored variant, so rejecting it with 422 does not hide rows, and it
    # prevents an arbitrary string from a request from being bound into
    # `VARIANT_WHERE_SQL` after `terminal_variant_counts` has run.
    variant: Annotated[str, Query(max_length=VARIANT_TOKEN_MAX_LEN)] = "all",
) -> OutcomesResponse:
    """Aggregate ALL terminal states by (status, outcome).

    `status`: CSV of terminal statuses (default: all). Accepts the retired
    status tokens that the plugin's outcomes profile declares as aliases: real
    status + the alias's `outcome`. Note: the alias sets `outcome` for the
    WHOLE response; combined with other statuses in the CSV the outcome filter
    also applies to those statuses; for mixes use the explicit params. Without
    a plugin there are no aliases and the token answers 422 like any unknown
    status. `outcome`: name, `other` (no outcome) or empty (all). `variant`:
    current/all/other/<kind>/<kind>~<label> (default all: machine contract
    with no implicit filter).

    Time window: each absent bound defaults independently: `since` to UTC
    midnight of the current day, `until` to now; for the full history pass an
    explicit old `since`. `filters.since`/`filters.until` of the response
    reflect the EFFECTIVE window. The default `until` is informational: it is
    not applied as a filter (see `_default_window`), so that a task closed
    "right now" cannot fall out through skew between the app clock and the
    Postgres one.
    """
    statuses, outcome_token = _parse_terminal_statuses(status_, outcome)

    # Window default on BOTH bounds: resolved once and used both for the
    # variant resolution and for the aggregation, so both see the same window
    # and `filters.since`/`filters.until` reflect the effective window. The
    # upper bound that goes to the SQL is `window.sql_until` (None when `until`
    # is the default), not the echoed instant.
    window = _default_window(since, until, now=datetime.now(UTC))
    vf = await _resolve_variant_lens(
        db, workspace_id=ctx.workspace_id, variant=variant, statuses=statuses, window=window
    )

    return await aggregate_outcomes(
        db,
        workspace_id=ctx.workspace_id,
        since=window.since,
        until=window.sql_until,
        reported_until=window.until,
        limit=limit,
        statuses=statuses,
        outcome=outcome_token or None,
        variant=vf,
    )


@router.get("/insights/signature/tasks", response_model=SignatureTasksResponse)
async def get_signature_tasks(
    ctx: PatContext = Depends(require_pat_workspace),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    signature: Annotated[
        str,
        Query(
            description=(
                "Normalized signature (text) as it appears in `by_signature[].signature`. "
                "Alternative to `signature_hash`; exactly one of the two must be passed."
            )
        ),
    ] = "",
    signature_hash: Annotated[
        str,
        Query(
            description=(
                "Hash of the signature (`by_signature[].signature_hash`): compact, "
                "URL-safe key. Alternative to `signature`; exactly one must be passed."
            )
        ),
    ] = "",
    since: Annotated[
        datetime | None,
        Query(
            description=(
                "Restricts to tasks closed (completed_at) on or after the date. "
                "Same semantics as `/insights/outcomes` (default: UTC midnight of the "
                "current day; without offset UTC is assumed)."
            )
        ),
    ] = None,
    until: Annotated[
        datetime | None,
        Query(
            description=(
                "Restricts to tasks closed (completed_at) on or before the date. "
                "Same semantics as `/insights/outcomes` (omitted: no upper bound)."
            )
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    after: Annotated[
        int | None,
        Query(ge=1, description="Pagination cursor: returns task ids strictly lower."),
    ] = None,
    status_: Annotated[str, Query(alias="status")] = "",
    outcome: Annotated[str, Query()] = "",
    variant: Annotated[str, Query(max_length=VARIANT_TOKEN_MAX_LEN)] = "all",
) -> SignatureTasksResponse:
    """Drill-down of a signature: ALL its task ids in the window, paginated.

    `/insights/failures` and `/insights/outcomes` cap the sample of task ids per
    group at 10; for a signature with `count > 10` this endpoint returns the
    COMPLETE set. The signature is referenced by `signature_hash`
    (recommended) or by its `signature` text, exactly one; both come in every
    `by_signature` group. The `status`/`outcome`/`variant` filters and the time
    window have the SAME semantics as `/insights/outcomes` (status default:
    all terminal statuses), so it serves both the failures and the outcomes
    drill-down. Paginated by descending id via `after`/`next_after`.
    """
    sig = signature.strip()
    sig_hash = signature_hash.strip()
    if bool(sig) == bool(sig_hash):
        raise HTTPException(
            status_code=422,
            detail="provide exactly one of `signature` or `signature_hash`",
        )
    # A malformed `signature_hash` matches no row, so without this guard the
    # endpoint scans the whole window and silently returns `200 []`, hiding the
    # client's typo. A well-formed hash that does not exist does return `[]`.
    if sig_hash and not is_valid_signature_hash(sig_hash):
        raise HTTPException(
            status_code=422,
            detail="`signature_hash` must be a 16-char lowercase hex digest",
        )

    statuses, outcome_token = _parse_terminal_statuses(status_, outcome)
    window = _default_window(since, until, now=datetime.now(UTC))
    vf = await _resolve_variant_lens(
        db, workspace_id=ctx.workspace_id, variant=variant, statuses=statuses, window=window
    )

    return await list_signature_task_ids(
        db,
        workspace_id=ctx.workspace_id,
        since=window.since,
        until=window.sql_until,
        reported_until=window.until,
        statuses=statuses,
        outcome=outcome_token or None,
        variant=vf,
        signature=sig or None,
        signature_hash=sig_hash or None,
        after=after,
        limit=limit,
    )


# --- Task secrets ----------------------------------------------------------
#
# Listing is open to any member; setting and deleting, only to the `owner`. No
# response carries the value or the ciphertext. Without `TASK_SECRETS_KEYS_PATH`
# setting answers 503 with code `task_secrets_unconfigured`; listing and
# deleting do not decrypt, so listing answers 200 with the metadata and
# `keys_configured: false`, and deleting keeps working.
#
# Per-worker scope: PUT and DELETE accept `?worker=` with the UUID or the
# visible name of the worker. Without `worker`, the workspace-wide secret.
# Errors: `invalid_worker` (422, empty `worker=`), `worker_not_found` (404,
# also for one from another workspace), `worker_ambiguous` (409, name shared by
# more than one worker) and `worker_deleted` (409, only when setting; deleting
# those of a deleted worker is allowed).


class ApiV1Error(HTTPException):
    """`HTTPException` with its own error code for the v1 error model."""

    def __init__(self, status_code: int, error_code: str, detail: str) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.error_code = error_code


def _require_task_secrets_keyring():
    try:
        return task_secrets.require_keyring()
    except task_secrets.TaskSecretsUnconfigured as exc:
        raise ApiV1Error(
            status.HTTP_503_SERVICE_UNAVAILABLE, "task_secrets_unconfigured", str(exc)
        ) from None


def _validated_secret_name(name: str, *, pattern_only: bool = False) -> str:
    """Validated name or 422. `pattern_only` (deletion) does not apply the denylist
    or the `ORCH_` rule: a row stored before its name became reserved must be
    deletable."""
    try:
        if pattern_only:
            return validate_secret_name_pattern(name)
        return validate_secret_name(name)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None


def _secret_item(meta: task_secrets.SecretMeta) -> SecretItem:
    return SecretItem(
        name=meta.name,
        fingerprint=meta.fingerprint,
        created_at=meta.created_at,
        updated_at=meta.updated_at,
        updated_by=meta.updated_by,
        worker_id=meta.worker_id,
        worker_name=meta.worker_name,
    )


_WORKER_QUERY_DOC = (
    "Worker id or name (its name, or hostname if unnamed) for a worker-scoped "
    "secret. Omit it for the workspace-wide secret."
)


async def _resolve_secret_worker(
    db: AsyncSession, workspace_id: str, ref: str | None, *, allow_deleted: bool
) -> task_secrets.WorkerRef | None:
    """Worker of `?worker=`, or None without the parameter. Each failure with its code.

    The reference is not trimmed: worker names are not trimmed on registration,
    so "worker-a " and "worker-a" are different workers and trimming would
    resolve to the wrong one. Empty, only spaces or with spaces at the edges is
    422 `invalid_worker`.
    """
    if ref is None:
        return None
    if ref.strip() == "":
        raise ApiV1Error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_worker",
            "worker must be a worker id or name; omit it for a workspace-wide secret",
        )
    if ref != ref.strip():
        raise ApiV1Error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_worker",
            "worker must not have leading or trailing whitespace; "
            "pass the worker id for a worker whose name has it",
        )
    try:
        return await task_secrets.resolve_worker(
            db, workspace_id=workspace_id, ref=ref, allow_deleted=allow_deleted
        )
    except task_secrets.WorkerNotFound as exc:
        raise ApiV1Error(status.HTTP_404_NOT_FOUND, "worker_not_found", str(exc)) from None
    except task_secrets.WorkerAmbiguous as exc:
        raise ApiV1Error(status.HTTP_409_CONFLICT, "worker_ambiguous", str(exc)) from None
    except task_secrets.WorkerDeleted as exc:
        raise ApiV1Error(status.HTTP_409_CONFLICT, "worker_deleted", str(exc)) from None


@router.get("/secrets", response_model=SecretListResponse)
async def list_task_secrets(
    ctx: PatContext = Depends(require_pat_workspace),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> SecretListResponse:
    """Metadata of the workspace secrets (name, fingerprint, dates, author)."""
    items = await task_secrets.list_secrets(db, ctx.workspace_id)
    return SecretListResponse(
        items=[_secret_item(m) for m in items],
        keys_configured=task_secrets_keyring.is_configured(),
    )


@router.put("/secrets/{name}", response_model=SecretItem)
async def put_task_secret(
    name: str,
    body: SecretPutRequest,
    worker: Annotated[str | None, Query(description=_WORKER_QUERY_DOC)] = None,
    ctx: PatContext = Depends(require_pat_workspace),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> SecretItem:
    """Create or replace a secret (owner). Returns the metadata, never the value.

    With `?worker=` (UUID or name), the secret of that worker.
    """
    require_owner(ctx)
    ring = _require_task_secrets_keyring()
    clean = _validated_secret_name(name)
    target = await _resolve_secret_worker(db, ctx.workspace_id, worker, allow_deleted=False)
    try:
        meta = await task_secrets.set_secret(
            db,
            ring,
            workspace_id=ctx.workspace_id,
            name=clean,
            value=body.value,
            actor_user_id=ctx.user.id,
            worker_id=target.id if target else None,
        )
    except task_secrets.WorkerDeleted as exc:
        # `set_secret` resolves the worker again: if it was deleted between both
        # lookups, the same explicit error as the first one, not a 500.
        raise ApiV1Error(status.HTTP_409_CONFLICT, "worker_deleted", str(exc)) from None
    except task_secrets.WorkerNotFound as exc:
        raise ApiV1Error(status.HTTP_404_NOT_FOUND, "worker_not_found", str(exc)) from None
    await db.commit()
    return _secret_item(meta)


@router.delete("/secrets/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task_secret(
    name: str,
    worker: Annotated[str | None, Query(description=_WORKER_QUERY_DOC)] = None,
    ctx: PatContext = Depends(require_pat_workspace),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Delete a secret (owner). 404 if it does not exist in the workspace.

    Deletes exactly one scope: without `?worker=`, the workspace one; with it,
    that worker's, even if the worker is deleted. It does not require the keys
    file: deletion decrypts nothing.
    """
    require_owner(ctx)
    clean = _validated_secret_name(name, pattern_only=True)
    target = await _resolve_secret_worker(db, ctx.workspace_id, worker, allow_deleted=True)
    deleted = await task_secrets.delete_secret(
        db,
        workspace_id=ctx.workspace_id,
        name=clean,
        actor_user_id=ctx.user.id,
        worker_id=target.id if target else None,
    )
    if not deleted:
        detail = (
            "secret not found" if target is None else f"secret not found for worker {target.name}"
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
