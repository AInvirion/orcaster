"""Live aggregation of errors from failed tasks.

Reads `tasks` + `task_executions` + `output_events` with no migration or new
capture, so it covers the backlog retroactively. Groups by exit_code, worker and
a normalized signature of the stderr tail (see `error_signatures`). The `example`
of each signature includes the actual command, payload, worker, timing and
stdout/stderr tails. It does not classify the nature of the error.
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server.error_signatures import (
    cap_bytes,
    fallback_signature,
    last_nonempty_line,
    normalize_signature,
)
from orcaster_server.error_signatures import (
    signature_hash as compute_signature_hash,
)
from orcaster_server.schemas.api_v1 import (
    ExitCodeCount,
    FailureExample,
    FailuresFilters,
    FailureSignatureGroup,
    FailuresResponse,
    OutcomesFilters,
    OutcomesResponse,
    SignatureTasksFilters,
    SignatureTasksResponse,
    StatusOutcomeCount,
    WorkerFailureCount,
)
from orcaster_server.task_variant import (
    VARIANT_WHERE_SQL,
    VariantFilter,
    variant_sql_binds,
    variant_token,
)

_TAIL_MAX_LINES = 20
_TAIL_MAX_BYTES = 4096
_TAIL_EVENT_FETCH = 50  # last events per stream to inspect for the tail
_TAIL_EVENT_MAX_CHARS = 8192  # per-event trim before concatenating (bounds the intermediate blob)
_SAMPLE_TASK_IDS = 10
_SIGNATURE_STDERR_EVENTS = 50  # last stderr events to concatenate to derive the signature


def _as_utc(dt: datetime | None) -> datetime | None:
    """Normalize `dt` to UTC (aware) for the `completed_at` filter.

    `completed_at` is timestamptz; without this normalization, asyncpg could
    interpret a naive date according to the session's `TimeZone`. Anchoring to UTC
    here makes the filter deterministic and independent of the connection.
    Returns an aware `datetime` (asyncpg serializes it to timestamptz) or None.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def start_of_utc_day(now: datetime) -> datetime:
    """UTC midnight of the day of `now`: default window for outcomes/failures.

    Single source for the web's "today" preset and the API's default window: the
    default window starts at UTC midnight of the current day. A naive `now` is
    anchored to UTC; an aware one in another zone is converted to UTC before
    truncating, so the cut is always UTC midnight.
    """
    aware = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return aware.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


# Step 1: one row per terminal task (failed/completed/cancelled) with the
# minimum needed to group. The signature is derived from the last non-empty line
# of the last _SIGNATURE_STDERR_EVENTS stderr events concatenated in order; this
# covers the case where the last event is whitespace-only (an empty 64 KiB chunk)
# or a line was split across chunks. Each event is bounded with
# `right(text, :max_chars)` before the `string_agg` (same as _SQL_EXAMPLE_TAILS):
# without that cap the intermediate blob would be 50x64 KiB per task, and this
# path walks ALL the selected tasks (not just `limit` examples). The bound is far
# above the 200 chars the signature uses, so it does not alter the grouping. The
# last line is extracted IN SQL (`regexp_split_to_table` + `ORDER BY ord DESC`):
# this way the row that travels to Python carries a single line per task, not the
# concatenated stderr blob. With the text bounded, the counts and the signature
# are resolved in a single Python pass over light rows (signature normalization
# uses a regex that is not portable to SQL, and `rep` is computed only once).
#
# The CTEs are AS MATERIALIZED: Postgres >= 12 inlines CTEs referenced only once,
# and the planner underestimates `sel` (the `status::text` cast and the variant
# OR leave it without useful statistics). With that estimate it picks a nested
# loop whose inner side is the full stderr aggregation, re-run for each row of
# `rep`: O(n^2), 282 s for a 7-day window with ~1,300 tasks (vs 0.6 s
# materialized). Each CTE is the snapshot of one pass; inlining them can only
# make the plan worse.
_SQL_TERMINAL_TASKS = text(
    """
    WITH sel AS MATERIALIZED (
        SELECT t.id AS task_id, t.status::text AS status, t.outcome
          FROM tasks t
         WHERE t.status::text = ANY(CAST(:statuses AS text[]))
           AND t.workspace_id = :workspace_id
           AND (CAST(:outcome_mode AS text) = 'all'
                OR (CAST(:outcome_mode AS text) = 'other' AND t.outcome IS NULL)
                OR (CAST(:outcome_mode AS text) = 'named'
                    AND t.outcome = CAST(:outcome AS text)))
           AND (CAST(:since AS timestamptz) IS NULL
                OR t.completed_at >= CAST(:since AS timestamptz))
           AND (CAST(:until AS timestamptz) IS NULL
                OR t.completed_at <= CAST(:until AS timestamptz))
    """
    + VARIANT_WHERE_SQL
    + """
    ),
    rep AS MATERIALIZED (
        SELECT DISTINCT ON (e.task_id)
               e.task_id,
               e.id              AS execution_id,
               e.worker_id::text AS worker_id,
               e.exit_code
          FROM task_executions e
          JOIN sel s ON s.task_id = e.task_id
         -- Representative execution PER CLASS of the task status: for failed
         -- the exact criterion (failed/expired) is kept, so the
         -- /insights/failures wrapper gives identical results.
         WHERE (s.status = 'failed' AND e.status IN ('failed', 'expired'))
            OR (s.status = 'completed' AND e.status = 'completed')
            OR (s.status = 'cancelled' AND e.status = 'cancelled')
         ORDER BY e.task_id, e.finished_at DESC NULLS LAST, e.assigned_at DESC
    ),
    stderr_concat AS MATERIALIZED (
        SELECT execution_id,
               string_agg(text, '' ORDER BY seq) AS last_text
          FROM (
              SELECT oe.execution_id, oe.seq,
                     right(oe.text, :max_chars) AS text,
                     row_number() OVER (
                         PARTITION BY oe.execution_id ORDER BY oe.seq DESC
                     ) AS rn
                FROM output_events oe
                JOIN rep r ON r.execution_id = oe.execution_id
               WHERE oe.stream = 'stderr'
          ) s
         WHERE rn <= :stderr_events
         GROUP BY execution_id
    ),
    last_stderr AS MATERIALIZED (
        SELECT c.execution_id,
               (
                   SELECT btrim(t.line, E' \\t\\r\\n\\f\\v')
                     FROM regexp_split_to_table(c.last_text, E'\\n')
                              WITH ORDINALITY AS t(line, ord)
                    WHERE t.line ~ '[^[:space:]]'
                    ORDER BY t.ord DESC
                    LIMIT 1
               ) AS last_line
          FROM stderr_concat c
    )
    SELECT s.task_id,
           s.status,
           s.outcome,
           r.execution_id::text AS execution_id,
           r.worker_id,
           r.exit_code,
           ls.last_line
      FROM sel s
      LEFT JOIN rep r          ON r.task_id = s.task_id
      LEFT JOIN last_stderr ls ON ls.execution_id = r.execution_id
     ORDER BY s.task_id DESC
    """
)

# Step 2: details (command + payload + timing) of the example executions.
_SQL_EXAMPLE_DETAILS = text(
    """
    SELECT e.id::text       AS execution_id,
           e.task_id,
           e.worker_id::text AS worker_id,
           e.exit_code,
           e.started_at,
           e.finished_at,
           t.command,
           t.payload
      FROM task_executions e
      JOIN tasks t ON t.id = e.task_id
     WHERE e.id = ANY(string_to_array(:execution_ids, ',')::uuid[])
       AND t.workspace_id = :workspace_id
    """
)

# Step 2: the tail (last non-empty lines) per (execution, stream) is built IN
# SQL, like the signature in step 1: the row that travels to Python already
# carries the trimmed tail, not the event blob (which in the worst case is
# N x 64 KiB per stream x `limit` examples). `recent` takes the last `:events`
# events and trims each to `:max_chars` (bounds the intermediate blob that
# `string_agg` materializes); `kept` rebuilds the text with a '' separator (lines
# split across chunks), splits it on '\n' and keeps the last `:max_lines`
# non-empty ones. The JOIN to tasks bounds by workspace: the execution_ids
# already arrive scoped from step 1, but the explicit filter avoids depending on
# that invariant from a distance.
_SQL_EXAMPLE_TAILS = text(
    """
    WITH recent AS (
        SELECT oe.execution_id, oe.stream, oe.seq,
               right(oe.text, :max_chars) AS text,
               row_number() OVER (
                   PARTITION BY oe.execution_id, oe.stream
                   ORDER BY oe.seq DESC
               ) AS rn
          FROM output_events oe
          JOIN task_executions e ON e.id = oe.execution_id
          JOIN tasks t ON t.id = e.task_id
         WHERE oe.execution_id = ANY(string_to_array(:execution_ids, ',')::uuid[])
           AND t.workspace_id = :workspace_id
           AND oe.stream IN ('stdout', 'stderr', 'command')
    ),
    body AS (
        SELECT execution_id, stream,
               string_agg(text, '' ORDER BY seq) AS blob
          FROM recent
         WHERE rn <= :events
         GROUP BY execution_id, stream
    ),
    kept AS (
        SELECT b.execution_id, b.stream, t.line, t.ord,
               row_number() OVER (
                   PARTITION BY b.execution_id, b.stream ORDER BY t.ord DESC
               ) AS lrn
          FROM body b,
               LATERAL regexp_split_to_table(b.blob, E'\\n')
                           WITH ORDINALITY AS t(line, ord)
         WHERE t.line ~ '[^[:space:]]'
    )
    SELECT execution_id::text AS execution_id, stream,
           string_agg(line, E'\\n' ORDER BY ord) AS tail_text
      FROM kept
     WHERE lrn <= :max_lines
     GROUP BY execution_id, stream
    """
)


# Variants present AMONG THE TERMINAL TASKS in `statuses` (same time scope as the
# aggregation; works for any subset of terminal statuses). Feeds the resolution
# of the "current" default (the vocabulary of the outcomes `<select>` is
# workspace-wide), which is computed over the variants actually present in the
# requested subset (not over all tasks): an empty current lens while an old
# version is on fire would be bad UX.
_SQL_TERMINAL_VARIANT_COUNTS = text(
    """
    SELECT variant_kind, variant_label, COUNT(*) AS n
      FROM tasks
     WHERE status::text = ANY(CAST(:statuses AS text[]))
       AND workspace_id = :workspace_id
       AND (CAST(:since AS timestamptz) IS NULL
            OR completed_at >= CAST(:since AS timestamptz))
       AND (CAST(:until AS timestamptz) IS NULL
            OR completed_at <= CAST(:until AS timestamptz))
     GROUP BY variant_kind, variant_label
    """
)


async def terminal_variant_counts(
    db: AsyncSession,
    *,
    workspace_id: str,
    statuses: tuple[str, ...],
    since: datetime | None,
    until: datetime | None,
) -> dict[tuple[str | None, str | None], int]:
    """Map ``(kind, label) -> n`` of the variants present among the terminal
    tasks whose status is in `statuses`."""
    rows = (
        await db.execute(
            _SQL_TERMINAL_VARIANT_COUNTS,
            {
                "workspace_id": workspace_id,
                "statuses": list(statuses),
                "since": _as_utc(since),
                "until": _as_utc(until),
            },
        )
    ).all()
    return {(r[0], r[1]): int(r[2]) for r in rows}


async def failed_variant_counts(
    db: AsyncSession,
    *,
    workspace_id: str,
    since: datetime | None,
    until: datetime | None,
) -> dict[tuple[str | None, str | None], int]:
    """Map ``(kind, label) -> n`` of the variants present among the failed tasks.

    Stable-contract wrapper over `terminal_variant_counts(statuses=("failed",))`.
    """
    return await terminal_variant_counts(
        db, workspace_id=workspace_id, statuses=("failed",), since=since, until=until
    )


def _outcome_mode_name(outcome: str | None) -> tuple[str, str | None]:
    """Translate the public outcome token into `(outcome_mode, outcome_name)`.

    `None` = all (`"all"`); `"other"` = no outcome (NULL); exact name =
    `"named"` with that name. Single source for `aggregate_outcomes` and the
    `list_signature_task_ids` drill-down, so that their outcome filter cannot
    diverge.
    """
    if outcome is None:
        return "all", None
    if outcome == "other":
        return "other", None
    return "named", outcome


async def _aggregate_terminal(
    db: AsyncSession,
    *,
    workspace_id: str,
    since: datetime | None,
    until: datetime | None,
    limit: int,
    statuses: tuple[str, ...],
    outcome_mode: str,
    outcome_name: str | None,
    variant: VariantFilter | None,
) -> dict:
    """Common body of `aggregate_failures`/`aggregate_outcomes`.

    Aggregates the terminal tasks whose status is in `statuses` (additionally
    filtered by outcome via `outcome_mode`/`outcome_name`: `"all"` = no filter,
    `"other"` = NULL outcome, `"named"` = exact `outcome_name`) into counts +
    signature groups, plus the breakdown by (status, outcome) (`so_counter`).

    `since`/`until` filter `completed_at` (timestamptz). A naive date (without a
    timezone offset) is assumed to be **UTC** here, before sending it to the
    Postgres CAST: this way the filter does not depend on the connection
    session's `TimeZone`.

    `variant` is the version LENS: when it is None the `all` mode (no filter) is
    used.

    Returns the raw pieces (`total`, `so_counter`, `by_exit_code`, `by_worker`,
    `by_signature`); each public wrapper builds its own Response.
    """
    rows = (
        (
            await db.execute(
                _SQL_TERMINAL_TASKS,
                {
                    "workspace_id": workspace_id,
                    "since": _as_utc(since),
                    "until": _as_utc(until),
                    "statuses": list(statuses),
                    "outcome_mode": outcome_mode,
                    "outcome": outcome_name,
                    "stderr_events": _SIGNATURE_STDERR_EVENTS,
                    "max_chars": _TAIL_EVENT_MAX_CHARS,
                    **variant_sql_binds(variant or VariantFilter(mode="all")),
                },
            )
        )
        .mappings()
        .all()
    )

    total = len(rows)
    so_counter: Counter = Counter()
    exit_counter: Counter = Counter()
    worker_counter: Counter = Counter()
    groups: OrderedDict[str, dict] = OrderedDict()

    for row in rows:
        so_counter[(row["status"], row["outcome"])] += 1
        exit_code = row["exit_code"]
        worker_id = row["worker_id"]
        # The last non-empty stderr line already comes extracted by SQL (NULL if
        # the representative has no usable stderr -> fallback).
        last_line = row["last_line"]
        signature = normalize_signature(last_line) if last_line else fallback_signature(exit_code)

        exit_counter[exit_code] += 1
        if worker_id is not None:
            worker_counter[worker_id] += 1

        g = groups.get(signature)
        if g is None:
            g = {
                "count": 0,
                "exit_codes": set(),
                "task_ids": [],
                "example_execution_id": row["execution_id"],
                "example_task_id": row["task_id"],
            }
            groups[signature] = g
        g["count"] += 1
        g["exit_codes"].add(exit_code)
        if len(g["task_ids"]) < _SAMPLE_TASK_IDS:
            g["task_ids"].append(row["task_id"])
        # Prefer an example with an execution (not None) to allow drill-down.
        if g["example_execution_id"] is None and row["execution_id"] is not None:
            g["example_execution_id"] = row["execution_id"]
            g["example_task_id"] = row["task_id"]

    top = sorted(groups.items(), key=lambda kv: kv[1]["count"], reverse=True)[:limit]

    example_ids = [g["example_execution_id"] for _, g in top if g["example_execution_id"]]
    details = await _load_examples(db, example_ids, workspace_id=workspace_id)

    by_signature = [
        FailureSignatureGroup(
            signature=signature,
            signature_hash=compute_signature_hash(signature),
            count=g["count"],
            exit_codes=sorted(g["exit_codes"], key=lambda c: (c is None, c)),
            example=_build_example(
                example_task_id=g["example_task_id"],
                execution_id=g["example_execution_id"],
                detail=details.get(g["example_execution_id"])
                if g["example_execution_id"]
                else None,
            ),
            sample_task_ids=g["task_ids"],
        )
        for signature, g in top
    ]

    by_exit_code = [
        ExitCodeCount(exit_code=code, count=cnt)
        for code, cnt in sorted(exit_counter.items(), key=lambda kv: kv[1], reverse=True)
    ]
    by_worker = [
        WorkerFailureCount(worker_id=wid, count=cnt)
        for wid, cnt in sorted(worker_counter.items(), key=lambda kv: kv[1], reverse=True)
    ]

    return {
        "total": total,
        "so_counter": so_counter,
        "by_exit_code": by_exit_code,
        "by_worker": by_worker,
        "by_signature": by_signature,
    }


def _reported_bound(until: datetime | None, reported_until: datetime | None) -> datetime | None:
    """Value to echo in `filters.until`.

    By default the same bound that filtered the SQL (`until`) is echoed. When the
    caller omits the upper bound in the SQL on purpose (the default window of the
    insights endpoints, which does not filter by `until` so as not to depend on
    app<->DB skew), it passes the effective instant in `reported_until` and that
    is the one that travels in the response, informational only.
    """
    return reported_until if reported_until is not None else until


async def aggregate_failures(
    db: AsyncSession,
    *,
    workspace_id: str,
    since: datetime | None,
    until: datetime | None,
    limit: int,
    variant: VariantFilter | None = None,
    reported_until: datetime | None = None,
) -> FailuresResponse:
    """Aggregate the workspace's failed tasks into counts + signature groups.

    `since`/`until` filter `completed_at` (timestamptz). A naive date (without a
    timezone offset) is assumed to be **UTC** here, before sending it to the
    Postgres CAST: this way the filter does not depend on the connection
    session's `TimeZone`. `until=None` = no upper bound.

    `reported_until` decouples the echo from the filter: when the caller leaves
    the upper bound open on purpose (`until=None`) but wants to report the
    window, it passes the effective instant here and `filters.until` echoes it
    without it having bounded the result. If omitted, `filters.until` echoes
    `until`.

    `variant` is the version LENS: when it is None the `all` mode (no filter) is
    used, preserving the behaviour of the `/api/v1/insights/failures` endpoint.

    Stable-contract wrapper: internally it is `_aggregate_terminal` fixed to
    `statuses=("failed",)` and with no outcome filter; the signature and the
    response shape do not change.
    """
    agg = await _aggregate_terminal(
        db,
        workspace_id=workspace_id,
        since=since,
        until=until,
        limit=limit,
        statuses=("failed",),
        outcome_mode="all",
        outcome_name=None,
        variant=variant,
    )

    return FailuresResponse(
        generated_at=datetime.now(UTC),
        filters=FailuresFilters(
            since=since, until=_reported_bound(until, reported_until), limit=limit
        ),
        total_failed_tasks=agg["total"],
        by_exit_code=agg["by_exit_code"],
        by_worker=agg["by_worker"],
        by_signature=agg["by_signature"],
    )


async def aggregate_outcomes(
    db: AsyncSession,
    *,
    workspace_id: str,
    since: datetime | None,
    until: datetime | None,
    limit: int,
    statuses: tuple[str, ...],
    outcome: str | None,
    variant: VariantFilter | None = None,
    reported_until: datetime | None = None,
) -> OutcomesResponse:
    """Aggregate ALL the requested terminal statuses by (status, outcome).

    `statuses`: subset of terminal statuses (`completed`/`failed`/`cancelled`) to
    include. `outcome`: `None` = all outcomes (plain + named); `"other"` = only
    tasks without an outcome (NULL); exact name = only that outcome.
    `variant` is the same version LENS as in `aggregate_failures`.
    `reported_until`: instant to echo in `filters.until` when the SQL's upper
    bound is open on purpose; see `aggregate_failures`.
    """
    outcome_mode, outcome_name = _outcome_mode_name(outcome)

    agg = await _aggregate_terminal(
        db,
        workspace_id=workspace_id,
        since=since,
        until=until,
        limit=limit,
        statuses=statuses,
        outcome_mode=outcome_mode,
        outcome_name=outcome_name,
        variant=variant,
    )

    by_status_outcome = [
        StatusOutcomeCount(status=status, outcome=out, count=n)
        for (status, out), n in sorted(
            agg["so_counter"].items(), key=lambda kv: kv[1], reverse=True
        )
    ]

    return OutcomesResponse(
        generated_at=datetime.now(UTC),
        filters=OutcomesFilters(
            since=since,
            until=_reported_bound(until, reported_until),
            limit=limit,
            status=list(statuses),
            outcome=outcome,
            variant=variant_token(variant) if variant is not None else "all",
        ),
        total_tasks=agg["total"],
        by_status_outcome=by_status_outcome,
        by_exit_code=agg["by_exit_code"],
        by_worker=agg["by_worker"],
        by_signature=agg["by_signature"],
    )


async def list_signature_task_ids(
    db: AsyncSession,
    *,
    workspace_id: str,
    since: datetime | None,
    until: datetime | None,
    statuses: tuple[str, ...],
    outcome: str | None,
    variant: VariantFilter | None,
    signature: str | None = None,
    signature_hash: str | None = None,
    after: int | None,
    limit: int,
    reported_until: datetime | None = None,
) -> SignatureTasksResponse:
    """Drill-down: ALL the task ids of a signature in the window, paged by id.

    `/insights/failures` caps the id sample at `_SAMPLE_TASK_IDS`; for a signature
    with `count > 10` there would be no way through the API to get the full set.
    Signature normalization lives in Python (`normalize_signature`) and cannot be
    expressed in the current SQL, so the drill-down reuses the SAME pass as the
    aggregation (`_SQL_TERMINAL_TASKS`), recomputes the signature per task and
    filters to the requested one.

    Selector: exactly one of `signature` (normalized text) or `signature_hash`
    (its hash, see `compute_signature_hash`). Validating "exactly one" is the
    endpoint's job; it is assumed already resolved here.

    Filters identical to `aggregate_outcomes` (statuses/outcome/variant/window),
    so it serves both `/insights/failures` (statuses=("failed",)) and
    `/insights/outcomes`. Paged by descending id: `after` returns ids STRICTLY
    lower; `next_after` is the cursor for the next page (None if there are no
    more).

    Cost: each page rescans the terminal tasks of the window (the same pass the
    aggregation already pays) and filters/pages in Python. Acceptable for an ops
    drill-down tool.
    """
    outcome_mode, outcome_name = _outcome_mode_name(outcome)
    rows = (
        (
            await db.execute(
                _SQL_TERMINAL_TASKS,
                {
                    "workspace_id": workspace_id,
                    "since": _as_utc(since),
                    "until": _as_utc(until),
                    "statuses": list(statuses),
                    "outcome_mode": outcome_mode,
                    "outcome": outcome_name,
                    "stderr_events": _SIGNATURE_STDERR_EVENTS,
                    "max_chars": _TAIL_EVENT_MAX_CHARS,
                    **variant_sql_binds(variant or VariantFilter(mode="all")),
                },
            )
        )
        .mappings()
        .all()
    )

    # Selector by text or by hash. With text it is compared directly; with a hash
    # the recomputed signature of each row is hashed (the hash is not reversible).
    match_by_hash = signature_hash is not None
    resolved_signature: str | None = signature
    matched: list[int] = []  # matching ids, already in id DESC order (from the SQL ORDER BY)
    for row in rows:
        last_line = row["last_line"]
        sig = normalize_signature(last_line) if last_line else fallback_signature(row["exit_code"])
        if match_by_hash:
            if compute_signature_hash(sig) != signature_hash:
                continue
        elif sig != signature:
            continue
        resolved_signature = sig
        task_id = row["task_id"]
        if after is not None and task_id >= after:
            continue
        matched.append(task_id)

    page = matched[:limit]
    next_after = page[-1] if len(matched) > limit else None
    resolved_hash = signature_hash if match_by_hash else compute_signature_hash(signature or "")

    return SignatureTasksResponse(
        generated_at=datetime.now(UTC),
        filters=SignatureTasksFilters(
            since=since,
            until=_reported_bound(until, reported_until),
            limit=limit,
            after=after,
            status=list(statuses),
            outcome=outcome,
            variant=variant_token(variant) if variant is not None else "all",
        ),
        signature=resolved_signature,
        signature_hash=resolved_hash,
        task_ids=page,
        next_after=next_after,
    )


async def _load_examples(
    db: AsyncSession, execution_ids: list[str], *, workspace_id: str
) -> dict[str, dict]:
    """Load details + tails of the example executions (<= limit)."""
    if not execution_ids:
        return {}
    joined = ",".join(execution_ids)
    detail_rows = (
        (
            await db.execute(
                _SQL_EXAMPLE_DETAILS,
                {"execution_ids": joined, "workspace_id": workspace_id},
            )
        )
        .mappings()
        .all()
    )
    tail_rows = (
        (
            await db.execute(
                _SQL_EXAMPLE_TAILS,
                {
                    "execution_ids": joined,
                    "events": _TAIL_EVENT_FETCH,
                    "max_chars": _TAIL_EVENT_MAX_CHARS,
                    "max_lines": _TAIL_MAX_LINES,
                    "workspace_id": workspace_id,
                },
            )
        )
        .mappings()
        .all()
    )

    # The tail per (execution_id, stream) already comes built in SQL (last
    # _TAIL_MAX_LINES non-empty lines): a single row per stream.
    tails: dict[tuple[str, str], str] = {}
    for r in tail_rows:
        tails[(r["execution_id"], r["stream"])] = r["tail_text"] or ""

    out: dict[str, dict] = {}
    for r in detail_rows:
        eid = r["execution_id"]
        stderr_text = tails.get((eid, "stderr"), "")
        stdout_text = tails.get((eid, "stdout"), "")
        command_text = tails.get((eid, "command"), "")
        out[eid] = {
            "task_id": r["task_id"],
            "worker_id": r["worker_id"],
            "exit_code": r["exit_code"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "command": r["command"],
            "payload": r["payload"] or {},
            # The worker emits the echo as "$ {cmd}\n"; strip the prefix for the response.
            "command_line": (lambda cl: cl[2:] if cl and cl.startswith("$ ") else cl)(
                last_nonempty_line(command_text)
            ),
            # The line count is already applied by the SQL (_TAIL_MAX_LINES); only
            # the byte cap here. A single source for the line limit.
            "stderr_tail": cap_bytes(stderr_text, max_bytes=_TAIL_MAX_BYTES),
            "stdout_tail": cap_bytes(stdout_text, max_bytes=_TAIL_MAX_BYTES),
        }
    return out


def _build_example(
    *, example_task_id: int, execution_id: str | None, detail: dict | None
) -> FailureExample:
    """Build the `FailureExample`; with no detail (no execution) return minimal fields."""
    if detail is None:
        return FailureExample(task_id=example_task_id, execution_id=execution_id)
    started = detail["started_at"]
    finished = detail["finished_at"]
    duration = (
        (finished - started).total_seconds()
        if started is not None and finished is not None
        else None
    )
    return FailureExample(
        task_id=detail["task_id"],
        execution_id=execution_id,
        exit_code=detail["exit_code"],
        worker_id=detail["worker_id"],
        command=detail["command"],
        command_line=detail["command_line"],
        payload=detail["payload"],
        started_at=started,
        finished_at=finished,
        duration_seconds=duration,
        stderr_tail=detail["stderr_tail"],
        stdout_tail=detail["stdout_tail"],
    )
