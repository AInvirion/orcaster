"""Tests for the pure task-variant functions.

The variant is a (kind, label) pair derived from the payload by a registry of extractors
supplied by plugins. The core ships no extractors: with an empty registry every
task is `other`. Cases that need an extractor use the test plugin from
`conftest.py` (kind `example-tool`, label = the version of `example-tool==X.Y.Z`) or a
registry built in the test itself with `install_registry`. Cases for concrete tools
live in each plugin repository.
"""

import pytest

from orcaster_server.task_variant import (
    VARIANT_SEP,
    VARIANT_TOKEN_MAX_LEN,
    accumulate_variant_grid,
    build_variant_options,
    clamp_variant,
    current_variant_keys,
    extract_variant,
    known_variant_kinds,
    resolve_variant_filter,
    variant_row_matches,
    variant_sort_key,
    variant_sql_binds,
    variant_token,
)
from tests.conftest import EXAMPLE_KIND, example_payload, example_provider, install_registry

OTHER_KIND = "other-tool"


def _other_extract(payload: dict) -> str | None:
    """Extractor for a second kind: the value of `payload["other_version"]`, if it is a str."""
    version = payload.get("other_version")
    return version if isinstance(version, str) else None


def _other_provider(registry) -> None:
    registry.add_variant_extractor(OTHER_KIND, _other_extract)


# --- empty registry: the core without plugins ------------------------------


def test_known_variant_kinds_is_empty_without_plugins():
    """Without plugins there is no vocabulary: every explicit kind is rejected by the API."""
    assert known_variant_kinds() == frozenset()


@pytest.mark.parametrize(
    "payload",
    [
        example_payload(),
        example_payload("example-tool --version", version="1.10.0"),
        {"requirements": [], "commands": ["echo hello"]},
        {"commands": ["uv pip install some-package==2.0.0"]},
        {},
        None,
    ],
)
def test_extract_variant_without_plugins_is_other(payload):
    """With an empty registry no payload has a variant, not even an `example-tool` one."""
    assert extract_variant(payload) == (None, None)


# --- registry with the test plugin ------------------------------------------


def test_known_variant_kinds_derives_from_the_registry(example_plugin):
    """The catalog comes from the extractor registry, not from a parallel list."""
    assert known_variant_kinds() == frozenset({EXAMPLE_KIND})


def test_every_extracted_kind_is_a_known_kind(example_plugin):
    """Every kind that ingestion CAN produce is in the catalog.

    If an extracted kind were missing here, filtering by it would give 422 on rows that do
    existen.
    """
    kind, _ = extract_variant(example_payload())
    assert kind in known_variant_kinds()


def test_example_plugin_extracts_the_variant(example_plugin):
    assert extract_variant(example_payload()) == (EXAMPLE_KIND, "1.2.3")
    assert extract_variant(example_payload(version="1.10.0")) == (EXAMPLE_KIND, "1.10.0")


def test_unrecognised_payload_is_other_with_plugin(example_plugin):
    assert extract_variant({"requirements": [], "commands": ["echo hello"]}) == (None, None)


def test_empty_payload_is_other(example_plugin):
    assert extract_variant({}) == (None, None)


def test_none_payload_is_other(example_plugin):
    assert extract_variant(None) == (None, None)


def test_malformed_commands_is_other(example_plugin):
    # commands is not a list of str
    assert extract_variant({"commands": "uv pip install example-tool==1.2.3"}) == (None, None)
    assert extract_variant({"commands": [123, {"x": 1}]}) == (None, None)


def test_non_dict_payload_is_other(example_plugin):
    assert extract_variant(["uv pip install example-tool==1.2.3"]) == (None, None)  # type: ignore[arg-type]


# --- precedence between extractors ------------------------------------------


def test_first_registered_extractor_wins():
    """If two extractors recognize the payload, the first in the registry wins."""
    payload = {**example_payload(), "other_version": "9.9.9"}
    install_registry(("example", example_provider), ("zz-other", _other_provider))
    assert extract_variant(payload) == (EXAMPLE_KIND, "1.2.3")


def test_later_extractor_applies_when_the_first_does_not_recognise():
    install_registry(("example", example_provider), ("zz-other", _other_provider))
    assert extract_variant({"other_version": "2.0.0"}) == (OTHER_KIND, "2.0.0")
    assert extract_variant({"commands": ["echo hello"]}) == (None, None)
    assert known_variant_kinds() == frozenset({EXAMPLE_KIND, OTHER_KIND})


# --- token cap at ingestion -------------------------------------------------
# The invariant: the `kind~label` token that surfaces re-emit in the URL
# (`?variant=`) fits in the `Query(max_length=VARIANT_TOKEN_MAX_LEN)` of the
# public plane. It is guaranteed at the SOURCE: `extract_variant` bounds what is persisted,
# including the label a plugin returns.


def _huge_version(digits: int) -> str:
    """Syntactically valid version (`\\d+.\\d+.\\d+`) but absurdly long."""
    return f"{'9' * digits}.0.0"


def test_extract_variant_clamps_a_plugin_label_to_max_len(example_plugin):
    kind, label = extract_variant(example_payload(version=_huge_version(120)))
    assert kind == EXAMPLE_KIND
    assert label is not None
    assert len(f"{kind}{VARIANT_SEP}{label}") == VARIANT_TOKEN_MAX_LEN


def test_clamped_variant_round_trips_through_the_query_param(example_plugin):
    """The filter token resolved from the persisted pair fits in the Query param."""
    kind, label = extract_variant(example_payload(version=_huge_version(200)))
    token = f"{kind}{VARIANT_SEP}{label}"
    vf = resolve_variant_filter(token, [(kind, label)], default_mode="current")
    assert vf.mode == "pair"
    assert (vf.kind, vf.label) == (kind, label)
    assert len(variant_token(vf)) <= VARIANT_TOKEN_MAX_LEN


def test_clamp_is_deterministic_and_groups_equal_prefixes(example_plugin):
    """Deterministic truncation: two labels with the same prefix collapse into the same pair.

    Accepted and documented behavior: two payloads whose
    labels only differ beyond the cutoff group together.
    """
    a = extract_variant(example_payload(version=f"{'9' * 90}.0.0"))
    b = extract_variant(example_payload(version=f"{'9' * 90}.0.1"))
    assert a == b
    assert a == extract_variant(example_payload(version=f"{'9' * 90}.0.0"))


def test_clamp_variant_is_noop_for_short_labels():
    """Usual labels pass through intact: the clamp does not bite."""
    assert clamp_variant(EXAMPLE_KIND, "1.2.0 / opt") == (EXAMPLE_KIND, "1.2.0 / opt")


def test_clamp_variant_other_and_missing_label():
    assert clamp_variant(None, None) == (None, None)
    assert clamp_variant(None, "1.2.3") == (None, None)  # without a kind there is no pair
    assert clamp_variant(EXAMPLE_KIND, None) == (EXAMPLE_KIND, None)


def test_clamp_variant_kind_longer_than_budget_drops_the_label():
    """A kind that consumes the whole budget loses the label, not the cap.

    Defensive (kinds are short registry slugs, not external data): the token
    of `kind` mode still fits in the param.
    """
    kind = "k" * (VARIANT_TOKEN_MAX_LEN + 10)
    ck, cl = clamp_variant(kind, "1.2.3")
    assert cl is None
    assert len(ck or "") <= VARIANT_TOKEN_MAX_LEN


# --- version ordering (to resolve "latest") ---------------------------------


def test_sort_key_orders_minor_and_patch_within_kind():
    assert variant_sort_key("1.2.3") > variant_sort_key("1.2.0")
    assert variant_sort_key("1.3.0") > variant_sort_key("1.2.14")


def test_sort_key_patch_ordering_double_digits():
    # 1.10.0 > 1.9.0 and 1.2.14 > 1.2.12 (two-digit numbers do not sort as strings)
    assert variant_sort_key("1.10.0") > variant_sort_key("1.9.0")
    assert variant_sort_key("1.2.14") > variant_sort_key("1.2.12")


def test_sort_key_uses_trailing_digits_as_tiebreaker():
    # same prefix, different second version -> sorts by the second
    assert variant_sort_key("1.2.3 / b4.5.6") > variant_sort_key("1.2.3 / b4.5.0")


def test_sort_key_handles_prefixed_label():
    assert variant_sort_key("v2.3.0") > variant_sort_key("v2.2.9")


def test_sort_key_none_or_empty_is_lowest():
    assert variant_sort_key(None) == ()
    assert variant_sort_key("") == ()
    assert variant_sort_key("0.0.1") > variant_sort_key(None)


# --- current_variant_keys: latest per kind (+ other if present) -------------

# Catalog of a kind with several versions.
_CATALOG = [
    (EXAMPLE_KIND, "1.10.0"),
    (EXAMPLE_KIND, "1.2.3"),
    (EXAMPLE_KIND, "1.2.0"),
    (EXAMPLE_KIND, "1.0.14"),
    (EXAMPLE_KIND, "1.0.12"),
]


def test_current_keys_latest_of_the_only_tool():
    # Without `other` present, current = only the latest of the single kind.
    assert current_variant_keys(_CATALOG) == {(EXAMPLE_KIND, "1.10.0")}


def test_current_keys_includes_other_when_present():
    present = [*_CATALOG, (None, None)]
    assert current_variant_keys(present) == {(EXAMPLE_KIND, "1.10.0"), (None, None)}


def test_current_keys_latest_per_kind_multiple_tools():
    # Two tools: current = the latest of EACH, not comparable with one another.
    present = [
        (EXAMPLE_KIND, "1.10.0"),
        (EXAMPLE_KIND, "1.2.3"),
        (OTHER_KIND, "v2.3.0"),
        (OTHER_KIND, "v2.2.9"),
        (None, None),
    ]
    assert current_variant_keys(present) == {
        (EXAMPLE_KIND, "1.10.0"),
        (OTHER_KIND, "v2.3.0"),
        (None, None),
    }


def test_current_keys_empty_when_nothing_present():
    assert current_variant_keys([]) == set()


# A suffix WITHOUT digits makes two labels of the same version tie in
# `variant_sort_key`. Current includes ALL the ties at the maximum of each kind, instead
# of keeping one depending on iteration order.
_TIE_CATALOG = [
    (OTHER_KIND, "1.1.0 / opt"),
    (OTHER_KIND, "1.2.0"),
    (OTHER_KIND, "1.2.0 / opt"),
    (OTHER_KIND, "opt"),
]


@pytest.mark.parametrize("order", [_TIE_CATALOG, list(reversed(_TIE_CATALOG))])
def test_current_keys_include_every_label_tied_at_the_max(order):
    assert current_variant_keys([*order, *_CATALOG]) == {
        (OTHER_KIND, "1.2.0"),
        (OTHER_KIND, "1.2.0 / opt"),
        (EXAMPLE_KIND, "1.10.0"),
    }


def test_current_tie_marks_both_labels_in_the_selector():
    present_counts = {key: 1 for key in _TIE_CATALOG}
    vf = resolve_variant_filter(None, present_counts.keys(), default_mode="current")
    assert set(zip(vf.current_kinds, vf.current_labels, strict=True)) == {
        (OTHER_KIND, "1.2.0"),
        (OTHER_KIND, "1.2.0 / opt"),
    }
    (tool,) = build_variant_options(present_counts, vf)["tools"]
    current = [v["label"] for v in tool["versions"] if v["is_current"]]
    assert sorted(current) == ["1.2.0", "1.2.0 / opt"]
    assert tool["versions"][-1]["label"] == "opt"  # without a version: at the bottom
    assert variant_row_matches(vf, OTHER_KIND, "1.2.0")
    assert not variant_row_matches(vf, OTHER_KIND, "1.1.0 / opt")


# --- resolve_variant_filter: token → VariantFilter --------------------------


# --- default_mode: the empty token is decided by the caller ----------------
# The contract behind finding 5: the helper used to pick `current` on its
# own, so a surface that only wanted "no filter" inherited a lens
# that hid the non-latest versions of each kind. Now the choice is a
# mandatory keyword and is written out in the caller.
#
# Each entry is parametrized, not in a `for` inside the assert: this way the failure
# names the token that broke and the other cases keep being exercised in the
# same run.

_EMPTY_TOKENS = ["", "   ", None]


@pytest.mark.parametrize("empty", _EMPTY_TOKENS)
def test_default_mode_all_applies_to_the_empty_token(empty: str | None):
    """Empty token + `default_mode="all"` -> `all` mode (no filter), not the lens."""
    vf = resolve_variant_filter(empty, _CATALOG, default_mode="all")
    assert vf.mode == "all"
    # `all` does not carry the arrays of the current set: there is nothing to bound.
    assert vf.current_kinds == () and vf.current_labels == ()


@pytest.mark.parametrize("empty", _EMPTY_TOKENS)
def test_default_mode_current_applies_to_the_empty_token(empty: str | None):
    """Same empty token, the other default: the admin UI lens stays intact."""
    vf = resolve_variant_filter(empty, _CATALOG, default_mode="current")
    assert vf.mode == "current"
    # current arrays = latest per kind (without the pseudo-other, which goes by IS NULL)
    assert set(zip(vf.current_kinds, vf.current_labels, strict=True)) == {(EXAMPLE_KIND, "1.10.0")}


@pytest.mark.parametrize("default_mode", ["all", "current"])
@pytest.mark.parametrize("token", ["current", "all", "other"])
def test_default_mode_does_not_override_the_explicit_token(token: str, default_mode: str):
    """`default_mode` covers the ABSENCE of intent, it never contradicts it.

    An explicit `?variant=current` resolves to the lens even in the caller
    that declares `default_mode="all"`; symmetrically, an explicit `all` does not fall
    into the lens because it comes from a surface that uses it by default.
    """
    assert resolve_variant_filter(token, _CATALOG, default_mode=default_mode).mode == token


def test_default_mode_is_mandatory():
    """Omitting the keyword is a `TypeError`, not a silently inherited default.

    Without this assert, restoring the default in the signature leaves the suite green (the
    callers pass the keyword explicitly) and the next surface that wires the
    helper inherits a default it did not choose, which is exactly the regression
    this test exists to prevent.
    """
    with pytest.raises(TypeError, match="default_mode"):
        resolve_variant_filter(None, ())  # type: ignore[call-arg]


@pytest.mark.parametrize("bad", ["latest", "", "   ", "CURRENT", "kind", "pair", None, 0])
def test_default_mode_invalid_raises_value_error(bad):
    """A `default_mode` outside the vocabulary fails on the first call.

    Nobody verifies the signature `Literal` (CI runs ruff and pytest, not a
    type checker), so the check lives at runtime. `"latest"` used to slip through
    as `kind` mode and answer 200 with an empty list; `None` died with a
    `TypeError` from `VARIANT_SEP in token`, without naming the offending parameter.
    """
    with pytest.raises(ValueError, match="default_mode"):
        resolve_variant_filter(None, _CATALOG, default_mode=bad)


def test_default_mode_invalid_raises_even_if_the_token_is_explicit():
    """The check is unconditional: it does not wait for an empty token to expose the wiring.

    If it lived inside the empty-token branch, a miswired caller would go
    unnoticed as long as all its requests carried an explicit `?variant=`.
    """
    with pytest.raises(ValueError, match="default_mode"):
        resolve_variant_filter("all", _CATALOG, default_mode="latest")


def test_resolve_all():
    vf = resolve_variant_filter("all", _CATALOG, default_mode="current")
    assert vf.mode == "all"


def test_resolve_other():
    vf = resolve_variant_filter("other", _CATALOG, default_mode="current")
    assert vf.mode == "other"


def test_resolve_kind_only():
    vf = resolve_variant_filter(EXAMPLE_KIND, _CATALOG, default_mode="current")
    assert vf.mode == "kind"
    assert vf.kind == EXAMPLE_KIND
    assert vf.label is None


def test_resolve_pair():
    vf = resolve_variant_filter(f"{EXAMPLE_KIND}~1.2.3", _CATALOG, default_mode="current")
    assert vf.mode == "pair"
    assert vf.kind == EXAMPLE_KIND
    assert vf.label == "1.2.3"


def test_resolve_pair_keeps_separators_inside_the_label():
    """Only the first `~` separates: the rest belongs to the label."""
    vf = resolve_variant_filter(f"{EXAMPLE_KIND}~1.2.0 / opt~x", _CATALOG, default_mode="current")
    assert (vf.mode, vf.kind, vf.label) == ("pair", EXAMPLE_KIND, "1.2.0 / opt~x")


def test_resolve_current_multiple_tools():
    present = [
        (EXAMPLE_KIND, "1.10.0"),
        (OTHER_KIND, "v2.3.0"),
        (OTHER_KIND, "v2.2.9"),
    ]
    vf = resolve_variant_filter(None, present, default_mode="current")
    assert set(zip(vf.current_kinds, vf.current_labels, strict=True)) == {
        (EXAMPLE_KIND, "1.10.0"),
        (OTHER_KIND, "v2.3.0"),
    }


# --- variant_row_matches: re-scaling of counts in Python --------------------


def test_row_matches_all():
    vf = resolve_variant_filter("all", _CATALOG, default_mode="current")
    assert variant_row_matches(vf, EXAMPLE_KIND, "1.0.12") is True
    assert variant_row_matches(vf, None, None) is True


def test_row_matches_other():
    vf = resolve_variant_filter("other", _CATALOG, default_mode="current")
    assert variant_row_matches(vf, None, None) is True
    assert variant_row_matches(vf, EXAMPLE_KIND, "1.10.0") is False


def test_row_matches_kind():
    vf = resolve_variant_filter(EXAMPLE_KIND, _CATALOG, default_mode="current")
    assert variant_row_matches(vf, EXAMPLE_KIND, "1.0.12") is True
    assert variant_row_matches(vf, EXAMPLE_KIND, "1.10.0") is True
    assert variant_row_matches(vf, None, None) is False
    assert variant_row_matches(vf, OTHER_KIND, "v2.3.0") is False


def test_row_matches_pair():
    vf = resolve_variant_filter(f"{EXAMPLE_KIND}~1.2.3", _CATALOG, default_mode="current")
    assert variant_row_matches(vf, EXAMPLE_KIND, "1.2.3") is True
    assert variant_row_matches(vf, EXAMPLE_KIND, "1.10.0") is False


def test_row_matches_current_latest_plus_other():
    present = [*_CATALOG, (None, None)]
    vf = resolve_variant_filter(None, present, default_mode="current")
    assert variant_row_matches(vf, EXAMPLE_KIND, "1.10.0") is True  # latest
    assert variant_row_matches(vf, None, None) is True  # other is always in current
    assert variant_row_matches(vf, EXAMPLE_KIND, "1.2.3") is False  # vieja, archivada


# --- variant_sql_binds: binds for _SQL_TASKS_WHERE --------------------------


def test_sql_binds_all():
    binds = variant_sql_binds(resolve_variant_filter("all", _CATALOG, default_mode="current"))
    assert binds["variant_mode"] == "all"


def test_sql_binds_pair():
    binds = variant_sql_binds(
        resolve_variant_filter(f"{EXAMPLE_KIND}~1.2.3", _CATALOG, default_mode="current")
    )
    assert binds["variant_mode"] == "pair"
    assert binds["variant_kind"] == EXAMPLE_KIND
    assert binds["variant_label"] == "1.2.3"


def test_sql_binds_current_carries_arrays():
    binds = variant_sql_binds(resolve_variant_filter(None, _CATALOG, default_mode="current"))
    assert binds["variant_mode"] == "current"
    assert binds["variant_current_kinds"] == [EXAMPLE_KIND]
    assert binds["variant_current_labels"] == ["1.10.0"]


def test_sql_binds_keys_present_for_every_mode():
    # asyncpg requires every SQL placeholder to be in the binds dict,
    # regardless of mode. All modes must return the same set of keys.
    expected = {
        "variant_mode",
        "variant_kind",
        "variant_label",
        "variant_current_kinds",
        "variant_current_labels",
    }
    for token in (None, "all", "other", EXAMPLE_KIND, f"{EXAMPLE_KIND}~1.10.0"):
        assert (
            set(variant_sql_binds(resolve_variant_filter(token, _CATALOG, default_mode="current")))
            == expected
        )


# --- variant_token: canonical filter token ----------------------------------


@pytest.mark.parametrize(
    "token",
    ["current", "all", "other", EXAMPLE_KIND, f"{EXAMPLE_KIND}~1.2.0 / opt"],
)
def test_variant_token_round_trips(token: str):
    vf = resolve_variant_filter(token, _CATALOG, default_mode="current")
    assert variant_token(vf) == token


# --- build_variant_options: selector structure (render only) --


def test_build_options_marks_current_and_selected_pair():
    present_counts = {
        (EXAMPLE_KIND, "1.10.0"): 12195,
        (EXAMPLE_KIND, "1.2.3"): 10802,
        (None, None): 9,
    }
    vf = resolve_variant_filter(
        f"{EXAMPLE_KIND}~1.2.3", list(present_counts), default_mode="current"
    )
    opts = build_variant_options(present_counts, vf)

    assert opts["has_other"] is True
    assert opts["is_current_default"] is False
    assert opts["selected_token"] == f"{EXAMPLE_KIND}~1.2.3"
    # a single tool, versions sorted desc by semver
    (tool,) = opts["tools"]
    assert tool["kind"] == EXAMPLE_KIND
    labels = [v["label"] for v in tool["versions"]]
    assert labels == ["1.10.0", "1.2.3"]
    latest, older = tool["versions"]
    assert latest["is_current"] is True and latest["selected"] is False
    assert older["is_current"] is False and older["selected"] is True
    assert latest["count"] == 12195 and older["count"] == 10802
    assert older["token"] == f"{EXAMPLE_KIND}{VARIANT_SEP}1.2.3"


def test_build_options_orders_tools_by_kind():
    present_counts = {(OTHER_KIND, "v2.3.0"): 1, (EXAMPLE_KIND, "1.2.3"): 1}
    vf = resolve_variant_filter(None, list(present_counts), default_mode="current")
    opts = build_variant_options(present_counts, vf)
    assert [t["kind"] for t in opts["tools"]] == [EXAMPLE_KIND, OTHER_KIND]


def test_build_options_current_keys_override_other_latest_mark():
    """`current_keys` decouples the `(current)` hint from the vocabulary.

    The outcomes page offers ALL the workspace variants but only
    some have terminal tasks; without the override, the hint would mark the latest
    of the catalog (1.10.0) even if not a single task of its own had finished.
    """
    present_counts = {
        (EXAMPLE_KIND, "1.10.0"): 12195,
        (EXAMPLE_KIND, "1.2.3"): 10802,
    }
    vf = resolve_variant_filter(None, [(EXAMPLE_KIND, "1.2.3")], default_mode="current")
    opts = build_variant_options(present_counts, vf, current_keys={(EXAMPLE_KIND, "1.2.3")})
    (tool,) = opts["tools"]
    latest, older = tool["versions"]
    assert latest["label"] == "1.10.0" and latest["is_current"] is False
    assert older["label"] == "1.2.3" and older["is_current"] is True
    # The vocabulary is NOT trimmed: both versions remain selectable.
    assert [v["label"] for v in tool["versions"]] == ["1.10.0", "1.2.3"]


def test_build_options_without_current_keys_derives_from_present_counts():
    """Without the kwarg, the historical behavior: the latest comes from the vocabulary."""
    present_counts = {
        (EXAMPLE_KIND, "1.10.0"): 1,
        (EXAMPLE_KIND, "1.2.3"): 1,
    }
    vf = resolve_variant_filter(None, list(present_counts), default_mode="current")
    (tool,) = build_variant_options(present_counts, vf)["tools"]
    assert [v["is_current"] for v in tool["versions"]] == [True, False]


def test_build_options_current_default_selects_no_single_version():
    present_counts = {
        (EXAMPLE_KIND, "1.10.0"): 12195,
        (EXAMPLE_KIND, "1.2.3"): 10802,
    }
    vf = resolve_variant_filter(None, list(present_counts), default_mode="current")
    opts = build_variant_options(present_counts, vf)
    assert opts["is_current_default"] is True
    assert opts["has_other"] is False
    assert all(not v["selected"] for t in opts["tools"] for v in t["versions"])


def test_build_options_empty():
    vf = resolve_variant_filter(None, [], default_mode="current")
    opts = build_variant_options({}, vf)
    assert opts == {
        "is_current_default": True,
        "has_other": False,
        "tools": [],
        "selected_token": "current",
    }


# --- accumulate_variant_grid: the single implementation of the loop ----------

_TERMINALS = ("completed", "failed", "cancelled")


def test_accumulate_sums_all_statuses_by_default():
    """Default = dashboard and status page behavior: workspace total."""
    rows = [
        (EXAMPLE_KIND, "1.10.0", "completed", "empty_result", 10),
        (EXAMPLE_KIND, "1.10.0", "queued", None, 3),
        (EXAMPLE_KIND, "1.2.3", "failed", None, 2),
        (None, None, "queued", None, 1),
    ]
    present_counts, grid = accumulate_variant_grid(rows)
    assert present_counts == {
        (EXAMPLE_KIND, "1.10.0"): 13,
        (EXAMPLE_KIND, "1.2.3"): 2,
        (None, None): 1,
    }
    # The grid does NOT re-aggregate: each scan row stays loose so that
    # `variant_row_matches` can re-scale status and outcome without going back to the DB.
    assert grid == rows


def test_accumulate_with_count_statuses_counts_only_those_statuses():
    """The outcomes page: the `· N` follows the terminal tasks, not the workspace."""
    rows = [
        (EXAMPLE_KIND, "1.10.0", "completed", "empty_result", 10),
        (EXAMPLE_KIND, "1.10.0", "failed", None, 5),
        (EXAMPLE_KIND, "1.10.0", "cancelled", None, 1),
        (EXAMPLE_KIND, "1.10.0", "queued", None, 2900),
    ]
    present_counts, _ = accumulate_variant_grid(rows, count_statuses=_TERMINALS)
    assert present_counts == {(EXAMPLE_KIND, "1.10.0"): 16}


def test_accumulate_the_key_survives_with_zero_count():
    """The selector vocabulary comes from ANY row, even one that adds nothing.

    This is the case that justifies the selector: a freshly queued version, with not a single
    terminal task, must remain selectable in the `<select>` of the outcomes page.
    """
    rows = [
        (EXAMPLE_KIND, "1.11.0", "queued", None, 500),
        (EXAMPLE_KIND, "1.10.0", "completed", None, 7),
    ]
    present_counts, _ = accumulate_variant_grid(rows, count_statuses=_TERMINALS)
    assert present_counts == {
        (EXAMPLE_KIND, "1.11.0"): 0,
        (EXAMPLE_KIND, "1.10.0"): 7,
    }
    # With a count of 0 the key stays alive: the selector offers it anyway.
    vf = resolve_variant_filter(None, present_counts.keys(), default_mode="current")
    (tool,) = build_variant_options(present_counts, vf)["tools"]
    assert [v["label"] for v in tool["versions"]] == ["1.11.0", "1.10.0"]


def test_accumulate_normalizes_types_of_the_row():
    """The scan brings status as a postgres enum and `n` as COUNT(*).

    `status::text` already arrives as str in the current queries, but the helper is pure and
    is also fed hand-built rows: normalizing here prevents an
    enum or a Decimal from leaking into the breakdown and breaking comparisons.
    """

    class _StatusEnum:
        def __str__(self) -> str:
            return "completed"

    present_counts, grid = accumulate_variant_grid(
        [(EXAMPLE_KIND, "1.2.3", _StatusEnum(), None, 4.0)]
    )
    assert grid == [(EXAMPLE_KIND, "1.2.3", "completed", None, 4)]
    assert present_counts == {(EXAMPLE_KIND, "1.2.3"): 4}


def test_accumulate_empty_grid():
    assert accumulate_variant_grid([]) == ({}, [])


def test_accumulate_ignores_extra_columns_of_the_row():
    """Scan rows are sequences; only the first 5 columns matter."""
    present_counts, grid = accumulate_variant_grid(
        [(EXAMPLE_KIND, "1.2.3", "completed", "empty_result", 2, "ignored column")]
    )
    assert grid == [(EXAMPLE_KIND, "1.2.3", "completed", "empty_result", 2)]
    assert present_counts == {(EXAMPLE_KIND, "1.2.3"): 2}
