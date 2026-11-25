"""Derivation of a task's *variant*.

The variant is the pair `(kind, label)`:
- `kind`: the tool that produced the task (e.g. ``"example-tool"``).
- `label`: the version of that tool, in the form its extractor defines
  (e.g. ``"1.2.3"``).

The data does not exist in structured form: it lives as a substring inside
``tasks.payload`` (JSONB, shaped ``{"commands": [...], "requirements": [...]}``).
This module is the SINGLE source of truth for how extractors are used: task
creation (and fork) calls it through ``extract_variant``.

Generic design: the concrete parsing lives in an **extractor registry**
(``orcaster_server/plugins.py``) that plugins contribute to. The core ships none: without
plugins, ``extract_variant`` returns ``(None, None)`` for every payload. A tool is
supported by registering its extractor from a plugin, without touching the schema.
The registry is a sequence of ``(kind, fn)`` pairs where each ``fn`` is
``payload -> label | None``: the KIND is declared by the registry, not by the function, so
``known_variant_kinds()`` (the vocabulary the public API validates) is derived from the
same structure and cannot drift out of sync. The first one that recognizes the payload wins.
If none recognizes it, the task is ``other`` → ``(None, None)``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

# --- variant token vocabulary ------------------------------------------------
# Separator of a specific variant's token in the query param: `<kind>~<label>`.
# `~` is URL-unreserved (RFC 3986) and appears neither in kinds (slugs `[a-z0-9-]+`)
# nor in usual version labels. `all`/`other`/`current` are RESERVED tokens: an
# extractor cannot register a kind with those names.
VARIANT_SEP = "~"

# Maximum length of the `<kind>~<label>` token. SOLE owner of the invariant: it is
# consumed by the ingestion clamp (`clamp_variant`, below) and by the `Query(max_length=...)`
# of the public surface (`orcaster_server/api/status_ui.py`), so the value the app
# PERSISTS can never produce a token that its own round-trip rejects.
#
# Why the cap exists: the status page re-emits `selected_variant` in the polling
# `hx-get` and in the selector's `<option>` `value`s, so a long label would make the
# served HTML generate requests that return 422 (the param was already bounded because
# it feeds the snapshot cache key; the cap is memory retained by the process, not a
# cosmetic detail). The limit is closed at the SOURCE instead of sanitizing every
# re-emission: a single guard instead of one per template.
VARIANT_TOKEN_MAX_LEN = 64


class _StandaloneRegistry:
    """Registry for a load by path, without the `orcaster_server` package: empty.

    The core ships no extractors of its own, so without `orcaster_server.plugins` there are none.
    """

    extractors: tuple[tuple[str, Callable[[dict], str | None]], ...] = ()
    kinds: frozenset[str] = frozenset()


def _active_registry():
    """Active server registry; an empty one if the module was loaded without `orcaster_server`.

    Operations tools load this file by path (`spec_from_file_location`) to use the
    filter's pure helpers (`VARIANT_SEP`, `clamp_variant`, ...) without the
    `orcaster_server` package on the path: that is why `orcaster_server.plugins` is
    imported here, lazily, and not at module level.
    """
    try:
        from orcaster_server.plugins import get_registry
    except ModuleNotFoundError as exc:
        if exc.name != "orcaster_server" and not (exc.name or "").startswith("orcaster_server."):
            raise
        return _StandaloneRegistry
    return get_registry()


def known_variant_kinds() -> frozenset[str]:
    """Kinds that the active registry knows how to produce.

    This is the vocabulary accepted by the filter's `kind` and `pair` modes in the
    public API: a token that is not here CANNOT match any row ingested by this
    process, so rejecting it with 422 hides no results and distinguishes a typo from
    "there are no tasks". Without plugins the set is empty and every explicit kind
    is rejected.

    CAUTION when using it: it validates INTENT, not what is stored. A
    `tasks.variant_kind` column written by an earlier version with a kind since
    retired from the registry would remain in the DB without being here. That is why
    the check lives at the API edge (a surface that receives free text from a
    machine), and NOT inside `resolve_variant_filter`, which also feeds the admin UI.
    """
    return _active_registry().kinds


def __getattr__(name: str) -> Any:
    """``KNOWN_VARIANT_KINDS`` is read from the active registry on every access (PEP 562).

    Plugins are loaded at startup, after this module is imported: a constant computed
    at import time would be empty or stale. The name is kept for callers that read it
    as an attribute; server code uses ``known_variant_kinds()``.
    """
    if name == "KNOWN_VARIANT_KINDS":
        return known_variant_kinds()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def clamp_variant(kind: str | None, label: str | None) -> tuple[str | None, str | None]:
    """Bound the ``(kind, label)`` pair so its token fits in the query param.

    Guaranteed invariant: ``len(f"{kind}{VARIANT_SEP}{label}") <= VARIANT_TOKEN_MAX_LEN``.
    It is applied at INGESTION (see ``extract_variant``), the only point through which
    the app writes ``tasks.variant_kind``/``variant_label``; this way no surface has to
    sanitize the token when re-emitting it.

    Rules:

    - ``kind is None`` → ``(None, None)``: without a tool there is no pair (``other``
      bucket); a lone label is not representable in the filter vocabulary.
    - The **label** bears the truncation, because the kind is short and stable (a slug
      from the extractor registry, e.g. ``"example-tool"``) while the label comes from
      the payload, which is external data. Budget:
      ``VARIANT_TOKEN_MAX_LEN - len(kind) - len(VARIANT_SEP)``.
    - **Deterministic** truncation (prefix, no marker or hash): the same long label
      always produces the same pair. Accepted consequence: two labels that differ only
      BEYOND the cut collapse into the same variant and group together in the lens.
      That is preferable to a hashed truncation (unreadable in the UI) or to rejecting
      the task over a derived, cosmetic field.
    - Edge defense: a kind so long that it leaves no budget for the label loses the
      label (the ``kind`` mode remains, whose token is also bounded). It does not
      happen today (kinds are defined by code, not by the payload) but it keeps the
      invariant closed.
    """
    if kind is None:
        return (None, None)
    kind = kind[:VARIANT_TOKEN_MAX_LEN]
    if label is None:
        return (kind, None)
    budget = VARIANT_TOKEN_MAX_LEN - len(kind) - len(VARIANT_SEP)
    if budget <= 0:
        return (kind, None)
    return (kind, label[:budget])


def extract_variant(payload: dict | None) -> tuple[str | None, str | None]:
    """Derive the ``(kind, label)`` variant from the payload.

    Tries the registered extractors in order; the first one that recognizes the payload
    wins. If none recognizes it (or the payload is None/malformed), the task is ``other``
    and ``(None, None)`` is returned.

    The result goes through ``clamp_variant``: what is PERSISTED is bounded to
    ``VARIANT_TOKEN_MAX_LEN`` of token, so neither a new extractor nor a hostile payload
    can generate a variant that the UI itself cannot re-emit. Extractors are free of
    repeating that computation.
    """
    if not isinstance(payload, dict):
        return (None, None)
    for kind, extractor in _active_registry().extractors:
        label = extractor(payload)
        if label is not None:
            return clamp_variant(kind, label)
    return (None, None)


def variant_sort_key(label: str | None) -> tuple[int, ...]:
    """Sort key of a version ``label``, as a tuple of integers.

    Extracts all digit groups in order (``"1.2.3 / b4.5.6"`` → ``(1,2,3,4,5,6)``;
    ``"v2.3.0"`` → ``(2,3,0)``), so the order is semantic (a two-digit patch is not
    compared as a string). It only makes sense to compare labels OF THE SAME
    ``kind`` (versions of different tools are not comparable with each other).
    ``None``/``""`` → ``()`` (the smallest of all).
    """
    if not label:
        return ()
    return tuple(int(n) for n in re.findall(r"\d+", label))


# --- variant view filter ------------------------------------------------------
# The variant is used as a LENS on the dashboard: it rescales the listing AND the
# counts gauge (default = "current" = latest of each tool + other). These helpers
# are pure (no DB) so they can be tested without postgres; the handler wires them
# to the queries.

_MODE_ALL = "all"
_MODE_OTHER = "other"
_MODE_CURRENT = "current"

# Values accepted by the `default_mode` keyword of `resolve_variant_filter`.
# It is DERIVED from the mode constants (the strings are not repeated) so the
# runtime check and the signature's `Literal` cannot drift out of sync.
_DEFAULT_MODES: tuple[str, ...] = (_MODE_CURRENT, _MODE_ALL)


@dataclass(frozen=True)
class VariantFilter:
    """Variant filter resolved from the query param.

    - ``mode``: ``current`` | ``all`` | ``other`` | ``kind`` | ``pair``.
    - ``kind``/``label``: populated in ``kind`` mode (kind only) and ``pair`` (exact pair).
    - ``current_kinds``/``current_labels``: parallel arrays of the "current" set
      (latest of each recognized kind), for the SQL ``unnest``. The ``other`` bucket
      of current mode is NOT in these arrays: ``IS NULL`` covers it.
    """

    mode: str
    kind: str | None = None
    label: str | None = None
    current_kinds: tuple[str, ...] = field(default_factory=tuple)
    current_labels: tuple[str, ...] = field(default_factory=tuple)


def current_variant_keys(
    present: Iterable[tuple[str | None, str | None]],
) -> set[tuple[str | None, str | None]]:
    """Set of "current" ``(kind, label)`` keys: the latest of EACH kind + ``other``.

    ``present`` = variants present (distinct). The latest is resolved by
    ``variant_sort_key`` WITHIN each kind (versions of different tools are not
    compared with each other). ``(None, None)`` (other) is included if present.

    Ties: ALL labels of the kind whose key equals the maximum are included.
    The key only looks at digits, so ``"1.2.0 / opt"`` and ``"1.2.0"`` (the same
    version of a tool with and without an optional stage) tie: they are parallel
    populations, not successive epochs, and keeping one according to iteration order
    would make the choice random. If a kind's distinct labels do not share the digit
    sequence, nothing changes.
    """
    labels_by_kind: dict[str, list[str | None]] = {}
    has_other = False
    for kind, label in present:
        if kind is None:
            has_other = True
            continue
        labels_by_kind.setdefault(kind, []).append(label)
    keys: set[tuple[str | None, str | None]] = set()
    for kind, labels in labels_by_kind.items():
        top = max(variant_sort_key(label) for label in labels)
        keys.update((kind, label) for label in labels if variant_sort_key(label) == top)
    if has_other:
        keys.add((None, None))
    return keys


def resolve_variant_filter(
    param: str | None,
    present: Iterable[tuple[str | None, str | None]],
    *,
    default_mode: Literal["current", "all"],
) -> VariantFilter:
    """Translate the ``variant`` query param into a ``VariantFilter``.

    - ``current`` → ``current`` mode (arrays = latest of each kind).
    - ``all`` → no filter. ``other`` → only the bucket without a variant.
    - ``<kind>`` → the whole tool. ``<kind>~<label>`` → exact pair.
    - absent/``""`` → whatever ``default_mode`` says, which is MANDATORY.

    Why ``default_mode`` has no default value: this helper is consumed by surfaces
    with opposite contracts. The admin UI and the status page want the LENS
    (``current`` = latest of each tool + ``other``), because there the variant is a
    view control and showing all epochs mixed together is noise. The public API wants
    the opposite: a listing that silently hides the non-latest versions of each kind
    is an incomplete answer that an automated consumer reads as "there are no more
    tasks". When the helper chose the default, each of those callers neutralized it by
    hand and in a different way, and the next surface to wire the helper inherited the
    other half's default without noticing. By requiring it as a keyword, the choice is
    written at the site that bears it and cannot be inherited by accident.

    ``default_mode`` applies ONLY to the empty or ``None`` token. An explicit
    ``"current"`` token resolves to ``current`` mode in all callers, including those
    that declare ``default_mode="all"``: the default covers the absence of intent, it
    does not contradict it.

    The signature's ``Literal`` is NOT checked on its own: CI runs ruff and pytest,
    not a type checker, so without a runtime check the annotation is only
    documentation. That is why ``default_mode`` is VALIDATED here against
    ``_DEFAULT_MODES`` and a value outside the vocabulary raises ``ValueError``.
    It is a programming error of the caller (not client input), so
    ``ValueError`` and not a 4xx. The check is UNCONDITIONAL, before looking at the
    token: a miswired caller must blow up on its first call, not stay latent until
    someone sends ``?variant=``. Without it, a ``default_mode="latest"`` slipped
    through as ``kind`` mode and answered 200 with an empty list, which is the same
    silent failure that ``known_variant_kinds()`` closes for client input.
    """
    if default_mode not in _DEFAULT_MODES:
        accepted = ", ".join(repr(m) for m in _DEFAULT_MODES)
        raise ValueError(
            f"resolve_variant_filter: default_mode must be one of {accepted}, got {default_mode!r}"
        )
    token = (param or "").strip()
    if not token:
        token = default_mode
    if token == _MODE_CURRENT:
        keys = current_variant_keys(present)
        pairs = sorted((k, v) for k, v in keys if k is not None)
        return VariantFilter(
            mode=_MODE_CURRENT,
            current_kinds=tuple(k for k, _ in pairs),
            current_labels=tuple(v for _, v in pairs),
        )
    if token == _MODE_ALL:
        return VariantFilter(mode=_MODE_ALL)
    if token == _MODE_OTHER:
        return VariantFilter(mode=_MODE_OTHER)
    if VARIANT_SEP in token:
        kind, label = token.split(VARIANT_SEP, 1)
        return VariantFilter(mode="pair", kind=kind, label=label)
    return VariantFilter(mode="kind", kind=token)


def variant_row_matches(vf: VariantFilter, kind: str | None, label: str | None) -> bool:
    """Does the ``(kind, label)`` row fall inside the filter? For rescaling counts in Python."""
    if vf.mode == _MODE_ALL:
        return True
    if vf.mode == _MODE_OTHER:
        return kind is None
    if vf.mode == "kind":
        return kind == vf.kind
    if vf.mode == "pair":
        return kind == vf.kind and label == vf.label
    if vf.mode == _MODE_CURRENT:
        if kind is None:
            return True
        return (kind, label) in set(zip(vf.current_kinds, vf.current_labels, strict=True))
    return False


def variant_sql_binds(vf: VariantFilter) -> dict:
    """Binds for the variant clause of ``_SQL_TASKS_WHERE``.

    ALWAYS returns the same set of keys (asyncpg requires every placeholder in the
    SQL to be present, whatever the mode).
    """
    return {
        "variant_mode": vf.mode,
        "variant_kind": vf.kind,
        "variant_label": vf.label,
        "variant_current_kinds": list(vf.current_kinds),
        "variant_current_labels": list(vf.current_labels),
    }


# WHERE clause of the variant filter, shared by the tasks partial
# (`_SQL_TASKS_WHERE`) and the failures aggregation (`_SQL_FAILED_TASKS`). SINGLE
# source of the filter so the modules do not drift out of sync. Assumes the `tasks`
# table is aliased as `t`. The binds are produced by `variant_sql_binds`; all with
# explicit CAST (asyncpg requires a declared type for placeholders that may be NULL
# or arrays). `current` mode: latest of each kind (arrays via `unnest`) + the `other`
# bucket (IS NULL) always included.
VARIANT_WHERE_SQL = """
      AND (
           CAST(:variant_mode AS text) = 'all'
           OR (CAST(:variant_mode AS text) = 'other' AND t.variant_kind IS NULL)
           OR (CAST(:variant_mode AS text) = 'kind'
                AND t.variant_kind = CAST(:variant_kind AS text))
           OR (CAST(:variant_mode AS text) = 'pair'
                AND t.variant_kind = CAST(:variant_kind AS text)
                AND t.variant_label = CAST(:variant_label AS text))
           OR (CAST(:variant_mode AS text) = 'current'
                AND (t.variant_kind IS NULL
                     OR (t.variant_kind, t.variant_label) IN (
                          SELECT k, l
                            FROM unnest(CAST(:variant_current_kinds AS text[]),
                                        CAST(:variant_current_labels AS text[])) AS c(k, l)
                     )))
      )
"""


def variant_token(vf: VariantFilter) -> str:
    """Canonical token of the filter (to rebuild the query string / the chip)."""
    if vf.mode == "pair":
        return f"{vf.kind}{VARIANT_SEP}{vf.label}"
    if vf.mode == "kind":
        return vf.kind or ""
    return vf.mode  # current | all | other


# Types of the grid that feeds the lens. The raw row comes from the scan
# `(variant_kind, variant_label, status, outcome, n)`: a single query, four
# surfaces (dashboard, tasks partial, outcomes, public status page).
VariantKey = tuple[str | None, str | None]
VariantGridRow = tuple[str | None, str | None, str, str | None, int]


def accumulate_variant_grid(
    rows: Iterable[Sequence[Any]],
    *,
    count_statuses: Collection[str] | None = None,
) -> tuple[dict[VariantKey, int], list[VariantGridRow]]:
    """Collapse the ``(variant_kind, variant_label, status, outcome, n)`` rows into two views.

    Sole implementation of the accumulation loop: it used to be copied in
    the four surfaces that run that scan, with one copy already diverged.

    Returns:

    - ``present_counts``: ``(kind, label) -> total``. The KEY comes from ANY
      row (the selector's vocabulary is everything present in the scan, even
      if its count ends up 0); the COUNT sums only the rows whose ``status``
      is in ``count_statuses``. ``None`` (default) = all statuses, which is
      what the dashboard and the status page want.
    - ``variant_grid``: the normalized grid (``status`` as ``str``, ``n`` as
      ``int``), for rescaling breakdowns in Python with ``variant_row_matches``
      without going back to the DB.

    The two axes are independent on purpose: the outcomes page needs the full
    workspace vocabulary but a ``· N`` that only counts terminal tasks, and
    that is exactly a restricted ``count_statuses``.
    """
    present_counts: dict[VariantKey, int] = {}
    variant_grid: list[VariantGridRow] = []
    for r in rows:
        vk, vl, st, oc, n = r[0], r[1], str(r[2]), r[3], int(r[4])
        variant_grid.append((vk, vl, st, oc, n))
        key = (vk, vl)
        present_counts.setdefault(key, 0)
        if count_statuses is None or st in count_statuses:
            present_counts[key] += n
    return present_counts, variant_grid


def build_variant_options(
    present_counts: Mapping[tuple[str | None, str | None], int],
    vf: VariantFilter,
    current_keys: Iterable[tuple[str | None, str | None]] | None = None,
) -> dict:
    """Structure of the hierarchical selector (the view only renders it).

    ``present_counts`` = total count per present variant ``(kind, label) -> n``.
    Returns the tools (sorted), each with its versions (descending by semver),
    marking ``is_current`` (the latest of the kind) and ``selected`` (the chosen
    pair), plus ``has_other`` and ``is_current_default``.

    ``current_keys`` decouples the ``(current)`` hint from the vocabulary. By
    default it is derived from ``present_counts``, which is correct when both
    come from the same set (dashboard, status page). The outcomes page does NOT
    share them: its vocabulary is the whole workspace, but the hint must come
    only from variants with at least one terminal task; otherwise a newly
    queued version, without a single finished task, would take the ``(current)``
    of a page that only aggregates terminals.
    """
    current = (
        set(current_keys)
        if current_keys is not None
        else current_variant_keys(present_counts.keys())
    )
    by_kind: dict[str, list[tuple[str | None, int]]] = {}
    for (kind, label), n in present_counts.items():
        if kind is None:
            continue
        by_kind.setdefault(kind, []).append((label, n))

    tools = []
    for kind in sorted(by_kind):
        versions = sorted(by_kind[kind], key=lambda x: variant_sort_key(x[0]), reverse=True)
        tools.append(
            {
                "kind": kind,
                "versions": [
                    {
                        "label": label,
                        "token": f"{kind}{VARIANT_SEP}{label}",
                        "count": n,
                        "is_current": (kind, label) in current,
                        "selected": vf.mode == "pair" and vf.kind == kind and vf.label == label,
                    }
                    for label, n in versions
                ],
            }
        )

    return {
        "is_current_default": vf.mode == _MODE_CURRENT,
        "has_other": (None, None) in present_counts,
        "tools": tools,
        "selected_token": variant_token(vf),
    }
