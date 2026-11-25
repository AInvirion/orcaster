"""Pure unit tests for the artifact filter helpers (no DB)."""

from orcaster_server.api.admin_ui import (
    _artifact_filter_binds,
    _glob_to_ilike,
    _like_prefix,
)


def test_glob_star_and_question():
    assert _glob_to_ilike("report2.5-*.json") == r"report2.5-%.json"
    assert _glob_to_ilike("file-?.txt") == r"file-_.txt"


def test_glob_escapes_sql_wildcards():
    # Literal `%` and `_` in the input must not act as SQL wildcards.
    assert _glob_to_ilike("100%_done") == r"100\%\_done"


def test_glob_escapes_backslash():
    assert _glob_to_ilike(r"a\b") == r"a\\b"


def test_like_prefix_appends_wildcard_and_escapes():
    assert _like_prefix("abc123") == "abc123%"
    assert _like_prefix("a_b%c") == r"a\_b\%c%"


def test_binds_mode_off_when_any_and_no_specific():
    binds = _artifact_filter_binds("any", "", "", "")
    assert binds["artifact_mode"] == "off"


def test_binds_mode_present_when_yes():
    binds = _artifact_filter_binds("yes", "", "", "")
    assert binds["artifact_mode"] == "present"


def test_binds_mode_present_when_any_with_specific():
    binds = _artifact_filter_binds("any", "*.json", "", "")
    assert binds["artifact_mode"] == "present"
    assert binds["artifact_path_like"] == "%.json"


def test_binds_mode_absent_ignores_specific():
    # "no" (no artifacts) ignores path/type/sha: the NOT EXISTS does not use them.
    binds = _artifact_filter_binds("no", "*.json", "text/plain", "abc")
    assert binds["artifact_mode"] == "absent"
    assert binds["artifact_path_like"] == ""
    assert binds["artifact_type"] == ""
    assert binds["artifact_sha256_prefix"] == ""


def test_binds_invalid_mode_falls_back_to_any():
    binds = _artifact_filter_binds("garbage", "", "", "")
    assert binds["artifact_mode"] == "off"


def test_binds_present_carries_type_and_sha():
    binds = _artifact_filter_binds("any", "", "application/json", "deadbeef")
    assert binds["artifact_mode"] == "present"
    assert binds["artifact_type"] == "application/json"
    assert binds["artifact_sha256_prefix"] == "deadbeef%"


def test_binds_sha_lowercased():
    # sha256 is stored as lowercase hex and filtered with LIKE (case-sensitive,
    # to use the btree text_pattern_ops): the bind normalizes to lowercase.
    binds = _artifact_filter_binds("any", "", "", "DEADBEEF")
    assert binds["artifact_sha256_prefix"] == "deadbeef%"
