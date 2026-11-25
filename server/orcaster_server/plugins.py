"""Server plugin registry: variant extractors and outcome profile.

The core knows nothing about any specific tool: what recognizes a task payload
(the *variant*, ``(kind, label)``) and what each exit code means (the *outcome
profile*) are supplied by **providers** that register themselves in a
``PluginRegistry``. This module is pure (no DB) and does not import from the rest
of the app, so that ``task_variant`` and ``task_outcomes`` can depend on it
without cycles.

A provider is an object with ``register(registry)`` or directly a callable
``register(registry)``. It calls:

- ``registry.add_variant_extractor(kind, fn)``: ``fn`` is ``payload -> label | None``.
  The KIND is declared by the registration, not by the function.
- ``registry.add_outcome_defaults(kind, {exit_code: OutcomeRule})``: the default map
  of exit codes for the tasks of that kind. The kind must have an extractor.
- ``registry.add_legacy_status_alias(status_token, outcome_name, status="completed")``:
  a retired status token that the route filters keep accepting (saved URLs) and
  that is translated to ``status`` + ``outcome=outcome_name``. It is part of the
  outcome profile: without a plugin there is no alias.

Discovery (``load_registry``):

1. Entry points of the ``orcaster.plugins`` group (``ENTRY_POINT_GROUP``).
2. An explicit, ordered list in the configuration (``ORCASTER_PLUGINS``).

Precedence, from highest to lowest: the plugins of the explicit list in the order
they are written, then the remaining entry points in alphabetical order of name.
The first extractor that recognizes a payload wins. Precedence only decides that
tie-break: a kind cannot be registered twice.

The core ships no providers of its own. With no plugin the registry stays empty:
``extract_variant`` returns ``(None, None)`` for every payload, no exit code has a
default classification (only ``payload.outcomes`` classifies) and no status alias
exists. Startup logs this with an explicit warning.

Every load error (a plugin that does not import, a configured name that does not
exist, a duplicate or invalid kind, defaults for a kind without an extractor, an
invalid or duplicate alias) raises ``PluginError``: the server fails at startup
instead of classifying tasks with a half-built registry.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from types import MappingProxyType
from typing import Any

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "orcaster.plugins"

# Kind slug: required by the vocabulary of the variant token (`<kind>~<label>`).
KIND_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
# Variant filter tokens that no extractor may use as a kind.
RESERVED_KINDS = frozenset({"all", "other", "current"})

# An extractor returns the variant label, or None if it does not recognize the payload.
Extractor = Callable[[dict], str | None]


class PluginError(RuntimeError):
    """A plugin could not be loaded or registered something invalid."""


@dataclass(frozen=True)
class OutcomeRule:
    """Nuance of a terminal close: outcome name and the real status it resembles."""

    name: str
    closes_as: str


@dataclass(frozen=True)
class LegacyStatusAlias:
    """Retired status token: equivalent to ``status`` plus the ``outcome`` filter."""

    status: str
    outcome: str


# Real statuses an alias can be translated to: the same as the `closes_as` of an
# outcome (`task_outcomes.CLOSES_AS_VALUES`).
LEGACY_ALIAS_TARGET_STATUSES = ("completed", "failed")


class PluginRegistry:
    """Extractors and outcome profile accumulated during loading."""

    def __init__(self) -> None:
        self._extractors: list[tuple[str, Extractor]] = []
        self._outcome_defaults: dict[str, Mapping[int, OutcomeRule]] = {}
        self._legacy_status_aliases: dict[str, LegacyStatusAlias] = {}
        self._current_provider = "<none>"

    def _set_provider(self, name: str) -> None:
        self._current_provider = name

    def add_variant_extractor(self, kind: str, extract: Extractor) -> None:
        if not isinstance(kind, str) or not KIND_RE.match(kind) or kind in RESERVED_KINDS:
            raise PluginError(
                f"plugin {self._current_provider!r}: invalid variant kind {kind!r} "
                f"(must match {KIND_RE.pattern} and not be one of {sorted(RESERVED_KINDS)})"
            )
        if not callable(extract):
            raise PluginError(
                f"plugin {self._current_provider!r}: extractor of {kind!r} is not callable"
            )
        if any(k == kind for k, _ in self._extractors):
            raise PluginError(
                f"plugin {self._current_provider!r}: variant kind {kind!r} is already registered"
            )
        self._extractors.append((kind, extract))

    def add_outcome_defaults(self, kind: str, rules: Mapping[int, OutcomeRule]) -> None:
        if kind in self._outcome_defaults:
            raise PluginError(
                f"plugin {self._current_provider!r}: outcome defaults of {kind!r} "
                "are already registered"
            )
        for code, rule in rules.items():
            if not (isinstance(code, int) and 1 <= code <= 255) or not isinstance(
                rule, OutcomeRule
            ):
                raise PluginError(
                    f"plugin {self._current_provider!r}: outcome defaults of {kind!r} "
                    f"must map exit codes 1-255 to OutcomeRule, got {code!r}: {rule!r}"
                )
        self._outcome_defaults[kind] = MappingProxyType(dict(rules))

    def add_legacy_status_alias(
        self, status_token: str, outcome_name: str, *, status: str = "completed"
    ) -> None:
        """Declares a retired status token that the filters keep accepting.

        The routes that filter by status (``/api/v1/insights/*``, ``/admin/outcomes``,
        ``/admin/tasks``) translate ``status_token`` to ``status`` + ``outcome_name``;
        each route keeps its own way of combining it with the rest of the filter.

        Validation (``PluginError`` on failure): the token and the outcome match the
        outcome slug (``^[a-z][a-z0-9_]{0,63}$``); the token is not a real status,
        ``all`` or ``other``, because otherwise the alias would shadow that value; the
        outcome is not a reserved name; ``status`` is ``completed`` or ``failed``; and
        a token is not declared twice.
        """
        # Local imports: this module does not import from the rest of the app when
        # loaded (`task_variant` uses it without cycles), and only this validation
        # needs the status and outcome vocabulary.
        from orcaster_server.task_outcomes import OUTCOME_NAME_RE, RESERVED_OUTCOME_NAMES

        who = f"plugin {self._current_provider!r}"
        if (
            not isinstance(status_token, str)
            or not OUTCOME_NAME_RE.match(status_token)
            or status_token in RESERVED_OUTCOME_NAMES
            or status_token == "all"
        ):
            raise PluginError(
                f"{who}: invalid legacy status alias {status_token!r} (must match "
                f"{OUTCOME_NAME_RE.pattern} and not be a status value, 'all' or 'other')"
            )
        if (
            not isinstance(outcome_name, str)
            or not OUTCOME_NAME_RE.match(outcome_name)
            or outcome_name in RESERVED_OUTCOME_NAMES
        ):
            raise PluginError(
                f"{who}: legacy status alias {status_token!r} has an invalid outcome "
                f"{outcome_name!r} (must match {OUTCOME_NAME_RE.pattern} and not be reserved)"
            )
        if status not in LEGACY_ALIAS_TARGET_STATUSES:
            raise PluginError(
                f"{who}: legacy status alias {status_token!r} must translate to one of "
                f"{list(LEGACY_ALIAS_TARGET_STATUSES)}, got {status!r}"
            )
        if status_token in self._legacy_status_aliases:
            raise PluginError(f"{who}: legacy status alias {status_token!r} is already registered")
        self._legacy_status_aliases[status_token] = LegacyStatusAlias(
            status=status, outcome=outcome_name
        )

    def validate(self) -> None:
        """Cross checks, once all the providers are loaded."""
        kinds = {k for k, _ in self._extractors}
        orphans = sorted(set(self._outcome_defaults) - kinds)
        if orphans:
            raise PluginError(
                f"outcome defaults registered for kinds without a variant extractor: {orphans}"
            )

    def freeze(self) -> Registry:
        self.validate()
        return Registry(
            extractors=tuple(self._extractors),
            outcome_defaults=MappingProxyType(dict(self._outcome_defaults)),
            legacy_status_aliases=MappingProxyType(dict(self._legacy_status_aliases)),
        )


@dataclass(frozen=True)
class Registry:
    """Immutable, already loaded registry."""

    extractors: tuple[tuple[str, Extractor], ...]
    outcome_defaults: Mapping[str, Mapping[int, OutcomeRule]]
    legacy_status_aliases: Mapping[str, LegacyStatusAlias] = MappingProxyType({})

    @property
    def kinds(self) -> frozenset[str]:
        return frozenset(k for k, _ in self.extractors)


@dataclass(frozen=True)
class Provider:
    """A named provider, for error messages and the startup log."""

    name: str
    register: Callable[[PluginRegistry], None]


def _as_provider(name: str, obj: Any) -> Provider:
    register = getattr(obj, "register", obj)
    if not callable(register):
        raise PluginError(f"plugin {name!r} is neither callable nor has a register(registry)")
    return Provider(name=name, register=register)


def discover_entry_points(
    entry_points: Iterable[metadata.EntryPoint] | None = None,
) -> dict[str, Provider]:
    """Loads the group's entry points; ``{name: Provider}`` in name order."""
    if entry_points is None:
        entry_points = metadata.entry_points(group=ENTRY_POINT_GROUP)
    found: dict[str, Provider] = {}
    for ep in sorted(entry_points, key=lambda e: e.name):
        if ep.name in found:
            raise PluginError(f"entry point {ep.name!r} of group {ENTRY_POINT_GROUP} is duplicated")
        try:
            obj = ep.load()
        except Exception as exc:
            raise PluginError(f"plugin {ep.name!r} ({ep.value}) failed to import: {exc!r}") from exc
        found[ep.name] = _as_provider(ep.name, obj)
    return found


def parse_plugin_list(raw: str | None) -> list[str]:
    """``"a, b"`` -> ``["a", "b"]``; empty -> ``[]``. A repeated name is an error."""
    names = [part.strip() for part in (raw or "").split(",") if part.strip()]
    duplicated = sorted({n for n in names if names.count(n) > 1})
    if duplicated:
        raise PluginError(f"plugin list names {duplicated} more than once")
    return names


def load_registry(
    *,
    configured: Sequence[str] = (),
    entry_points: Iterable[metadata.EntryPoint] | None = None,
) -> Registry:
    """Builds the registry with the precedence documented in the module docstring."""
    discovered = discover_entry_points(entry_points)
    unknown = [n for n in configured if n not in discovered]
    if unknown:
        raise PluginError(
            f"configured plugins {unknown} are not installed "
            f"(entry points of group {ENTRY_POINT_GROUP}: {sorted(discovered)})"
        )
    ordered = [discovered[n] for n in configured]
    ordered += [p for n, p in discovered.items() if n not in configured]

    registry = PluginRegistry()
    for provider in ordered:
        registry._set_provider(provider.name)
        try:
            provider.register(registry)
        except PluginError:
            raise
        except Exception as exc:
            raise PluginError(f"plugin {provider.name!r} failed to register: {exc!r}") from exc
    frozen = registry.freeze()
    if ordered:
        log.info(
            "plugins: loaded %s (variant kinds: %s; outcome defaults for: %s; "
            "legacy status aliases: %s)",
            [p.name for p in ordered],
            [k for k, _ in frozen.extractors],
            sorted(frozen.outcome_defaults),
            sorted(frozen.legacy_status_aliases),
        )
    else:
        log.warning(
            "plugins: none loaded (ORCASTER_PLUGINS empty and no entry point in group %s): "
            "no variant is extracted, no exit code has a default outcome and no legacy "
            "status alias exists; only payload.outcomes classifies exits",
            ENTRY_POINT_GROUP,
        )
    return frozen


# --- active registry of the process ---------------------------------------------
_active: Registry | None = None


def get_registry() -> Registry:
    """Active registry; loaded on first use (or at startup, see ``main.lifespan``)."""
    global _active
    if _active is None:
        from orcaster_server.config import settings

        _active = load_registry(configured=parse_plugin_list(settings.plugins))
    return _active


def reset_registry() -> None:
    """Discards the active registry; the next ``get_registry`` reloads it (tests)."""
    global _active
    _active = None
