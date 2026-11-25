"""Generic terminal outcomes: parsing of the `payload.outcomes` block and
exit_code -> (status, outcome) classification. Pure module, no DB.

The core ships no outcomes profile: the default classification of an exit
code only exists if a plugin provides it. Cases with a profile use the `example`
test plugin from `tests/conftest.py`."""

import pytest

from orcaster_server.task_outcomes import (
    OutcomeRule,
    classify_terminal,
    parse_outcomes_block,
)
from tests.conftest import EXAMPLE_OUTCOMES, example_payload

_BLOCK = {"outcomes": {"exit_codes": {"3": {"name": "empty_result", "closes_as": "completed"}}}}


# Ad hoc payload: no extractor recognises it -> `other` variant. Curl commands
# are the typical case (curl exit 7 = could not connect to the host).
_AD_HOC = {"commands": ["curl -fsSL https://example.test/f -o f", "./f"]}


def test_parse_absent_returns_empty() -> None:
    assert parse_outcomes_block(None) == {}
    assert parse_outcomes_block({}) == {}
    assert parse_outcomes_block({"commands": ["echo hi"]}) == {}


def test_parse_valid_block() -> None:
    rules = parse_outcomes_block(_BLOCK)
    assert rules == {3: OutcomeRule(name="empty_result", closes_as="completed")}


def test_parse_several_codes_and_failed() -> None:
    payload = {
        "outcomes": {
            "exit_codes": {
                "4": {"name": "skipped_gated", "closes_as": "completed"},
                "5": {"name": "access_denied", "closes_as": "failed"},
            }
        }
    }
    rules = parse_outcomes_block(payload)
    assert rules[4].closes_as == "completed"
    assert rules[5] == OutcomeRule(name="access_denied", closes_as="failed")


@pytest.mark.parametrize(
    "block,msg",
    [
        ({"outcomes": []}, "must be an object"),
        ({"outcomes": {"exit_codes": []}}, "must be an object"),
        ({"outcomes": {"exit_codes": {}, "extra": 1}}, "unknown keys"),
        (
            {"outcomes": {"exit_codes": {"0": {"name": "x", "closes_as": "completed"}}}},
            "1-255",
        ),
        (
            {"outcomes": {"exit_codes": {"256": {"name": "x", "closes_as": "completed"}}}},
            "1-255",
        ),
        (
            {"outcomes": {"exit_codes": {"abc": {"name": "x", "closes_as": "completed"}}}},
            "1-255",
        ),
        # Superscripts and similar: "²" passes isdigit() but int() rejects it;
        # isdecimal() discards it before int().
        (
            {"outcomes": {"exit_codes": {"²": {"name": "x", "closes_as": "completed"}}}},
            "1-255",
        ),
        (
            {"outcomes": {"exit_codes": {"3": {"name": "x", "closes_as": "completed", "z": 1}}}},
            "unknown keys",
        ),
        ({"outcomes": {"exit_codes": {"3": {"name": "x"}}}}, "closes_as"),
        (
            {"outcomes": {"exit_codes": {"3": {"name": "x", "closes_as": "expired"}}}},
            "closes_as",
        ),
        (
            {"outcomes": {"exit_codes": {"3": {"name": "Empty-Result", "closes_as": "failed"}}}},
            "name",
        ),
        (
            {"outcomes": {"exit_codes": {"3": {"name": "completed", "closes_as": "failed"}}}},
            "reserved",
        ),
        (
            {"outcomes": {"exit_codes": {"3": {"name": "other", "closes_as": "failed"}}}},
            "reserved",
        ),
        (
            {"outcomes": {"exit_codes": {"3": {"name": "expired", "closes_as": "failed"}}}},
            "reserved",
        ),
    ],
)
def test_parse_rejects_invalid_forms(block: dict, msg: str) -> None:
    with pytest.raises(ValueError, match=msg):
        parse_outcomes_block(block)


@pytest.mark.parametrize("raw_code", ["03", "003", "٣"])
def test_parse_normalizes_non_canonical_key(raw_code: str) -> None:
    # The key is normalised to the canonical integer instead of being rejected
    # (leading zeros, Unicode digits). Exit codes are integers.
    payload = {"outcomes": {"exit_codes": {raw_code: {"name": "x", "closes_as": "completed"}}}}
    assert parse_outcomes_block(payload) == {3: OutcomeRule(name="x", closes_as="completed")}


@pytest.mark.parametrize(
    "exit_codes",
    [
        {"3": {"name": "a", "closes_as": "completed"}, "03": {"name": "b", "closes_as": "failed"}},
        {"3": {"name": "a", "closes_as": "completed"}, "٣": {"name": "b", "closes_as": "failed"}},
    ],
)
def test_parse_rejects_exit_code_collision(exit_codes: dict) -> None:
    # Two distinct JSON keys that normalise to the same exit code are ambiguous
    # (the last one would silently win): it must fail explicitly.
    with pytest.raises(ValueError, match="already declared"):
        parse_outcomes_block({"outcomes": {"exit_codes": exit_codes}})


def test_classify_non_canonical_key_is_not_lost() -> None:
    # A task created under the lax validation with key "03" no longer falls back to
    # the default because of a ValueError; the declared rule applies.
    payload = {"outcomes": {"exit_codes": {"03": {"name": "no_layers", "closes_as": "failed"}}}}
    assert classify_terminal("failed", 3, payload) == ("failed", "no_layers")


@pytest.mark.parametrize("payload", [None, {}, _AD_HOC])
def test_classify_without_plugin_never_labels_an_exit_code(payload: dict | None) -> None:
    # Core without plugins: no exit code has a default classification. Without a
    # `payload.outcomes` block, every failed close is a plain `failed` with NULL outcome.
    for exit_code in range(1, 256):
        assert classify_terminal("failed", exit_code, payload) == ("failed", None), exit_code


def test_classify_without_plugin_ignores_example_tool_shape() -> None:
    # Without the plugin, an `example-tool`-shaped payload is not recognised: the payload
    # shape is not enough to apply a profile that no plugin registered.
    for exit_code in EXAMPLE_OUTCOMES:
        assert classify_terminal("failed", exit_code, example_payload()) == ("failed", None)


def test_classify_profile_applies_to_recognised_payload(example_plugin) -> None:
    # With the plugin, the profile labels a task of its tool that has no block of its own.
    assert classify_terminal("failed", 3, example_payload()) == ("completed", "empty_result")
    for exit_code, rule in EXAMPLE_OUTCOMES.items():
        assert classify_terminal("failed", exit_code, example_payload()) == (
            rule.closes_as,
            rule.name,
        ), exit_code


def test_classify_profile_does_not_reach_unrecognised_payload(example_plugin) -> None:
    # The profile is scoped per tool. A task the extractor does not recognise
    # (`other` variant) closes as a plain `failed` even if the exit code matches.
    for exit_code in EXAMPLE_OUTCOMES:
        assert classify_terminal("failed", exit_code, _AD_HOC) == ("failed", None), exit_code
        assert classify_terminal("failed", exit_code, None) == ("failed", None), exit_code
        assert classify_terminal("failed", exit_code, {}) == ("failed", None), exit_code


def test_classify_profile_does_not_invent_codes(example_plugin) -> None:
    # A code outside the profile is not invented, even for a recognised task.
    assert classify_terminal("failed", 1, example_payload()) == ("failed", None)
    assert classify_terminal("failed", 8, example_payload()) == ("failed", None)


def test_classify_payload_block_wins_over_profile(example_plugin) -> None:
    payload = {
        **example_payload(),
        "outcomes": {
            "exit_codes": {
                "3": {"name": "custom_empty", "closes_as": "failed"},
                "4": {"name": "custom_invalid", "closes_as": "completed"},
            }
        },
    }
    assert classify_terminal("failed", 3, payload) == ("failed", "custom_empty")
    assert classify_terminal("failed", 4, payload) == ("completed", "custom_invalid")
    # A code the block does not declare still falls back to the profile.
    assert classify_terminal("failed", 5, payload) == ("failed", "access_denied")


def test_classify_payload_block_applies_without_plugin() -> None:
    # The payload block depends neither on the registry nor on the tool: the declared
    # vocabulary belongs to the task.
    own = {
        **_AD_HOC,
        "outcomes": {"exit_codes": {"7": {"name": "no_connection", "closes_as": "failed"}}},
    }
    assert classify_terminal("failed", 7, own) == ("failed", "no_connection")


def test_classify_only_reclassifies_failed() -> None:
    # Mirror of the terminal status taxonomy: cancelled/completed pass through
    # untouched, ignoring exit_code.
    assert classify_terminal("cancelled", 3, _BLOCK) == ("cancelled", None)
    assert classify_terminal("completed", 3, _BLOCK) == ("completed", None)
    assert classify_terminal("completed", 0, None) == ("completed", None)
    assert classify_terminal("failed", None, _BLOCK) == ("failed", None)


def test_classify_non_failed_statuses_pass_through_with_plugin(example_plugin) -> None:
    assert classify_terminal("cancelled", 3, example_payload()) == ("cancelled", None)
    assert classify_terminal("completed", 3, example_payload()) == ("completed", None)
    assert classify_terminal("failed", None, example_payload()) == ("failed", None)


def test_classify_without_match_is_plain_failed() -> None:
    assert classify_terminal("failed", 1, _BLOCK) == ("failed", None)
    assert classify_terminal("failed", 137, None) == ("failed", None)


def test_classify_malformed_payload_does_not_break(example_plugin) -> None:
    # An old task with a broken block must NEVER break /complete: the block is ignored
    # and the profile of its tool applies, if it has one.
    broken = {**example_payload(), "outcomes": {"exit_codes": {"3": "no-es-dict"}}}
    assert classify_terminal("failed", 3, broken) == ("completed", "empty_result")
    assert classify_terminal("failed", 1, broken) == ("failed", None)
    # A broken block on an unrecognised task falls back to a plain `failed`: the
    # unreadable block does not widen the profile scope.
    broken_ad_hoc = {**_AD_HOC, "outcomes": {"exit_codes": {"3": "no-es-dict"}}}
    assert classify_terminal("failed", 3, broken_ad_hoc) == ("failed", None)


def test_classify_malformed_payload_without_plugin_is_plain_failed() -> None:
    broken = {**example_payload(), "outcomes": {"exit_codes": {"3": "no-es-dict"}}}
    assert classify_terminal("failed", 3, broken) == ("failed", None)
