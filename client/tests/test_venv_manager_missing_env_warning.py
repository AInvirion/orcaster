"""The warning for a missing ``task_env`` is emitted once per execution.

``build_scrubbed_base_env`` is invoked several times per execution (``uv venv``,
``uv pip install`` and ``_build_env``), so the warning cannot live there:
``ephemeral_venv`` emits it only once on entry. These tests run the real
``ephemeral_venv`` with the ``uv`` subprocess faked (they do not require
``uv`` installed).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from orcaster_worker import venv_manager

LOGGER = "orcaster.worker"
MISSING_MSG = "absent from the worker environment"


class _FakeProc:
    pid = 4242
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""


@pytest.fixture
def fake_uv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict[str, str]]:
    """Fakes the uv subprocess. Returns the ``env`` each one was launched with."""
    spawned_envs: list[dict[str, str]] = []

    async def fake_exec(*argv: str, **kwargs: object) -> _FakeProc:
        spawned_envs.append(dict(kwargs["env"]))  # type: ignore[call-overload]
        return _FakeProc()

    python = tmp_path / "python"
    python.write_text("")
    monkeypatch.setattr(venv_manager.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(venv_manager, "_venv_python", lambda _venv_path: python)
    return spawned_envs


@pytest.fixture
def build_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str] | None]:
    """Spy on ``build_scrubbed_base_env`` that delegates to the real one."""
    calls: list[list[str] | None] = []
    real = venv_manager.build_scrubbed_base_env

    def spy(
        declared: list[str] | None,
        *,
        source: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
    ):
        calls.append(declared)
        return real(declared, source=source, secrets=secrets)

    monkeypatch.setattr(venv_manager, "build_scrubbed_base_env", spy)
    return calls


@pytest.fixture(autouse=True)
def worker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRESENT_646", "s3cr3t-value-must-not-leak-646")
    monkeypatch.delenv("ABSENT_646", raising=False)
    monkeypatch.delenv("ALSO_ABSENT_646", raising=False)


def _missing_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if MISSING_MSG in r.getMessage()]


async def _run(
    execution_id: str,
    task_env: list[str] | None,
    requirements: list[str],
    secrets: dict[str, str] | None = None,
) -> None:
    async with venv_manager.ephemeral_venv(
        execution_id,
        requirements,
        uv_bin="uv",
        uv_cache_dir=None,
        task_env=task_env,
        secrets=secrets,
    ):
        pass


async def test_warns_once_even_though_env_is_built_several_times(
    caplog: pytest.LogCaptureFixture,
    fake_uv: list[dict[str, str]],
    build_calls: list[list[str] | None],
) -> None:
    declared = ["PRESENT_646", "ABSENT_646", "ALSO_ABSENT_646"]
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await _run("exec-a", declared, requirements=["six"])

    # uv venv + uv pip install + _build_env: several call sites ran.
    assert len(fake_uv) == 2
    assert len(build_calls) >= 2
    records = _missing_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    # ephemeral_venv receives the task id in its `execution_id` parameter (main.py).
    assert message.startswith("task_id=exec-a ")
    assert "execution_id=" not in message
    assert "ABSENT_646" in message
    assert "ALSO_ABSENT_646" in message
    assert "2 name(s)" in message
    assert "s3cr3t-value-must-not-leak-646" not in caplog.text


async def test_warns_once_per_execution_not_per_process(
    caplog: pytest.LogCaptureFixture,
    fake_uv: list[dict[str, str]],
    build_calls: list[list[str] | None],
) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await _run("exec-1", ["ABSENT_646"], requirements=["six"])
        await _run("exec-2", ["ABSENT_646"], requirements=["six"])

    records = _missing_records(caplog)
    assert len(records) == 2
    assert "task_id=exec-1 " in records[0].getMessage()
    assert "task_id=exec-2 " in records[1].getMessage()


async def test_warns_before_spawning_uv_so_a_uv_failure_still_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fake_uv: list[dict[str, str]],
) -> None:
    class _FailingProc(_FakeProc):
        returncode = 2

    async def failing_exec(*argv: str, **kwargs: object) -> _FailingProc:
        return _FailingProc()

    monkeypatch.setattr(venv_manager.asyncio, "create_subprocess_exec", failing_exec)
    with (
        caplog.at_level(logging.WARNING, logger=LOGGER),
        pytest.raises(venv_manager.VenvCreationError),
    ):
        await _run("exec-fail", ["ABSENT_646"], requirements=[])

    assert len(_missing_records(caplog)) == 1


async def test_silent_when_every_declared_name_is_present(
    caplog: pytest.LogCaptureFixture,
    fake_uv: list[dict[str, str]],
    build_calls: list[list[str] | None],
) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await _run("exec-ok", ["PRESENT_646"], requirements=["six"])

    assert len(build_calls) >= 2
    assert _missing_records(caplog) == []


async def test_silent_in_legacy_mode(
    caplog: pytest.LogCaptureFixture,
    fake_uv: list[dict[str, str]],
    build_calls: list[list[str] | None],
) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await _run("exec-legacy", None, requirements=["six"])

    assert len(build_calls) >= 2
    assert _missing_records(caplog) == []


async def test_missing_name_is_not_injected_into_uv_env(
    fake_uv: list[dict[str, str]],
) -> None:
    # The warning does not change the semantics: the missing name is still not injected.
    await _run("exec-env", ["PRESENT_646", "ABSENT_646"], requirements=["six"])

    assert fake_uv, "uv was not launched"
    for env in fake_uv:
        assert env["PRESENT_646"] == "s3cr3t-value-must-not-leak-646"
        assert "ABSENT_646" not in env


async def test_invalid_entry_warns_once_with_the_count_and_never_its_text(
    caplog: pytest.LogCaptureFixture,
    fake_uv: list[dict[str, str]],
    build_calls: list[list[str] | None],
) -> None:
    # `--env API_TOKEN=hf_abc123`: the value must not reach the worker log.
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await _run("exec-invalid", ["API_TOKEN=hf_abc123"], requirements=["six"])

    assert len(build_calls) >= 2
    records = [r for r in caplog.records if "task_env" in r.getMessage()]
    assert len(records) == 1
    assert records[0].getMessage().startswith("task_id=exec-invalid ")
    assert "1 entry(ies)" in records[0].getMessage()
    assert "hf_abc123" not in caplog.text
    assert "API_TOKEN" not in caplog.text


async def test_token_prefixed_entry_warns_once_with_the_count_and_never_its_text(
    caplog: pytest.LogCaptureFixture,
    fake_uv: list[dict[str, str]],
    build_calls: list[list[str] | None],
) -> None:
    # `--env hf_AbC123`: a pasted token instead of the name; it is only counted, once.
    declared = ["hf_AbC123", "ABSENT_646", "ghp_x"]
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await _run("exec-token", declared, requirements=["six"])

    assert len(build_calls) >= 2
    records = _missing_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert message.startswith("task_id=exec-token ")
    assert "'ABSENT_646'" in message
    assert "2 entry(ies) are not valid variable names (text omitted)" in message
    assert "hf_AbC123" not in caplog.text
    assert "ghp_x" not in caplog.text


async def test_a_token_prefixed_name_in_the_worker_env_is_injected_and_not_warned(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fake_uv: list[dict[str, str]],
) -> None:
    # The injection does not change: a declared name that exists in the worker environment
    # reaches uv even if it has a token prefix, and produces no warning.
    monkeypatch.setenv("hf_x", "worker-value-646")
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await _run("exec-lower", ["hf_x"], requirements=["six"])

    assert fake_uv, "uv was not launched"
    for env in fake_uv:
        assert env["hf_x"] == "worker-value-646"
    assert _missing_records(caplog) == []
    assert "worker-value-646" not in caplog.text


async def test_a_name_delivered_as_a_server_secret_is_injected_and_not_warned(
    caplog: pytest.LogCaptureFixture,
    fake_uv: list[dict[str, str]],
) -> None:
    # build_scrubbed_base_env injects the server secret; the warning must
    # use the same rule and not say the name is missing or was not injected.
    server_secret = "s3rv3r-secret-value-must-not-leak-646"
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await _run(
            "exec-server",
            ["SERVER_ONLY_646", "ABSENT_646"],
            requirements=["six"],
            secrets={"SERVER_ONLY_646": server_secret},
        )

    assert fake_uv, "uv was not launched"
    for env in fake_uv:
        assert env["SERVER_ONLY_646"] == server_secret
    records = _missing_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "'ABSENT_646'" in message
    assert "SERVER_ONLY_646" not in message
    assert "1 name(s)" in message
    assert server_secret not in caplog.text


async def test_silent_when_every_declared_name_comes_from_a_server_secret(
    caplog: pytest.LogCaptureFixture,
    fake_uv: list[dict[str, str]],
) -> None:
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await _run(
            "exec-all-server",
            ["SERVER_ONLY_646"],
            requirements=[],
            secrets={"SERVER_ONLY_646": "s3rv3r-secret-value-must-not-leak-646"},
        )

    assert _missing_records(caplog) == []
    assert "s3rv3r-secret-value-must-not-leak-646" not in caplog.text
