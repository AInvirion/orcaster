"""Runner tests. They use standard POSIX binaries (echo, false)."""

from __future__ import annotations

import shutil
import sys

import pytest

from orcaster_worker.runner import execute

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX binaries not available on Windows CI"
)


async def test_execute_echo_captures_stdout():
    echo = shutil.which("echo") or "/bin/echo"
    res = await execute(echo, ["hello", "world"])
    assert res.exit_code == 0
    assert "hello world" in res.stdout
    assert res.stderr == ""


async def test_execute_nonzero_exit():
    false_bin = shutil.which("false") or "/usr/bin/false"
    res = await execute(false_bin)
    assert res.exit_code != 0


async def test_execute_missing_command_raises():
    with pytest.raises(FileNotFoundError):
        await execute("/definitely/not/a/real/binary-xyz", [])
