"""Parity of the reserved-names denylist between worker and server.

On receipt, the worker drops the names the server rejects when saving.
The two lists live in different packages, so this test reads the server's source
with `ast` (without importing the package) and requires names, prefixes and
suffixes to be identical. The server has the mirror test.

For each pattern, the text, the flags and the method the other side uses to
apply it are compared: the three can drift separately and only the first is visible
when reading the pattern. And since parity derives the expectation from the other side's
declaration, it cannot audit it -- two copies that added `re.I` together would still agree --,
so at the end there are behavior tests on this side, which do not read the other
source.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from orcaster_worker.task_env import (
    _TOKEN_PREFIX_RE,
    _VALID_NAME,
    BASE_ENV_PASSTHROUGH,
    RESERVED_ENV_NAMES,
    RESERVED_ENV_PREFIXES,
    RESERVED_ENV_SUFFIXES,
    is_reserved_env_name,
    missing_declared_names,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ENV_NAMES = REPO_ROOT / "server" / "orcaster_server" / "env_names.py"


def _literal_constants(path: Path) -> dict[str, object]:
    """Module constants assigned with a literal, or `frozenset(<literal>)`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value = node.target.id, node.value
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name, value = node.targets[0].id, node.value
        else:
            continue
        if value is None:
            continue
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "frozenset"
            and len(value.args) == 1
        ):
            value = value.args[0]
        try:
            out[name] = ast.literal_eval(value)
        except ValueError:
            continue
    return out


def test_reserved_names_match_server() -> None:
    server = _literal_constants(SERVER_ENV_NAMES)
    assert set(server["RESERVED_SECRET_NAMES"]) == set(RESERVED_ENV_NAMES)
    assert tuple(server["RESERVED_SECRET_PREFIXES"]) == RESERVED_ENV_PREFIXES
    assert tuple(server["RESERVED_SECRET_SUFFIXES"]) == RESERVED_ENV_SUFFIXES


def test_reserved_lists_are_uppercase() -> None:
    """The comparison is `name.upper()` against these lists."""
    for entry in (*RESERVED_ENV_NAMES, *RESERVED_ENV_PREFIXES, *RESERVED_ENV_SUFFIXES):
        assert entry == entry.upper(), entry


def test_base_passthrough_and_path_are_reserved() -> None:
    """The worker's base environment is never taken from the server."""
    for name in ("PATH", *BASE_ENV_PASSTHROUGH):
        assert name in RESERVED_ENV_NAMES, name


@pytest.mark.parametrize(
    "name",
    ["path", "Path", "Ld_Preload", "git_ssh_command", "pythonpath", "http_proxy", "x_ca_bundle"],
)
def test_reserved_is_case_insensitive(name: str) -> None:
    assert is_reserved_env_name(name)


@pytest.mark.parametrize(
    "name",
    ["GITHUB_TOKEN", "HF_TOKEN", "GITHUB_TOKENS", "OPENROUTER_API_KEY", "hf_token"],
)
def test_common_tokens_stay_allowed(name: str) -> None:
    assert not is_reserved_env_name(name)


# --- parity of FLAGS and of the match method ----------------------------------
#
# Comparing only the pattern text lets through two drifts that change the
# behavior without changing a letter of the pattern: a flag (`re.I` on one side would
# make `HF_TOKEN` be rejected as a credential) and the method it is called with
# (`search` instead of `match` would reject `MY_hf_VAR`). Both are tested here.
#
# What each half proves, which are two distinct claims:
#
# - CONFORMITY between the two copies (pattern, flags and method) is given by the tests
#   that read the other source with `ast`. They derive the expectation from the declaration, so
#   they cannot audit the declaration: if both copies added `re.I`, parity
#   would stay green.
# - the RULE itself is pinned by the behavior tests, which do not read the other
#   source: they run the input through THIS side's code and require the result.
#   They are the ones that fail if both copies drift together.


_FLAG_NAMES = {
    "A": re.A,
    "ASCII": re.A,
    "I": re.I,
    "IGNORECASE": re.I,
    "L": re.L,
    "LOCALE": re.L,
    "M": re.M,
    "MULTILINE": re.M,
    "S": re.S,
    "DOTALL": re.S,
    "U": re.U,
    "UNICODE": re.U,
    "X": re.X,
    "VERBOSE": re.X,
    "NOFLAG": re.NOFLAG,
}


def _flag_value(node: ast.expr) -> int:
    """Evaluates a flags expression of `re.compile` (`re.I`, `re.I | re.M`, `0`).

    Fails loudly on any other form. An `except: return 0` here would be
    a check whose failure mode is silence: a flag that cannot be read
    would be indistinguishable from having no flags.
    """
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _flag_value(node.left) | _flag_value(node.right)
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return int(node.value)
    if isinstance(node, ast.Attribute) and node.attr in _FLAG_NAMES:
        return int(_FLAG_NAMES[node.attr])
    if isinstance(node, ast.Name) and node.id in _FLAG_NAMES:
        return int(_FLAG_NAMES[node.id])
    raise AssertionError(f"cannot read the flags of re.compile: {ast.dump(node)}")


def _compiled_regex(path: Path, name: str) -> re.Pattern[str]:
    """Recompiles the `re.compile(<pattern>, <flags>)` of `<name>` in the source of `path`.

    Returns a compiled object rather than the text, so that `.pattern` and
    `.flags` can be compared with the other side's: `re.Pattern.flags` includes the
    implicit `re.UNICODE` of a `str` pattern, so comparing against the other side
    recompiled is the only way to avoid a false negative.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
        ):
            continue
        call = node.value
        assert isinstance(call, ast.Call), name
        assert isinstance(call.func, ast.Attribute) and call.func.attr == "compile", name
        assert len(call.args) <= 2, f"{name}: re.compile with unexpected args"
        pattern = ast.literal_eval(call.args[0])
        assert isinstance(pattern, str), name
        flags = 0
        if len(call.args) == 2:
            flags = _flag_value(call.args[1])
        for kw in call.keywords:
            assert kw.arg == "flags", f"{name}: unexpected kwarg {kw.arg!r} in re.compile"
            flags |= _flag_value(kw.value)
        return re.compile(pattern, flags)
    raise AssertionError(f"{name} is not assigned in {path}")


def _methods_called_on(path: Path, name: str) -> set[str]:
    """The methods `<name>` is used with in the source of `path`.

    Every occurrence of the name has to be an attribute access (`<name>.match`).
    If someone passes it around some other way (`RE = <name>`; `RE.search(x)`), the method
    stops being visible here: that fails instead of returning an incomplete set.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    methods: set[str] = set()
    attribute_values = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == name:
                methods.add(node.attr)
        elif (
            isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, ast.Load)
            and id(node) not in attribute_values
        ):
            raise AssertionError(
                f"{name} is used in {path.name} without calling a method on it "
                "(an alias?): this test can no longer see which method is applied"
            )
    assert methods, f"{name} is not used in {path}"
    return methods


def test_env_name_pattern_matches_server() -> None:
    """The worker and the server apply the same variable-name pattern.

    Pattern AND flags: a `re.I` on one side does not change a letter of the pattern.
    """
    server = _compiled_regex(SERVER_ENV_NAMES, "_ENV_NAME_RE")
    assert _VALID_NAME.pattern == server.pattern
    assert _VALID_NAME.flags == server.flags


def test_token_prefix_pattern_matches_server() -> None:
    """The worker does not list in its logs what the server rejects on create."""
    server = _compiled_regex(SERVER_ENV_NAMES, "_TOKEN_PREFIX_RE")
    assert _TOKEN_PREFIX_RE.pattern == server.pattern
    assert _TOKEN_PREFIX_RE.flags == server.flags


def test_server_applies_the_patterns_with_the_same_methods() -> None:
    """The method the server applies each pattern with, not only the pattern.

    `_ENV_NAME_RE` is evaluated against the whole string (`fullmatch`, not `match` with
    `$`: `$` accepts a trailing newline) and `_TOKEN_PREFIX_RE` only at the start
    (`match`, not `search`: `MY_hf_VAR` is a valid name). This side applies them
    the same way, and the behavior tests below pin that.
    """
    assert _methods_called_on(SERVER_ENV_NAMES, "_ENV_NAME_RE") == {"fullmatch"}
    assert _methods_called_on(SERVER_ENV_NAMES, "_TOKEN_PREFIX_RE") == {"match"}


# --- behavior of THIS side (derives nothing from the other source) -----------
#
# `missing_declared_names` is the public route of `_listable_name`: a valid name
# is LISTED (comes back in the list) and an entry that has no name shape or starts
# with a token prefix is only COUNTED, because its text may be a secret.


def _listed(name: str) -> tuple[list[str], int]:
    return missing_declared_names([name], source={}, secrets={})


@pytest.mark.parametrize(
    "name",
    [
        # Uppercase: with `re.I`, each of these would be counted instead of listed.
        "HF_TOKEN",
        "GHP_TOKEN",
        "GHO_X",
        "GITHUB_PAT_X",
        # Anchored at the start: with `search`, each of these would be counted.
        "MY_hf_VAR",
        "X_ghp_Y",
        "A_github_pat_B",
        "SK_X",
        "GH_TOKEN",
    ],
)
def test_a_name_that_only_contains_a_token_prefix_is_listed(name: str) -> None:
    """The guard is PREFIX-based and case-sensitive: `_TOKEN_PREFIX_RE.match`."""
    assert _listed(name) == ([name], 0)


@pytest.mark.parametrize("entry", ["hf_x", "ghp_x", "gho_x", "github_pat_x", "glpat-x", "sk-x"])
def test_an_entry_that_starts_with_a_token_prefix_is_only_counted(entry: str) -> None:
    """Positive control of the test above: without this, an inert guard would pass it."""
    assert _listed(entry) == ([], 1)


@pytest.mark.parametrize("name", ["OK", "_X", "A0", "a_b_9"])
def test_a_valid_name_is_listed(name: str) -> None:
    assert _listed(name) == ([name], 0)


@pytest.mark.parametrize(
    "name",
    [
        # `match` would list the first three (it matches a prefix and stops).
        "OK\n",
        "OK X",
        "OK=v",
        # `search` would list the last two (it finds a name further inside).
        "1OK",
        "-OK",
    ],
)
def test_a_name_is_judged_whole(name: str) -> None:
    """`_VALID_NAME.fullmatch`: neither a valid prefix nor an inner piece is enough."""
    assert _listed(name) == ([], 1)
