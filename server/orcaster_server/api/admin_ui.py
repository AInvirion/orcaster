"""HTMX dashboard.

GET-only endpoints for the admin UI. They coexist with
`admin_tasks` and `admin_workers` (POST/DELETE) without colliding.

HX-Request convention:
- `GET /admin/workers` and `GET /admin/tasks` always return the partial
  (table fragment). The dashboard embeds them via `hx-get` with polling.
  Opening the path directly in the browser still serves a valid HTML
  fragment (no <html>), enough for debugging. The canonical "wrapper"
  page lives at `/admin/`.
- `GET /admin/tasks/{id}` and `GET /admin/workers/{id}` always return the
  full page (with <html>); the inner polling uses `hx-select` to
  replace only the executions block.

auth: gated by require_workspace (session + workspace membership); routes that
change the workspace (rename, invitations, share links, secrets) are owner-only
through `require_owner`.
"""

from __future__ import annotations

import asyncio
import json
import math
import zipfile
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs, quote, urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server import package_version, signing, storage
from orcaster_server.api.admin_tasks import (
    _SQL_FORK_PARAMS,
    _SQL_FORK_PARAMS_BATCH,
    _params_changed,
)
from orcaster_server.artifact_indicator_status import read_sync_status
from orcaster_server.artifacts_store import artifact_abs_path, iter_artifact_bytes
from orcaster_server.audit import record_admin_action
from orcaster_server.auth.config import AuthConfig
from orcaster_server.auth.service.invitation import create_invitation
from orcaster_server.config import settings
from orcaster_server.db import get_session
from orcaster_server.deps import WorkspaceContext, require_workspace
from orcaster_server.dispatch_eligibility import block_reason_text, classify_dispatch_block
from orcaster_server.env_names import validate_secret_name
from orcaster_server.failures import aggregate_outcomes, start_of_utc_day, terminal_variant_counts
from orcaster_server.models import TERMINAL_EXECUTION_STATUSES
from orcaster_server.output_log import resolve_execution_in_workspace, stream_output_gzip
from orcaster_server.pats.service import (
    delete_pat_by_id,
    list_pats,
    mint_pat_returning_id,
    revoke_pat_by_id,
)
from orcaster_server.sse_bus import SseMessage
from orcaster_server.status.snapshot import compute_progress
from orcaster_server.status.tokens import (
    create_share_token,
    list_share_tokens,
    revoke_share_token,
)
from orcaster_server.task_outcomes import OUTCOME_NAME_RE, legacy_status_aliases
from orcaster_server.task_secrets import blocked as task_secrets_blocked
from orcaster_server.task_secrets import keyring as task_secrets_keyring
from orcaster_server.task_secrets import service as task_secrets
from orcaster_server.task_secrets.gate import (
    TASK_SECRETS_MIN_WORKER_VERSION_STR,
    task_uses_stored_secrets_sql,
    worker_supports_task_secrets,
)
from orcaster_server.task_variant import (
    VARIANT_WHERE_SQL,
    VariantFilter,
    accumulate_variant_grid,
    build_variant_options,
    current_variant_keys,
    resolve_variant_filter,
    variant_row_matches,
    variant_sql_binds,
    variant_token,
)
from orcaster_server.workspaces.deps import require_owner
from orcaster_server.workspaces.enrollment import (
    delete_enrollment_token_by_id,
    list_enrollment_tokens,
    list_token_uses,
    mint_enrollment_token_returning_id,
    revoke_enrollment_token_by_id,
)
from orcaster_server.workspaces.service import (
    get_workspace_name,
    list_members,
    list_membership_workspaces,
    rename_workspace,
    set_active_workspace,
)

router = APIRouter(prefix="/admin", tags=["admin-ui"], include_in_schema=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
WorkspaceDep = Annotated[WorkspaceContext, Depends(require_workspace)]

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Cache flush: an old worker ignores `clear_cache` and never
# acks, so `cache_flush_done_at` is never stamped. After this TTL the pending
# state is considered "timed out" (derived from age, without mutating the DB);
# a new "Clear cache" re-stamps `cache_flush_requested_at` and restarts the cycle.
CACHE_FLUSH_TTL = timedelta(minutes=10)


# --- Jinja filters ----------------------------------------------------------


def _relative_time(value: datetime | None) -> str:
    """Human-readable difference from `now()`. Assumes UTC if the datetime is naive
    (postgres timestamptz arrives with tz; tests may send naive)."""
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - value
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _iso_utc(value: datetime | None) -> str:
    """Stable ISO 8601 for tooltips/`datetime=` attributes."""
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat(timespec="seconds")


def _dt_human(value: datetime | None) -> str:
    """`%Y-%m-%d %H:%M:%S` in UTC. For cells where we want a compact absolute
    timestamp instead of a relative one. Naive -> assumes UTC."""
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


# Tuple of English month names (1-based index: _MONTHS[1] == "Jan").
# We do not use strftime("%b") so as not to depend on the server locale.
# Must match the MONTHS array in app.js.
_MONTHS = (
    "",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _dt_compact(value: datetime | None) -> str:
    """UTC timestamp in the format `"Mmm DD HH:MM"` (e.g. `"Jun 18 12:34"`).

    No year or seconds — visible fallback for the RUNTIME column before
    app.js rewrites it to adaptive local time. Naive -> assumes UTC."""
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    dt = value.astimezone(UTC)
    return f"{_MONTHS[dt.month]} {dt.day:02d} {dt.hour:02d}:{dt.minute:02d}"


def _args_pretty(payload: dict | None) -> str:
    """Summarizes the payload for the listing's "Args" column.

    The worker contract is `commands: list[str]` +
    `requirements: list[str]`. We show the first command (with a
    `(+N more)` suffix if there are more) and, if there are `requirements`, a short
    badge. If the payload does not fit that contract, we return the raw JSON so
    that nothing is hidden.
    """
    if not payload:
        return ""
    commands = payload.get("commands")
    requirements = payload.get("requirements")
    looks_like_commands = isinstance(commands, list) and all(isinstance(c, str) for c in commands)
    if not looks_like_commands:
        return json.dumps(payload, ensure_ascii=False, separators=(", ", ": "))
    parts: list[str] = []
    if commands:
        head = commands[0]
        if len(commands) > 1:
            head = f"{head}  (+{len(commands) - 1} more)"
        parts.append(head)
    if isinstance(requirements, list) and requirements:
        parts.append(f"[reqs: {len(requirements)}]")
    return "  ".join(parts)


def _payload_commands(payload: dict | None) -> list[str] | None:
    """Command list if the payload has the `commands` + `requirements` shape, `None` otherwise.

    Returns `None` when the payload is not a dict, has no `commands`, it is not
    a list, is empty, or any element is not a `str`. The template uses
    `None` to branch to the "raw JSON" mode.
    """
    if not isinstance(payload, dict):
        return None
    commands = payload.get("commands")
    if not isinstance(commands, list) or not commands:
        return None
    if not all(isinstance(c, str) for c in commands):
        return None
    return commands


def _payload_json(payload: dict | None) -> str:
    """Canonical JSON of the payload for the Copy button and the line viewer.

    `indent=2`, `sort_keys=True` (stable order across renders),
    `ensure_ascii=False` (literal unicode, not escaped). Returns `"{}"` if the
    payload is falsy or not serializable — never raises, the template must not break.
    """
    if not payload:
        return "{}"
    try:
        return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return "{}"


def _duration_human(seconds: int | float | None) -> str:
    """Compact duration: "850ms", "12.4s", "3m 12s", "1h 04m"."""
    if seconds is None:
        return "—"
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "—"
    if s < 0:
        return "—"
    if s < 1:
        return f"{int(round(s * 1000))}ms"
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        m = int(s // 60)
        rem = int(round(s - m * 60))
        return f"{m}m {rem:02d}s"
    h = int(s // 3600)
    m = int((s - h * 3600) // 60)
    return f"{h}h {m:02d}m"


def _mb_human(mb: int | float | None) -> str:
    """Formats MB -> "1.2 GB" / "512 MB" / "?". Keeps cells compact when
    narrow: GB with 1 decimal up to 10, then integer."""
    if mb is None:
        return "?"
    try:
        mb_val = float(mb)
    except (TypeError, ValueError):
        return "?"
    if mb_val < 0:
        return "?"
    if mb_val >= 1024:
        gb = mb_val / 1024
        return f"{gb:.1f} GB" if gb < 10 else f"{int(round(gb))} GB"
    return f"{int(round(mb_val))} MB"


def _thousands(n: int | None) -> str:
    """Formats an integer with a thousands separator (4762 -> "4,762"). The COUNT
    of the artifacts indicator is never NULL, but we harden the filter: None/non-int -> "0"."""
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "0"


def _artifact_indicator_enabled() -> bool:
    """True if the artifacts indicator is active (configured suffix is non-empty).
    Callable Jinja global (not a value): evaluated on every render to honor
    config changes/monkeypatch. base.html omits the <li> and its polling when it is
    False, instead of rendering an empty pill that polls /admin/artifact-indicator every
    30s for no reason."""
    return bool(settings.artifact_indicator_suffix)


def _usage_pct(used_mb: int | float | None, total_mb: int | float | None) -> int:
    """Percentage 0..100 clamped. Returns 0 if any datum is missing."""
    if not total_mb or used_mb is None:
        return 0
    try:
        pct = (float(used_mb) / float(total_mb)) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return 0
    if pct < 0:
        return 0
    if pct > 100:
        return 100
    return int(round(pct))


def _load_tier(pct: int | None) -> str:
    """Load tier by usage % (unified 50/80 thresholds), reusable
    for RAM, Disk and CPU. 'none' when there is no data (renders gray)."""
    if pct is None:
        return "none"
    if pct > 80:
        return "high"
    if pct > 50:
        return "mid"
    return "low"


# POSIX convention: a process terminated by signal N reports exit code 128+N.
# We map only the real signals (129..159) to a readable name for the badge
# title. The rest (1, 2, 255…) are errors of the task itself.
_SIGNAL_EXIT_NAMES = {
    129: "SIGHUP",
    130: "SIGINT",
    131: "SIGQUIT",
    134: "SIGABRT",
    135: "SIGBUS",
    136: "SIGFPE",
    137: "SIGKILL/OOM",
    139: "SIGSEGV",
    141: "SIGPIPE",
    143: "SIGTERM",
}


def _exit_severity(code: int | None) -> str:
    """Severity class of an exit code in the failures view.

    The color encodes the CLASS of failure for triage, it is not decoration:
    - 129..159 (POSIX 128+signal: SIGKILL/OOM 137, SIGTERM 143, SIGSEGV 139…)
      is death by signal → infrastructure → ``is-danger`` (red).
    - any other non-zero exit code (1, 2, 255…) is the task
      failing on its own logic → ``is-warn`` (amber).
    - no exit code (None: expired or unreported) does not classify → neutral (the
      base ``.badge``, gray).

    This does not mutate the backend, which deliberately does not classify the nature of
    the error (``orcaster_server/failures.py``); classification lives only in presentation.
    """
    if code is None or code == 0:
        return ""
    if 129 <= code <= 159:
        return "is-danger"
    return "is-warn"


def _exit_hint(code: int | None) -> str:
    """Text of the exit code badge ``title``: explains the failure class.

    Hidden until hover, so it does not clutter the dense UI; helps color-blind users (the
    status is not conveyed by color alone) and anyone who does not know 137 by heart.
    """
    if code is None:
        return "no exit code reported — expired or lost"
    if code == 0:
        return ""
    if 129 <= code <= 159:
        name = _SIGNAL_EXIT_NAMES.get(code, f"signal {code - 128}")
        return f"signal termination ({name}) — infrastructure"
    return "application error — task exited non-zero"


templates.env.filters["relative_time"] = _relative_time
templates.env.filters["iso_utc"] = _iso_utc
templates.env.filters["dt_human"] = _dt_human
templates.env.filters["dt_compact"] = _dt_compact
templates.env.filters["mb_human"] = _mb_human
templates.env.filters["thousands"] = _thousands
templates.env.filters["duration_human"] = _duration_human
templates.env.filters["args_pretty"] = _args_pretty
templates.env.filters["payload_commands"] = _payload_commands
templates.env.filters["payload_json"] = _payload_json
templates.env.filters["load_tier"] = _load_tier
templates.env.filters["exit_severity"] = _exit_severity
templates.env.filters["exit_hint"] = _exit_hint
templates.env.globals["usage_pct"] = _usage_pct
templates.env.globals["artifact_indicator_enabled"] = _artifact_indicator_enabled

# Tooltips for status chips: single source of truth consumed by both the
# dashboard filter chips and the status badge in the ID column. Short
# text, in English (UI rule). Covers both task_status and execution_status
# values that may appear in other fragments.
_STATUS_TOOLTIPS = {
    "queued": "Waiting for a worker to claim it",
    "dispatching": "Being assigned to one or more workers right now",
    "paused": "Held by an operator; will not be dispatched",
    "completed": "Reached its target count of successful runs",
    "cancelled": "Stopped by an operator before completion",
    "failed": "Hit its retry budget without succeeding",
    "assigned": "Reserved for a worker that has not started yet",
    "claimed": "Picked up by a worker; about to start",
    "running": "Currently executing on a worker",
    "expired": "Claim window elapsed before the worker started",
}
templates.env.globals["status_tooltips"] = _STATUS_TOOLTIPS


# --- SQL --------------------------------------------------------------------

_SQL_LIST_WORKERS = text(
    """
    SELECT w.id::text AS id, w.hostname, w.name, w.os, w.arch, w.status,
           w.ram_mb, w.ram_used_mb, w.disk_mb, w.disk_used_mb, w.cpu_pct,
           w.version, w.last_heartbeat_at, w.free_disk_fraction,
           COALESCE(a.active_count, 0) AS active_count,
           (SELECT te.task_id
              FROM task_executions te
             WHERE te.worker_id = w.id
               AND te.status = 'running'
             ORDER BY te.assigned_at DESC
             LIMIT 1) AS active_task_id
      FROM workers w
      LEFT JOIN (
           SELECT worker_id, COUNT(*) AS active_count
             FROM task_executions
            WHERE status IN ('assigned','claimed','running')
            GROUP BY worker_id
      ) a ON a.worker_id = w.id
     WHERE w.deleted_at IS NULL
       AND w.workspace_id = :workspace_id
     ORDER BY w.registered_at DESC
     LIMIT 200
    """
)

# WHERE shared between the list and the filtered count. Keeping it as a constant
# avoids deriving the same filter twice (the count must be exactly the
# universe that is listed pre-LIMIT).
_SQL_TASKS_WHERE = (
    """
    WHERE t.workspace_id = :workspace_id
      AND (CAST(:status_filter AS text) IS NULL
           OR t.status::text = ANY(string_to_array(CAST(:status_filter AS text), ',')))
      AND (CAST(:q AS text) = ''
           OR t.command ILIKE '%' || CAST(:q AS text) || '%'
           OR t.payload::text ILIKE '%' || CAST(:q AS text) || '%'
           OR (CAST(:q_is_numeric AS boolean) AND t.id::text LIKE CAST(:q AS text) || '%'))
      AND (
           CAST(:artifact_mode AS text) = 'off'
           OR (CAST(:artifact_mode AS text) = 'present' AND EXISTS (
                 SELECT 1
                   FROM task_executions ae
                   JOIN task_artifacts aa ON aa.execution_id = ae.id
                  WHERE ae.task_id = t.id
                    AND (CAST(:artifact_type AS text) = ''
                         OR aa.content_type = CAST(:artifact_type AS text))
                    AND (CAST(:artifact_sha256_prefix AS text) = ''
                         OR aa.sha256 LIKE CAST(:artifact_sha256_prefix AS text))
                    AND (CAST(:artifact_path_like AS text) = ''
                         OR aa.rel_path ILIKE CAST(:artifact_path_like AS text))
              ))
           OR (CAST(:artifact_mode AS text) = 'absent' AND NOT EXISTS (
                 SELECT 1
                   FROM task_executions ae2
                   JOIN task_artifacts aa2 ON aa2.execution_id = ae2.id
                  WHERE ae2.task_id = t.id
              ))
      )
      AND (CAST(:outcome_filter AS text) IS NULL
           OR (
                (t.outcome IS NOT NULL
                 AND t.outcome = ANY(string_to_array(CAST(:outcome_filter AS text), ',')))
                OR (
                     'other' = ANY(string_to_array(CAST(:outcome_filter AS text), ','))
                     AND t.outcome IS NULL
                   )
              ))
"""
    + VARIANT_WHERE_SQL
)

# Template of the main SELECT. ORDER BY is concatenated in Python with
# values already validated by whitelist (see `_SORT_COLUMNS`/`_SORT_DIRS`); we do not
# parametrize because asyncpg does not support placeholders in ORDER BY clauses.
_SQL_LIST_TASKS_TEMPLATE = (
    """
    SELECT t.id, t.command, t.payload, t.status, t.outcome, t.target_mode, t.target_count,
           t.created_at, t.parent_task_id,
           -- Most recent execution (highest assigned_at): feeds BOTH the
           -- RUNTIME column (started_at/finished_at) AND the inline failure
           -- signal (exit_code/stderr_tail). A single LATERAL guarantees that
           -- both columns come from the same attempt, avoiding discrepancies
           -- between the time window and the displayed exit_code.
           lx.started_at  AS last_started_at,
           lx.finished_at AS last_finished_at,
           -- `done`: for `all_available` it counts only the current epoch of the
           -- snapshot (migration 0033) — the retry re-snapshots and
           -- `target_count` is re-synced to the new snapshot, so the
           -- "done / N" must measure that epoch (without the scope, completed rows from
           -- old epochs inflate done above N). count-mode is
           -- cumulative over the whole history, same as its closing.
           COALESCE(SUM(CASE WHEN e.status = 'completed'
                              AND (t.target_mode <> 'all_available'
                                   OR e.snapshot_epoch = t.snapshot_epoch)
                             THEN 1 ELSE 0 END), 0) AS done,
           lx.exit_code   AS last_exit_code,
           lx.stderr_tail AS last_stderr_tail,
           -- Labels of workers that have at least one execution for this
           -- task. We prefer `w.name` if present, falling back to `w.hostname` (which
           -- is NOT NULL). `array_agg(DISTINCT …)` yields an alphabetically
           -- sorted array — stable order across polls without needing an
           -- explicit ORDER BY. Tasks without executions return `{}`.
           COALESCE(
             array_remove(
               array_agg(DISTINCT COALESCE(NULLIF(w.name, ''), w.hostname)),
               NULL
             ),
             ARRAY[]::text[]
           ) AS worker_labels,
           -- "Waiting for an eligible worker". An `all_available`
           -- `dispatching` task with NO execution in its current epoch has
           -- nobody to wait for: nobody will claim it and nothing will close it
           -- until a suitable worker joins and housekeeping
           -- re-snapshots it. Without this column the row is indistinguishable from one
           -- that is being dispatched right now. The reason (no workers
           -- online vs no capability) lives in the detail view, which can afford
           -- the per-gate count query; the listing is polled every 5s.
           (t.target_mode = 'all_available'
            AND t.status = 'dispatching'
            AND NOT EXISTS (
                  SELECT 1 FROM task_executions ae3
                   WHERE ae3.task_id = t.id
                     AND ae3.snapshot_epoch = t.snapshot_epoch
                )) AS awaiting_eligible
      FROM tasks t
      LEFT JOIN task_executions e ON e.task_id = t.id
      LEFT JOIN workers w ON w.id = e.worker_id
      LEFT JOIN LATERAL (
           -- Most recent execution of the task (highest assigned_at). assigned_at
           -- is NOT NULL (default now()); the tiebreak by attempt_index DESC
           -- covers the case of two executions with the same assigned_at. The leading
           -- index of task_executions_attempt_uk filters by task_id; assigned_at
           -- is not in the index, so ORDER BY is resolved in memory
           -- (cheap: a task typically has few attempts).
           -- stderr_tail: last 200 chars of stderr (rtrim) — where the relevant
           -- trace usually lives. The UI truncates again to ~70 visual.
           -- The stderr lives in output_events (the stdout/stderr
           -- columns of task_executions were dropped in 0049);
           -- it is rebuilt by concatenating the last events in seq order,
           -- same criterion as the tails in failures.py. 5 events × 400
           -- chars give ample margin for a 200 post-rtrim tail, and the
           -- uq_output_events_execution_seq index resolves the ORDER BY
           -- (although it does not cover `stream`: the filter is applied while scanning).
           -- The CASE avoids paying for the subquery on non-failed rows — the
           -- only condition under which the template shows it — because
           -- this listing is polled every 5s with ~50 rows, mostly non-failed.
           SELECT ex.started_at,
                  ex.finished_at,
                  ex.exit_code,
                  CASE WHEN t.status = 'failed' THEN (
                    SELECT NULLIF(
                             RIGHT(
                               regexp_replace(
                                 string_agg(s.text, '' ORDER BY s.seq),
                                 '\\s+$', ''
                               ),
                               200
                             ),
                             ''
                           )
                      FROM (
                          SELECT oe.seq, RIGHT(oe.text, 400) AS text
                            FROM output_events oe
                           WHERE oe.execution_id = ex.id
                             AND oe.stream = 'stderr'
                           ORDER BY oe.seq DESC
                           LIMIT 5
                      ) s
                  ) END AS stderr_tail
             FROM task_executions ex
            WHERE ex.task_id = t.id
            ORDER BY ex.assigned_at DESC, ex.attempt_index DESC
            LIMIT 1
      ) lx ON TRUE
    """
    + _SQL_TASKS_WHERE
    + """
     GROUP BY t.id, lx.started_at, lx.finished_at, lx.exit_code, lx.stderr_tail
    """
)

# Whitelist of sortable columns. `done` is the SELECT alias (Postgres
# allows ORDER BY alias even with GROUP BY). The `t.id DESC` tiebreaker
# guarantees stable order when several rows share a key.
_SORT_COLUMNS = {"created_at", "status", "done", "target_count"}
_SORT_DIRS = {"asc", "desc"}
_SORT_EXPR = {
    "created_at": "t.created_at",
    "status": "t.status",
    "done": "done",
    "target_count": "t.target_count",
}

# --- Artifact filter ----------------------------------------
# Pure helpers: translate UI controls into SQL binds. They are kept
# as module-level functions (not closures) so they can be tested without a DB.

_HAS_ARTIFACTS_VALUES = {"any", "yes", "no"}

# Short labels for the artifact chips; the real param travels in remove_qs.
_ARTIFACT_CHIP_LABELS = {
    "has_artifacts": "artifacts",
    "artifact_path": "path",
    "artifact_type": "type",
    "artifact_sha256": "sha256",
}


def _glob_to_ilike(glob: str) -> str:
    """Translates a user glob into an ILIKE pattern anchored to the whole value.

    `*` → `%`, `?` → `_`. The literal `%`/`_`/`\\` in the input are escaped so
    they do not act as SQL wildcards. The resulting pattern has NO wildcards
    at the edges, so ILIKE matches the whole `rel_path` (anchored): the
    operator writes `reports/*.json` or `*/x.json` for subfolders.
    """
    out: list[str] = []
    for ch in glob:
        if ch == "*":
            out.append("%")
        elif ch == "?":
            out.append("_")
        elif ch in ("%", "_", "\\"):
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _like_prefix(value: str) -> str:
    """LIKE prefix pattern: escapes wildcards in the input and appends a final `%`."""
    esc = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return esc + "%"


def _artifact_filter_binds(
    has_artifacts: str,
    artifact_path: str,
    artifact_type: str,
    artifact_sha256: str,
) -> dict[str, str]:
    """Computes the artifact filter binds from the query params.

    Returns `artifact_mode` (off | present | absent) and the filter binds.
    - `off`: `has_artifacts=any` with no specific filters → neutral clause.
    - `present`: `has_artifacts=yes`, or `any` with some specific filter →
      EXISTS with the filters applied.
    - `absent`: `has_artifacts=no` → NOT EXISTS; specific filters are
      ignored (they do not apply to absence).
    """
    mode = has_artifacts if has_artifacts in _HAS_ARTIFACTS_VALUES else "any"
    path = (artifact_path or "").strip()
    ctype = (artifact_type or "").strip()
    # sha256 lives in lowercase hex and the SQL filters with LIKE (case-sensitive,
    # the only mode that uses the text_pattern_ops btree): lowercase here
    # so that an uppercase prefix still matches.
    sha = (artifact_sha256 or "").strip().lower()
    specific = bool(path or ctype or sha)

    if mode == "no":
        artifact_mode = "absent"
    elif mode == "yes" or (mode == "any" and specific):
        artifact_mode = "present"
    else:
        artifact_mode = "off"

    present = artifact_mode == "present"
    return {
        "artifact_mode": artifact_mode,
        "artifact_type": ctype if present else "",
        "artifact_path_like": _glob_to_ilike(path) if (present and path) else "",
        "artifact_sha256_prefix": _like_prefix(sha) if (present and sha) else "",
    }


# Counts. The total is O(1) in Postgres with an estimate, but the exact COUNT(*)
# over `tasks` is cheap up to millions of rows; we keep it exact to avoid
# introducing inconsistencies with the listing. The filtered one uses COUNT(DISTINCT
# t.id) because the JOIN with task_executions multiplies rows.
_SQL_COUNT_TASKS_TOTAL = text("SELECT COUNT(*) FROM tasks WHERE workspace_id = :workspace_id")
# Breakdown by status for the subtitle `Tasks · 12 queued · 3 failed · …`.
# A single query with GROUP BY (vs N queries per bucket or vs a Python scan
# over the LIMIT 200 list, which would be incomplete). Returns only buckets with
# count>0, so the template does not have to filter.
_SQL_COUNT_TASKS_BY_STATUS = text(
    "SELECT status::text AS status, COUNT(*) AS n FROM tasks "
    "WHERE workspace_id = :workspace_id GROUP BY status"
)
# Breakdown by (variant, status) for the variant lens filter. A
# single scan feeds EVERYTHING in Python: present variants (selector), the
# "current" set (latest per kind + other) and the status breakdown rescaled to the
# selected variant. Small cardinality (~variants × statuses), one more GROUP BY
# than the status-only one.
_SQL_COUNT_TASKS_BY_VARIANT_STATUS = text(
    "SELECT variant_kind, variant_label, status::text AS status, outcome, COUNT(*) AS n "
    "FROM tasks WHERE workspace_id = :workspace_id "
    "GROUP BY variant_kind, variant_label, status, outcome"
)
_SQL_COUNT_TASKS_FILTERED = text(
    """
    SELECT COUNT(DISTINCT t.id)
      FROM tasks t
      LEFT JOIN task_executions e ON e.task_id = t.id
    """
    + _SQL_TASKS_WHERE
)

# Distinct content-types produced in the workspace — feeds the <datalist>
# of the artifact filter. Only queried when the artifact filter is
# active, so as not to pay the DB round-trip on every poll of the partial.
_SQL_DISTINCT_CONTENT_TYPES = text(
    """
    SELECT DISTINCT aa.content_type
      FROM task_artifacts aa
      JOIN task_executions ae ON ae.id = aa.execution_id
      JOIN tasks t ON t.id = ae.task_id
     WHERE t.workspace_id = :workspace_id
     ORDER BY aa.content_type
     LIMIT 200
    """
)

# Valid universe of task status (must match TASK_STATUS_VALUES in
# models.py and the `task_status` ENUM of migration 0001). Note: `execution_status`
# values (assigned/claimed/running/expired) are not included.
# The `t.status` column filtered here is
# task_status, which has only 6 values. We filter by those.
_TASK_STATUS_VALUES = (
    "queued",
    "dispatching",
    "paused",
    "completed",
    "cancelled",
    "failed",
)

# Valid page_size values for the tasks listing pagination.
# Do not allow an arbitrary LIMIT from the client.
_PAGE_SIZE_VALUES = {25, 50, 100}

# Explicit CAST to text: asyncpg fails with `AmbiguousParameterError` if the
# placeholders can receive NULL without a declared type.
_SQL_LIST_ADMIN_ACTIONS = text(
    """
    SELECT id, ts, action, target_kind, target_id, reason, actor, payload
      FROM admin_actions
     WHERE workspace_id = :workspace_id
       AND (CAST(:action AS text) IS NULL OR action = CAST(:action AS text))
       AND (CAST(:target_kind AS text) IS NULL
            OR target_kind = CAST(:target_kind AS text))
     ORDER BY ts DESC, id DESC
     LIMIT :limit
    """
)

# Universe of valid values for the audit log filters. They must match
# the CHECK constraints of `admin_actions` (current state as of migration 0056; we do not
# use a native enum to ease evolution). Without this, existing actions
# missing from the list render in the table but cannot be filtered.
# `tests/test_migration_0048.py` compares this tuple against the real DB CHECK,
# so a new value in a future migration breaks the test until it is added here.
_ACTION_VALUES = (
    "delete_worker",
    "cancel_task",
    "delete_task",
    "bulk_cancel_task",
    "bulk_delete_task",
    "retry_task",
    "auto_retry_task",
    "fork_task",
    "mint_pat",
    "revoke_pat",
    "delete_pat",
    "mint_enrollment_token",
    "revoke_enrollment_token",
    "delete_enrollment_token",
    "clear_worker_cache",
    "bulk_retry_task",
    "consume_enrollment_token",
    "pause_worker",
    "resume_worker",
    "mint_share_token",
    "revoke_share_token",
    # Migration 0056: task secrets.
    "set_task_secret",
    "delete_task_secret",
    "deliver_task_secrets",
)
_TARGET_KIND_VALUES = ("worker", "task", "pat", "enrollment_token", "share_token", "task_secret")


_SQL_GET_TASK = text(
    """
    SELECT id, command, status, outcome, target_mode, target_count, priority, created_at,
           payload, target_worker_id::text AS target_worker_id,
           target_worker_pool, delivery_mode, claim_timeout_seconds,
           parent_task_id, retry_count, snapshot_epoch, eligibility, last_error,
           -- The current epoch of the snapshot has NO execution.
           -- With `all_available` + `dispatching` that is exactly "this task
           -- is waiting for a suitable worker" (see `_awaiting_eligible_reason`).
           NOT EXISTS (
             SELECT 1 FROM task_executions e
              WHERE e.task_id = tasks.id
                AND e.snapshot_epoch = tasks.snapshot_epoch
           ) AS epoch_has_no_executions
      FROM tasks WHERE id = :id AND workspace_id = :workspace_id
    """
)

# Scoped via the parent task's workspace: it only returns executions if the
# task belongs to the active workspace (EXISTS over tasks). If the task belongs to
# another tenant, it returns 0 rows (and the handler already answered 404 earlier).
_SQL_TASK_EXECUTIONS = text(
    """
    SELECT e.id::text AS id,
           e.attempt_index,
           e.snapshot_epoch,
           e.worker_id::text AS worker_id,
           w.hostname AS worker_hostname,
           e.status, e.outcome, e.started_at, e.finished_at,
           e.exit_code, e.assigned_at,
           EXTRACT(EPOCH FROM (e.finished_at - e.started_at)) AS duration_seconds
      FROM task_executions e
      LEFT JOIN workers w ON w.id = e.worker_id
     WHERE e.task_id = :id
       AND EXISTS (
             SELECT 1 FROM tasks t
              WHERE t.id = e.task_id AND t.workspace_id = :workspace_id
       )
     ORDER BY e.attempt_index DESC, e.started_at DESC NULLS LAST
    """
)

# Forks: direct children of a task are the tasks whose
# parent_task_id points to it (inverse query over the partial index
# tasks_parent_task_id_idx). Only one level (direct children). Scoped by
# workspace_id like the rest of the handler so as not to leak children of another tenant.
_FORK_CHILDREN_LIMIT = 20

_SQL_COUNT_FORK_CHILDREN = text(
    """
    SELECT count(*) FROM tasks
     WHERE parent_task_id = :id AND workspace_id = :workspace_id
    """
)

_SQL_LIST_FORK_CHILDREN = text(
    """
    SELECT id, status, created_at
      FROM tasks
     WHERE parent_task_id = :id AND workspace_id = :workspace_id
     ORDER BY id DESC
     LIMIT :limit
    """
)

_SQL_GET_WORKER = text(
    """
    SELECT id::text AS id, hostname, name, os, arch, ram_mb, ram_used_mb,
           disk_mb, disk_used_mb, version, status, registered_at, last_heartbeat_at,
           deleted_at, cache_flush_requested_at, cache_flush_done_at,
           free_disk_fraction
      FROM workers WHERE id = :id AND workspace_id = :workspace_id
    """
)

_SQL_WORKER_EXECUTIONS = text(
    """
    SELECT e.id::text AS id,
           e.task_id,
           e.attempt_index,
           e.status,
           e.assigned_at,
           e.started_at,
           e.finished_at,
           e.exit_code,
           t.payload AS task_payload,
           EXTRACT(EPOCH FROM (e.finished_at - e.started_at)) AS duration_seconds
      FROM task_executions e
      LEFT JOIN tasks t ON t.id = e.task_id
     WHERE e.worker_id = :id
       AND EXISTS (
             SELECT 1 FROM workers w
              WHERE w.id = e.worker_id AND w.workspace_id = :workspace_id
       )
     ORDER BY e.assigned_at DESC NULLS LAST,
              e.started_at DESC NULLS LAST
     LIMIT 200
    """
)

_SQL_WORKER_THROUGHPUT = text(
    """
    SELECT
        COUNT(*) FILTER (WHERE status IN ('assigned','claimed','running'))
            AS active_count,
        COUNT(*) FILTER (
            WHERE status IN ('completed','running','claimed')
              AND (finished_at > now() - interval '60 seconds'
                   OR (finished_at IS NULL
                       AND started_at > now() - interval '60 seconds'))
        ) AS recent_count
      FROM task_executions
     WHERE worker_id = :id
       AND EXISTS (
             SELECT 1 FROM workers w
              WHERE w.id = task_executions.worker_id
                AND w.workspace_id = :workspace_id
       )
    """
)

_SQL_GET_EXECUTION = text(
    """
    SELECT e.id::text AS id,
           e.status, e.exit_code, e.attempt_index
      FROM task_executions e
      JOIN tasks t ON t.id = e.task_id
     WHERE e.task_id = :task_id AND e.worker_id = :worker_id
       AND t.workspace_id = :workspace_id
     ORDER BY e.attempt_index DESC
     LIMIT 1
    """
)

# Full capture: the output of an execution lives in `output_events`
# (single source of truth). The dashboard polls incrementally by `seq` and
# renders interleaved in reading order (seq == temporal order because the
# worker assigns seq and ts under the same lock). `after` is the last seq already seen.
#
# Pagination: each request brings at most one page of events.
# The first load (`after=0`) returns only the first page,
# so a verbose execution never returns an arbitrarily
# large page. If the page comes back full (there are more), the poller
# re-emits itself with `hx-trigger="load ..."` to catch up
# immediately — also on terminal
# executions.
_OUTPUT_EVENTS_PAGE_SIZE = 500

_SQL_GET_OUTPUT_EVENTS = text(
    """
    SELECT seq, ts, stream, text
      FROM output_events
     WHERE execution_id = CAST(:execution_id AS uuid)
       AND seq > :after
     ORDER BY seq
     LIMIT :limit
    """
)


def _output_event_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """Projects an output_events row into the dict the template consumes.

    For stream='command' (echo '$ <cmd>') the trailing \\n is trimmed: the class
    .out-command is display:block and the break would be left over as an empty line.
    stdout/stderr is preserved intact (flows inline with white-space:pre-wrap).
    """
    text_val = row["text"]
    if row["stream"] == "command":
        text_val = text_val.rstrip("\n")
    return {
        "seq": row["seq"],
        "stream": row["stream"],
        "text": text_val,
        "ts_display": row["ts"].strftime("%H:%M:%S.%f")[:-3] if row["ts"] else "",
    }


# Resolution by stable UUID: a task_executions row has a unique identity
# via `id` (UUID). This allows linking to
# historical attempts without the ambiguity of (task_id, worker_id) when there are
# several attempts by the same worker.
_SQL_GET_EXECUTION_BY_UUID = text(
    """
    SELECT e.id::text AS id,
           e.task_id,
           e.worker_id::text AS worker_id,
           e.status, e.exit_code, e.attempt_index,
           e.started_at, e.finished_at
      FROM task_executions e
      JOIN tasks t ON t.id = e.task_id
     WHERE e.id = CAST(:execution_id AS uuid)
       AND t.workspace_id = :workspace_id
    """
)

# `collected_at`: the row is still listed — it is evidence that
# the task produced the file — but its bytes were purged from disk by the
# operational routine. DOWNLOAD surfaces treat it separately (410/exclusion
# from the zip); the listing's EXISTS filters, the artifacts indicator and the
# counts keep counting it.
_SQL_LIST_ARTIFACTS = text(
    """
    SELECT a.id, a.rel_path, a.size_bytes, a.sha256, a.content_type, a.created_at,
           a.collected_at
      FROM task_artifacts a
     WHERE a.execution_id = CAST(:execution_id AS uuid)
     ORDER BY a.rel_path
    """
)

_SQL_GET_ARTIFACT_BY_ID = text(
    """
    SELECT a.id, a.rel_path, a.size_bytes, a.content_type, a.collected_at
      FROM task_artifacts a
      JOIN task_executions e ON e.id = a.execution_id
      JOIN tasks t ON t.id = e.task_id
     WHERE a.id = :artifact_id
       AND a.execution_id = CAST(:execution_id AS uuid)
       AND t.workspace_id = :workspace_id
    """
)

# Unique items in a workspace = COUNT(DISTINCT basename(rel_path))
# of the artifacts whose rel_path ends in the configured suffix. The
# basename (split_part(..., '/', -1))
# acts as the unique id of the item: the same item reported
# under different paths/executions collapses to 1 (catalog coverage, not
# repeats). The LIKE assumes the suffix contains no %/_ wildcards (valid for
# a typical suffix such as ".report.json").
_SQL_COUNT_INDICATOR_ARTIFACTS = text(
    """
    SELECT COUNT(DISTINCT split_part(aa.rel_path, '/', -1)) AS n
      FROM task_artifacts aa
      JOIN task_executions ae ON ae.id = aa.execution_id
      JOIN tasks t            ON t.id  = ae.task_id
     WHERE t.workspace_id = :workspace_id
       AND aa.rel_path LIKE :suffix_pattern
    """
)


# Terminal states of an execution: when the execution is in one of
# these, the dashboard does NOT keep refreshing the output fragment. The
# non-terminal ones (assigned/claimed/running) trigger HTMX polling.
# Derived from models.py (single source).
_TERMINAL_EXEC_STATUSES: frozenset[str] = frozenset(TERMINAL_EXECUTION_STATUSES)


# --- Helpers ----------------------------------------------------------------


def _aggregates(
    executions: list[dict[str, Any]],
    target_count: int,
    *,
    target_mode: str = "count",
    snapshot_epoch: int = 0,
) -> dict[str, int]:
    """Summary of executions for the task-detail header.

    For `all_available` the counters are scoped to the current epoch of the
    snapshot: the retry re-snapshots and `target_count` is
    re-synced to the new snapshot, so the "done / N" must measure that
    epoch — the full history remains visible in the executions table.
    count-mode is cumulative (same as its closing) and does not filter.
    """
    agg = {
        "done": 0,
        "failed": 0,
        "expired": 0,
        "cancelled": 0,
        "running": 0,
        "assigned": 0,
        # Outcomes: breakdown by name of the attempts of the epoch
        # (a completed execution with an outcome DOES count toward done — it is a success
        # with a nuance — but the nuance is shown separately).
        "outcomes": {},
        "target_count": target_count,
    }
    for e in executions:
        if target_mode == "all_available" and e.get("snapshot_epoch", 0) != snapshot_epoch:
            continue
        s = e["status"]
        if s == "completed":
            agg["done"] += 1
        elif s in ("failed", "expired", "cancelled", "running", "assigned"):
            agg[s] += 1
        elif s == "claimed":
            agg["assigned"] += 1
        # Only terminal executions contribute to the breakdown: a residual outcome
        # on an active execution (inconsistent data) must not inflate the badge.
        oc = e.get("outcome")
        if oc and s in _TERMINAL_EXEC_STATUSES:
            agg["outcomes"][oc] = agg["outcomes"].get(oc, 0) + 1
    return agg


async def _workspace_header(session: AsyncSession, ctx: WorkspaceContext) -> dict[str, Any]:
    """Context for the workspace switcher + user for the header (base.html).

    Returns the id/name/role of the active workspace plus the list of memberships
    (id + name) to populate the dropdown, and the display_name of the logged-in user
    for the account menu (Sign out). It is merged into the context of every
    full-page handler so that both render in all views that
    extend base.html. HTMX partials do not need it.

    It also exposes the server version at runtime for the "About" line of the
    user menu (only the server; each worker's version lives in its
    own component, not as an aggregated fleet summary here).
    """
    memberships = await list_membership_workspaces(session, ctx.user.id)
    active_name = next((name for wid, name, _ in memberships if wid == ctx.workspace_id), "")
    server_version = package_version()
    return {
        "ws_active_id": ctx.workspace_id,
        "ws_active_name": active_name,
        "ws_role": ctx.role,
        "ws_memberships": [{"id": wid, "name": name} for wid, name, _ in memberships],
        "user_display_name": ctx.user.display_name,
        "server_version": server_version,
    }


# --- Endpoints --------------------------------------------------------------


# auth: gated by require_workspace (session + membership); role-based authz (owner/member) TODO
@router.get("/", response_class=HTMLResponse)
async def admin_index(
    request: Request, session: SessionDep, ctx: WorkspaceDep, variant: str = ""
) -> HTMLResponse:
    ctx_data = await _workspace_header(session, ctx)
    # The `variant` of the admin plane does NOT carry `Query(max_length=...)` (unlike
    # the public plane, see `status_ui.VariantParam`). This is deliberate and
    # asymmetric: here the param feeds no cache key (there is no retained memory
    # to bound), the caller is authenticated, and an unknown token
    # degrades to a filter that matches no rows — much more benign for the
    # operator than a 422 for the whole page. The selector round-trip invariant
    # is guaranteed by the ingestion clamp (`clamp_variant`), not a cap
    # per handler.
    # Variant lens selector: the <select name="variant"> lives in the
    # dashboard.html shell (outside #tasks-panel), so the FULL PAGE
    # must render its options and the selection. The panel poll carries the
    # variant via hx-include="#filter-form". Same scan as the partial; the
    # options are a snapshot of page load (the default current is recomputed
    # on every poll, so it follows the latest even without reloading the page).
    variant_rows = (
        await session.execute(
            _SQL_COUNT_TASKS_BY_VARIANT_STATUS, {"workspace_id": ctx.workspace_id}
        )
    ).all()
    present_counts, variant_grid = accumulate_variant_grid(variant_rows)
    vf = resolve_variant_filter(variant, present_counts.keys(), default_mode="current")
    ctx_data["variant_options"] = build_variant_options(present_counts, vf)
    # Parity with the partial (admin_tasks_partial handler line): '' = current.
    ctx_data["selected_variant"] = variant_token(vf) if vf.mode != "current" else ""
    # Dynamic outcome chips: only the outcomes actually
    # present UNDER THE variant LENS — a chip whose outcome only
    # exists in archived versions would filter to 0 rows. They are derived from the same
    # grid of the variant scan (the row already carries outcome), with no extra query;
    # computed once per page load (the partial does NOT recompute them on
    # every 3s poll so as not to repeat the scan).
    outcome_lens_counts: dict[str, int] = {}
    for vk, vl, _st, oc, n in variant_grid:
        if oc is not None and variant_row_matches(vf, vk, vl):
            outcome_lens_counts[oc] = outcome_lens_counts.get(oc, 0) + n
    # Order by descending count, not alphabetical: the DOM order is
    # the priority order of the rail's visual clamp — the JS hides from the
    # end backwards until the block fits in its lines and what is left over
    # goes behind `+N more`. With alphabetical order what got hidden depended on the
    # initial letter; by frequency, what is hidden is the long tail. The
    # alphabetical tiebreak keeps the order stable across loads when two outcomes
    # tie on count. The cap of 20 remains the ceiling of the vocabulary.
    ctx_data["outcome_chips"] = [
        {"name": name, "count": count}
        for name, count in sorted(outcome_lens_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
    ]
    return templates.TemplateResponse(request, "dashboard.html", ctx_data)


# auth: gated by require_workspace (session + membership); any member may switch.
@router.post("/workspace/switch")
async def admin_workspace_switch(
    session: SessionDep,
    ctx: WorkspaceDep,
    workspace_id: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Switches the user's active workspace and reloads the dashboard in its scope.

    Classic form POST (works without JS). `workspace_id` is read with `Form()`
    (FastAPI uses `python-multipart`, a repo dependency). If the
    user is not a member of the requested `workspace_id`, `set_active_workspace`
    returns False and we respond 404 (we do not leak existence). On success
    we commit and redirect 303 to `/admin/`, which `require_workspace` will resolve
    with the new active workspace.
    """
    if not workspace_id:
        raise HTTPException(status_code=404, detail="workspace not found")
    ok = await set_active_workspace(session, user_id=ctx.user.id, workspace_id=workspace_id)
    if not ok:
        raise HTTPException(status_code=404, detail="workspace not found")
    await session.commit()
    return RedirectResponse(url="/admin/", status_code=303)


# --- Workspaces management page ---------------------------------------


def _auth_config() -> AuthConfig:
    """Builds a minimal AuthConfig from `settings` for `create_invitation`.

    It only needs rp_id/rp_name/origin (the dataclass default TTLs are the
    same ones the app uses). Mirrors `orcaster_server/cli/dev_invite.py`.
    """
    return AuthConfig(
        rp_id=settings.webauthn_rp_id,
        rp_name=settings.webauthn_rp_name,
        origin=settings.webauthn_origin,
    )


# Rename and invitations are owner-only (`require_owner`, shared with
# `/api/v1`). Enrollment tokens are self-service: any
# member can issue them and revoke their own; they do not use this guard.
_require_owner = require_owner


def _token_status(tok: Any) -> str:
    """Derived state of an enrollment token for the UI.

    Priority: revoked > expired > exhausted > active. `revoked` is terminal;
    `expired` is computed against `now()`; `exhausted` = uses are used up
    (`use_count >= max_uses`, covers an already consumed single-use); the rest is
    `active` (includes a partially consumed multi-use).
    """
    if tok.revoked_at is not None:
        return "revoked"
    expires = tok.expires_at
    if expires is not None:
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= datetime.now(UTC):
            return "expired"
    if tok.use_count >= tok.max_uses:
        return "exhausted"
    return "active"


# One-shot flash cookie for the secret revealed after a POST (PRG).
# Mint/invite do POST→303→GET so as not to leave the page in POST state (a
# reload or F5 would re-run the mutation and create an extra token/invitation).
# The secret does not fit in the URL (it is secret and must not end up in history/logs),
# so it travels in this ephemeral cookie: HttpOnly, short-lived, and the GET
# consumes and deletes it immediately. Scope limited to /admin/workspaces.
_SECRET_FLASH_COOKIE = "ws_secret_flash"
_SECRET_FLASH_MAX_AGE = 120  # seconds; minimal window for the PRG redirect.
_SECRET_FLASH_PATH = "/admin/workspaces"


def _set_secret_flash(
    resp: Response,
    *,
    invite_url: str | None = None,
    enrollment_token: str | None = None,
) -> None:
    """Stores the revealed secret in the flash cookie so the GET can read it."""
    data = {}
    if invite_url:
        data["invite_url"] = invite_url
    if enrollment_token:
        data["enrollment_token"] = enrollment_token
    if not data:
        return
    resp.set_cookie(
        _SECRET_FLASH_COOKIE,
        urlencode(data),
        max_age=_SECRET_FLASH_MAX_AGE,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path=_SECRET_FLASH_PATH,
    )


async def _workspaces_context(
    session: AsyncSession,
    ctx: WorkspaceContext,
    *,
    invite_url: str | None = None,
    enrollment_token: str | None = None,
    error: str | None = None,
    show_all: bool = False,
) -> dict[str, Any]:
    """Context shared by the GET page and the re-renders after a POST.

    Fetches members and tokens scoped to the active workspace. `invite_url` /
    `enrollment_token` are the one-time secrets shown once
    after creating them. `error` re-renders the page with a message (e.g. empty
    name).

    `show_all` controls the token view filter: by default (False)
    only active tokens are listed; with True all are listed (including
    used/revoked/expired). The toggle lives in the URL (`?show=all`).
    """
    header = await _workspace_header(session, ctx)
    members = await list_members(session, ctx.workspace_id)
    # Owner sees all the workspace tokens; member only their own.
    creator_filter = None if ctx.role == "owner" else ctx.user.id
    token_rows = await list_enrollment_tokens(
        session,
        ctx.workspace_id,
        created_by_user_id=creator_filter,
        active_only=not show_all,
    )
    tokens = [
        {
            "id": str(t.id),
            "label": t.label,
            "status": _token_status(t),
            "expires_at": t.expires_at,
            "used_at": t.used_at,
            "used_by_worker_id": str(t.used_by_worker_id) if t.used_by_worker_id else None,
            "revoked_at": t.revoked_at,
            "use_count": t.use_count,
            "max_uses": t.max_uses,
        }
        for t in token_rows
    ]
    return {
        **header,
        "is_owner": ctx.role == "owner",
        "members": [
            {"display_name": name, "role": role, "created_at": created_at}
            for (name, role, created_at) in members
        ],
        "tokens": tokens,
        "invite_url": invite_url,
        "enrollment_token": enrollment_token,
        "server_base_url": settings.webauthn_origin,
        "error": error,
        "show_all": show_all,
    }


# auth: gated by require_workspace; owner controls rendered only when role == owner.
@router.get("/workspaces", response_class=HTMLResponse)
async def admin_workspaces_page(
    request: Request, session: SessionDep, ctx: WorkspaceDep
) -> Response:
    # The newly created secret (if any) arrives via the PRG flash cookie;
    # it is shown once in the modal and the cookie is deleted in this same response.
    raw_flash = request.cookies.get(_SECRET_FLASH_COOKIE)
    invite_url = enrollment_token = None
    if raw_flash:
        parsed = parse_qs(raw_flash)
        invite_url = parsed.get("invite_url", [None])[0]
        enrollment_token = parsed.get("enrollment_token", [None])[0]
    show_all = request.query_params.get("show") == "all"
    ctx_data = await _workspaces_context(
        session,
        ctx,
        invite_url=invite_url,
        enrollment_token=enrollment_token,
        show_all=show_all,
    )
    response = templates.TemplateResponse(request, "workspaces.html", ctx_data)
    if raw_flash:
        response.delete_cookie(_SECRET_FLASH_COOKIE, path=_SECRET_FLASH_PATH)
    return response


# auth: gated by require_workspace; the server pubkey is not secret and
# ANY worker (of any member) needs it to verify signatures, so
# it is served to every authenticated member. It is the same key for the whole
# instance, there is no per-workspace isolation to preserve.
@router.get("/workspaces/pubkey")
async def admin_workspace_pubkey(ctx: WorkspaceDep) -> Response:
    try:
        pem = signing.get_public_key_pem()
    except RuntimeError as exc:
        # Same criterion as /admin/tasks: key not configured → 503,
        # with Retry-After so clients/monitoring know to retry.
        raise HTTPException(
            status_code=503,
            detail="server signing key not configured",
            headers={"Retry-After": "60"},
        ) from exc
    return Response(
        content=pem,
        media_type="application/x-pem-file",
        headers={"Content-Disposition": 'attachment; filename="server-ed25519.pub"'},
    )


# auth: owner-only (members get 403).
@router.post("/workspaces/rename")
async def admin_workspace_rename(
    request: Request,
    session: SessionDep,
    ctx: WorkspaceDep,
    name: Annotated[str, Form()] = "",
):
    _require_owner(ctx)
    clean = name.strip()
    if not clean:
        # Re-render with error and 400 (a no-JS form gets the page back).
        ctx_data = await _workspaces_context(session, ctx, error="Workspace name cannot be empty.")
        return templates.TemplateResponse(request, "workspaces.html", ctx_data, status_code=400)
    await rename_workspace(session, workspace_id=ctx.workspace_id, name=clean)
    await session.commit()
    return RedirectResponse(url="/admin/workspaces", status_code=303)


# auth: owner-only (members get 403).
@router.post("/workspaces/invite")
async def admin_workspace_invite(
    session: SessionDep,
    ctx: WorkspaceDep,
    role: Annotated[str, Form()] = "",
) -> RedirectResponse:
    _require_owner(ctx)
    if role not in ("owner", "member"):
        raise HTTPException(status_code=400, detail="role must be 'owner' or 'member'")
    # The label is human-readable text (workspace name); the acceptance
    # page still derives workspace and role from the payload.
    ws_name = await get_workspace_name(session, ctx.workspace_id)
    token = await create_invitation(
        session,
        _auth_config(),
        payload={"workspace_id": ctx.workspace_id, "role": role},
        label=ws_name,
        created_by_user_id=ctx.user.id,
    )
    await session.commit()
    invite_url = f"{settings.webauthn_origin}/auth/accept/{token}"
    # PRG: the secret travels via the flash cookie and is revealed after the redirect (GET).
    # This way the page is not left in POST state (a reload/F5 does not recreate the invitation).
    resp = RedirectResponse(url="/admin/workspaces", status_code=303)
    _set_secret_flash(resp, invite_url=invite_url)
    return resp


# TTLs selectable at mint (bounded set; the real maximum, 30d, is well
# below the 90d security ceiling, so no additional cap is required).
_ENROLLMENT_TTL_CHOICES = {"1h": 3600, "1d": 86400, "7d": 604800, "30d": 2592000}
_ENROLLMENT_MAX_USES_CAP = 1000


# auth: gated by require_workspace; any member can mint (owner and member).
@router.post("/workspaces/enrollment-tokens")
async def admin_workspace_mint_token(
    request: Request,
    session: SessionDep,
    ctx: WorkspaceDep,
    label: Annotated[str | None, Form()] = None,
    max_uses: Annotated[str, Form()] = "1",
    ttl: Annotated[str, Form()] = "7d",
) -> Response:
    clean_label = label.strip() if label and label.strip() else None
    ttl_seconds = _ENROLLMENT_TTL_CHOICES.get(ttl)
    # `max_uses` is received as str so that empty / non-numeric falls into the
    # same 400 re-render as out-of-range (instead of a 422 coercion error).
    try:
        max_uses_n = int(max_uses)
    except (TypeError, ValueError):
        max_uses_n = 0
    if ttl_seconds is None or not (1 <= max_uses_n <= _ENROLLMENT_MAX_USES_CAP):
        # Same pattern as rename: re-render with error and 400 (no-JS fallback).
        ctx_data = await _workspaces_context(session, ctx, error="Invalid token settings.")
        return templates.TemplateResponse(request, "workspaces.html", ctx_data, status_code=400)
    token, token_id = await mint_enrollment_token_returning_id(
        session,
        workspace_id=ctx.workspace_id,
        created_by_user_id=ctx.user.id,
        label=clean_label,
        max_uses=max_uses_n,
        ttl_seconds=ttl_seconds,
    )
    # Every credential issuance is audited, including self-service enrollment.
    await record_admin_action(
        session,
        workspace_id=ctx.workspace_id,
        action="mint_enrollment_token",
        target_kind="enrollment_token",
        target_id=token_id,
        actor=ctx.user.id,
    )
    await session.commit()
    # PRG: the one-time secret travels via the flash cookie and is revealed after the
    # redirect (GET). Without this, the page was left in POST state and a programmatic
    # reload (revoke) or an F5 re-ran the mint, creating extra tokens.
    resp = RedirectResponse(url="/admin/workspaces", status_code=303)
    _set_secret_flash(resp, enrollment_token=token)
    return resp


# auth: gated by require_workspace; owner revokes any token of the workspace;
# member can only revoke their own. Token of another workspace / foreign creator
# (member) → 404.
@router.post("/workspaces/enrollment-tokens/{token_id}/revoke")
async def admin_workspace_revoke_token(
    request: Request,
    session: SessionDep,
    ctx: WorkspaceDep,
    token_id: str,
):
    # Malformed UUID → 404 (not a 500 from a pg parse error). We normalize to the
    # canonical form: `UUID()` accepts variants (`urn:uuid:`, braces `{...}`) that the
    # Postgres CAST rejects with DataError → 500 if we passed the raw string.
    try:
        token_id = str(UUID(token_id))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=404, detail="token not found") from exc
    # Owner can revoke any token of the workspace; member only their own.
    creator_filter = None if ctx.role == "owner" else ctx.user.id
    ok = await revoke_enrollment_token_by_id(
        session,
        token_id=token_id,
        workspace_id=ctx.workspace_id,
        created_by_user_id=creator_filter,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="token not found")
    # Credential revocation is audited, including self-service enrollment.
    await record_admin_action(
        session,
        workspace_id=ctx.workspace_id,
        action="revoke_enrollment_token",
        target_kind="enrollment_token",
        target_id=token_id,
        actor=ctx.user.id,
    )
    await session.commit()
    # Revocable (active) tokens appear in the default view, but the
    # operator may be on `?show=all`; preserving the filter keeps the
    # no-JS fallback (native form POST → 303) from kicking them out of the filter mid-
    # management. The JS path already preserves the URL via
    # data-confirm-after="reload".
    redirect_url = "/admin/workspaces"
    if request.query_params.get("show") == "all":
        redirect_url += "?show=all"
    return RedirectResponse(url=redirect_url, status_code=303)


# auth: gated by require_workspace; same model as revoke (owner deletes
# any of the workspace; member only their own). Only deletes dead tokens
# (revoked/expired); active/used → 404 (enforced in SQL, see
# `delete_enrollment_token_by_id`). Token of another workspace / foreign creator → 404.
@router.post("/workspaces/enrollment-tokens/{token_id}/delete")
async def admin_workspace_delete_token(
    request: Request,
    session: SessionDep,
    ctx: WorkspaceDep,
    token_id: str,
):
    # Malformed UUID → 404 (not a 500 from a pg parse error). We normalize to the
    # canonical form: `UUID()` accepts variants (`urn:uuid:`, braces `{...}`) that the
    # Postgres CAST rejects with DataError → 500 if we passed the raw string.
    try:
        token_id = str(UUID(token_id))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=404, detail="token not found") from exc
    creator_filter = None if ctx.role == "owner" else ctx.user.id
    ok = await delete_enrollment_token_by_id(
        session,
        token_id=token_id,
        workspace_id=ctx.workspace_id,
        created_by_user_id=creator_filter,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="token not found")
    # Credential deletion is audited, including self-service enrollment.
    await record_admin_action(
        session,
        workspace_id=ctx.workspace_id,
        action="delete_enrollment_token",
        target_kind="enrollment_token",
        target_id=token_id,
        actor=ctx.user.id,
    )
    await session.commit()
    # Deletable (terminal) tokens only show under `?show=all`; preserving
    # the filter keeps the no-JS fallback (native form POST → 303) from kicking the
    # operator to the Active view mid-cleanup. The JS path
    # already preserves the URL via data-confirm-after="reload".
    redirect_url = "/admin/workspaces"
    if request.query_params.get("show") == "all":
        redirect_url += "?show=all"
    return RedirectResponse(url=redirect_url, status_code=303)


# auth: gated by require_workspace; owner sees the uses of any token of the
# workspace; member only those of their own. Token of another workspace / foreign
# creator / unknown id → 404.
@router.get("/workspaces/enrollment-tokens/{token_id}/uses", response_class=HTMLResponse)
async def admin_workspace_token_uses(
    request: Request,
    session: SessionDep,
    ctx: WorkspaceDep,
    token_id: str,
) -> Response:
    try:
        token_id = str(UUID(token_id))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=404, detail="token not found") from exc
    creator_filter = None if ctx.role == "owner" else ctx.user.id
    uses = await list_token_uses(
        session,
        token_id=token_id,
        workspace_id=ctx.workspace_id,
        created_by_user_id=creator_filter,
    )
    if uses is None:
        raise HTTPException(status_code=404, detail="token not found")
    ctx_data = {
        "uses": [
            {
                "worker_id": str(u.worker_id) if u.worker_id else None,
                "used_at": u.used_at,
            }
            for u in uses
        ]
    }
    return templates.TemplateResponse(request, "workspaces_token_uses.html", ctx_data)


# --- Personal access tokens -------------------------------------
#
# They mirror the self-service pattern of enrollment tokens: any
# member issues/lists/revokes/deletes their own; the owner sees and operates the whole
# workspace. The page lives at /admin/pats (PATs belong to the user, not the
# workspace), with its own path-scoped flash cookie. Every mint/revoke/delete is
# audited in admin_actions.

# Dedicated flash cookie to reveal the PAT secret once after the PRG.
# Path scoped to /admin/pats: a cookie with path /admin/workspaces would not be sent
# on the GET of /admin/pats, so the secret needs its own cookie+path pair.
_PAT_SECRET_FLASH_COOKIE = "pat_secret_flash"
_PAT_SECRET_FLASH_PATH = "/admin/pats"

_SHARE_SECRET_FLASH_COOKIE = "share_secret_flash"
_SHARE_SECRET_FLASH_PATH = "/admin/share-links"

# Expiration dropdown options (GitHub style). "" = no expiration.
_PAT_EXPIRY_DAYS = {"30", "60", "90"}


def _pat_status(pat: Any) -> str:
    """Derived state of a PAT for the UI: revoked > expired > active.

    `revoked` is terminal; `expired` is computed against `now()`. Unlike
    enrollment tokens, a PAT has no `used` state (it is not single-use).
    """
    if pat.revoked_at is not None:
        return "revoked"
    expires = pat.expires_at
    if expires is not None:
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= datetime.now(UTC):
            return "expired"
    return "active"


def _set_pat_secret_flash(resp: Response, *, pat_token: str) -> None:
    """Stores the newly issued token in the flash cookie so the GET can read it."""
    resp.set_cookie(
        _PAT_SECRET_FLASH_COOKIE,
        urlencode({"pat_token": pat_token}),
        max_age=_SECRET_FLASH_MAX_AGE,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path=_PAT_SECRET_FLASH_PATH,
    )


async def _pats_context(
    session: AsyncSession,
    ctx: WorkspaceContext,
    *,
    pat_token: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Context of the PATs page (header + role-scoped listing).

    Owner sees all the workspace's PATs; member only their own.
    `pat_token` is the one-time secret shown once after the mint.
    """
    header = await _workspace_header(session, ctx)
    # Owner sees all the workspace's PATs; member only their own.
    user_filter = None if ctx.role == "owner" else ctx.user.id
    pat_rows = await list_pats(session, ctx.workspace_id, user_id=user_filter)
    pats = [
        {
            "id": str(p.id),
            "label": p.label,
            "status": _pat_status(p),
            "expires_at": p.expires_at,
            "last_used_at": p.last_used_at,
        }
        for p in pat_rows
    ]
    return {
        **header,
        "is_owner": ctx.role == "owner",
        "pats": pats,
        "pat_token": pat_token,
        "server_base_url": settings.webauthn_origin,
        "error": error,
    }


# auth: gated by require_workspace; any member sees their PATs (owner sees all).
@router.get("/pats", response_class=HTMLResponse)
async def admin_pats_page(request: Request, session: SessionDep, ctx: WorkspaceDep) -> Response:
    # The newly issued secret (if any) arrives via the PRG flash cookie;
    # it is shown once and the cookie is deleted in this same response.
    raw_flash = request.cookies.get(_PAT_SECRET_FLASH_COOKIE)
    pat_token = None
    if raw_flash:
        pat_token = parse_qs(raw_flash).get("pat_token", [None])[0]
    ctx_data = await _pats_context(session, ctx, pat_token=pat_token)
    response = templates.TemplateResponse(request, "pats.html", ctx_data)
    if raw_flash:
        response.delete_cookie(_PAT_SECRET_FLASH_COOKIE, path=_PAT_SECRET_FLASH_PATH)
    return response


# auth: gated by require_workspace; any member can issue their own PAT.
@router.post("/pats")
async def admin_pat_mint(
    session: SessionDep,
    ctx: WorkspaceDep,
    label: Annotated[str | None, Form()] = None,
    expires_in_days: Annotated[str, Form()] = "",
) -> RedirectResponse:
    clean_label = label.strip() if label and label.strip() else None
    expires_at: datetime | None = None
    days = (expires_in_days or "").strip()
    if days:
        if days not in _PAT_EXPIRY_DAYS:
            raise HTTPException(status_code=400, detail="invalid expiration")
        expires_at = datetime.now(UTC) + timedelta(days=int(days))
    token, pat_id = await mint_pat_returning_id(
        session,
        user_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        label=clean_label,
        expires_at=expires_at,
    )
    # Every credential issuance is audited.
    await record_admin_action(
        session,
        workspace_id=ctx.workspace_id,
        action="mint_pat",
        target_kind="pat",
        target_id=pat_id,
        actor=ctx.user.id,
    )
    await session.commit()
    # PRG: the one-time secret travels via the flash cookie and is revealed after the
    # redirect (GET), not in POST state (an F5 would not issue another PAT).
    resp = RedirectResponse(url="/admin/pats", status_code=303)
    _set_pat_secret_flash(resp, pat_token=token)
    return resp


# auth: gated by require_workspace; owner revokes any PAT of the workspace;
# member only their own. PAT of another workspace / foreign owner (member) → 404.
@router.post("/pats/{token_id}/revoke")
async def admin_pat_revoke(
    session: SessionDep,
    ctx: WorkspaceDep,
    token_id: str,
):
    # Malformed UUID → 404 (not a 500 from a pg parse error). We normalize to the
    # canonical form: `UUID()` accepts variants (`urn:uuid:`, braces `{...}`) that the
    # Postgres CAST rejects with DataError → 500 if we passed the raw string.
    try:
        token_id = str(UUID(token_id))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=404, detail="token not found") from exc
    user_filter = None if ctx.role == "owner" else ctx.user.id
    ok = await revoke_pat_by_id(
        session,
        token_id=token_id,
        workspace_id=ctx.workspace_id,
        user_id=user_filter,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="token not found")
    await record_admin_action(
        session,
        workspace_id=ctx.workspace_id,
        action="revoke_pat",
        target_kind="pat",
        target_id=token_id,
        actor=ctx.user.id,
    )
    await session.commit()
    return RedirectResponse(url="/admin/pats", status_code=303)


# auth: same model as revoke. Only deletes dead PATs (revoked/expired);
# live ones → 404 (enforced in SQL, see `delete_pat_by_id`). Cross-tenant / foreign → 404.
@router.post("/pats/{token_id}/delete")
async def admin_pat_delete(
    session: SessionDep,
    ctx: WorkspaceDep,
    token_id: str,
):
    # Malformed UUID → 404 (not a 500 from a pg parse error). We normalize to the
    # canonical form: `UUID()` accepts variants (`urn:uuid:`, braces `{...}`) that the
    # Postgres CAST rejects with DataError → 500 if we passed the raw string.
    try:
        token_id = str(UUID(token_id))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=404, detail="token not found") from exc
    user_filter = None if ctx.role == "owner" else ctx.user.id
    ok = await delete_pat_by_id(
        session,
        token_id=token_id,
        workspace_id=ctx.workspace_id,
        user_id=user_filter,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="token not found")
    await record_admin_action(
        session,
        workspace_id=ctx.workspace_id,
        action="delete_pat",
        target_kind="pat",
        target_id=token_id,
        actor=ctx.user.id,
    )
    await session.commit()
    return RedirectResponse(url="/admin/pats", status_code=303)


# auth: gated by require_workspace; owner-only (members get 403).
@router.get("/share-links", response_class=HTMLResponse)
async def admin_share_links_page(
    request: Request, session: SessionDep, ctx: WorkspaceDep
) -> Response:
    _require_owner(ctx)
    raw_flash = request.cookies.get(_SHARE_SECRET_FLASH_COOKIE)
    share_token = None
    if raw_flash:
        share_token = parse_qs(raw_flash).get("share_token", [None])[0]
    # With a keyring, the URL of each active link is decrypted to display it;
    # without a keyring it is minted as before (URL shown only once) and the page says so.
    # A configured but broken keys file raises `KeyringError` (explicit
    # failure; startup already validates it). It re-reads the file if it changed
    # on disk, so as not to show "cannot be decrypted" for a key this process did not
    # get to load.
    keyring = task_secrets_keyring.get_keyring_fresh()
    tokens = await list_share_tokens(session, workspace_id=ctx.workspace_id, keyring=keyring)
    response = templates.TemplateResponse(
        request,
        "share_links.html",
        {
            "tokens": tokens,
            "share_token": share_token,
            "urls_recoverable": keyring is not None,
            "base_url": settings.public_base_url.rstrip("/"),
        },
    )
    if raw_flash:
        response.delete_cookie(_SHARE_SECRET_FLASH_COOKIE, path=_SHARE_SECRET_FLASH_PATH)
    return response


# auth: gated by require_workspace; owner-only (members get 403). The origin is
# validated by `require_session`.
@router.post("/share-links")
async def admin_share_link_mint(
    request: Request,
    session: SessionDep,
    ctx: WorkspaceDep,
    label: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    _require_owner(ctx)
    clean_label = label.strip() if label and label.strip() else None
    token, token_id = await create_share_token(
        session,
        workspace_id=ctx.workspace_id,
        label=clean_label,
        # Re-reads the file if it changed on disk; the URL is encrypted with the
        # current primary key, not the one this process loaded at startup.
        keyring=task_secrets_keyring.get_keyring_fresh(),
    )
    # Minting a share-link grants public read access to the
    # fleet summary, so it is audited like any other credential.
    # The id is returned by `create_share_token` (RETURNING id::text). `actor` comes
    # from the session, as in the six credential calls of this
    # same file.
    await record_admin_action(
        session,
        workspace_id=ctx.workspace_id,
        action="mint_share_token",
        target_kind="share_token",
        target_id=token_id,
        actor=ctx.user.id,
        payload={"label": clean_label},
    )
    await session.commit()
    # PRG: the secret travels via the flash cookie and is revealed after the redirect (GET),
    # not in POST state (an F5 would not issue another token). Without a keyring it is the
    # only time it is shown.
    resp = RedirectResponse(url="/admin/share-links", status_code=303)
    resp.set_cookie(
        _SHARE_SECRET_FLASH_COOKIE,
        f"share_token={token}",
        max_age=_SECRET_FLASH_MAX_AGE,
        path=_SHARE_SECRET_FLASH_PATH,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )
    return resp


# auth: gated by require_workspace; owner-only (members get 403). The origin is
# validated by `require_session`.
@router.post("/share-links/{token_id}/revoke")
async def admin_share_link_revoke(
    request: Request,
    session: SessionDep,
    ctx: WorkspaceDep,
    token_id: str,
) -> RedirectResponse:
    _require_owner(ctx)
    # Malformed UUID → 404 (not a 500 from a pg parse error). Same criterion as PATs.
    try:
        token_id = str(UUID(token_id))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=404, detail="token not found") from exc
    ok = await revoke_share_token(session, token_id=token_id, workspace_id=ctx.workspace_id)
    if not ok:
        raise HTTPException(status_code=404, detail="token not found")
    # After the `ok` guard: a revocation that answers 404 is not audited.
    await record_admin_action(
        session,
        workspace_id=ctx.workspace_id,
        action="revoke_share_token",
        target_kind="share_token",
        target_id=token_id,
        actor=ctx.user.id,
    )
    await session.commit()
    return RedirectResponse(url="/admin/share-links", status_code=303)


# --- Task secrets -----------------------------------
# The page lists the metadata (name, fingerprint, dates, author) to any
# member; only the owner sets or deletes. No response carries the value or the
# ciphertext: neither does the flash cookie (only the name), the redirect `Location`
# nor the error re-render (the value field comes back empty).

_TASK_SECRETS_FLASH_COOKIE = "task_secrets_flash"
_TASK_SECRETS_FLASH_PATH = "/admin/secrets"
_TASK_SECRETS_FLASH_KINDS = {"saved": "Secret {name} saved", "deleted": "Secret {name} deleted"}


def _task_secrets_keyring_or_503() -> task_secrets_keyring.TaskSecretsKeyring:
    """Secrets keyring, or 503 if `TASK_SECRETS_KEYS_PATH` is not defined."""
    try:
        return task_secrets.require_keyring()
    except task_secrets.TaskSecretsUnconfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _set_task_secrets_flash(
    resp: Response, *, kind: str, name: str, worker_name: str | None = None
) -> None:
    """Flash cookie with the PRG success notice. It carries the secret name and,
    with worker scope, the worker's display name; never the value."""
    data = {kind: name}
    if worker_name:
        data["worker"] = worker_name
    resp.set_cookie(
        _TASK_SECRETS_FLASH_COOKIE,
        urlencode(data),
        max_age=_SECRET_FLASH_MAX_AGE,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path=_TASK_SECRETS_FLASH_PATH,
    )


def _task_secrets_flash_message(raw: str) -> str | None:
    parsed = parse_qs(raw)
    worker = parsed.get("worker", [None])[0]
    for kind, template in _TASK_SECRETS_FLASH_KINDS.items():
        name = parsed.get(kind, [None])[0]
        if name:
            suffix = f" for worker {worker}" if worker else ""
            return template.format(name=name) + suffix + "."
    return None


async def _count_queued_stored_secret_tasks(session: AsyncSession, workspace_id: str) -> int:
    """`queued` tasks of the workspace that declare a name with a stored secret.

    Same predicate as the poll gate (`task_uses_stored_secrets_sql`). Only
    queried when the keys file is not configured: it is the data for the
    banner that warns about the waiting tasks.
    """
    row = await session.execute(
        text(
            "SELECT count(*) FROM tasks t "
            "WHERE t.workspace_id = :ws AND t.status = 'queued' "
            f"AND {task_uses_stored_secrets_sql('t')}"
        ),
        {"ws": workspace_id},
    )
    return int(row.scalar_one())


async def _task_secrets_context(
    session: AsyncSession,
    ctx: WorkspaceContext,
    *,
    flash: str | None = None,
    form_name: str = "",
    form_worker: str = "",
    name_error: str | None = None,
    value_error: str | None = None,
    worker_error: str | None = None,
) -> dict[str, Any]:
    """Context of `secrets.html`. `form_name` and `form_worker` repopulate the
    name and the chosen worker after an error; the value never comes back."""
    header = await _workspace_header(session, ctx)
    secrets = await task_secrets.list_secrets(session, ctx.workspace_id)
    workers = await task_secrets.list_scope_workers(session, ctx.workspace_id)
    configured = task_secrets_keyring.is_configured()
    queued_secret_tasks = 0
    if not configured and secrets:
        queued_secret_tasks = await _count_queued_stored_secret_tasks(session, ctx.workspace_id)
    # Names the poll could not decrypt in the last 15 minutes. Only
    # `undecryptable`: `keys_missing` is already covered by the "not configured" notice.
    # They are names, never values; any member who sees the page sees them.
    undecryptable_names = sorted(
        {
            name
            for refusal in task_secrets_blocked.recent_refusals(str(ctx.workspace_id))
            if refusal.kind == "undecryptable"
            for name in refusal.names
        }
    )
    # Two workers with the same display name are told apart by the id prefix.
    counts: dict[str, int] = {}
    for w in workers:
        counts[w.name] = counts.get(w.name, 0) + 1
    worker_options = [
        {"id": w.id, "label": w.name if counts[w.name] == 1 else f"{w.name} ({w.id[:8]})"}
        for w in workers
    ]
    return {
        **header,
        "is_owner": ctx.role == "owner",
        "configured": configured,
        "queued_secret_tasks": queued_secret_tasks,
        "undecryptable_names": undecryptable_names,
        "secrets": secrets,
        "worker_options": worker_options,
        "flash": flash,
        "form_name": form_name,
        "form_worker": form_worker,
        "name_error": name_error,
        "value_error": value_error,
        "worker_error": worker_error,
    }


# auth: gated by require_workspace; any member sees the metadata, only the
# owner sees the forms.
@router.get("/secrets", response_class=HTMLResponse)
async def admin_secrets_page(request: Request, session: SessionDep, ctx: WorkspaceDep) -> Response:
    raw_flash = request.cookies.get(_TASK_SECRETS_FLASH_COOKIE)
    # The Delete with `data-confirm-url` does a fetch and the fetch follows the 303 with a
    # non-navigational GET before the reload. That GET does not consume the flash, so that
    # the reload shows it. Without `Sec-Fetch-Mode` (old browsers) it is consumed.
    fetch_mode = request.headers.get("sec-fetch-mode")
    consume = bool(raw_flash) and fetch_mode in (None, "navigate")
    flash = _task_secrets_flash_message(raw_flash) if consume and raw_flash else None
    ctx_data = await _task_secrets_context(session, ctx, flash=flash)
    response = templates.TemplateResponse(request, "secrets.html", ctx_data)
    if consume:
        response.delete_cookie(_TASK_SECRETS_FLASH_COOKIE, path=_TASK_SECRETS_FLASH_PATH)
    return response


_PADDED_WORKER_ERROR = (
    "worker must not have leading or trailing whitespace; "
    "pass the worker id for a worker whose name has it"
)


def normalize_secret_form_value(value: str) -> str:
    """Converts the textarea's CRLF to LF and strips a single trailing newline.

    A `<input type=password>` discards line breaks, so
    a token copied with its trailing newline would be stored clean; with the textarea
    it would be stored with it. Same as `orcaster secret set` from a pipe: exactly
    one trailing newline.
    """
    value = value.replace("\r\n", "\n")
    return value.removesuffix("\n")


# auth: gated by require_workspace; owner-only (members get 403). The origin is
# validated by `require_session`.
@router.post("/secrets")
async def admin_secret_set(
    request: Request,
    session: SessionDep,
    ctx: WorkspaceDep,
    name: Annotated[str, Form()] = "",
    value: Annotated[str, Form()] = "",
    worker: Annotated[str, Form()] = "",
) -> Response:
    _require_owner(ctx)
    ring = _task_secrets_keyring_or_503()
    clean_name = name.strip()
    # `worker` is not trimmed: worker names are not trimmed
    # on registration, and trimming could resolve to another worker.
    clean_worker = worker
    # The value comes from a <textarea>, and the browser sends its line
    # breaks as CRLF (HTML Standard, form submission). Whoever pastes a PEM or a
    # JSON wrote LF; some key parsers reject the CR.
    value = normalize_secret_form_value(value)
    name_error = value_error = worker_error = None
    target = None
    try:
        validate_secret_name(clean_name)
    except ValueError as exc:
        name_error = str(exc)
    try:
        task_secrets.validate_secret_value(value)
    except ValueError as exc:
        # The messages of `validate_secret_value` do not quote the value.
        value_error = str(exc)
    if clean_worker and clean_worker != clean_worker.strip():
        worker_error = _PADDED_WORKER_ERROR
    elif clean_worker:
        # "" is the secret of the whole workspace; otherwise, the chosen worker.
        try:
            target = await task_secrets.resolve_worker(
                session, workspace_id=ctx.workspace_id, ref=clean_worker
            )
        except (
            task_secrets.WorkerNotFound,
            task_secrets.WorkerDeleted,
            task_secrets.WorkerAmbiguous,
        ) as exc:
            worker_error = str(exc)
    if name_error or value_error or worker_error:
        # Re-render with 400 and an empty value field: the value does not come back to the HTML.
        # A rejected name does not come back either: it may be the pasted token instead
        # of the name or a `NAME=value`. If the name is valid, it is kept.
        ctx_data = await _task_secrets_context(
            session,
            ctx,
            form_name="" if name_error else clean_name,
            form_worker=clean_worker,
            name_error=name_error,
            value_error=value_error,
            worker_error=worker_error,
        )
        return templates.TemplateResponse(request, "secrets.html", ctx_data, status_code=400)
    try:
        await task_secrets.set_secret(
            session,
            ring,
            workspace_id=ctx.workspace_id,
            name=clean_name,
            value=value,
            actor_user_id=ctx.user.id,
            worker_id=target.id if target else None,
        )
    except (task_secrets.WorkerNotFound, task_secrets.WorkerDeleted) as exc:
        # `set_secret` resolves the worker again: if it was deleted between the two
        # lookups, the same inline error as the first, not a 500. It fails
        # before writing, so there is nothing to undo.
        ctx_data = await _task_secrets_context(
            session,
            ctx,
            form_name=clean_name,
            form_worker=clean_worker,
            worker_error=str(exc),
        )
        return templates.TemplateResponse(request, "secrets.html", ctx_data, status_code=400)
    await session.commit()
    resp = RedirectResponse(url="/admin/secrets", status_code=303)
    _set_task_secrets_flash(
        resp, kind="saved", name=clean_name, worker_name=target.name if target else None
    )
    return resp


# auth: gated by require_workspace; owner-only (members get 403). The origin is
# validated by `require_session`.
@router.post("/secrets/{name}/delete")
async def admin_secret_delete(
    request: Request,
    session: SessionDep,
    ctx: WorkspaceDep,
    name: str,
    worker: str | None = None,
) -> RedirectResponse:
    """Deletes a scope: without `?worker=` the workspace one; with it, that
    worker's, even if it is deleted, so it can be cleaned up."""
    _require_owner(ctx)
    target = None
    if worker is not None:
        # An empty `?worker=`, whitespace-only or with surrounding spaces is neither
        # "no worker" nor trimmed: 422, like the API.
        if worker.strip() == "":
            raise HTTPException(status_code=422, detail="worker must be a worker id or name")
        if worker != worker.strip():
            raise HTTPException(status_code=422, detail=_PADDED_WORKER_ERROR)
        try:
            target = await task_secrets.resolve_worker(
                session, workspace_id=ctx.workspace_id, ref=worker, allow_deleted=True
            )
        except task_secrets.WorkerNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except task_secrets.WorkerAmbiguous as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
    deleted = await task_secrets.delete_secret(
        session,
        workspace_id=ctx.workspace_id,
        name=name,
        actor_user_id=ctx.user.id,
        worker_id=target.id if target else None,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="secret not found")
    await session.commit()
    resp = RedirectResponse(url="/admin/secrets", status_code=303)
    _set_task_secrets_flash(
        resp, kind="deleted", name=name, worker_name=target.name if target else None
    )
    return resp


def _is_htmx(request: Request) -> bool:
    """Minimal heuristic to tell requests from the htmx dashboard frontend
    from direct navigations (bookmark / share-link). The /admin/tasks and
    /admin/workers routes answer with partials for htmx and redirect to the dashboard
    when someone types or shares them.
    """
    return request.headers.get("HX-Request", "").lower() == "true"


def _normalize_filter(value: str | None, allowed: tuple[str, ...]) -> str | None:
    """Returns `value` if it is in `allowed`; treats an empty string as None
    (<select> elements send "" when there is no selection)."""
    if value is None or value == "":
        return None
    return value if value in allowed else None


def _heartbeat_age_seconds(hb: datetime | None, now: datetime) -> int | None:
    """Age in seconds of a worker's last heartbeat.

    Single source of the computation for its two consumers: the worker detail,
    which uses it for the three-band liveness dot (ok under 30 s, warn between
    30 and 120 s, danger above), and the deck, which uses it for the
    `paused (stale)` affix of the badge.

    Returns `None` if the worker never sent a heartbeat. Normalizes a naive timestamp to
    UTC, because a read without tzinfo would make the subtraction fail. The `max(0, ...)`
    bounds a heartbeat dated in the future (worker clock ahead, or DB clock
    drift relative to the process): the age is never negative.

    `now` comes in as a parameter and is not read in here: the deck needs to
    compare the N cards against the same instant, and this way the helper is
    tested without a clock or a DB.
    """
    if hb is None:
        return None
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=UTC)
    return max(0, int((now - hb).total_seconds()))


class _Today:
    """Sentinel of the "today" preset: `since` = UTC midnight of the current day.

    It cannot be expressed as a fixed `timedelta` (it depends on the time of day), and it
    must be told apart from `None` (which means `all`, no lower bound).
    """


_TODAY = _Today()

# Single source of the time window presets: (value, label, delta).
# `delta=None` means `all` (no lower bound); `delta=_TODAY` means the
# UTC midnight of the current day. From here are derived the normalization
# whitelist (`_RANGE_DELTAS`) and the (value, label) list the UI renders
# (`_RANGE_LABELS`), so adding a preset is a single line and there is no way
# for the three representations to get out of sync.
_RANGE_PRESETS: tuple[tuple[str, str, timedelta | _Today | None], ...] = (
    ("today", "Today", _TODAY),
    ("24h", "24h", timedelta(hours=24)),
    ("7d", "7d", timedelta(days=7)),
    ("30d", "30d", timedelta(days=30)),
    ("all", "All", None),
)
_RANGE_DELTAS: dict[str, timedelta | _Today | None] = {
    value: delta for value, _, delta in _RANGE_PRESETS
}
_RANGE_LABELS: list[tuple[str, str]] = [(value, label) for value, label, _ in _RANGE_PRESETS]
# Default window = current day: the expensive case (full history)
# is no longer the default; "All" is one click away for whoever needs it.
_RANGE_DEFAULT = "today"
_LIMIT_DEFAULT = 50
_LIMIT_OPTIONS: list[int] = [25, 50, 100, 200]
# Ceiling of the limit = last preset of the selector. The selector and the URL
# contract share a single source of truth, so they cannot diverge.
_LIMIT_MAX = _LIMIT_OPTIONS[-1]


def _normalize_range(value: str | None) -> str:
    """Returns `value` if it is in the presets whitelist; otherwise, the default."""
    if value in _RANGE_DELTAS:
        return value  # type: ignore[return-value]
    return _RANGE_DEFAULT


def _range_to_since(range_value: str, now: datetime) -> datetime | None:
    """Translates a preset into `since` (UTC).

    `all` => None (no lower bound); `today` => UTC midnight of the day of `now`;
    the rest => `now - delta`.
    """
    delta = _RANGE_DELTAS.get(range_value)
    if delta is None:
        return None
    if isinstance(delta, _Today):
        return start_of_utc_day(now)
    return now - delta


def _clamp_limit(value: int) -> int:
    """Bounds `limit` to the maximum the selector offers (1.._LIMIT_MAX).

    The ceiling is the last preset, not a loose 1000: a hand-typed `?limit=1000` no
    longer renders hundreds of `<details>` beyond what the UI offers.
    """
    return max(1, min(_LIMIT_MAX, value))


_STATUS_PRESETS: tuple[tuple[str, str], ...] = (
    ("failed", "Failed"),
    ("completed", "Completed"),
    ("cancelled", "Cancelled"),
    ("all", "All"),
)
# The three terminal statuses. The radio picks a subset; the version `<select>`
# uses ALL of them (see `_outcomes_context`), which is what makes it invariant to the
# radio.
_TERMINAL_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled")

_SQL_OUTCOME_OPTIONS = text(
    """
    SELECT DISTINCT outcome
      FROM tasks
     WHERE workspace_id = :workspace_id
       AND outcome IS NOT NULL
     ORDER BY outcome
     LIMIT 50
    """
)


async def _outcomes_context(
    session: AsyncSession,
    ctx: WorkspaceContext,
    range_param: str | None,
    limit_param: int,
    variant_param: str = "",
    status_param: str = "failed",
    outcome_param: str = "",
    include_variant_options: bool = True,
) -> dict[str, Any]:
    """Context shared by the page and the outcomes partial.

    Normalizes `range`/`limit`, translates the preset into `since` (UTC) and calls
    `aggregate_outcomes` with the session's workspace (not the PAT endpoint).

    `variant_param` is the version LENS: the "current" default is resolved
    over the variants present AMONG THE TERMINAL TASKS of the current status.

    The version `<select>`, on the other hand, is INVARIANT to the filters: the
    keys are all the variants of the workspace, the `· N` counts the all-time
    terminal tasks of each one and the `(current)` hint marks the latest among
    those that have any terminal. The HTMX swap does not redraw it, so data
    depending on the filter would look stale; see the body comment.

    `include_variant_options` controls whether `variant_options` is computed (an
    extra workspace-wide scan): the version `<select>` lives in `outcomes.html`
    outside `#failures-block` (the partial's hx-target), so
    `admin_outcomes_partial` passes `False` so as not to repeat the full GROUP BY
    over `tasks` on every filter change and throw the result away — same
    discipline as the `admin_index` comment on `outcome_chips`.

    `status_param` is the terminal status radio (`failed`/`completed`/
    `cancelled`/`all`, default `failed`); `outcome_param` filters by exact
    outcome (empty = all).
    """
    selected_range = _normalize_range(range_param)
    selected_limit = _clamp_limit(limit_param)
    since = _range_to_since(selected_range, datetime.now(UTC))

    token = (status_param or "failed").strip()
    outcome_token = (outcome_param or "").strip()
    # Alias for saved URLs. A retired status token that the plugin's outcomes profile
    # declares is translated into its real status and sets `outcome` only if the
    # request carries none. Without a plugin there is no alias; the token hits the 400 below.
    alias = legacy_status_aliases().get(token)
    if alias is not None:
        token, outcome_token = alias.status, outcome_token or alias.outcome
    if token not in ("failed", "completed", "cancelled", "all"):
        raise HTTPException(status_code=400, detail=f"unknown terminal status {token!r}")
    statuses = _TERMINAL_STATUSES if token == "all" else (token,)

    present = await terminal_variant_counts(
        session, workspace_id=ctx.workspace_id, statuses=statuses, since=since, until=None
    )
    vf = resolve_variant_filter(variant_param, present.keys(), default_mode="current")
    # The Version <select> is a CATALOG, not a report: nothing it
    # shows depends on the current filters. The reason is mechanical — it lives in
    # `outcomes.html` outside `#failures-block` (the hx-target), so the
    # HTMX swap never redraws it; any data that followed the radio or the window
    # would stay from the initial render and would lie as soon as the operator changes status
    # (table on `completed`, select with the numbers of `failed`).
    #
    # Hence the three axes:
    #   - KEY    → all the variants of the workspace (same source as the
    #     dashboard, `_SQL_COUNT_TASKS_BY_VARIANT_STATUS`), so one can jump to
    #     an archived version even if it has no terminal tasks today.
    #   - `· N`  → all-time TERMINAL tasks of that version, the three statuses
    #     together. Counting only the radio's status would tie the number to the filter;
    #     counting ALL statuses would give a `· 2905` mostly `queued` on
    #     a page that only talks about tasks that have already finished.
    #   - `(current)` hint → latest of each kind AMONG the variants with some
    #     terminal task (not of the bare vocabulary: a freshly enqueued version
    #     is not the current one of this page).
    #
    # The LENS (vf) is another thing and DOES depend on the filters: it is resolved over
    # `present` (terminal tasks of the current window and status), because a
    # page whose default lens ends up empty while an old version burns would be
    # bad UX. That the hint does not follow it is deliberate: the hint describes the
    # catalog, not the filter.
    #
    # The partial does NOT recompute any of this (include_variant_options=False): the
    # result would be thrown away on every filter change.
    all_variant_counts: dict[tuple[str | None, str | None], int] = {}
    if include_variant_options:
        variant_rows = (
            await session.execute(
                _SQL_COUNT_TASKS_BY_VARIANT_STATUS, {"workspace_id": ctx.workspace_id}
            )
        ).all()
        # `count_statuses` restricted to the terminal ones is exactly the divergence
        # to watch for: the key comes from any row (vocabulary = whole
        # workspace), the `· N` only from the all-time terminal ones.
        all_variant_counts, _ = accumulate_variant_grid(
            variant_rows, count_statuses=_TERMINAL_STATUSES
        )
    outcomes = await aggregate_outcomes(
        session,
        workspace_id=ctx.workspace_id,
        since=since,
        until=None,
        limit=selected_limit,
        statuses=statuses,
        outcome=outcome_token or None,
        variant=vf,
    )
    outcome_rows = (
        await session.execute(_SQL_OUTCOME_OPTIONS, {"workspace_id": ctx.workspace_id})
    ).all()
    outcome_options = [r[0] for r in outcome_rows if r[0] != "other"] + ["other"]
    # The `<select>` marks `selected` by equality; a valid `limit` outside the
    # preset list (e.g. `?limit=75`) would leave the control with no selected
    # option. Injecting it as an extra option keeps displayed == applied.
    limit_options = (
        _LIMIT_OPTIONS
        if selected_limit in _LIMIT_OPTIONS
        else sorted({*_LIMIT_OPTIONS, selected_limit})
    )
    result: dict[str, Any] = {
        "outcomes": outcomes,
        "selected_range": selected_range,
        "selected_limit": selected_limit,
        "range_presets": _RANGE_LABELS,
        "limit_options": limit_options,
        "selected_variant": variant_token(vf),
        "status_presets": _STATUS_PRESETS,
        "selected_status": token,
        "selected_outcome": outcome_token,
        "outcome_options": outcome_options,
    }
    if include_variant_options:
        # `current_keys` from the variants WITH terminal tasks (count > 0),
        # not from the full vocabulary: a version whose tasks are all still
        # in flight cannot be the `(current)` of a page of terminal tasks.
        terminal_keys = {k for k, n in all_variant_counts.items() if n > 0}
        result["variant_options"] = build_variant_options(
            all_variant_counts, vf, current_keys=current_variant_keys(terminal_keys)
        )
    return result


# auth: gated by require_workspace (session + membership); role-based authz (owner/member) TODO
@router.get("/outcomes", response_class=HTMLResponse)
async def admin_outcomes_page(
    request: Request,
    session: SessionDep,
    ctx: WorkspaceDep,
    range: str | None = None,
    limit: int = _LIMIT_DEFAULT,
    variant: str = "",
    status: str = "failed",
    outcome: str = "",
) -> HTMLResponse:
    """Full page of the terminal-task outcomes aggregation."""
    return templates.TemplateResponse(
        request,
        "outcomes.html",
        {
            **await _workspace_header(session, ctx),
            **await _outcomes_context(session, ctx, range, limit, variant, status, outcome),
        },
    )


# auth: gated by require_workspace (session + membership); role-based authz (owner/member) TODO
@router.get("/outcomes/partial", response_class=HTMLResponse)
async def admin_outcomes_partial(
    request: Request,
    session: SessionDep,
    ctx: WorkspaceDep,
    range: str | None = None,
    limit: int = _LIMIT_DEFAULT,
    variant: str = "",
    status: str = "failed",
    outcome: str = "",
) -> HTMLResponse:
    """HTMX partial of the outcomes aggregation; same filtering rules.

    `include_variant_options=False`: the version <select> lives in
    outcomes.html outside #failures-block (this partial's hx-target), so
    recomputing it on every filter change would pay a full GROUP BY over
    `tasks` whose result the template does not even read. Since the control is
    invariant to the filters, there is nothing to refresh either.
    """
    return templates.TemplateResponse(
        request,
        "_outcomes.html",
        await _outcomes_context(
            session,
            ctx,
            range,
            limit,
            variant,
            status,
            outcome,
            include_variant_options=False,
        ),
    )


# No session dependency: it is a pure redirect, auth is resolved at /outcomes.
@router.get("/failures")
async def admin_failures_redirect(request: Request) -> RedirectResponse:
    """Legacy route: the page used to live at /failures; 308 keeps method and query."""
    qs = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/admin/outcomes{qs}", status_code=308)


# No session dependency: it is a pure redirect, auth is resolved at /outcomes/partial.
@router.get("/failures/partial")
async def admin_failures_partial_redirect(request: Request) -> RedirectResponse:
    """Legacy route: the partial used to live at /failures/partial; 308 keeps method and query."""
    qs = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/admin/outcomes/partial{qs}", status_code=308)


async def _fetch_admin_actions(
    session: AsyncSession,
    *,
    workspace_id: str,
    action: str | None,
    target_kind: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                _SQL_LIST_ADMIN_ACTIONS,
                {
                    "workspace_id": workspace_id,
                    "action": _normalize_filter(action, _ACTION_VALUES),
                    "target_kind": _normalize_filter(target_kind, _TARGET_KIND_VALUES),
                    "limit": max(1, min(limit, 500)),
                },
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


# auth: gated by require_workspace (session + membership); role-based authz (owner/member) TODO
@router.get("/actions", response_class=HTMLResponse)
async def admin_actions_page(
    request: Request,
    session: SessionDep,
    ctx: WorkspaceDep,
    action: str | None = None,
    target_kind: str | None = None,
    limit: int = 100,
) -> HTMLResponse:
    """Full audit log page with optional query-string filters."""
    actions = await _fetch_admin_actions(
        session,
        workspace_id=ctx.workspace_id,
        action=action,
        target_kind=target_kind,
        limit=limit,
    )
    return templates.TemplateResponse(
        request,
        "admin_actions.html",
        {
            **await _workspace_header(session, ctx),
            "actions": actions,
            "selected_action": _normalize_filter(action, _ACTION_VALUES) or "",
            "selected_target_kind": _normalize_filter(target_kind, _TARGET_KIND_VALUES) or "",
            "limit": limit,
            "action_values": _ACTION_VALUES,
            "target_kind_values": _TARGET_KIND_VALUES,
        },
    )


# auth: gated by require_workspace (session + membership); role-based authz (owner/member) TODO
@router.get("/actions/partial", response_class=HTMLResponse)
async def admin_actions_partial(
    request: Request,
    session: SessionDep,
    ctx: WorkspaceDep,
    action: str | None = None,
    target_kind: str | None = None,
    limit: int = 100,
) -> HTMLResponse:
    """Partial for polling/refresh; same filtering rules."""
    actions = await _fetch_admin_actions(
        session,
        workspace_id=ctx.workspace_id,
        action=action,
        target_kind=target_kind,
        limit=limit,
    )
    return templates.TemplateResponse(
        request,
        "_admin_actions_table.html",
        {"actions": actions},
    )


# auth: gated by require_workspace (session + membership); role-based authz (owner/member) TODO
@router.get("/storage")
async def admin_storage_partial(
    request: Request,
    session: SessionDep,
    ctx: WorkspaceDep,
):
    """Storage indicator partial: server disk + quota of the
    active workspace. Refreshed by HTMX from the header."""
    if not _is_htmx(request):
        return RedirectResponse(url="/admin/", status_code=303)

    state = await storage.storage_status(session, ctx.workspace_id, settings=settings)
    total_bytes = await storage.disk_total_bytes_async(settings.artifacts_root)
    free_bytes = state.free_bytes
    used_disk_bytes = max(0, total_bytes - free_bytes) if total_bytes else 0

    mb = 1024 * 1024
    disk_pct = _usage_pct(used_disk_bytes / mb, total_bytes / mb) if total_bytes else 0
    quota_pct = _usage_pct(state.usage_bytes / mb, state.quota_bytes / mb)

    return templates.TemplateResponse(
        request,
        "_storage_indicator.html",
        {
            "disk_free_mb": free_bytes / mb if total_bytes else None,
            "disk_pct": disk_pct,
            "ws_used_mb": state.usage_bytes / mb,
            "ws_quota_mb": state.quota_bytes / mb,
            "ws_quota_overridden": state.quota_overridden,
            "quota_pct": quota_pct,
            "paused": state.paused_reason is not None,
        },
    )


# auth: gated by require_workspace (session + membership); role-based authz (owner/member) TODO
@router.get("/artifact-indicator")
async def admin_artifact_indicator_partial(
    request: Request,
    session: SessionDep,
    ctx: WorkspaceDep,
):
    """Artifacts indicator partial: unique artifacts whose name
    ends in the configured suffix, in the active workspace. Refreshed by HTMX from
    the topbar. Empty suffix => feature off (empty body, pill hidden)."""
    if not _is_htmx(request):
        return RedirectResponse(url="/admin/", status_code=303)

    suffix = settings.artifact_indicator_suffix
    if not suffix:
        return HTMLResponse("")

    count = (
        await session.execute(
            _SQL_COUNT_INDICATOR_ARTIFACTS,
            {"workspace_id": ctx.workspace_id, "suffix_pattern": f"%{suffix}"},
        )
    ).scalar_one()

    # uploaded = what was actually uploaded to the sync destination.
    # Failure-tolerant read: missing/corrupt status => None => the template
    # shows only `produced`. Never 500. Scoping: produced is per-workspace,
    # uploaded is global (a single corpus).
    status = read_sync_status(settings.artifact_indicator_sync_status_path)

    return templates.TemplateResponse(
        request,
        "_artifact_indicator.html",
        {
            "count": count,
            "uploaded": status.uploaded_total if status else None,
            "commit_sha": status.commit_sha if status else None,
            "pushed_at": status.pushed_at if status else None,
        },
    )


# auth: gated by require_workspace (session + membership); role-based authz (owner/member) TODO
@router.get("/workers")
async def admin_workers_partial(request: Request, session: SessionDep, ctx: WorkspaceDep):
    # /admin/workers serves the _workers_deck.html partial for htmx (deck poller
    # on the dashboard). When someone types it directly, we redirect to the
    # dashboard so they do not see an orphan fragment without nav/header.
    if not _is_htmx(request):
        return RedirectResponse(url="/admin/", status_code=303)
    rows = (
        (await session.execute(_SQL_LIST_WORKERS, {"workspace_id": ctx.workspace_id}))
        .mappings()
        .all()
    )
    workers = [dict(r) for r in rows]
    # Age of the last heartbeat per worker. The deck needs it to mark
    # `paused (stale)`: a paused and powered-off worker stays `paused`
    # with a stale heartbeat indefinitely (housekeeping only transitions
    # `online`), so for that state the badge stopped indicating
    # connectivity. A single `now` for the whole fleet: the N cards are compared
    # against the same instant.
    now = datetime.now(UTC)
    for w in workers:
        w["heartbeat_age_seconds"] = _heartbeat_age_seconds(w.get("last_heartbeat_at"), now)
        # The deck flags the worker below the task secrets version
        # floor; the server does not hand it tasks that use them.
        w["task_secrets_supported"] = worker_supports_task_secrets(w.get("version"))
    # If all workers share the same `os · arch`, we summarize it in the
    # pane header (via hx-swap-oob) and omit the chip on each card; with
    # a homogeneous fleet (the common case) the tag was repeated N times adding no
    # signal. If they differ, we leave the chip on each card to tell them apart.
    platforms = {f"{w['os']} · {w['arch']}" for w in workers if w.get("os") and w.get("arch")}
    common_platform = platforms.pop() if len(platforms) == 1 else None
    return templates.TemplateResponse(
        request,
        "_workers_deck.html",
        {
            "workers": workers,
            # Worker control is owner-only: members get no actions menu.
            "is_owner": ctx.role == "owner",
            "common_platform": common_platform,
            "heartbeat_timeout_seconds": settings.worker_heartbeat_timeout_seconds,
            # Disk gate floor, so the deck flags the third
            # state (worker `online` but gated by disk) that the status badge
            # could not distinguish. 0.0 = gate disabled (never flagged).
            "claim_min_free_fraction": settings.worker_claim_min_free_fraction,
            # Task secrets version floor, for the deck notice.
            "task_secrets_min_version": TASK_SECRETS_MIN_WORKER_VERSION_STR,
        },
    )


# auth: gated by require_workspace (session + membership); role-based authz (owner/member) TODO
@router.get("/workers.json")
async def admin_workers_json(session: SessionDep, ctx: WorkspaceDep) -> list[dict[str, Any]]:
    """Compact list of workers for the "New task" modal selector.

    Returns the fields the form needs to build
    `target=worker:<id>` / `workers:<ids>` / `all:<ids>`, plus `version` so
    a client can read each worker's version without scraping the HTML.
    It reuses the full SQL and projects — the cost is negligible and keeps
    a single source of truth for the order and state of each worker.

    `version` is `null` when the worker has not yet reported a version.

    `task_secrets_supported` (additive) says whether that version reaches the
    task secrets floor (`orcaster_server/task_secrets/gate.py`): without it, the server
    does not hand it tasks that use stored secrets. `false` also without a
    reported version (fails closed, same as the poll).
    """
    rows = (
        (await session.execute(_SQL_LIST_WORKERS, {"workspace_id": ctx.workspace_id}))
        .mappings()
        .all()
    )
    return [
        {
            "id": r["id"],
            "hostname": r["hostname"],
            "name": r["name"],
            "status": r["status"],
            "version": r["version"],
            "task_secrets_supported": worker_supports_task_secrets(r["version"]),
        }
        for r in rows
    ]


def _progress_scope_note(
    vf: VariantFilter, has_other: bool, cancelled: int, active_filters: bool
) -> str:
    """Note under the dashboard progress bar. It always states the
    version lens it measures (default current, which includes the `other` bucket
    if present); if there are cancelled ones, that they are left out of the total (the "X of Y"
    subtitle does count them); and with active filters, that status/outcome/search/
    artifact do not apply. UI in English."""
    if vf.mode == "current":
        scope = (
            "Scope: current version of each tool, plus unversioned tasks."
            if has_other
            else "Scope: current version of each tool."
        )
    elif vf.mode == "all":
        scope = "Scope: all versions."
    elif vf.mode == "other":
        scope = "Scope: tasks with no detected version."
    elif vf.mode == "kind":
        scope = f"Scope: all {vf.kind} versions."
    else:
        scope = f"Scope: {vf.kind} {vf.label} only."
    parts = [scope]
    if cancelled:
        parts.append(f"{cancelled} cancelled excluded from the total.")
    if active_filters:
        parts.append("Status, outcome, search and artifact filters do not apply to progress.")
    return " ".join(parts)


# auth: gated by require_workspace (session + membership); role-based authz (owner/member) TODO
@router.get("/tasks")
async def admin_tasks_partial(
    request: Request,
    session: SessionDep,
    ctx: WorkspaceDep,
    status: str = "",
    outcome: str = "",
    q: str = "",
    sort: str = "created_at",
    dir: str = "desc",
    page: int = 1,
    page_size: int = 50,
    has_artifacts: str = "any",
    artifact_path: str = "",
    artifact_type: str = "",
    artifact_sha256: str = "",
    variant: str = "",
):
    """Tasks partial with optional status filters and free-text search.

    - `status`: CSV of valid task_status values (e.g. `failed,cancelled`). Any
      unknown token yields 400 with actionable detail. Empty string =
      no filter (all statuses). Accepts the retired status aliases that the
      plugin's outcomes profile declares: saved URLs keep working,
      translated to the alias's real status plus its
      outcome. Without a plugin there is no alias and the token answers 400.
    - `outcome`: CSV of outcome names (`outcome` column, e.g.
      `empty_result`). A token that does not match `OUTCOME_NAME_RE` → 400.
    - `q`: case-insensitive substring over `command` and `payload`. If the
      query is numeric it also does a prefix match on `id::text`.
    - `sort`: column to sort by. Whitelist in `_SORT_COLUMNS`.
      Default `created_at`. Value outside the whitelist → HTTP 400.
    - `dir`: `asc` | `desc`. Default `desc`. Invalid value → silent
      fallback to `desc` (the UI chips should not be able to send anything
      else; treat as noise and do not break the polling).

    If the client is NOT htmx (someone typed/shared /admin/tasks?...) it is
    redirected to the dashboard preserving the query params so the filters
    apply there. This prevents /admin/tasks from rendering an orphan fragment.

    The template `_tasks_table.html` consumes `sort`/`dir`/`total_count`/
    `filtered_count` to render the sort state and the counts.
    """
    # Validate sort/dir before the redirect so a malformed sort fails
    # fast whether HTMX or not.
    if sort not in _SORT_COLUMNS:
        raise HTTPException(
            status_code=400,
            detail=(f"unknown sort column: {sort!r}. valid: {', '.join(sorted(_SORT_COLUMNS))}"),
        )
    # Invalid `dir`: silent fallback to 'desc'. The UI only sends whitelist
    # values; an odd value is noise (old link, manual tampering).
    dir_norm = dir if dir in _SORT_DIRS else "desc"

    if page_size not in _PAGE_SIZE_VALUES:
        page_size = 50
    if page < 1:
        page = 1

    if not _is_htmx(request):
        # page/page_size are deliberately omitted from the redirect: pagination
        # state is operational and ephemeral (the set of tasks changes with
        # polling), not shareable by URL. app.js does not bootstrap it from the
        # query either, unlike sort/dir. A paginated deep link falls to page 1.
        params_qs: dict[str, str] = {}
        if status:
            params_qs["status"] = status
        if outcome:
            params_qs["outcome"] = outcome
        if q:
            params_qs["q"] = q
        if sort != "created_at":
            params_qs["sort"] = sort
        if dir_norm != "desc":
            params_qs["dir"] = dir_norm
        if has_artifacts in _HAS_ARTIFACTS_VALUES and has_artifacts != "any":
            params_qs["has_artifacts"] = has_artifacts
        if (artifact_path or "").strip():
            params_qs["artifact_path"] = artifact_path.strip()
        if (artifact_type or "").strip():
            params_qs["artifact_type"] = artifact_type.strip()
        if (artifact_sha256 or "").strip():
            params_qs["artifact_sha256"] = artifact_sha256.strip()
        if (variant or "").strip():
            params_qs["variant"] = variant.strip()
        qs = urlencode(params_qs)
        target = "/admin/" + (("?" + qs) if qs else "")
        return RedirectResponse(url=target, status_code=303)

    status_clean = (status or "").strip()
    tokens: list[str] = []
    if status_clean:
        tokens = [t.strip() for t in status_clean.split(",") if t.strip()]

    outcome_clean = (outcome or "").strip()
    outcome_tokens = [t.strip() for t in outcome_clean.split(",") if t.strip()]

    # Legacy alias — saved URLs with a retired status that the plugin's
    # outcomes profile declares keep working: the token is removed, its real
    # status is added and its outcome is ADDED to the outcomes list (it does not replace it). Runs
    # BEFORE the validation against `_TASK_STATUS_VALUES` so the legacy token does not
    # trigger the 400. Without a plugin there is no alias and the token falls into that 400.
    aliases = legacy_status_aliases()
    for alias_token in [t for t in tokens if t in aliases]:
        alias = aliases[alias_token]
        tokens = [t for t in tokens if t != alias_token]
        if alias.status not in tokens:
            tokens.append(alias.status)
        if alias.outcome not in outcome_tokens:
            outcome_tokens.append(alias.outcome)

    if tokens:
        bad = [t for t in tokens if t not in _TASK_STATUS_VALUES]
        if bad:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown status value(s): {', '.join(sorted(set(bad)))}. "
                    f"valid: {', '.join(_TASK_STATUS_VALUES)}"
                ),
            )

    bad_outcomes = [t for t in outcome_tokens if not OUTCOME_NAME_RE.match(t)]
    if bad_outcomes:
        raise HTTPException(
            status_code=400,
            detail=(f"invalid outcome value(s): {', '.join(sorted(set(bad_outcomes)))}"),
        )

    q_clean = (q or "").strip()
    q_is_numeric = q_clean.isdigit() if q_clean else False

    artifact_binds = _artifact_filter_binds(
        has_artifacts, artifact_path, artifact_type, artifact_sha256
    )
    artifact_active = artifact_binds["artifact_mode"] != "off"

    # Active artifact (param, value) pairs, to rebuild query strings
    # (non-HTMX redirect and the chips' remove_qs). `has_artifacts=any` is the
    # default and is not serialized; path/type/sha only if not empty.
    _ha_clean = has_artifacts if has_artifacts in _HAS_ARTIFACTS_VALUES else "any"
    artifact_qs_items: list[tuple[str, str]] = []
    if _ha_clean != "any":
        artifact_qs_items.append(("has_artifacts", _ha_clean))
    if (artifact_path or "").strip():
        artifact_qs_items.append(("artifact_path", artifact_path.strip()))
    if (artifact_type or "").strip():
        artifact_qs_items.append(("artifact_type", artifact_type.strip()))
    if (artifact_sha256 or "").strip():
        artifact_qs_items.append(("artifact_sha256", artifact_sha256.strip()))

    # Variant lens filter: a single scan of the breakdown by
    # (variant, status) feeds EVERYTHING — the selector, the "current" set (latest of
    # each kind + other) and the status breakdown rescaled to the variant. It must
    # run before building `params` because the mode and its binds come from it.
    variant_rows = (
        await session.execute(
            _SQL_COUNT_TASKS_BY_VARIANT_STATUS, {"workspace_id": ctx.workspace_id}
        )
    ).all()
    # variant_grid = [(kind, label, status, outcome, n)]; present_counts[(kind,label)] = total.
    present_counts, variant_grid = accumulate_variant_grid(variant_rows)
    vf = resolve_variant_filter(variant, present_counts.keys(), default_mode="current")

    # When there is no status filter, we pass NULL to the cast to short-circuit
    # the WHERE. asyncpg needs an explicit CAST to text to accept NULL in
    # placeholders with no declared type.
    offset = (page - 1) * page_size
    params = {
        "status_filter": ",".join(tokens) if tokens else None,
        "outcome_filter": ",".join(outcome_tokens) if outcome_tokens else None,
        "q": q_clean,
        "q_is_numeric": q_is_numeric,
        "workspace_id": ctx.workspace_id,
        "page_size": page_size,
        "offset": offset,
        **artifact_binds,
        **variant_sql_binds(vf),
    }

    # ORDER BY built by f-string ONLY with already whitelisted values; there is no
    # injection vector because neither `sort` nor `dir_norm` come from free
    # strings. The `t.id DESC` tiebreaker keeps the order stable when
    # several rows share a sort key (e.g. same status, same done).
    order_expr = _SORT_EXPR[sort]
    sql = text(
        _SQL_LIST_TASKS_TEMPLATE
        + f" ORDER BY {order_expr} {dir_norm.upper()}, t.id DESC LIMIT :page_size OFFSET :offset"
    )
    rows = (await session.execute(sql, params)).mappings().all()
    tasks = [dict(r) for r in rows]
    # The variant is a LENS, NOT an active filter: it rescales the breakdown but
    # does NOT collapse the subtitle to "X of Y" mode (we reserve that for
    # status/q/artifact, the reduced-scope filters INSIDE the lens).
    active_filters = bool(tokens) or bool(outcome_tokens) or bool(q_clean) or artifact_active

    # `status_counts` feeds the subtitle's inline breakdown (`12 queued ·
    # 3 failed · …`), rescaled to the selected variant (default = current).
    # The breakdown is already in memory (`variant_grid`): we sum the rows whose
    # (kind, label) falls within the lens — no extra round-trip. `filtered_count`
    # only hits the DB when status/q/artifact are active (the WHERE already includes the
    # variant); otherwise it is the lens total. `outcome_counts` is the same
    # breakdown but by outcome: only the full page renders it
    # as chips (dashboard.html); the partial passes it for parity/future OOB.
    status_counts: dict[str, int] = {}
    outcome_counts: dict[str, int] = {}
    for vk, vl, st, oc, n in variant_grid:
        if variant_row_matches(vf, vk, vl):
            status_counts[st] = status_counts.get(st, 0) + n
            if oc:
                outcome_counts[oc] = outcome_counts.get(oc, 0) + n
    total_count = sum(status_counts.values())
    # Dashboard progress bar: same math as the public status page,
    # over the variant lens breakdown. Status/outcome/q/
    # artifact do NOT apply (progress is for the full scope of the version);
    # the note always states the measured version, the cancelled ones and those filters.
    progress = compute_progress(status_counts)
    progress_scope_note = _progress_scope_note(
        vf, (None, None) in present_counts, status_counts.get("cancelled", 0), active_filters
    )
    if active_filters:
        filtered_count = (await session.execute(_SQL_COUNT_TASKS_FILTERED, params)).scalar_one()
    else:
        filtered_count = total_count

    # --- Pagination ---
    result_count = filtered_count if active_filters else total_count
    total_pages = max(1, math.ceil(result_count / page_size))
    # Clamp: if the requested page exceeds the total, serve the last page and emit
    # OOB to resync the hidden input on the client.
    page_clamped = page > total_pages
    if page_clamped:
        page = total_pages
        offset = (page - 1) * page_size
        params["offset"] = offset
        rows = (await session.execute(sql, params)).mappings().all()
        tasks = [dict(r) for r in rows]
    has_prev = page > 1
    has_next = page < total_pages

    # Fork lineage for the list. For each forked row we recompute whether the
    # fork changed params relative to the parent by reusing _params_changed (same
    # source of truth as the detail view). A single batch query bounded to the ids
    # of the page avoids inflating the main SELECT and its GROUP BY. If the parent
    # ended up outside the workspace or is not comparable, we leave None (the
    # link will be shown without the "modified" mark).
    fork_ids: set[int] = set()
    for t in tasks:
        pid = t.get("parent_task_id")
        if pid is not None:
            fork_ids.add(int(pid))
            fork_ids.add(int(t["id"]))
    fork_params_by_id: dict[int, dict] = {}
    if fork_ids:
        fp_rows = (
            (
                await session.execute(
                    _SQL_FORK_PARAMS_BATCH,
                    {"ids": sorted(fork_ids), "workspace_id": ctx.workspace_id},
                )
            )
            .mappings()
            .all()
        )
        fork_params_by_id = {int(r["id"]): dict(r) for r in fp_rows}
    for t in tasks:
        pid = t.get("parent_task_id")
        if pid is not None and int(pid) in fork_params_by_id and int(t["id"]) in fork_params_by_id:
            t["parent_params_changed"] = _params_changed(
                fork_params_by_id[int(pid)], fork_params_by_id[int(t["id"])]
            )
        else:
            t["parent_params_changed"] = None

    # Operational urgency order for the subtitle. The task_status enum does not
    # include 'running' (that lives in execution_status); 'dispatching' is the
    # equivalent state at task level (workers claiming/executing).
    # We show up to 4 non-zero buckets; if there are more, we append a "·…".
    _STATUS_ORDER = (
        "failed",
        "queued",
        "dispatching",
        "paused",
        "completed",
        "cancelled",
    )
    subtitle_buckets = [
        {"status": s, "count": status_counts[s]}
        for s in _STATUS_ORDER
        if status_counts.get(s, 0) > 0
    ]
    subtitle_buckets_overflow = len(subtitle_buckets) > 4
    subtitle_buckets = subtitle_buckets[:4]
    # "All completed": the collapsed subtitle `Tasks · N completed` is reserved
    # ONLY for the 100% green case (every task in `completed` state). `failed`
    # and `cancelled` are terminal but NOT successful: if they exist, the template
    # falls back to the per-bucket breakdown. The big number must never say
    # "completed" about tasks that failed — honest density (product principle).
    # If there are no tasks at all, total=0 and the empty state already covers it.
    all_completed = total_count > 0 and status_counts.get("completed", 0) == total_count

    # Dismissible chips: each chip carries a link that rebuilds the current query
    # string MINUS its own param. We render on the server (not JS)
    # so the dashboard stays usable without JS and the chips work as
    # honest links (back/forward, open in a new tab, share).
    active_filter_chips: list[dict[str, str]] = []

    # The variant travels in ALL chips: removing them returns to the selected
    # lens, not to "all". `current` is the default and is not serialized.
    _variant_qs = variant_token(vf) if vf.mode != "current" else None

    def _with_variant(qs_params: dict[str, str]) -> dict[str, str]:
        if _variant_qs:
            qs_params["variant"] = _variant_qs
        return qs_params

    def _with_artifact(qs_params: dict[str, str]) -> dict[str, str]:
        for k, v in artifact_qs_items:
            qs_params[k] = v
        return _with_variant(qs_params)

    if tokens:
        # One chip per status token so the operator can
        # remove just one (e.g. unpin `failed` and keep `cancelled`).
        for tok in tokens:
            remaining = [t for t in tokens if t != tok]
            qs_params: dict[str, str] = {}
            if remaining:
                qs_params["status"] = ",".join(remaining)
            if outcome_tokens:
                qs_params["outcome"] = ",".join(outcome_tokens)
            if q_clean:
                qs_params["q"] = q_clean
            if sort != "created_at":
                qs_params["sort"] = sort
            if dir_norm != "desc":
                qs_params["dir"] = dir_norm
            active_filter_chips.append(
                {
                    "label": "status",
                    "value": tok,
                    "remove_qs": urlencode(_with_artifact(qs_params)),
                }
            )
    if outcome_tokens:
        # Dismissible chip per outcome, same pattern as status.
        for tok in outcome_tokens:
            remaining = [t for t in outcome_tokens if t != tok]
            qs_params = {}
            if tokens:
                qs_params["status"] = ",".join(tokens)
            if remaining:
                qs_params["outcome"] = ",".join(remaining)
            if q_clean:
                qs_params["q"] = q_clean
            if sort != "created_at":
                qs_params["sort"] = sort
            if dir_norm != "desc":
                qs_params["dir"] = dir_norm
            active_filter_chips.append(
                {
                    "label": "outcome",
                    "value": tok,
                    "remove_qs": urlencode(_with_artifact(qs_params)),
                }
            )
    if q_clean:
        qs_params = {}
        if tokens:
            qs_params["status"] = ",".join(tokens)
        if outcome_tokens:
            qs_params["outcome"] = ",".join(outcome_tokens)
        if sort != "created_at":
            qs_params["sort"] = sort
        if dir_norm != "desc":
            qs_params["dir"] = dir_norm
        active_filter_chips.append(
            {
                "label": "q",
                "value": q_clean,
                "remove_qs": urlencode(_with_artifact(qs_params)),
            }
        )

    # Artifact chips: each one is removed while preserving the other filters
    # (status, outcome, q, sort, dir, and the OTHER artifact params).
    def _base_qs() -> dict[str, str]:
        qs_params: dict[str, str] = {}
        if tokens:
            qs_params["status"] = ",".join(tokens)
        if outcome_tokens:
            qs_params["outcome"] = ",".join(outcome_tokens)
        if q_clean:
            qs_params["q"] = q_clean
        if sort != "created_at":
            qs_params["sort"] = sort
        if dir_norm != "desc":
            qs_params["dir"] = dir_norm
        return _with_variant(qs_params)

    # NOTE: no enumerate() — ruff runs with bugbear (B) and an unused index
    # triggers B007.
    for param_key, value in artifact_qs_items:
        qs_params = _base_qs()
        for k, v in artifact_qs_items:
            if k != param_key:
                qs_params[k] = v
        active_filter_chips.append(
            {
                "label": _ARTIFACT_CHIP_LABELS[param_key],
                "value": value,
                "remove_qs": urlencode(qs_params),
            }
        )

    # Content-types datalist: only when the artifact filter is active;
    # otherwise we avoid a DB round-trip on every poll of the partial.
    if artifact_active:
        content_type_rows = (
            (await session.execute(_SQL_DISTINCT_CONTENT_TYPES, {"workspace_id": ctx.workspace_id}))
            .scalars()
            .all()
        )
        artifact_content_types = list(content_type_rows)
    else:
        artifact_content_types = []

    return templates.TemplateResponse(
        request,
        "_tasks_table.html",
        {
            "tasks": tasks,
            "active_filters": active_filters,
            "active_filter_chips": active_filter_chips,
            "sort": sort,
            "dir": dir_norm,
            "total_count": total_count,
            "filtered_count": filtered_count,
            "status_counts": status_counts,
            "outcome_counts": outcome_counts,
            "subtitle_buckets": subtitle_buckets,
            "subtitle_buckets_overflow": subtitle_buckets_overflow,
            "all_completed": all_completed,
            "progress": progress,
            "progress_scope_note": progress_scope_note,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "has_prev": has_prev,
            "has_next": has_next,
            "page_clamped": page_clamped,
            "artifact_content_types": artifact_content_types,
            # Variant selector (the template renders it; passing it keeps a
            # single deploy). `selected_variant` is "" in the default current.
            "variant_options": build_variant_options(present_counts, vf),
            "selected_variant": _variant_qs or "",
        },
    )


# auth: gated by require_workspace (session + membership); role-based authz (owner/member) TODO
@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def admin_task_detail(
    task_id: int, request: Request, session: SessionDep, ctx: WorkspaceDep
) -> HTMLResponse:
    task_row = (
        (await session.execute(_SQL_GET_TASK, {"id": task_id, "workspace_id": ctx.workspace_id}))
        .mappings()
        .first()
    )
    if task_row is None:
        raise HTTPException(status_code=404, detail="task not found")
    exec_rows = (
        (
            await session.execute(
                _SQL_TASK_EXECUTIONS, {"id": task_id, "workspace_id": ctx.workspace_id}
            )
        )
        .mappings()
        .all()
    )
    executions = [dict(r) for r in exec_rows]
    task = dict(task_row)
    agg = _aggregates(
        executions,
        task["target_count"],
        target_mode=task["target_mode"],
        snapshot_epoch=task["snapshot_epoch"],
    )
    fork_body = _build_fork_body(task, executions)
    latest_attempt = max(
        (e["attempt_index"] for e in executions if e.get("attempt_index") is not None),
        default=None,
    )
    # Artifacts of the last attempt: those of the attempt with the highest
    # attempt_index are shown by default. `executions` comes ORDER BY attempt_index
    # DESC, so the first with that attempt_index is the last attempt.
    latest_execution_id: str | None = None
    latest_artifacts: list[dict[str, Any]] = []
    if latest_attempt is not None:
        latest_execution_id = next(
            (e["id"] for e in executions if e["attempt_index"] == latest_attempt),
            None,
        )
        if latest_execution_id is not None:
            art_rows = (
                (await session.execute(_SQL_LIST_ARTIFACTS, {"execution_id": latest_execution_id}))
                .mappings()
                .all()
            )
            latest_artifacts = [
                {
                    "id": r["id"],
                    "rel_path": r["rel_path"],
                    "size_bytes": r["size_bytes"],
                    "content_type": r["content_type"],
                    # Non-null → no download link, "Collected" badge.
                    "collected_at": r["collected_at"],
                }
                for r in art_rows
            ]
    # Fork classification (re-derived at render). If this task is a fork
    # (parent_task_id not NULL), we compare its params against the parent with the
    # same helper as the endpoint. If the parent was deleted (ON DELETE SET
    # NULL), parent_task_id is already NULL and we do not enter here.
    fork_params_changed: bool | None = None
    if task.get("parent_task_id") is not None:
        parent_row = (
            (
                await session.execute(
                    _SQL_FORK_PARAMS,
                    {"id": task["parent_task_id"], "workspace_id": ctx.workspace_id},
                )
            )
            .mappings()
            .first()
        )
        child_row = (
            (
                await session.execute(
                    _SQL_FORK_PARAMS, {"id": task_id, "workspace_id": ctx.workspace_id}
                )
            )
            .mappings()
            .first()
        )
        if parent_row is not None and child_row is not None:
            fork_params_changed = _params_changed(dict(parent_row), dict(child_row))
    # Forks: direct children of this task (inverse query by parent_task_id).
    # Total count + listing bounded to _FORK_CHILDREN_LIMIT (order id DESC).
    fork_children_count = (
        await session.execute(
            _SQL_COUNT_FORK_CHILDREN, {"id": task_id, "workspace_id": ctx.workspace_id}
        )
    ).scalar_one()
    fork_children_rows = (
        (
            await session.execute(
                _SQL_LIST_FORK_CHILDREN,
                {"id": task_id, "workspace_id": ctx.workspace_id, "limit": _FORK_CHILDREN_LIMIT},
            )
        )
        .mappings()
        .all()
    )
    fork_children = [dict(r) for r in fork_children_rows]
    awaiting_reason = await _awaiting_eligible_reason(session, task, workspace_id=ctx.workspace_id)
    return templates.TemplateResponse(
        request,
        "task_detail.html",
        {
            **await _workspace_header(session, ctx),
            "task": task,
            # Reason for the wait, or None if the task is not waiting.
            "awaiting_eligible_reason": awaiting_reason,
            "awaiting_eligible_text": (
                block_reason_text(awaiting_reason) if awaiting_reason else None
            ),
            "required_capabilities": _required_capabilities(task.get("eligibility")),
            "executions": executions,
            "agg": agg,
            "latest_attempt": latest_attempt,
            "latest_execution_id": latest_execution_id,
            "latest_artifacts": latest_artifacts,
            "fork_body_json": json.dumps(fork_body),
            "fork_params_changed": fork_params_changed,
            "fork_children": fork_children,
            "fork_children_count": fork_children_count,
            "fork_children_limit": _FORK_CHILDREN_LIMIT,
        },
    )


def _required_capabilities(eligibility: Any) -> list[str]:
    """The requirements of `eligibility.capabilities`, to name them in the UI.

    Returns `[]` for any other shape (including the one in the old DDL
    sketch, an object): the UI is not the place where this is validated, and a task with
    an invalid shape **is satisfied by no worker** (fail-closed),
    so its wait is explained all the same without the list.
    """
    raw = eligibility if isinstance(eligibility, dict) else {}
    requirements = raw.get("capabilities")
    if not isinstance(requirements, list):
        return []
    return [r for r in requirements if isinstance(r, str)]


async def _awaiting_eligible_reason(
    session: AsyncSession, task: dict[str, Any], *, workspace_id: str
) -> str | None:
    """Why this task is waiting for a suitable worker, or `None` if it is not waiting.

    It waits when it is `all_available`, is `dispatching` and its current epoch
    has no execution: nobody will claim it and nothing will close it
    until a suitable worker joins (picked up by
    `housekeeping._resnapshot_awaiting_all_available`).

    The reason is **measured on every render**, on purpose: it describes the state of the
    fleet now, not the one at the time the task was created. A task that was born without
    online workers and today has three without the capability must state the
    latter.
    """
    if task.get("target_mode") != "all_available" or task.get("status") != "dispatching":
        return None
    if not task.get("epoch_has_no_executions"):
        return None
    raw_pool = task.get("target_worker_pool")
    return await classify_dispatch_block(
        session,
        task_id=int(task["id"]),
        workspace_id=workspace_id,
        pool=[str(p) for p in raw_pool] if raw_pool else None,
        min_free_fraction=settings.worker_claim_min_free_fraction,
    )


def _build_fork_body(task: dict[str, Any], executions: list[dict[str, Any]]) -> dict[str, Any]:
    """Rebuilds the POST body `/admin/tasks/{id}/fork` to fork a task.

    Rules:
    - `idempotency_key`: NOT copied (partial UNIQUE index; the fork would collide).
    - `run_at`: NOT copied (we want to run now, not in the past).
    - Targeting: `target_mode` WINS. If the task was `all_available`, the fork
      preserves that intent (open `all-available`, or `all:<ids>` if it had a
      restricted pool) — even if it already ran on one or more workers. This
      matters because the retry of an `all_available` re-snapshots but does NOT
      mutate `target_mode`/`target_worker_pool`, so the current state DOES
      reflect the original intent. For `count` without a pool: if the worker of
      the last attempt is known, `worker:<id>` is forced; otherwise, `<N>`.
    - `delivery_mode='push'` requires `target_worker_id`; it is kept only if
      the final target is a single 'worker:<uuid>'.
    """
    target_mode = task["target_mode"]
    pool = task.get("target_worker_pool")
    target_worker_id = task.get("target_worker_id")

    target_str: str
    if target_mode == "all_available":
        # The intent "run on all available" is preserved over
        # any worker that has executed. A non-NULL `target_worker_pool`
        # means `all:<ids>` (restricted subset); NULL = open all-available.
        target_str = "all:" + ",".join(pool) if pool else "all-available"
    elif pool:
        target_str = f"worker:{pool[0]}" if len(pool) == 1 else "workers:" + ",".join(pool)
    elif target_worker_id:
        target_str = f"worker:{target_worker_id}"
    else:
        last_worker = _last_attempt_worker_id(executions)
        if last_worker is not None:
            target_str = f"worker:{last_worker}"
        else:
            target_str = str(int(task["target_count"]))

    body: dict[str, Any] = {
        "payload": task.get("payload") or {},
        "target": target_str,
        "priority": int(task["priority"]),
    }
    if task.get("claim_timeout_seconds") is not None:
        body["claim_timeout_seconds"] = int(task["claim_timeout_seconds"])
    if (
        task.get("delivery_mode") == "push"
        and target_str.startswith("worker:")
        and "," not in target_str
    ):
        body["delivery_mode"] = "push"
        body["target_worker_id"] = target_str[len("worker:") :]
    return body


def _last_attempt_worker_id(executions: list[dict[str, Any]]) -> str | None:
    """Returns the worker_id of the most recent attempt (finished_at or
    started_at). If none started, returns None.
    """
    candidates = [
        e for e in executions if e.get("started_at") is not None or e.get("finished_at") is not None
    ]
    if not candidates:
        return None

    def _key(e: dict[str, Any]) -> datetime:
        ts = e.get("finished_at") or e.get("started_at")
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ts

    candidates.sort(key=_key, reverse=True)
    wid = candidates[0].get("worker_id")
    return str(wid) if wid else None


async def _render_execution_output(
    *,
    request: Request,
    session: SessionDep,
    ctx: WorkspaceDep,
    execution: dict,
    task_id: int,
    worker_id: str,
    poll_base: str,
    partial: bool,
    after: int,
) -> HTMLResponse:
    """Shared render of the event-based output view.

    Polls incrementally by `seq`: fetches only events with `seq > after`
    (at most one page, see `_OUTPUT_EVENTS_PAGE_SIZE`), formats them
    (interleaved in reading order) and builds the `poll_url` with the new
    cursor. `partial` returns only the polling fragment (new lines
    + poller + status OOB); otherwise, the full page.

    The poller is emitted if the execution is still alive (`every 2s` cadence) or if
    a page is pending (`load delay:100ms` to catch up immediately,
    also applies to terminal executions with a backlog).
    """
    # LIMIT page+1: the extra row only signals that there are more; it is not rendered.
    ev_rows = (
        (
            await session.execute(
                _SQL_GET_OUTPUT_EVENTS,
                {
                    "execution_id": execution["id"],
                    "after": after,
                    "limit": _OUTPUT_EVENTS_PAGE_SIZE + 1,
                },
            )
        )
        .mappings()
        .all()
    )
    has_more = len(ev_rows) > _OUTPUT_EVENTS_PAGE_SIZE
    events = [_output_event_view(r) for r in ev_rows[:_OUTPUT_EVENTS_PAGE_SIZE]]
    new_after = events[-1]["seq"] if events else after
    live = execution["status"] not in _TERMINAL_EXEC_STATUSES
    poll_url = f"{poll_base}?partial=1&after={new_after}"
    poll_trigger = "load delay:100ms" if has_more else "every 2s"
    template = "_execution_output_poll.html" if partial else "execution_output.html"
    header = {} if partial else await _workspace_header(session, ctx)
    return templates.TemplateResponse(
        request,
        template,
        {
            **header,
            "task_id": task_id,
            "worker_id": worker_id,
            "execution": execution,
            "events": events,
            "live": live,
            "has_more": has_more,
            "poll_trigger": poll_trigger,
            "poll_url": poll_url,
            "sse_enabled": settings.sse_enabled,
            "execution_id": execution["id"],
        },
    )


# auth: gated by require_workspace (session + membership); role-based authz (owner/member) TODO
@router.get(
    "/tasks/{task_id}/executions/{worker_id}/output",
    response_class=HTMLResponse,
)
async def admin_execution_output(
    task_id: int,
    worker_id: str,
    request: Request,
    session: SessionDep,
    ctx: WorkspaceDep,
    partial: bool = False,
    after: int = 0,
) -> HTMLResponse:
    row = (
        (
            await session.execute(
                _SQL_GET_EXECUTION,
                {
                    "task_id": task_id,
                    "worker_id": worker_id,
                    "workspace_id": ctx.workspace_id,
                },
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="execution not found")
    execution = dict(row)
    return await _render_execution_output(
        request=request,
        session=session,
        ctx=ctx,
        execution=execution,
        task_id=task_id,
        worker_id=worker_id,
        poll_base=f"/admin/tasks/{task_id}/executions/{worker_id}/output",
        partial=partial,
        after=after,
    )


# auth: gated by require_workspace (session + membership); the control buttons are
# rendered only for owners (`is_owner`), matching the owner-only control endpoints.
@router.get("/workers/{worker_id}", response_class=HTMLResponse)
async def admin_worker_detail(
    worker_id: str, request: Request, session: SessionDep, ctx: WorkspaceDep
) -> HTMLResponse:
    worker_row = (
        (
            await session.execute(
                _SQL_GET_WORKER, {"id": worker_id, "workspace_id": ctx.workspace_id}
            )
        )
        .mappings()
        .first()
    )
    if worker_row is None:
        raise HTTPException(status_code=404, detail="worker not found")
    exec_rows = (
        (
            await session.execute(
                _SQL_WORKER_EXECUTIONS, {"id": worker_id, "workspace_id": ctx.workspace_id}
            )
        )
        .mappings()
        .all()
    )
    throughput = (
        await session.execute(
            _SQL_WORKER_THROUGHPUT, {"id": worker_id, "workspace_id": ctx.workspace_id}
        )
    ).mappings().first() or {"active_count": 0, "recent_count": 0}
    # Liveness dot: age of the last heartbeat in seconds, via the helper shared
    # with the deck. The template classifies it into three bands (worker_detail.html).
    heartbeat_age_seconds = _heartbeat_age_seconds(
        worker_row.get("last_heartbeat_at"), datetime.now(UTC)
    )
    # State of the Clear cache button. `pending_raw` is "requested and not yet completed"
    # (or re-requested after the last flush). If it has gone more than CACHE_FLUSH_TTL without
    # ack it is considered "timed out" (old worker that never acks). The
    # timed-out state is derived from age and does NOT mutate the DB: it re-enables the button
    # and removes "Cleaning cache…". A new "Clear cache" re-stamps `requested_at`.
    req_at = worker_row.get("cache_flush_requested_at")
    done_at = worker_row.get("cache_flush_done_at")
    pending_raw = req_at is not None and (done_at is None or done_at < req_at)
    cache_flush_timed_out = False
    if pending_raw:
        req_at_aware = req_at if req_at.tzinfo is not None else req_at.replace(tzinfo=UTC)
        cache_flush_timed_out = (datetime.now(UTC) - req_at_aware) > CACHE_FLUSH_TTL
    cache_flush_pending = pending_raw and not cache_flush_timed_out
    cache_flush_done_at = done_at if (done_at is not None and not pending_raw) else None
    # Server-side disk gate: closes the
    # observability gap left by the worker-side gate. A gated worker looked
    # `online` and idle, indistinguishable from a healthy one with no work. The flag is
    # derived from the reported fraction against the central threshold. NULL or threshold
    # 0.0 → not gated.
    floor = settings.worker_claim_min_free_fraction
    frac = worker_row.get("free_disk_fraction")
    disk_gated = floor > 0.0 and frac is not None and frac < floor
    return templates.TemplateResponse(
        request,
        "worker_detail.html",
        {
            **await _workspace_header(session, ctx),
            "worker": dict(worker_row),
            "executions": [dict(r) for r in exec_rows],
            "throughput": dict(throughput),
            "heartbeat_age_seconds": heartbeat_age_seconds,
            "cache_flush_pending": cache_flush_pending,
            "cache_flush_timed_out": cache_flush_timed_out,
            "cache_flush_requested_at": req_at,
            "cache_flush_done_at": cache_flush_done_at,
            "disk_gated": disk_gated,
            "claim_min_free_fraction": floor,
            # Worker control is owner-only: members get no control buttons.
            "is_owner": ctx.role == "owner",
        },
    )


# --- Execution output by UUID -----------------------------------
#
# Endpoint that uses the UUID PK of task_executions.
# It resolves each attempt in a stable way (unambiguous with N attempts of the same
# worker). The attempts table in `task_detail.html` links here using
# `execution.id` directly. The legacy endpoint
# (`/tasks/{task_id}/executions/{worker_id}/output`) is kept so as not to
# break bookmarks or external integrations; it resolves to the last attempt of the
# (task, worker) pair — behavior known as "history-loss".


# auth: gated by require_workspace (session + membership); role-based authz (owner/member) TODO
@router.get(
    "/executions/{execution_id}/output",
    response_class=HTMLResponse,
)
async def admin_execution_output_by_uuid(
    execution_id: str,
    request: Request,
    session: SessionDep,
    ctx: WorkspaceDep,
    partial: bool = False,
    after: int = 0,
) -> HTMLResponse:
    """Resolves an attempt by its stable UUID.

    Returns the same template as the legacy endpoint (or only the polling
    fragment if `partial`). The output is served from `output_events`
    with incremental polling by `seq > after`.
    """
    # Validate the UUID format before touching the DB; a malformed UUID must
    # return 404 (not a 500 from a unique_violation/pg parse error). Normalize
    # to the canonical form: `UUID()` accepts variants (`urn:uuid:`, braces) that
    # the Postgres CAST rejects with DataError.
    try:
        execution_id = str(UUID(execution_id))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=404, detail="execution not found") from exc

    row = (
        (
            await session.execute(
                _SQL_GET_EXECUTION_BY_UUID,
                {"execution_id": execution_id, "workspace_id": ctx.workspace_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="execution not found")
    execution = dict(row)
    return await _render_execution_output(
        request=request,
        session=session,
        ctx=ctx,
        execution=execution,
        task_id=execution["task_id"],
        worker_id=execution["worker_id"],
        poll_base=f"/admin/executions/{execution_id}/output",
        partial=partial,
        after=after,
    )


# --- Full output download ------------------------
#
# Generates the file on the fly from `output_events` — with no persisted
# materialization (materializing only pays off once `output_events` has retention/TTL:
# materialize before purging). The snapshot is fixed with MAX(seq) at the start: if the
# execution is still alive, what exists up to that moment is downloaded, and the
# download ends even if the worker keeps emitting events. Each batch uses its own
# short session (not the request's): a slow client downloading GBs does not
# hold a pool connection hostage.


# auth: gated by require_workspace (session + membership); role-based authz (owner/member) TODO
@router.get("/executions/{execution_id}/output/download")
async def admin_execution_output_download(
    execution_id: str,
    session: SessionDep,
    ctx: WorkspaceDep,
    format: Literal["text", "jsonl"] = "text",
) -> StreamingResponse:
    """Downloads the full output of an execution as a gzip file.

    `text` (default): raw concatenation of the `text` values in `seq` order
    (stdout+stderr interleaved, faithful to the terminal, no prefixes). `jsonl`: one
    JSON line per event `{seq, ts, stream, text}`. Transit uses
    `Content-Encoding: gzip`; browsers and `curl --compressed` deliver the file
    already decompressed. Invalid `format` → 422 (FastAPI validation).
    """
    # Resolves+authorizes by UUID-in-workspace (404 if malformed or cross-tenant)
    # and normalizes to the canonical form, sanitizing the filename of the
    # Content-Disposition by construction along the way. Same helper as the v1 endpoint.
    execution_id = await resolve_execution_in_workspace(
        session, workspace_id=ctx.workspace_id, execution_id=execution_id
    )
    # Release the request connection now: the generator uses its own short sessions.
    await session.rollback()
    return stream_output_gzip(execution_id, format)


# --- Artifact download ---------------------------------------


async def _resolve_execution_scoped(
    session: AsyncSession, ctx: WorkspaceContext, execution_id: str
) -> dict:
    """Normalizes the UUID and validates the workspace. 404 if malformed or cross-tenant."""
    try:
        execution_id = str(UUID(execution_id))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=404, detail="execution not found") from exc
    row = (
        (
            await session.execute(
                _SQL_GET_EXECUTION_BY_UUID,
                {"execution_id": execution_id, "workspace_id": ctx.workspace_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="execution not found")
    return {**dict(row), "id": execution_id}


# auth: gated by require_workspace (session + membership)
@router.get("/executions/{execution_id}/artifacts")
async def admin_execution_artifacts_list(
    execution_id: str,
    session: SessionDep,
    ctx: WorkspaceDep,
) -> list[dict[str, Any]]:
    """Lists (JSON) the artifacts of an execution.

    Non-null `collected_at` = the bytes were purged from disk after being
    backed up; the row remains as evidence and the download
    answers `410`.
    """
    execution = await _resolve_execution_scoped(session, ctx, execution_id)
    rows = (
        (await session.execute(_SQL_LIST_ARTIFACTS, {"execution_id": execution["id"]}))
        .mappings()
        .all()
    )
    return [
        {
            "id": r["id"],
            "rel_path": r["rel_path"],
            "size_bytes": r["size_bytes"],
            "sha256": r["sha256"],
            "content_type": r["content_type"],
            "created_at": r["created_at"].isoformat(),
            "collected_at": (
                r["collected_at"].isoformat() if r["collected_at"] is not None else None
            ),
        }
        for r in rows
    ]


def _collected_detail(collected_at: datetime) -> str:
    """Detail of the `410 Gone` of a collected artifact.

    Distinguishes the legitimate case ("the bytes were purged because they are
    already backed up") from the `404 artifact bytes missing`, which stays reserved
    for a real disk↔DB inconsistency. The date is in UTC, like every
    timestamp the server emits; the UI rewrites it to local time.
    """
    return (
        f"artifact collected on {collected_at.date().isoformat()} (UTC): "
        "bytes were purged after being backed up to external storage"
    )


# auth: gated by require_workspace (session + membership)
@router.get("/executions/{execution_id}/artifacts/{artifact_id}/download")
async def admin_execution_artifact_download(
    execution_id: str,
    artifact_id: int,
    session: SessionDep,
    ctx: WorkspaceDep,
) -> StreamingResponse:
    """Downloads an artifact by id, streaming from the filesystem."""
    execution = await _resolve_execution_scoped(session, ctx, execution_id)
    row = (
        (
            await session.execute(
                _SQL_GET_ARTIFACT_BY_ID,
                {
                    "artifact_id": artifact_id,
                    "execution_id": execution["id"],
                    "workspace_id": ctx.workspace_id,
                },
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    # Collected: the mark is the source of truth of the contract and is
    # evaluated BEFORE looking at the disk. If residual bytes remained from a half-done
    # purge, serving them would contradict the quota (which no longer counts them) and
    # would hide the real state of the row.
    if row["collected_at"] is not None:
        raise HTTPException(status_code=410, detail=_collected_detail(row["collected_at"]))
    abs_path = artifact_abs_path(settings.artifacts_root, execution["id"], row["rel_path"])
    if not abs_path.is_file():
        raise HTTPException(status_code=404, detail="artifact bytes missing")
    # validate_rel_path (on upload) rejects `..`/backslash/absolute paths but not
    # quotes, CR/LF or non-ASCII characters; sanitizing the basename avoids a
    # malformed Content-Disposition or header injection with an adversarially named artifact.
    # RFC 6266: emit filename= (ASCII, fallback) + filename*=
    # (UTF-8 percent-encoded) so modern browsers show the real name
    # and old ones get a readable version.
    raw_name = PurePosixPath(row["rel_path"]).name
    ascii_name = (
        raw_name.replace('"', "_")
        .replace("\r", "")
        .replace("\n", "")
        .encode("ascii", "replace")
        .decode("ascii")
    )
    disposition = (
        f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(raw_name, safe='')}"
    )
    return StreamingResponse(
        iter_artifact_bytes(abs_path),
        media_type=row["content_type"] or "application/octet-stream",
        headers={
            "Content-Disposition": disposition,
            "Content-Length": str(row["size_bytes"]),
        },
    )


class _StreamingZipSink:
    """Write-only, non-seekable file-like for `zipfile.ZipFile`.

    Accumulates what is written in a buffer and keeps its own monotonic `tell()`, so
    that `ZipFile` computes the central directory offsets correctly even though
    we drain the buffer between entries. `drain()` returns and empties what is pending.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._pos = 0

    def write(self, data: bytes) -> int:
        self._buf.extend(data)
        self._pos += len(data)
        return len(data)

    def tell(self) -> int:
        return self._pos

    def flush(self) -> None:  # ZipFile.close() calls it
        pass

    def drain(self) -> Iterator[bytes]:
        if self._buf:
            yield bytes(self._buf)
            self._buf.clear()


# auth: gated by require_workspace (session + membership)
@router.get("/executions/{execution_id}/artifacts.zip")
async def admin_execution_artifacts_zip(
    execution_id: str,
    session: SessionDep,
    ctx: WorkspaceDep,
) -> StreamingResponse:
    """Zip of all AVAILABLE artifacts of an execution, streaming.
    Preserves the rel_path as the path inside the zip."""
    execution = await _resolve_execution_scoped(session, ctx, execution_id)
    rows = (
        (await session.execute(_SQL_LIST_ARTIFACTS, {"execution_id": execution["id"]}))
        .mappings()
        .all()
    )

    # Collected ones are excluded from the zip: their row exists but their
    # bytes no longer do. Excluding them is NOT "silently omitting" (the case below): the
    # row explicitly declares that it was purged, and the listing and the
    # detail view keep showing it marked. If ALL are collected, there is no
    # zip to deliver and the 410 explains the cause (an empty zip or a 404 would lie).
    available = [r for r in rows if r["collected_at"] is None]
    if rows and not available:
        latest = max(r["collected_at"] for r in rows)
        raise HTTPException(
            status_code=410,
            detail=(
                f"all artifacts collected (last on {latest.date().isoformat()} UTC): "
                "bytes were purged after being backed up to external storage"
            ),
        )

    # Pre-validate the existence of ALL bytes BEFORE starting to stream.
    # A StreamingResponse has already emitted the 200 + headers as soon as the first
    # chunk is iterated; failing mid-stream would deliver a truncated zip with a
    # misleading 200. If any artifact is missing, abort here with 404 (consistent with the
    # individual download) instead of silently omitting it from the zip.
    paths = [
        (artifact_abs_path(settings.artifacts_root, execution["id"], r["rel_path"]), r["rel_path"])
        for r in available
    ]
    for abs_path, rel_path in paths:
        if not abs_path.is_file():
            raise HTTPException(status_code=404, detail=f"artifact bytes missing: {rel_path!r}")

    def _generate() -> Iterator[bytes]:
        # Non-seekable sink: `ZipFile` keeps its own offset counter
        # (`zf.start_dir`/`fp.tell()` proxied) when the stream is not seekable,
        # so draining (`drain`) what has been written so far does not break the offsets
        # of the central directory. Draining after each `write` bounds the memory
        # peak to one artifact. ZIP_STORED: no recompression (the bytes are already
        # on disk; the network already negotiates gzip separately if applicable).
        sink = _StreamingZipSink()
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED) as zf:
            for abs_path, rel_path in paths:
                zf.write(abs_path, arcname=rel_path)
                yield from sink.drain()
        yield from sink.drain()

    return StreamingResponse(
        _generate(),
        media_type="application/zip",
        headers={
            "Content-Disposition": (f'attachment; filename="artifacts-{execution["id"]}.zip"')
        },
    )


# ---------------------------------------------------------------------------
# SSE: real-time event streaming
# ---------------------------------------------------------------------------


def format_sse(message: SseMessage) -> str:
    """Serializes an SseMessage to the SSE protocol format.

    The protocol requires each line of the body to carry its own `data:`
    prefix (an unprefixed `\\n` ends the field and corrupts the frame). So
    we normalize line breaks (`\\r\\n`/`\\r` → `\\n`) and emit one
    `data:` per line. `data=""` produces a single empty `data:`, byte-for-byte
    identical to the signal frame the dashboard emits today.
    """
    normalized = message.data.replace("\r\n", "\n").replace("\r", "\n")
    data_block = "".join(f"data: {line}\n" for line in normalized.split("\n"))
    return f"event: {message.event}\n{data_block}\n"


@router.get("/events")
async def admin_events(request: Request, ctx: WorkspaceDep):
    """Dashboard SSE stream for the operator's active workspace.

    One connection per operator, multiplexed: it emits events named by
    entity (`output-<id>`, `task-<id>`, ...). The HTMX client uses
    `hx-trigger="sse:<event>"` to refresh the granular fragment. Gated by
    SSE_ENABLED: if it is off it returns 404 (the UI falls back to polling).
    """
    if not settings.sse_enabled:
        raise HTTPException(status_code=404, detail="sse disabled")

    # Lazy import: the bus lives in orcaster_server.main (per-process instance).
    from orcaster_server.main import sse_bus_instance

    # Optional topics filter (CSV, e.g. `output` or `task,worker`): the page
    # only consumes certain event types. Without the param → None (receives everything).
    topics_raw = request.query_params.get("topics")
    topics: frozenset[str] | None = None
    if topics_raw is not None:
        parsed = {t.strip() for t in topics_raw.split(",") if t.strip()}
        topics = frozenset(parsed) if parsed else None

    workspace_id = str(ctx.workspace_id)
    queue = sse_bus_instance.subscribe(workspace_id, topics=topics)
    heartbeat = max(1, int(settings.sse_heartbeat_interval_seconds))

    async def _generate():
        # Cleanup on disconnect is guaranteed by Starlette: when the
        # client disconnects it cancels the generator, which raises CancelledError in the
        # await and runs the finally block (unsubscribe). That is why an
        # explicit request.is_disconnected() check is not needed.
        try:
            yield ": connected\n\n"
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                    yield format_sse(message)
                except TimeoutError:
                    # Cloudflare anti-524 keepalive.
                    yield ": ping\n\n"
        finally:
            sse_bus_instance.unsubscribe(workspace_id, queue)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
