"""Matching of `tasks.eligibility` against `workers.capabilities`.

Both columns are persisted, and this module is the single source of the matching
contract: a task that declares capabilities is offered only to a worker that
satisfies them.

Contract
--------

Worker side (`workers.capabilities`, JSONB): a **flat** map from name to boolean
or number. Example: ``{"gpu": true, "gpu_memory": 8192}``. Nothing else: strings,
`null`, lists and nested objects are rejected with 422 at registration, and the poll
ignores them, keeping the stored value. Strings are excluded on purpose: they have
no total order, so there is no honest way to evaluate `>=` on them, and admitting
them would open two different semantics for the same operator.

Task side (`eligibility.capabilities`): a **list of strings**. Each entry is

- ``name``: the worker must declare `name` with a *truthy* value (`true`, or a
  nonzero number);
- ``name<op>value`` with `op` in ``>=``, ``<=``, ``==``, ``>``, ``<`` and a numeric
  `value`: the worker must declare `name` with a **numeric** value that
  satisfies the comparison.

A worker qualifies when it satisfies **all** the entries. `eligibility` without the
`capabilities` key (including `{}`) matches any worker.

`explicit > implicit`
---------------------

A requirement that this module cannot evaluate is **not** degraded to a silent "does
not match" at creation: `POST /admin/tasks`, `POST /api/v1/tasks` and the fork
respond 422 naming the invalid entry. At dispatch, in contrast, there is no
caller to respond to: an already persisted row with a `capabilities` that is not
a list, or with an entry that does not parse, **fails closed**: no worker
satisfies it. The alternative, ignoring the requirement, is exactly the defect
this module corrects.

The other `eligibility` keys (`min_ram_mb`, `os`, `tags_any`, ...) have no
reader and no validation: they are free-form JSON, outside the scope of this module.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

# A capability name must be expressible inside a requirement, so it cannot
# contain operator characters or spaces.
CAPABILITY_NAME_PATTERN = r"[A-Za-z_][A-Za-z0-9_.-]*"

# SINGLE requirement pattern, shared by the Python validation and by the dispatch
# SQL predicate. Sharing it is the point: two copies would drift and the server
# would accept at creation a requirement the claim cannot evaluate.
# Postgres ARE syntax and Python `re` coincide on everything used here,
# except the `$` anchor: in Python (`re.match`/`re.search`) it also accepts a
# trailing `\n`, while in Postgres (ARE without newline-sensitive mode) it only
# anchors at the end of the string. That is why the Python side ALWAYS uses
# `fullmatch`, which requires consuming the whole string, like `regexp_match`
# with `^...$`.
# Groups: 1 = name, 2 = operator (NULL if none), 3 = value.
CAPABILITY_REQUIREMENT_PATTERN = (
    rf"^({CAPABILITY_NAME_PATTERN})(?:(>=|<=|==|>|<)(-?[0-9]+(?:\.[0-9]+)?))?$"
)

_CAPABILITY_NAME_RE = re.compile(rf"^{CAPABILITY_NAME_PATTERN}$")
_CAPABILITY_REQUIREMENT_RE = re.compile(CAPABILITY_REQUIREMENT_PATTERN)


class EligibilityError(ValueError):
    """Invalid entry in `eligibility.capabilities` or in `capabilities`."""


def validate_worker_capabilities(value: Any) -> dict:
    """Validate a worker's capabilities map. Returns the same map.

    Raises `EligibilityError` with the name of the offending key.
    """
    if not isinstance(value, Mapping):
        raise EligibilityError(f"capabilities must be a JSON object, got {_kind(value)}")
    for name, declared in value.items():
        if not isinstance(name, str) or not _CAPABILITY_NAME_RE.fullmatch(name):
            raise EligibilityError(
                f"capability name {name!r} is not usable in an eligibility "
                f"requirement; it must match {CAPABILITY_NAME_PATTERN}"
            )
        # `bool` before `int`: in Python `True` is an `int`, and the map
        # distinguishes the two forms (presence vs. numeric comparison).
        if isinstance(declared, bool):
            continue
        if isinstance(declared, int):
            continue
        # NaN and Infinity: `json.loads` accepts them, but they are not valid JSON and
        # the JSONB INSERT would fail with a 500. They also have no useful order for
        # evaluating `>=`. They are rejected here, with the key name.
        if isinstance(declared, float):
            if not math.isfinite(declared):
                raise EligibilityError(
                    f"capability {name!r} must be a finite number, got {declared!r}"
                )
            continue
        raise EligibilityError(
            f"capability {name!r} must be a boolean or a number, got {_kind(declared)}"
        )
    return dict(value)


def validate_eligibility(value: Any) -> dict:
    """Validate `eligibility`. Only interprets the `capabilities` key.

    Returns the same dict. Raises `EligibilityError` naming the invalid entry.
    """
    if not isinstance(value, Mapping):
        raise EligibilityError(f"eligibility must be a JSON object, got {_kind(value)}")
    if "capabilities" not in value:
        return dict(value)
    requirements = value["capabilities"]
    if not isinstance(requirements, list):
        raise EligibilityError(
            "eligibility.capabilities must be a list of requirement strings "
            "(e.g. ['gpu', 'gpu_memory>=5200']), got " + _kind(requirements)
        )
    for entry in requirements:
        parse_capability_requirement(entry)
    return dict(value)


def parse_capability_requirement(entry: Any) -> tuple[str, str | None, float | None]:
    """`'gpu_memory>=5200'` → `('gpu_memory', '>=', 5200.0)`.

    `'gpu'` → `('gpu', None, None)`. Anything else raises
    `EligibilityError`.
    """
    if not isinstance(entry, str):
        raise EligibilityError(
            f"eligibility.capabilities entries must be strings, got {_kind(entry)}"
        )
    m = _CAPABILITY_REQUIREMENT_RE.fullmatch(entry)
    if m is None:
        raise EligibilityError(
            f"eligibility.capabilities entry {entry!r} is not a valid requirement: "
            "expected 'name' or 'name<op>number' with op in >=, <=, ==, >, <"
        )
    name, op, raw_value = m.group(1), m.group(2), m.group(3)
    return name, op, float(raw_value) if raw_value is not None else None


def _kind(value: Any) -> str:
    return type(value).__name__


def capability_match_sql(*, requirements: str, capabilities: str) -> str:
    """SQL predicate "this worker satisfies `eligibility.capabilities`".

    `requirements` is a SQL expression that evaluates to the JSONB value of
    `eligibility -> 'capabilities'` (NULL if the task does not declare the key);
    `capabilities` is a SQL expression that evaluates to the candidate worker's
    JSONB `capabilities`. Both are interpolated at build time from literals in the
    code; they never carry user input.

    Single source of the matching in the three paths that offer work
    (`_SQL_POLL_PICK_PUSH`, `_SQL_POLL_CLAIM_OPEN_LOCK` and the two
    `_SQL_INSERT_SNAPSHOT*`). It lives in SQL and not in Python because the open-claim
    picks the candidate with `ORDER BY priority DESC, run_at LIMIT 1 FOR UPDATE
    SKIP LOCKED`: filtering after picking would leave the worker without trying the
    next task and would reopen the starvation that the pick already solved.

    Each `COALESCE(..., false)` is deliberate: if the worker does not declare the
    capability, `jsonb_typeof` gives NULL, the comparison gives NULL, the clause's
    `NOT` would give NULL and the row would NOT enter the `EXISTS`, that is, the
    requirement would be considered met. Explicit fail-closed.
    """
    req = f"({requirements})"
    caps = f"({capabilities})"
    # `CAST(... AS ...)` and not the double-colon syntax: the `sqlalchemy.text()`
    # lexer mistakes `::type` for a bind param (same reason as the
    # `cast(:worker_id AS text)` in `_SQL_POLL_CLAIM_OPEN_LOCK`).
    numeric = f"CAST({caps} ->> p.m[1] AS numeric)"
    bound = "CAST(p.m[3] AS numeric)"
    return f"""
        CASE
          -- Without the `capabilities` key: matches everything.
          WHEN {req} IS NULL THEN true
          -- Unknown shape (e.g. an object): fails closed. Creation already
          -- rejects it with 422; this covers rows persisted without validation.
          WHEN jsonb_typeof({req}) <> 'array' THEN false
          ELSE NOT EXISTS (
            SELECT 1
              FROM jsonb_array_elements_text({req}) AS req(spec)
              LEFT JOIN LATERAL (
                     SELECT regexp_match(req.spec, '{CAPABILITY_REQUIREMENT_PATTERN}') AS m
                   ) p ON true
             WHERE NOT COALESCE(
                CASE
                  -- Requirement that does not parse: nobody satisfies it.
                  WHEN p.m IS NULL THEN false
                  -- Bare `name`: presence with a truthy value.
                  WHEN p.m[2] IS NULL THEN
                    CASE jsonb_typeof({caps} -> p.m[1])
                      WHEN 'boolean' THEN ({caps} -> p.m[1]) = CAST('true' AS jsonb)
                      WHEN 'number' THEN {numeric} <> 0
                      ELSE false
                    END
                  -- `name<op>value`: requires a NUMERIC value on the worker.
                  WHEN jsonb_typeof({caps} -> p.m[1]) IS DISTINCT FROM 'number' THEN false
                  WHEN p.m[2] = '>=' THEN {numeric} >= {bound}
                  WHEN p.m[2] = '<=' THEN {numeric} <= {bound}
                  WHEN p.m[2] = '==' THEN {numeric} = {bound}
                  WHEN p.m[2] = '>' THEN {numeric} > {bound}
                  WHEN p.m[2] = '<' THEN {numeric} < {bound}
                  ELSE false
                END, false)
          )
        END
    """.strip()
