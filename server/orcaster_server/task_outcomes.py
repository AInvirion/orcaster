"""Generic terminal outcomes declared by the task.

An *outcome* is the nuance of a terminal close: the ``(name, closes_as)`` pair
derived from the command's exit code. The task declares it in its payload::

    {"outcomes": {"exit_codes": {"3": {"name": "empty_result", "closes_as": "completed"}}}}

- ``name``: slug of the outcome (``outcome`` column in tasks/task_executions).
- ``closes_as``: the REAL terminal status it is equivalent to (``completed`` | ``failed``).

The server classifies in ``/complete`` (the worker is unchanged: it reports ``failed`` +
exit_code for any exit != 0). Precedence: the payload map wins per exit_code; if it
does not declare that code, the default map **of the task's tool** applies, which comes
from the outcome profile of the plugin registry (``orcaster_server/plugins.py``,
``add_outcome_defaults``); if there is none either, a plain ``failed``.

The core ships no default map: without a plugin that contributes a profile, only
``payload.outcomes`` classifies an exit code. The scope is per tool: a server-wide map
would interpret one tool's exit code contract wherever it came from, and low codes
collide with common uses (curl's exit 3, 4 and 7 are "malformed URL", "could not
resolve host" and "could not connect"). The tool is derived from the payload with
``task_variant.extract_variant``, the same function that writes ``tasks.variant_kind``:
a row that a default labels is always a row that the variant lens shows with that
default's kind.

This module is pure (no DB), mirroring the ``task_variant`` pattern.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from orcaster_server.models import EXECUTION_STATUS_VALUES, TASK_STATUS_VALUES
from orcaster_server.plugins import LegacyStatusAlias, OutcomeRule, get_registry
from orcaster_server.task_variant import extract_variant

# Outcome slug: lowercase, starts with a letter, up to 64 chars.
OUTCOME_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# Terminal statuses an outcome can be equivalent to. Closed string enum,
# extensible later (e.g. an `expired`-style disposition that does not consume
# budget) without breaking existing payloads.
CLOSES_AS_VALUES = ("completed", "failed")

# An outcome cannot be named like a status (task or execution) nor like the
# filter token `other` (= NULL outcome in the UI): this avoids ambiguity in filters.
RESERVED_OUTCOME_NAMES = (
    frozenset(TASK_STATUS_VALUES) | frozenset(EXECUTION_STATUS_VALUES) | {"other"}
)


def legacy_status_aliases() -> Mapping[str, LegacyStatusAlias]:
    """Retired status aliases from the active outcome profile (token -> alias).

    Empty without plugins: a retired token then gets the same response as any
    unknown status on the route that receives it.
    """
    return get_registry().legacy_status_aliases


def parse_outcomes_block(payload: dict | None) -> dict[int, OutcomeRule]:
    """Parse and validate ``payload['outcomes']`` -> ``{exit_code: OutcomeRule}``.

    ``{}`` if the payload is not a dict or has no block. ``ValueError`` (actionable
    message) if the shape is invalid; the create/fork schema turns it into a 422.
    Exit 0 is NOT remappable: it is always a plain ``completed``.
    """
    if not isinstance(payload, dict):
        return {}
    block = payload.get("outcomes")
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise ValueError("payload.outcomes must be an object")
    extra = set(block) - {"exit_codes"}
    if extra:
        raise ValueError(f"payload.outcomes has unknown keys: {sorted(extra)}")
    exit_codes = block.get("exit_codes")
    if exit_codes is None:
        return {}
    if not isinstance(exit_codes, dict):
        raise ValueError("payload.outcomes.exit_codes must be an object")

    rules: dict[int, OutcomeRule] = {}
    for raw_code, raw_rule in exit_codes.items():
        # Exit codes are integers: the key is normalized to canonical decimal
        # instead of being rejected. isdecimal() accepts ASCII and Unicode digits
        # ("٣") and guarantees int() will not fail (it rules out "²", "+3", " 3 ").
        # Leading zeros ("03") normalize to 3. The only ambiguity is two distinct
        # JSON keys landing on the same exit code (the last would overwrite): that
        # is rejected explicitly below.
        if not (isinstance(raw_code, str) and raw_code.isdecimal() and 1 <= int(raw_code) <= 255):
            raise ValueError(
                f"payload.outcomes.exit_codes key {raw_code!r} must be an integer "
                "string in 1-255 (exit 0 is always plain 'completed')"
            )
        code = int(raw_code)
        if code in rules:
            raise ValueError(
                f"payload.outcomes.exit_codes key {raw_code!r} normalizes to exit code "
                f"{code}, already declared by another key"
            )
        if not isinstance(raw_rule, dict):
            raise ValueError(f"payload.outcomes.exit_codes[{raw_code!r}] must be an object")
        extra_rule = set(raw_rule) - {"name", "closes_as"}
        if extra_rule:
            raise ValueError(
                f"payload.outcomes.exit_codes[{raw_code!r}] has unknown keys: {sorted(extra_rule)}"
            )
        name = raw_rule.get("name")
        closes_as = raw_rule.get("closes_as")
        if not (isinstance(name, str) and OUTCOME_NAME_RE.match(name)):
            raise ValueError(
                f"payload.outcomes.exit_codes[{raw_code!r}].name must match "
                "^[a-z][a-z0-9_]{0,63}$"
            )
        if name in RESERVED_OUTCOME_NAMES:
            raise ValueError(
                f"payload.outcomes.exit_codes[{raw_code!r}].name {name!r} is reserved "
                "(status values and 'other' are not valid outcome names)"
            )
        if closes_as not in CLOSES_AS_VALUES:
            raise ValueError(
                f"payload.outcomes.exit_codes[{raw_code!r}].closes_as must be one of "
                f"{list(CLOSES_AS_VALUES)}"
            )
        rules[code] = OutcomeRule(name=name, closes_as=closes_as)
    return rules


def classify_terminal(
    reported_status: str, exit_code: int | None, payload: dict | None
) -> tuple[str, str | None]:
    """Translate (reported status, exit_code, payload) -> ``(final status, outcome)``.

    Only reclassifies ``failed`` reports with an exit_code: the worker uses
    ``failed`` for any exit != 0 and the server decides the semantics. Other
    statuses pass through untouched with outcome ``None``. A malformed block in an
    old task is ignored silently (/complete must never fail because of it): the
    default of its tool applies, if it has one. Pure and side-effect free:
    testable in isolation.
    """
    if reported_status != "failed" or exit_code is None:
        return (reported_status, None)
    try:
        rules = parse_outcomes_block(payload)
    except ValueError:
        rules = {}
    rule = rules.get(exit_code) or _default_rule(exit_code, payload)
    if rule is None:
        return ("failed", None)
    return (rule.closes_as, rule.name)


def _default_rule(exit_code: int, payload: dict | None) -> OutcomeRule | None:
    """Default rule for ``exit_code`` according to the tool that produced the task.

    ``None`` if the tool is not recognized (variant ``other``) or the plugin registry
    gave it no default map: that close is a plain ``failed``. The kind comes from
    ``extract_variant``, the same function that writes ``tasks.variant_kind``, so there
    is no second parse of the payload to keep in sync with the first.
    """
    kind, _label = extract_variant(payload)
    if kind is None:
        return None
    defaults = get_registry().outcome_defaults.get(kind)
    if defaults is None:
        return None
    return defaults.get(exit_code)
