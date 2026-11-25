"""Parity of the reserved-name denylist and of the name rules.

The server rejects on save the names that the worker discards on receipt.
The two lists live in different packages, so this test reads the worker source
with `ast` (without importing the package) and requires that names, prefixes and
suffixes are identical. The same applies to the variable-name pattern: the server
rejects on creation what the worker would discard on execution. And to the token
prefix: the server rejects on creation an entry that starts with a known prefix
(`ghp_`, `gho_`...) and the worker does not list it in its logs with the same
pattern. The worker has the mirror test of the denylist.

For each pattern, the text, the flags and the method the other side applies it
with are compared: the three can drift separately and only the first is visible
when reading the pattern. And since the parity derives the expected value from
the other side's declaration, it cannot audit that declaration (two copies that
both added `re.I` would still agree), so at the end there are behavior tests for
this side that do not read the other source.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from orcaster_server.env_names import (
    _ENV_NAME_RE,
    _TOKEN_PREFIX_RE,
    RESERVED_SECRET_NAMES,
    RESERVED_SECRET_PREFIXES,
    RESERVED_SECRET_SUFFIXES,
    is_valid_env_name,
    validate_task_env_entries,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_TASK_ENV = REPO_ROOT / "client" / "src" / "orcaster_worker" / "task_env.py"


def _literal_constants(path: Path) -> dict[str, object]:
    """Module constants assigned a literal, or `frozenset(<literal>)`."""
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


def test_reserved_names_match_worker() -> None:
    worker = _literal_constants(WORKER_TASK_ENV)
    assert set(worker["RESERVED_ENV_NAMES"]) == set(RESERVED_SECRET_NAMES)
    assert tuple(worker["RESERVED_ENV_PREFIXES"]) == RESERVED_SECRET_PREFIXES
    assert tuple(worker["RESERVED_ENV_SUFFIXES"]) == RESERVED_SECRET_SUFFIXES


def test_reserved_lists_are_uppercase() -> None:
    """The comparison is `name.upper()` against these lists."""
    for entry in (*RESERVED_SECRET_NAMES, *RESERVED_SECRET_PREFIXES, *RESERVED_SECRET_SUFFIXES):
        assert entry == entry.upper(), entry


def test_env_name_pattern_matches_worker() -> None:
    """The server and the worker apply the same variable-name pattern.

    Pattern AND flags: a `re.I` on one side does not change a letter of the pattern.
    """
    worker = _compiled_regex(WORKER_TASK_ENV, "_VALID_NAME")
    assert _ENV_NAME_RE.pattern == worker.pattern
    assert _ENV_NAME_RE.flags == worker.flags


def test_token_prefix_pattern_matches_worker() -> None:
    """The server rejects on creation what the worker refuses to list in its logs."""
    worker = _compiled_regex(WORKER_TASK_ENV, "_TOKEN_PREFIX_RE")
    assert _TOKEN_PREFIX_RE.pattern == worker.pattern
    assert _TOKEN_PREFIX_RE.flags == worker.flags


# --- parity of FLAGS and of the match method ---------------------------------
#
# Comparing only the pattern text lets through two drifts that change the
# behavior without changing a letter of the pattern: a flag (`re.I` on one side would
# make a token-named variable be rejected as a credential) and the method it is called with
# (`search` instead of `match` would reject a name with an embedded prefix). Both are tested here.
#
# What each half tests, which are two distinct claims:
#
# - the CONFORMITY between the two copies (pattern, flags and method) is given by
#   the tests that read the other source with `ast`. They derive the expected value
#   from the declaration, so they cannot audit the declaration: if both copies
#   added `re.I`, the parity would stay green.
# - the RULE itself is fixed by the behavior tests, which do not read the other
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
    """Evaluate a `re.compile` flags expression (`re.I`, `re.I | re.M`, `0`).

    Fails loudly on any other form. An `except: return 0` here would be a check
    whose failure mode is silence: a flag that cannot be read would be
    indistinguishable from there being no flags.
    """
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _flag_value(node.left) | _flag_value(node.right)
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return int(node.value)
    if isinstance(node, ast.Attribute) and node.attr in _FLAG_NAMES:
        return int(_FLAG_NAMES[node.attr])
    if isinstance(node, ast.Name) and node.id in _FLAG_NAMES:
        return int(_FLAG_NAMES[node.id])
    raise AssertionError(f"cannot read the re.compile flags: {ast.dump(node)}")


def _compiled_regex(path: Path, name: str) -> re.Pattern[str]:
    """Recompile the `re.compile(<pattern>, <flags>)` of `<name>` in the source of `path`.

    Returns a compiled object rather than the text, so that `.pattern` and
    `.flags` can be compared with the other side's: `re.Pattern.flags` includes the
    implicit `re.UNICODE` of a `str` pattern, so comparing against the recompiled
    one from the other side is the only way to avoid a false negative.
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

    Every READ of the name must be an attribute access (`<name>.match`).
    If someone passes it elsewhere (`RE = <name>`; `RE.search(x)`, or passing it
    as an argument), the method stops being visible here: that fails instead of
    returning an incomplete set. The assignment that defines it (`ast.Store`) is
    not a read and does not count.
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


def test_worker_applies_the_patterns_with_the_same_methods() -> None:
    """The method the worker applies each pattern with, not only the pattern.

    `_VALID_NAME` is evaluated over the whole string (`fullmatch`, not `match` with
    `$`: `$` accepts a trailing newline) and `_TOKEN_PREFIX_RE` only at the start
    (`match`, not `search`: a name with an embedded prefix is valid). This side applies them
    the same way, and the behavior tests below fix that.
    """
    assert _methods_called_on(WORKER_TASK_ENV, "_VALID_NAME") == {"fullmatch"}
    assert _methods_called_on(WORKER_TASK_ENV, "_TOKEN_PREFIX_RE") == {"match"}


# --- behavior of THIS side (derives nothing from the other source) -----------


@pytest.mark.parametrize(
    "name",
    [
        # Uppercase: with `re.I`, each of these would be rejected as a credential.
        "HF_TOKEN",
        "GHP_TOKEN",
        "GHO_X",
        "GITHUB_PAT_X",
        # Anchored at the start: with `search`, each of these would be rejected.
        "MY_hf_VAR",
        "X_ghp_Y",
        "A_github_pat_B",
        "SK_X",
        "GH_TOKEN",
    ],
)
def test_a_name_that_only_contains_a_token_prefix_is_accepted(name: str) -> None:
    """The guard is PREFIX-based and case-sensitive: `_TOKEN_PREFIX_RE.match`."""
    validate_task_env_entries([name], field="payload.task_env")


@pytest.mark.parametrize("entry", ["hf_x", "ghp_x", "gho_x", "github_pat_x", "glpat-x", "sk-x"])
def test_an_entry_that_starts_with_a_token_prefix_is_rejected(entry: str) -> None:
    """Positive control for the test above: without it, an inert guard would pass."""
    with pytest.raises(ValueError, match="known token prefix"):
        validate_task_env_entries([entry], field="payload.task_env")


@pytest.mark.parametrize("name", ["OK", "_X", "A0", "a_b_9"])
def test_a_valid_name_is_a_valid_name(name: str) -> None:
    assert is_valid_env_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        # `match` would accept the first three (it matches a prefix and stops).
        "OK\n",
        "OK X",
        "OK=v",
        # `search` would accept the last two (it finds a name further inside).
        "1OK",
        "-OK",
    ],
)
def test_a_name_is_judged_whole(name: str) -> None:
    """`_ENV_NAME_RE.fullmatch`: neither a valid prefix nor an inner fragment is enough."""
    assert is_valid_env_name(name) is False
