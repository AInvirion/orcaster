"""Dispatch gate for tasks with stored secrets, by worker version.

A worker older than the central secrets support parses the poll response key by
key and ignores `secrets`: it would run the task with its own `EXTRA_ENV`, or
without the variable, while the server audited a delivery that did not happen.
Product decision: the server does not deliver a task that uses stored secrets to
a worker below the floor. The task stays `queued` for that worker and one that
does support them takes it; in `all_available` that worker is left out of the
snapshot.

Single source of the two halves of the predicate, like `capability_match_sql`
in `orcaster_server/eligibility.py`:

- `payload_uses_stored_secrets_sql` / `task_uses_stored_secrets_sql`: "the
  task declares in `payload.task_env` a name with a secret stored in its
  workspace". Mirrors `service.declared_task_env` (only strings of a list;
  any other shape declares nothing).
- `payload_declares_task_env_sql`: "the task declares a name in
  `payload.task_env`", without looking at `task_secrets`. Used by housekeeping,
  which cannot depend on rows that the operator deletes to unblock the task.
- `worker_supports_task_secrets` / `worker_supports_task_secrets_sql`: "the
  worker version reaches the floor". Both versions (Python and SQL) have the
  same semantics; a parity test runs them over the same table.

The SQL fragments are interpolated at build time from literals in the code,
never from user input.
"""

from __future__ import annotations

import re

# Worker version floor to receive tasks that use stored secrets. It names the
# release that added central secrets support to the worker: the first one that
# reads `secrets` from the poll response.
TASK_SECRETS_MIN_WORKER_VERSION: tuple[int, int, int] = (0, 47, 0)

TASK_SECRETS_MIN_WORKER_VERSION_STR = ".".join(str(p) for p in TASK_SECRETS_MIN_WORKER_VERSION)

# `MAJOR.MINOR.PATCH` prefix, with an optional `v`. Whatever follows is ignored:
# versions may carry suffixes (`+patched`, `+unknown`, see
# `client/src/orcaster_worker/__init__.py::package_version`). `[0-9]` and not
# `\d`: in Python `\d` accepts Unicode digits and in Postgres it does not. `[.]`
# and not `\.`, so that the same literal works in the SQL string without escapes.
_VERSION_PREFIX_PATTERN = "^v?([0-9]+)[.]([0-9]+)[.]([0-9]+)"
_VERSION_PREFIX_RE = re.compile(_VERSION_PREFIX_PATTERN)
# In SQL a single group with the three numbers: `substring(... from ...)`
# returns the first group and `string_to_array` splits it on the dot.
_VERSION_PREFIX_SQL_PATTERN = "^v?([0-9]+[.][0-9]+[.][0-9]+)"


# Window, in seconds, during which the last secrets refusal to a worker counts
# as recent (`workers.task_secrets_refused_at`, migration 0060). While it lasts:
# the open-claim fairness yield does not give up to it the tasks that use stored
# secrets (`api/poll.py`) and housekeeping does not expire its pre-assigned
# tasks of that kind (`housekeeping.py`). Longer than the idle poll cadence
# (30 s) and than the interval at which the poll renews the mark.
TASK_SECRETS_REFUSAL_WINDOW_SECONDS = 120


def worker_recently_refused_sql(refused_at_ref: str) -> str:
    """SQL predicate "a secrets delivery to the worker was refused recently".

    `refused_at_ref` is an SQL expression that evaluates to its
    `task_secrets_refused_at` (may be NULL: never refused, false).
    """
    return (
        f"COALESCE({refused_at_ref} > now() - make_interval("
        f"secs => {TASK_SECRETS_REFUSAL_WINDOW_SECONDS}), false)"
    )


def worker_supports_task_secrets(version: str | None) -> bool:
    """Does the worker version reach `TASK_SECRETS_MIN_WORKER_VERSION`?

    Fails closed: `None`, empty or a version without a `MAJOR.MINOR.PATCH`
    prefix gives `False`. It is not known whether that worker reads `secrets`,
    and delivering the task to it is the defect this gate prevents.
    """
    if not isinstance(version, str):
        return False
    m = _VERSION_PREFIX_RE.match(version.strip())
    if m is None:
        return False
    parsed = tuple(int(g) for g in m.groups())
    return parsed >= TASK_SECRETS_MIN_WORKER_VERSION


def worker_supports_task_secrets_sql(version_ref: str) -> str:
    """SQL predicate equivalent to `worker_supports_task_secrets`.

    `version_ref` is an SQL expression that evaluates to the worker version
    (`text`, may be NULL). Comparison of `numeric[]` arrays and not `int[]`:
    a huge number in the version cannot overflow and make the whole poll
    statement fail. `COALESCE(..., false)`: NULL or a version that does not
    parse (the `substring` gives NULL) does not reach the floor.
    """
    floor = ", ".join(str(p) for p in TASK_SECRETS_MIN_WORKER_VERSION)
    # `CAST(... AS ...)` and not the double-colon syntax: the lexer of
    # `sqlalchemy.text()` mistakes `::type` for a bind param.
    return (
        "COALESCE("
        "CAST(string_to_array("
        f"substring(btrim({version_ref}) from '{_VERSION_PREFIX_SQL_PATTERN}'), '.'"
        ") AS numeric[]) "
        f">= CAST(ARRAY[{floor}] AS numeric[]), false)"
    )


def payload_uses_stored_secrets_sql(*, payload_ref: str, workspace_ref: str) -> str:
    """SQL predicate "the payload declares a name with a stored secret".

    `payload_ref` is an SQL expression that evaluates to the task's JSONB
    `payload` and `workspace_ref` one that evaluates to its `workspace_id`.
    True if any name in `payload.task_env` has a row in `task_secrets` for that
    workspace. Mirrors `service.declared_task_env`: only a list declares names,
    and `@>` over `jsonb_build_array(name)` matches only equal string elements.
    A task without `task_env` (legacy mode) does not use server secrets.

    Scope: it counts rows of any scope, workspace-wide or of any worker, without
    looking at `s.worker_id`. This is deliberately conservative: a worker below
    the floor is left out of every task whose name has any stored row, even if
    the row belongs to another worker and would not apply to it. That way the
    predicate does not depend on the worker making the poll (the `all_available`
    snapshots do not have it) and never delivers to an old worker a task that
    another capable one would have received with its secret.

    Single source of the "task" half of the gate: the poll picks use it (via
    `task_uses_stored_secrets_sql`) and so do the `all_available` snapshots,
    which read the payload of the task just inserted or re-queued.
    """
    payload = f"({payload_ref})"
    return (
        "EXISTS ("
        "SELECT 1 FROM task_secrets s "
        f"WHERE s.workspace_id = ({workspace_ref}) "
        f"AND jsonb_typeof({payload} -> 'task_env') = 'array' "
        f"AND {payload} -> 'task_env' @> jsonb_build_array(s.name))"
    )


def payload_declares_task_env_sql(payload_ref: str) -> str:
    """SQL predicate "the payload declares a name in `task_env`".

    `payload_ref` is an SQL expression that evaluates to the task's JSONB
    `payload`. Mirrors `service.declared_task_env`: only a list with some string
    element declares names (an empty list, another shape or a payload without
    `task_env` declare nothing). It does not query `task_secrets`: it is a
    property of the payload, which does not change when a secret is set or
    deleted.

    Housekeeping uses it to decide whether the pre-assigned task of a refused
    worker may have been refused because of its secrets. With
    `task_uses_stored_secrets_sql` the exemption stopped applying as soon as the
    operator deleted the last secret (the documented way to unblock the task)
    while the worker's mark was still fresh. The `CASE` keeps
    `jsonb_array_elements` from failing on a `task_env` that is not a list.
    """
    env = f"({payload_ref}) -> 'task_env'"
    return (
        f"CASE WHEN jsonb_typeof({env}) = 'array' THEN EXISTS ("
        f"SELECT 1 FROM jsonb_array_elements({env}) AS declared(name) "
        "WHERE jsonb_typeof(declared.name) = 'string') ELSE false END"
    )


def task_uses_stored_secrets_sql(task_ref: str) -> str:
    """SQL predicate "the task uses stored secrets of its workspace".

    `task_ref` is the alias of a `tasks` row (e.g. `t`). Delegates to
    `payload_uses_stored_secrets_sql` with the payload and workspace of that row.
    """
    return payload_uses_stored_secrets_sql(
        payload_ref=f"{task_ref}.payload", workspace_ref=f"{task_ref}.workspace_id"
    )
