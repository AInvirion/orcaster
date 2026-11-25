"""Tests for the push-command dispatcher.

Covers only ``run_batch`` (reduced scope).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from orcaster_worker.config import Settings
from orcaster_worker.dispatcher import dispatch_run_batch

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="run_batch tests assume POSIX /bin/sh; Windows path not yet validated.",
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        server_url="http://server.test",  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        run_batch_per_command_timeout_seconds=10,
    )


async def test_run_batch_success_two_commands(settings: Settings) -> None:
    """Two echos, both exit=0, stdout captured via on_output in order, status 'ok'."""
    stdout_chunks: list[str] = []

    async def on_output(out=None, err=None, command=None) -> None:
        if out:
            stdout_chunks.append(out)

    status, result = await dispatch_run_batch(
        {"commands": ["echo hello", "echo world"], "timeout_seconds": 5},
        settings,
        on_output=on_output,
    )
    assert status == "ok"
    assert result["exit_codes"] == [0, 0]
    assert result["failed_at"] is None
    assert "stdout" not in result and "stderr" not in result
    joined = "".join(stdout_chunks)
    assert "hello" in joined
    assert "world" in joined
    # Order: hello appears before world.
    assert joined.index("hello") < joined.index("world")


async def test_run_batch_stops_on_failure(settings: Settings) -> None:
    """Command 2 fails with exit=2; command 3 is NOT executed; failed_at=1."""
    stdout_chunks: list[str] = []

    async def on_output(out=None, err=None, command=None) -> None:
        if out:
            stdout_chunks.append(out)

    status, result = await dispatch_run_batch(
        {
            "commands": [
                "echo first",
                "sh -c 'exit 2'",
                "echo should-not-run",
            ],
            "timeout_seconds": 5,
        },
        settings,
        on_output=on_output,
    )
    assert status == "error"
    assert result["exit_codes"] == [0, 2]
    assert result["failed_at"] == 1
    assert "stdout" not in result and "stderr" not in result
    joined = "".join(stdout_chunks)
    assert "first" in joined
    assert "should-not-run" not in joined


async def test_run_batch_timeout_kills_process(settings: Settings) -> None:
    """sleep 60 with timeout_seconds=1 -> status 'timeout'.

    We mock ``kill_process_group`` to avoid blocking the test if the
    real process is slow; but the inner ``asyncio.wait_for`` guarantees
    that we return quickly.
    """
    stderr_chunks: list[str] = []

    async def on_output(out=None, err=None, command=None) -> None:
        if err:
            stderr_chunks.append(err)

    with patch("orcaster_worker.dispatcher.kill_process_group") as mock_kill:
        status, result = await dispatch_run_batch(
            {"commands": ["sleep 60"], "timeout_seconds": 1},
            settings,
            on_output=on_output,
        )

    assert status == "timeout"
    assert mock_kill.called, "kill_process_group must be invoked on timeout"
    # We report failed_at=None (it was not an exit_code != 0, it was a timeout).
    assert result["failed_at"] is None
    assert "stdout" not in result and "stderr" not in result
    joined = "".join(stderr_chunks)
    assert "timeout" in joined.lower()


async def test_run_batch_kill_uses_configurable_cancel_grace(tmp_path: Path) -> None:
    """The kill grace (SIGTERM->SIGKILL) must come from ``settings.cancel_grace_seconds``,
    not from a hardcoded value. We check the second positional arg of
    ``kill_process_group`` when a command times out."""
    s = Settings(
        server_url="http://server.test",  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        run_batch_per_command_timeout_seconds=10,
        cancel_grace_seconds=3.0,
    )
    with patch("orcaster_worker.dispatcher.kill_process_group") as mock_kill:
        status, _ = await dispatch_run_batch(
            {"commands": ["sleep 60"], "timeout_seconds": 0.2},
            s,
        )

    assert status == "timeout"
    assert mock_kill.called, "kill_process_group must be invoked on timeout"
    grace_arg = mock_kill.call_args.args[1]
    assert grace_arg == 3.0, f"esperaba grace=cancel_grace_seconds=3.0, got {grace_arg!r}"


async def test_run_batch_empty_commands_is_ok(settings: Settings) -> None:
    """``commands=[]`` -> status 'ok', exit_codes=[], no output."""
    chunks: list[tuple[str | None, str | None]] = []

    async def on_output(out=None, err=None, command=None) -> None:
        chunks.append((out, err))

    status, result = await dispatch_run_batch(
        {"commands": [], "timeout_seconds": 5},
        settings,
        on_output=on_output,
    )
    assert status == "ok"
    assert result["exit_codes"] == []
    assert result["failed_at"] is None
    assert "stdout" not in result and "stderr" not in result
    assert chunks == []


async def test_run_batch_missing_commands_returns_error(settings: Settings) -> None:
    """Payload without the ``commands`` key -> status 'error' with a message via on_output."""
    stderr_chunks: list[str] = []

    async def on_output(out=None, err=None, command=None) -> None:
        if err:
            stderr_chunks.append(err)

    status, result = await dispatch_run_batch(
        {"timeout_seconds": 5},
        settings,
        on_output=on_output,
    )
    assert status == "error"
    assert result["exit_codes"] == []
    assert "stdout" not in result and "stderr" not in result
    assert any("commands" in chunk for chunk in stderr_chunks)


async def test_run_batch_invalid_commands_type_returns_error(settings: Settings) -> None:
    """``commands`` that is not a list of strings -> status 'error' via on_output."""
    stderr_chunks: list[str] = []

    async def on_output(out=None, err=None, command=None) -> None:
        if err:
            stderr_chunks.append(err)

    status, result = await dispatch_run_batch(
        {"commands": ["ok", 42, "also-ok"]},  # type: ignore[list-item]
        settings,
        on_output=on_output,
    )
    assert status == "error"
    assert "stdout" not in result and "stderr" not in result
    assert any("commands" in chunk for chunk in stderr_chunks)


async def test_run_batch_payload_not_dict_returns_error(settings: Settings) -> None:
    """Payload None or non-dict -> status 'error' with a message via on_output."""
    stderr_chunks: list[str] = []

    async def on_output(out=None, err=None, command=None) -> None:
        if err:
            stderr_chunks.append(err)

    status, result = await dispatch_run_batch(None, settings, on_output=on_output)
    assert status == "error"
    assert "stdout" not in result and "stderr" not in result
    joined = "".join(stderr_chunks)
    assert "object" in joined.lower() or "dict" in joined.lower()


async def test_run_batch_uses_default_timeout_when_missing(settings: Settings) -> None:
    """Without ``timeout_seconds``, it uses settings.run_batch_per_command_timeout_seconds."""
    # settings has per_command=10; echo finishes instantly.
    status, result = await dispatch_run_batch(
        {"commands": ["echo quick"]},
        settings,
    )
    assert status == "ok"
    assert result["exit_codes"] == [0]


async def test_run_batch_propagates_env_to_subprocess(settings: Settings) -> None:
    """The caller can inject an env dict (e.g. the venv's PATH)
    that replaces the subprocess env."""
    stdout_chunks: list[str] = []

    async def on_output(out=None, err=None, command=None) -> None:
        if out:
            stdout_chunks.append(out)

    status, result = await dispatch_run_batch(
        {"commands": ["echo $ORCASTER_TEST_MARKER"], "timeout_seconds": 5},
        settings,
        env={"ORCASTER_TEST_MARKER": "marker-ok", "PATH": "/usr/bin:/bin"},
        on_output=on_output,
    )
    assert status == "ok"
    assert "marker-ok" in "".join(stdout_chunks)


async def test_dispatch_run_batch_returns_abandoned_status_when_abandon_fires(
    settings: Settings,
) -> None:
    """If abandon_event is set mid-batch, the dispatcher must return
    ``("abandoned", result)`` -- not ``"ok"`` -- so that _run_external can
    tell the case apart via the return value (not by re-reading the event).

    Strategy: the first command finishes quickly; the second is ``sleep`` and
    during its execution we set the abandon_event from an auxiliary task.
    """
    abandon_event = asyncio.Event()
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    async def on_output(out=None, err=None, command=None) -> None:
        if out:
            stdout_chunks.append(out)
        if err:
            stderr_chunks.append(err)

    async def _set_after(delay: float) -> None:
        await asyncio.sleep(delay)
        abandon_event.set()

    setter = asyncio.create_task(_set_after(0.3))
    try:
        with patch("orcaster_worker.dispatcher.kill_process_group"):
            status, result = await dispatch_run_batch(
                {
                    "commands": ["echo first", "sleep 30"],
                    "timeout_seconds": 10,
                },
                settings,
                abandon_event=abandon_event,
                on_output=on_output,
            )
    finally:
        if not setter.done():
            setter.cancel()

    assert status == "abandoned", f"expected 'abandoned', got {status!r}"
    assert "stdout" not in result and "stderr" not in result
    assert "first" in "".join(stdout_chunks)
    assert "abandoned" in "".join(stderr_chunks).lower()
    assert result["failed_at"] is None


async def test_run_batch_env_none_uses_inherited_env(settings: Settings) -> None:
    """If env=None (default), the subprocess inherits the worker's env."""
    import os

    stdout_chunks: list[str] = []

    async def on_output(out=None, err=None, command=None) -> None:
        if out:
            stdout_chunks.append(out)

    os.environ["ORCASTER_INHERIT_TEST"] = "inherited-value"
    try:
        status, result = await dispatch_run_batch(
            {"commands": ["echo $ORCASTER_INHERIT_TEST"], "timeout_seconds": 5},
            settings,
            on_output=on_output,
        )
        assert status == "ok"
        assert "inherited-value" in "".join(stdout_chunks)
    finally:
        os.environ.pop("ORCASTER_INHERIT_TEST", None)


async def test_run_batch_orcaster_cache_dir_reaches_subprocess(
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Scratch cache: if the caller passes ORCASTER_CACHE_DIR in env, the
    subprocess sees it and the dir exists on disk.

    Smoke test of the dispatcher<->subprocess contract; the real injection from
    `_run_external` is covered in test_executor_task.py.
    """
    from orcaster_worker.task_cache import ensure_task_cache_dir

    stdout_chunks: list[str] = []

    async def on_output(out=None, err=None, command=None) -> None:
        if out:
            stdout_chunks.append(out)

    cache_dir = ensure_task_cache_dir(tmp_path / "cache", "smoke-key")
    status, result = await dispatch_run_batch(
        {
            "commands": [
                'test -d "$ORCASTER_CACHE_DIR" && echo cache-dir-ok',
            ],
            "timeout_seconds": 5,
        },
        settings,
        env={"ORCASTER_CACHE_DIR": str(cache_dir), "PATH": "/usr/bin:/bin"},
        on_output=on_output,
    )
    assert status == "ok"
    assert "cache-dir-ok" in "".join(stdout_chunks)


# ---------------------------------------------------------------------------
# Per-command timeout
# ---------------------------------------------------------------------------


@pytest.fixture
def settings_per_cmd(tmp_path: Path) -> Settings:
    """Settings with a 1 second per-command timeout (for timeout tests)."""
    return Settings(
        server_url="http://server.test",  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        run_batch_per_command_timeout_seconds=1,
    )


async def test_per_command_timeout_cmd2_timeouts_cmd3_runs(settings: Settings) -> None:
    """Batch of 3 commands: #1 ok, #2 times out, #3 still runs with a fresh window.

    With per-command semantics, cmd #2's timeout does not steal time from
    cmd #3; it gets its own full window and finishes ok.
    """
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    async def on_output(out=None, err=None, command=None) -> None:
        if out:
            stdout_chunks.append(out)
        if err:
            stderr_chunks.append(err)

    with patch("orcaster_worker.dispatcher.kill_process_group"):
        status, result = await dispatch_run_batch(
            {
                "commands": [
                    "echo first",
                    "sleep 60",  # times out with timeout_seconds=1
                    "echo third",
                ],
                "timeout_seconds": 1,
            },
            settings,
            on_output=on_output,
        )

    # The batch ends with status "timeout" because at least one command timed out.
    assert status == "timeout"
    assert "stdout" not in result and "stderr" not in result
    # Cmd #1 ran ok (exit_code 0).
    assert result["exit_codes"][0] == 0
    # stderr identifies which command timed out (index 1).
    joined_err = "".join(stderr_chunks)
    assert "1" in joined_err or "sleep" in joined_err
    # Cmd #3 ("echo third") ran after the timeout of #2.
    assert "third" in "".join(stdout_chunks)


async def test_per_command_timeout_name_in_stderr(settings: Settings) -> None:
    """When a command times out, stderr identifies the exact name/index."""
    stderr_chunks: list[str] = []

    async def on_output(out=None, err=None, command=None) -> None:
        if err:
            stderr_chunks.append(err)

    with patch("orcaster_worker.dispatcher.kill_process_group"):
        status, result = await dispatch_run_batch(
            {
                "commands": ["sleep 60"],
                "timeout_seconds": 1,
            },
            settings,
            on_output=on_output,
        )

    assert status == "timeout"
    assert "stdout" not in result and "stderr" not in result
    # stderr must mention the command that timed out.
    joined = "".join(stderr_chunks)
    assert "sleep 60" in joined or "command #0" in joined


async def test_per_command_fast_and_slow_with_sufficient_timeout_both_ok(
    settings: Settings,
) -> None:
    """Fast + slow command within the per-command timeout: both OK."""
    stdout_chunks: list[str] = []

    async def on_output(out=None, err=None, command=None) -> None:
        if out:
            stdout_chunks.append(out)

    status, result = await dispatch_run_batch(
        {
            "commands": [
                "echo fast",
                "sleep 0.1 && echo slow",
            ],
            "timeout_seconds": 10,
        },
        settings,
        on_output=on_output,
    )

    assert status == "ok"
    assert result["exit_codes"] == [0, 0]
    assert "stdout" not in result and "stderr" not in result
    joined = "".join(stdout_chunks)
    assert "fast" in joined
    assert "slow" in joined


async def test_per_command_uses_default_from_new_setting(tmp_path: Path) -> None:
    """Without timeout_seconds in the payload, it uses run_batch_per_command_timeout_seconds."""
    s = Settings(
        server_url="http://server.test",  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        run_batch_per_command_timeout_seconds=10,
    )
    status, result = await dispatch_run_batch(
        {"commands": ["echo quick"]},
        s,
    )
    assert status == "ok"
    assert result["exit_codes"] == [0]


async def test_per_command_timeout_does_not_accumulate(settings: Settings) -> None:
    """Verifies that the timeout is NOT cumulative: 3 commands of 0.5s with
    timeout_seconds=2 must all pass if each one is individual (0.5 < 2),
    even though the sum 1.5s < 2s in this case. The real test verifies that the
    third command does NOT time out even though the sum of elapsed would exceed the budget
    of the old model."""
    # With the old semantics (total budget), 3x sleep 0.4 = 1.2s < 2s ok.
    # With the new per-command semantics, each one gets 2s -- also ok.
    # The test that discriminates: we use a small timeout_seconds that would allow
    # one command but not the total.
    # 3x sleep 0.2 = 0.6s total. With timeout_per_cmd=0.5: 0.2 < 0.5 -> all ok.
    # With the old model and timeout_total=0.5: 0.6s > 0.5 -> it would fail on the 3rd.
    stdout_chunks: list[str] = []

    async def on_output(out=None, err=None, command=None) -> None:
        if out:
            stdout_chunks.append(out)

    with patch("orcaster_worker.dispatcher.kill_process_group"):
        status, result = await dispatch_run_batch(
            {
                "commands": [
                    "sleep 0.2 && echo a",
                    "sleep 0.2 && echo b",
                    "sleep 0.2 && echo c",
                ],
                "timeout_seconds": 0.5,
            },
            settings,
            on_output=on_output,
        )

    # With per-command semantics: each 0.2s < 0.5s -> all ok.
    assert status == "ok"
    assert "stdout" not in result and "stderr" not in result
    joined = "".join(stdout_chunks)
    assert "a" in joined
    assert "b" in joined
    assert "c" in joined


# ---------------------------------------------------------------------------
# Incremental streaming with on_output callback
# ---------------------------------------------------------------------------


async def test_on_output_receives_chunks_in_order(settings: Settings) -> None:
    """The on_output callback receives the full stdout, without truncating to 16 KiB."""
    collected: list[str] = []

    async def on_output(stdout=None, stderr=None, command=None) -> None:
        if stdout:
            collected.append(stdout)

    big = "x" * 50_000  # >> the old 16 KiB
    status, result = await dispatch_run_batch(
        {"commands": [f"printf %s {big}"], "timeout_seconds": 10},
        settings,
        on_output=on_output,
    )
    assert status == "ok"
    assert "".join(collected) == big  # not truncated
    assert "stdout" not in result and "stderr" not in result


async def test_no_truncation_in_result(settings: Settings) -> None:
    """The result no longer exposes the 'truncated' key (the server does the capping)."""
    status, result = await dispatch_run_batch({"commands": ["echo hi"]}, settings)
    assert status == "ok"
    assert "truncated" not in result


async def test_on_output_receives_stderr_chunks(settings: Settings) -> None:
    """The on_output callback receives the stderr chunks written by the command."""
    collected_stderr: list[str] = []

    async def on_output(stdout=None, stderr=None, command=None) -> None:
        if stderr:
            collected_stderr.append(stderr)

    payload = "hello-stderr"
    status, result = await dispatch_run_batch(
        {"commands": [f"sh -c 'printf %s {payload} >&2'"], "timeout_seconds": 10},
        settings,
        on_output=on_output,
    )
    assert status == "ok"
    joined = "".join(collected_stderr)
    assert joined == payload, f"collected stderr {joined!r} != expected {payload!r}"
    assert "stderr" not in result


# ---------------------------------------------------------------------------
# Payload validation errors travel as output_events
# ---------------------------------------------------------------------------


async def test_invalid_payload_emits_diagnostic_via_on_output() -> None:
    """Payload validation errors travel as output_events (via
    on_output), not in a result["stderr"] that nobody persists."""
    chunks: list[tuple[str | None, str | None]] = []

    async def on_output(out=None, err=None, command=None) -> None:
        chunks.append((out, err))

    s = Settings(
        server_url="http://server.test",  # type: ignore[arg-type]
        state_dir="/tmp/test-dispatcher-diag",
    )
    status, result = await dispatch_run_batch(
        {"commands": "not-a-list"},  # invalid
        s,
        on_output=on_output,
    )
    assert status == "error"
    assert "stdout" not in result and "stderr" not in result
    assert any(err and "commands" in err for _out, err in chunks)


# ---------------------------------------------------------------------------
# Ephemeral cwd per execution
# ---------------------------------------------------------------------------


async def test_run_batch_cwd_honored(settings: Settings, tmp_path: Path) -> None:
    """The subprocess writes a file with a RELATIVE path; the file appears
    in the passed cwd (tmp_path/run_dir), NOT in the test process's cwd."""
    run_dir = tmp_path / "run_dir"
    run_dir.mkdir()

    status, result = await dispatch_run_batch(
        {"commands": ["sh -c 'echo hi > out.txt'"], "timeout_seconds": 5},
        settings,
        cwd=str(run_dir),
    )

    assert status == "ok"
    assert result["exit_codes"] == [0]
    # The file must exist in the injected cwd.
    assert (run_dir / "out.txt").exists(), "out.txt must be in run_dir"
    # It must not have appeared in the test process's cwd.
    import os

    assert not (Path(os.getcwd()) / "out.txt").exists(), (
        "out.txt must NOT be in the test process's cwd"
    )


async def test_run_batch_two_cwds_stay_isolated(settings: Settings, tmp_path: Path) -> None:
    """Two calls to dispatch_run_batch with different cwds do not get mixed up:
    each one writes its file only in its own directory."""
    dir_a = tmp_path / "dir_a"
    dir_b = tmp_path / "dir_b"
    dir_a.mkdir()
    dir_b.mkdir()

    status_a, _ = await dispatch_run_batch(
        {"commands": ["sh -c 'echo a > marker_a.txt'"], "timeout_seconds": 5},
        settings,
        cwd=str(dir_a),
    )
    status_b, _ = await dispatch_run_batch(
        {"commands": ["sh -c 'echo b > marker_b.txt'"], "timeout_seconds": 5},
        settings,
        cwd=str(dir_b),
    )

    assert status_a == "ok"
    assert status_b == "ok"
    assert (dir_a / "marker_a.txt").exists()
    assert not (dir_a / "marker_b.txt").exists()
    assert (dir_b / "marker_b.txt").exists()
    assert not (dir_b / "marker_a.txt").exists()


# ---------------------------------------------------------------------------
# Command echo
# ---------------------------------------------------------------------------


async def test_run_batch_emits_command_echo(settings: Settings) -> None:
    """Each command is echoed as a 'command' event ('$ <cmd>\n') before its output."""
    events: list[tuple[str, str]] = []

    async def on_output(out=None, err=None, command=None):
        if out is not None:
            events.append(("stdout", out))
        if err is not None:
            events.append(("stderr", err))
        if command is not None:
            events.append(("command", command))

    status, result = await dispatch_run_batch(
        {"commands": ["echo hello", "echo world"], "timeout_seconds": 5},
        settings,
        on_output=on_output,
    )
    assert status == "ok"
    cmd_texts = [t for s, t in events if s == "command"]
    assert cmd_texts == ["$ echo hello\n", "$ echo world\n"]
    # The echo of the first command precedes its stdout.
    streams = [s for s, _ in events]
    first_cmd_idx = streams.index("command")
    first_out_idx = next(i for i, (s, t) in enumerate(events) if s == "stdout" and "hello" in t)
    assert first_cmd_idx < first_out_idx
