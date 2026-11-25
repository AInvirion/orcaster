"""Parity of the `ops/release` pre-swap gate with `Settings` and the worker guard.

`ops/release/install-release.sh` decides in awk whether an env.N leaves the worker with
a variable from the list of variables that require isolation
(`ORCH_ISOLATION_REQUIRED_ENV`) and without a true `ORCH_ENFORCE_ISOLATION`,
and aborts the deploy if so. The decision has to match the worker's
at startup (pydantic `Settings` and `_ensure_strict_isolation_or_die`): if the
gate approves an env.N that the worker rejects, the deploy continues and the worker
ends up in a crash loop. This test runs the script's real function (extracted from the
file, without sourcing it whole) against the same values the worker receives,
as `podman --env-file` delivers them (with at most a trailing `\\r`, nothing else), so
that shell and Python cannot diverge.

The core names no workload: gate and worker receive the same neutral
list (`EXAMPLE_PROTECTED_VAR`) on the same line of the env.N. A malformed
list aborts both (the gate with code 4, the worker with
`ValidationError`).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from orcaster_worker.config import Settings
from orcaster_worker.main import _ensure_strict_isolation_or_die

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_RELEASE = REPO_ROOT / "ops" / "release" / "install-release.sh"

# Raw values of the `ORCH_ENFORCE_ISOLATION=<value>` line of the env.N.
RAW_FLAG_VALUES = [
    "1",
    "true",
    "True",
    "TRUE",
    "yes",
    "YES",
    "on",
    "On",
    "t",
    "T",
    "y",
    "Y",
    "true\r",
    "0",
    "false",
    "off",
    "no",
    "n",
    "f",
    "",
    " YES ",
    '"on"',
    "'t'",
    " 1",
    "1 ",
    "true\r\r",
    "\ttrue",
    "enabled",
    "2",
]

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("awk") is None,
    reason="the gate runs in bash + awk",
)


PROTECTED = "EXAMPLE_PROTECTED_VAR"
LIST_VAR = "ORCH_ISOLATION_REQUIRED_ENV"

# Raw values of the `ORCH_ISOLATION_REQUIRED_ENV=<value>` line of the env.N.
RAW_LIST_VALUES = [
    PROTECTED,
    f" {PROTECTED} ",
    f"A_OTHER,{PROTECTED}",
    f"A_OTHER {PROTECTED}\tB_OTHER",
    f",,{PROTECTED},,",
    f"{PROTECTED}\r",
    f"{PROTECTED},{PROTECTED}",
    "A_OTHER",
    "",
    "   ",
    "*_TOKEN",
    f'"{PROTECTED}"',
    f"{PROTECTED}=x",
    "1BAD",
    f"{PROTECTED};x",
    f"{PROTECTED}.x",
]


def _gate_function() -> str:
    text = INSTALL_RELEASE.read_text(encoding="utf-8")
    match = re.search(r"^isolation_vars_status\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert match, "isolation_vars_status is not in install-release.sh"
    return match.group(0)


def _podman_delivers(raw: str) -> str:
    # `podman --env-file` yields at most a trailing `\r` and delivers the rest as is.
    return raw[:-1] if raw.endswith("\r") else raw


def _worker_starts(
    monkeypatch: pytest.MonkeyPatch, *, flag: str | None, names: str | None, held: bool
) -> bool:
    """True if `Settings` loads and the startup guard does not exit."""
    for var in ("ORCH_ENFORCE_ISOLATION", LIST_VAR, PROTECTED, "A_OTHER", "B_OTHER"):
        monkeypatch.delenv(var, raising=False)
    if flag is not None:
        monkeypatch.setenv("ORCH_ENFORCE_ISOLATION", flag)
    if names is not None:
        monkeypatch.setenv(LIST_VAR, names)
    if held:
        monkeypatch.setenv(PROTECTED, "parity-value")
    try:
        settings = Settings(_env_file=None)
        _ensure_strict_isolation_or_die(settings)
    except (ValidationError, SystemExit):
        # The worker does not start: the gate must abort the deploy.
        return False
    return True


def _gate_run(env_file: str) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(
        ["bash", "-c", f"{_gate_function()}\nisolation_vars_status"],
        input=env_file.encode(),
        capture_output=True,
        check=False,
    )
    assert proc.returncode in (0, 3, 4), f"the gate failed with {proc.returncode}: {proc.stderr!r}"
    assert proc.stderr == b""
    assert b"parity-value" not in proc.stdout
    if proc.returncode != 3:
        assert proc.stdout == b""
    return proc


def _env_file(*, flag: str | None, names: str | None, held: bool) -> str:
    lines = []
    if names is not None:
        lines.append(f"{LIST_VAR}={names}")
    if held:
        lines.append(f"{PROTECTED}=parity-value")
    if flag is not None:
        lines.append(f"ORCH_ENFORCE_ISOLATION={flag}")
    return "".join(f"{line}\n" for line in lines)


@pytest.mark.parametrize("raw", RAW_FLAG_VALUES, ids=repr)
def test_gate_matches_worker_flag_values(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    gate = _gate_run(_env_file(flag=raw, names=PROTECTED, held=True))
    starts = _worker_starts(monkeypatch, flag=_podman_delivers(raw), names=PROTECTED, held=True)
    assert (gate.returncode == 0) is starts
    if gate.returncode == 3:
        assert gate.stdout.strip() == PROTECTED.encode()


@pytest.mark.parametrize("flag", ["false", "true"], ids=repr)
@pytest.mark.parametrize("raw", RAW_LIST_VALUES, ids=repr)
def test_gate_matches_worker_list_values(
    monkeypatch: pytest.MonkeyPatch, raw: str, flag: str | None
) -> None:
    gate = _gate_run(_env_file(flag=flag, names=raw, held=True))
    starts = _worker_starts(monkeypatch, flag=flag, names=_podman_delivers(raw), held=True)
    assert (gate.returncode == 0) is starts


@pytest.mark.parametrize("raw", RAW_LIST_VALUES, ids=repr)
def test_gate_is_never_laxer_than_worker_without_flag_line(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    # Without the line the worker runs strict (code default `true`),
    # but the gate does not presume it: it requires an explicit value (a live
    # host's env.N carries it stamped). It may abort a deploy the worker would tolerate,
    # never let through one the worker would reject.
    gate = _gate_run(_env_file(flag=None, names=raw, held=True))
    starts = _worker_starts(monkeypatch, flag=None, names=_podman_delivers(raw), held=True)
    assert starts or gate.returncode != 0


@pytest.mark.parametrize("flag", [None, "false", "true"], ids=repr)
def test_gate_matches_worker_without_list(
    monkeypatch: pytest.MonkeyPatch, flag: str | None
) -> None:
    # Empty built-in list: without the line there is nothing to protect, with any flag.
    gate = _gate_run(_env_file(flag=flag, names=None, held=True))
    assert gate.returncode == 0
    assert _worker_starts(monkeypatch, flag=flag, names=None, held=True)


@pytest.mark.parametrize("flag", [None, "false", "true"], ids=repr)
def test_gate_ignores_absent_variable(monkeypatch: pytest.MonkeyPatch, flag: str | None) -> None:
    # Without the variable there is nothing to protect: the gate passes with any flag.
    gate = _gate_run(_env_file(flag=flag, names=PROTECTED, held=False))
    assert gate.returncode == 0
    assert _worker_starts(monkeypatch, flag=flag, names=PROTECTED, held=False)
    blank = _gate_run(f"{LIST_VAR}={PROTECTED}\n{PROTECTED}=  \nORCH_ENFORCE_ISOLATION=false\n")
    assert blank.returncode == 0


def test_gate_last_list_line_wins() -> None:
    # Like podman's `--env-file`: the last occurrence of the line wins.
    env_file = f"{LIST_VAR}={PROTECTED}\n{LIST_VAR}=A_OTHER\n{PROTECTED}=parity-value\n"
    assert _gate_run(env_file).returncode == 0
