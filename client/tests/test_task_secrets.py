"""Task secrets delivered by the server, worker side.

Covers: parsing of `secrets` in the poll response (including an old server and
unknown keys), server > worker environment precedence when building the env,
per-task log with names only, masking of the values in everything the
worker sends back and in the local idempotency storage.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from orcaster_worker.config import Settings
from orcaster_worker.idempotency import get_push_result, store_push_result
from orcaster_worker.output_streamer import OutputStreamer
from orcaster_worker.poll import PollResponse, PollTaskPayload
from orcaster_worker.secret_redaction import MASK, MIN_MASK_LEN, SecretRedactor
from orcaster_worker.task_env import build_scrubbed_base_env, describe_env_sources
from orcaster_worker.venv_manager import _build_env, _spawn_uv

WID = "11111111-1111-1111-1111-111111111111"
SECRET = "hf_server_value_0123456789"
OTHER = "ghp_other_server_value_abcdef"
SHORT = "short7x"  # 7 characters: below the threshold, not masked


def _wire_task(**extra: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": 42,
        "command": "__run_batch__",
        "payload": {"requirements": [], "commands": ["echo hi"], "task_env": ["API_TOKEN"]},
        "created_at": "2026-10-26T00:00:00Z",
        "delivery_mode": "claim",
        "signed": True,
        "signature": None,
        "idempotency_key": None,
        "worker_id": WID,
        "target_worker_id": None,
        "lease_token": "lease-42",
        "lease_expires_at": "2030-01-01T00:00:00Z",
    }
    task.update(extra)
    return task


def _task(
    *,
    task_id: int = 42,
    payload: dict[str, Any] | None = None,
    secrets: dict[str, str] | None = None,
    secrets_error: str | None = None,
    idempotency_key: str | None = None,
    command: str = "__run_batch__",
) -> PollTaskPayload:
    return PollTaskPayload(
        task_id=task_id,
        command=command,
        payload=payload
        if payload is not None
        else {"requirements": [], "commands": ["echo hi"], "task_env": ["API_TOKEN"]},
        created_at="2026-10-26T00:00:00Z",
        delivery_mode="claim",
        signed=False,
        signature=None,
        idempotency_key=idempotency_key,
        worker_id=WID,
        target_worker_id=None,
        lease_token=f"lease-{task_id}",
        lease_expires_at="2030-01-01T00:00:00Z",
        secrets=secrets or {},
        secrets_error=secrets_error,
    )


# --- Poll parsing --------------------------------------------------------------


def test_poll_parses_secrets() -> None:
    resp = PollResponse.from_wire({"task": _wire_task(secrets={"API_TOKEN": SECRET})})
    assert resp.task is not None
    assert dict(resp.task.secrets) == {"API_TOKEN": SECRET}
    assert resp.task.secrets_error is None


def test_poll_old_server_without_secrets_parses_as_empty() -> None:
    """New worker against an old server: without the key, `secrets` is `{}`."""
    resp = PollResponse.from_wire({"task": _wire_task()})
    assert resp.task is not None
    assert dict(resp.task.secrets) == {}
    assert resp.task.secrets_error is None


def test_poll_null_secrets_parses_as_empty() -> None:
    resp = PollResponse.from_wire({"task": _wire_task(secrets=None)})
    assert resp.task is not None
    assert dict(resp.task.secrets) == {}
    assert resp.task.secrets_error is None


def test_poll_unknown_extra_keys_still_parse() -> None:
    """Keys this worker does not know (top-level and in the task) are ignored."""
    resp = PollResponse.from_wire(
        {
            "task": _wire_task(some_future_field={"x": 1}),
            "future_top_level": [1, 2],
            "next_poll_after_seconds": 5,
        }
    )
    assert resp.task is not None
    assert resp.task.task_id == 42
    assert resp.next_poll_after_seconds == 5


@pytest.mark.parametrize(
    "bad",
    [
        ["API_TOKEN", SECRET],
        "API_TOKEN=" + SECRET,
        {"API_TOKEN": 12345678901},
        {"API_TOKEN": SECRET, "OTHER": None},
    ],
)
def test_poll_malformed_secrets_flagged_without_values(bad: Any) -> None:
    """A malformed `secrets` is not discarded silently: `secrets_error` is set
    (it names the problem, never values) and `secrets` is empty."""
    resp = PollResponse.from_wire({"task": _wire_task(secrets=bad)})
    assert resp.task is not None
    assert dict(resp.task.secrets) == {}
    assert resp.task.secrets_error
    assert SECRET not in resp.task.secrets_error
    assert "12345678901" not in resp.task.secrets_error


def test_task_repr_does_not_include_secret_values() -> None:
    resp = PollResponse.from_wire({"task": _wire_task(secrets={"API_TOKEN": SECRET})})
    assert SECRET not in repr(resp.task)
    assert SECRET not in str(resp.task)
    assert SECRET not in repr(resp)


# --- Task environment -----------------------------------------------------------

SRC = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/orcaster",
    "API_TOKEN": "worker_hf_value_xxxxxxxx",
    "GITHUB_TOKEN": "worker_gh_value_xxxxxxxx",
    "ORCH_API_KEY": "orch_secret",
}


def test_declared_name_prefers_server_secret() -> None:
    env = build_scrubbed_base_env(
        ["API_TOKEN", "GITHUB_TOKEN"], source=SRC, secrets={"API_TOKEN": SECRET}
    )
    assert env["API_TOKEN"] == SECRET
    assert env["GITHUB_TOKEN"] == "worker_gh_value_xxxxxxxx"


def test_secret_not_declared_is_not_injected() -> None:
    env = build_scrubbed_base_env([], source=SRC, secrets={"API_TOKEN": SECRET})
    assert "API_TOKEN" not in env


def test_orch_prefix_denied_even_from_server_secrets() -> None:
    env = build_scrubbed_base_env(["ORCH_API_KEY"], source=SRC, secrets={"ORCH_API_KEY": SECRET})
    assert "ORCH_API_KEY" not in env


def test_legacy_mode_ignores_server_secrets() -> None:
    env = build_scrubbed_base_env(None, source=SRC, secrets={"API_TOKEN": SECRET})
    assert env == SRC


def test_describe_env_sources_classifies_names() -> None:
    sources = describe_env_sources(
        ["API_TOKEN", "GITHUB_TOKEN", "MISSING_TOKEN", "ORCH_API_KEY"],
        source=SRC,
        secrets={"API_TOKEN": SECRET, "UNDECLARED": OTHER},
    )
    assert sources.server == ("API_TOKEN",)
    assert sources.worker_env == ("GITHUB_TOKEN",)
    assert sources.missing == ("MISSING_TOKEN",)
    assert sources.denied == ("ORCH_API_KEY",)
    assert sources.ignored == ("UNDECLARED",)


def test_describe_env_sources_legacy_reports_ignored_secrets() -> None:
    sources = describe_env_sources(None, source=SRC, secrets={"API_TOKEN": SECRET})
    assert sources.legacy is True
    assert sources.ignored == ("API_TOKEN",)
    assert sources.server == ()


def test_describe_env_sources_render_has_names_only() -> None:
    sources = describe_env_sources(["API_TOKEN"], source=SRC, secrets={"API_TOKEN": SECRET})
    line = sources.render()
    assert "API_TOKEN" in line
    assert SECRET not in line
    assert "server" in line


def test_venv_build_env_uses_server_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_TOKEN", "worker_hf_value_xxxxxxxx")
    env = _build_env(Path("/tmp/venv"), ["API_TOKEN"], secrets={"API_TOKEN": SECRET})
    assert env["API_TOKEN"] == SECRET
    assert env["VIRTUAL_ENV"] == "/tmp/venv"


@pytest.mark.skipif(sys.platform == "win32", reason="usa /bin/sh")
async def test_spawn_uv_env_gets_server_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """`uv venv`/`uv pip install` see the same secrets as the commands."""
    monkeypatch.setenv("API_TOKEN", "worker_hf_value_xxxxxxxx")
    rc, out, _ = await _spawn_uv(
        ["/bin/sh", "-c", 'printf %s "$API_TOKEN"'],
        abandon_event=None,
        task_env=["API_TOKEN"],
        secrets={"API_TOKEN": SECRET},
    )
    assert rc == 0
    assert out == SECRET


# --- Redactor ---------------------------------------------------------------------


def test_redactor_masks_values_of_min_length() -> None:
    r = SecretRedactor([SECRET, SHORT])
    text = f"token={SECRET} short={SHORT}"
    assert r.redact(text) == f"token={MASK} short={SHORT}"


def test_redactor_longest_value_first() -> None:
    longer = SECRET + "_suffix"
    r = SecretRedactor([SECRET, longer])
    assert r.redact(f"a {longer} b {SECRET}") == f"a {MASK} b {MASK}"


def test_redactor_inactive_is_identity() -> None:
    r = SecretRedactor([SHORT, ""])
    assert not r.active
    assert r.redact(f"x {SHORT}") == f"x {SHORT}"


def test_redactor_redacts_nested_objects() -> None:
    r = SecretRedactor([SECRET])
    obj = {"a": [SECRET, {"b": f"pre {SECRET}"}], "n": 3}
    assert r.redact_obj(obj) == {"a": [MASK, {"b": f"pre {MASK}"}], "n": 3}


def test_redactor_split_holds_back_secret_prefix() -> None:
    r = SecretRedactor([SECRET])
    emit, carry = r.split("hello " + SECRET[:5])
    assert emit == "hello "
    assert carry == SECRET[:5]
    emit, carry = r.split("plain text")
    assert (emit, carry) == ("plain text", "")


_PREFIX_A = "ghp_SHORTTOKEN1"
_PREFIX_B = _PREFIX_A + "_EXTRA_PART_9876"
_PREFIX_LINE = "token=" + _PREFIX_B + "\n"


def _substrings(value: str, size: int) -> set[str]:
    return {value[i : i + size] for i in range(len(value) - size + 1)}


@pytest.mark.parametrize("cut", range(1, len(_PREFIX_LINE)))
def test_redactor_split_value_prefix_of_another_never_leaks(cut: int) -> None:
    """B starts with A: a cut inside B must not mask A and leave the
    rest of B in view."""
    r = SecretRedactor([_PREFIX_A, _PREFIX_B])
    emit1, held = r.split(_PREFIX_LINE[:cut])
    emit2, held = r.split(held + _PREFIX_LINE[cut:])
    out = emit1 + emit2 + r.redact(held)
    for piece in _substrings(_PREFIX_B, MIN_MASK_LEN):
        assert piece not in out, (cut, out)
    assert out == f"token={MASK}\n"


def test_redactor_split_does_not_cut_inside_a_full_match() -> None:
    """The tail of a full value that is also a prefix of another must not
    split the full value and emit its head unmasked."""
    whole = "abcdefghij"
    other = "ghijXXXXXXXX"
    r = SecretRedactor([whole, other])
    emit, held = r.split("x " + whole)
    out = emit + r.redact(held)
    assert "abcdef" not in emit
    assert out == f"x {MASK}"


# --- OutputStreamer with masking ----------------------------------------------------


class _CaptureAPI:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.completes: list[dict[str, Any]] = []

    async def output(self, task_id: int, *, lease_token: str, events: list[dict]) -> None:
        self.events.extend(events)

    async def complete(self, task_id: int, *, lease_token: str, exit_code, status, **kw) -> None:
        self.completes.append({"status": status, "exit_code": exit_code, **kw})


def _streamer(api: _CaptureAPI, redactor: SecretRedactor | None) -> OutputStreamer:
    return OutputStreamer(
        api,  # type: ignore[arg-type]
        42,
        "lease-42",
        max_bytes=8192,
        interval=0.01,
        buffer_max_bytes=1 << 20,
        redactor=redactor,
    )


def _joined(events: list[dict[str, Any]], stream: str) -> str:
    return "".join(e["text"] for e in events if e["stream"] == stream)


async def test_streamer_masks_value_in_single_chunk() -> None:
    api = _CaptureAPI()
    s = _streamer(api, SecretRedactor([SECRET]))
    await s.feed(stdout=f"token is {SECRET}\n")
    await s.aclose(drain_deadline=5)
    assert _joined(api.events, "stdout") == f"token is {MASK}\n"


async def test_streamer_masks_value_split_across_chunks() -> None:
    api = _CaptureAPI()
    s = _streamer(api, SecretRedactor([SECRET]))
    await s.feed(stdout="token is " + SECRET[:6])
    await s.feed(stdout=SECRET[6:15])
    await s.feed(stdout=SECRET[15:] + " end\n")
    await s.aclose(drain_deadline=5)
    out = _joined(api.events, "stdout")
    assert SECRET not in out
    assert out == f"token is {MASK} end\n"


async def test_streamer_flushes_held_prefix_on_close() -> None:
    api = _CaptureAPI()
    s = _streamer(api, SecretRedactor([SECRET]))
    await s.feed(stderr="tail " + SECRET[:4])
    await s.aclose(drain_deadline=5)
    assert _joined(api.events, "stderr") == "tail " + SECRET[:4]


async def test_streamer_flushes_held_prefix_before_command_echo() -> None:
    api = _CaptureAPI()
    s = _streamer(api, SecretRedactor([SECRET]))
    await s.feed(stdout="first " + SECRET[:3])
    await s.feed(command=f"$ echo {SECRET}\n")
    await s.aclose(drain_deadline=5)
    # What was held back goes out as its own stdout event, before the command echo.
    assert [e["stream"] for e in api.events][-1] == "command"
    assert _joined(api.events, "stdout") == "first " + SECRET[:3]
    assert _joined(api.events, "command") == f"$ echo {MASK}\n"
    seqs = [e["seq"] for e in api.events]
    assert seqs == sorted(seqs)


async def test_streamer_held_tail_does_not_truncate_full_read_chunks() -> None:
    """A full read chunk (64 KiB) plus the held-back tail exceeds the
    per-event limit: it is split into several events instead of truncating."""
    from orcaster_worker.output_streamer import _EVENT_TEXT_MAX_CHARS, _TRUNCATION_MARKER

    api = _CaptureAPI()
    s = OutputStreamer(
        api,  # type: ignore[arg-type]
        42,
        "lease-42",
        max_bytes=1 << 20,
        interval=0.01,
        buffer_max_bytes=1 << 22,
        redactor=SecretRedactor([SECRET]),
    )
    first = "a" * (65536 - 1) + SECRET[0]
    second = "b" * 65536
    await s.feed(stdout=first)
    await s.feed(stdout=second)
    await s.aclose(drain_deadline=5)
    out = _joined(api.events, "stdout")
    assert out == first + second
    assert _TRUNCATION_MARKER not in out
    assert all(len(e["text"]) <= _EVENT_TEXT_MAX_CHARS for e in api.events)
    seqs = [e["seq"] for e in api.events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


async def test_streamer_without_redactor_is_unchanged() -> None:
    api = _CaptureAPI()
    s = _streamer(api, None)
    await s.feed(stdout=SECRET)
    await s.aclose(drain_deadline=5)
    assert _joined(api.events, "stdout") == SECRET


# --- Local idempotency ------------------------------------------------------------


def test_store_push_result_redacts(tmp_path: Path) -> None:
    store_push_result(
        tmp_path,
        "key-1",
        status="completed",
        exit_code=0,
        stdout=f"out {SECRET}",
        stderr=f"err {SECRET}",
        result={"echo": SECRET, "list": [f"x{SECRET}"]},
        redactor=SecretRedactor([SECRET]),
    )
    stored = get_push_result(tmp_path, "key-1")
    assert stored is not None
    assert SECRET not in json.dumps(stored)
    assert stored["stdout"] == f"out {MASK}"
    assert stored["result"] == {"echo": MASK, "list": [f"x{MASK}"]}


# --- Integration in main ------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        server_url="http://server.test",  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        lease_seconds=5,
        uv_bin="uv",
        uv_cache_dir=tmp_path / "uv-cache",
        cache_root=tmp_path / "cache",
    )


def _fake_venv_factory(captured: dict[str, Any]):
    class _FakeVenv:
        def __init__(self, env: dict[str, str]) -> None:
            self.env = env

    @asynccontextmanager
    async def _fake_ephemeral_venv(**kwargs):
        captured["venv_kwargs"] = kwargs
        env = build_scrubbed_base_env(kwargs.get("task_env"), secrets=kwargs.get("secrets"))
        yield _FakeVenv(env)

    return _fake_ephemeral_venv


@pytest.mark.parametrize("enforce", [False, True])
async def test_run_external_secrets_reach_env_and_output_is_masked(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    enforce: bool,
) -> None:
    from orcaster_worker import main as main_mod

    settings.enforce_isolation = enforce
    monkeypatch.setenv("API_TOKEN", "worker_hf_value_xxxxxxxx")
    monkeypatch.setenv("GITHUB_TOKEN", "worker_gh_value_xxxxxxxx")
    captured: dict[str, Any] = {}

    async def fake_dispatch(
        payload, _settings, *, env=None, cwd=None, abandon_event=None, on_output=None
    ):
        captured["env"] = dict(env or {})
        assert on_output is not None
        await on_output(command=f"$ echo {SECRET}\n")
        await on_output(f"stdout {SECRET[:9]}", None)
        await on_output(f"{SECRET[9:]} done\n", None)
        await on_output(None, f"stderr {SECRET}\n")
        return "ok", {"exit_codes": [0], "failed_at": None}

    api = _CaptureAPI()
    task = _task(
        payload={
            "requirements": [],
            "commands": ["echo hi"],
            "task_env": ["API_TOKEN", "GITHUB_TOKEN", "NOT_SET_ANYWHERE"],
        },
        secrets={"API_TOKEN": SECRET},
    )
    caplog.set_level(logging.DEBUG)
    with (
        patch("orcaster_worker.main.dispatch_run_batch", new=fake_dispatch),
        patch("orcaster_worker.main.ephemeral_venv", new=_fake_venv_factory(captured)),
    ):
        await main_mod._run_external(api, settings, main_mod.BusyState(), asyncio.Event(), task)

    assert captured["venv_kwargs"]["secrets"] == {"API_TOKEN": SECRET}
    assert captured["env"]["API_TOKEN"] == SECRET
    assert captured["env"]["GITHUB_TOKEN"] == "worker_gh_value_xxxxxxxx"
    texts = json.dumps(api.events)
    assert SECRET not in texts
    assert _joined(api.events, "stdout") == f"stdout {MASK} done\n"
    assert _joined(api.events, "stderr") == f"stderr {MASK}\n"
    assert api.completes[-1]["status"] == "completed"

    source_lines = [r.getMessage() for r in caplog.records if "task_env sources" in r.getMessage()]
    assert len(source_lines) == 1
    line = source_lines[0]
    assert "task_id=42" in line
    assert "server=[API_TOKEN]" in line
    assert "worker_env=[GITHUB_TOKEN]" in line
    assert "missing=[NOT_SET_ANYWHERE]" in line
    assert all(SECRET not in r.getMessage() for r in caplog.records)


async def test_run_external_venv_failure_message_is_masked(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    from orcaster_worker import main as main_mod
    from orcaster_worker.venv_manager import VenvInstallError

    @asynccontextmanager
    async def failing_venv(**kwargs):
        raise VenvInstallError(f"uv pip install failed: bad index https://u:{SECRET}@x")
        yield  # pragma: no cover

    api = _CaptureAPI()
    caplog.set_level(logging.DEBUG)
    with patch("orcaster_worker.main.ephemeral_venv", new=failing_venv):
        await main_mod._run_external(
            api,
            settings,
            main_mod.BusyState(),
            asyncio.Event(),
            _task(secrets={"API_TOKEN": SECRET}),
        )
    assert api.completes[-1]["status"] == "failed"
    stderr = _joined(api.events, "stderr")
    assert "venv setup failed" in stderr
    assert SECRET not in stderr
    assert MASK in stderr
    assert all(SECRET not in r.getMessage() for r in caplog.records)


async def test_run_external_runner_error_is_masked(settings: Settings) -> None:
    from orcaster_worker import main as main_mod

    @asynccontextmanager
    async def boom_venv(**kwargs):
        raise RuntimeError(f"unexpected {SECRET}")
        yield  # pragma: no cover

    api = _CaptureAPI()
    with patch("orcaster_worker.main.ephemeral_venv", new=boom_venv):
        await main_mod._run_external(
            api,
            settings,
            main_mod.BusyState(),
            asyncio.Event(),
            _task(secrets={"API_TOKEN": SECRET}),
        )
    stderr = _joined(api.events, "stderr")
    assert "runner error" in stderr
    assert SECRET not in stderr


async def test_emit_synthetic_output_is_masked(settings: Settings) -> None:
    from orcaster_worker import main as main_mod

    api = _CaptureAPI()
    task = _task(secrets={"API_TOKEN": SECRET})
    await main_mod._emit_synthetic_output(api, settings, task, stderr=f"boom {SECRET}")  # type: ignore[arg-type]
    assert _joined(api.events, "stderr") == f"boom {MASK}"


async def test_reserved_handler_crash_is_masked_and_idempotency_redacted(
    settings: Settings,
) -> None:
    from orcaster_worker import main as main_mod

    async def crash(*args, **kwargs):
        raise RuntimeError(f"bad {SECRET}")

    api = _CaptureAPI()
    task = _task(
        command="__noop__",
        payload={},
        secrets={"API_TOKEN": SECRET},
        idempotency_key="idem-1",
    )
    with patch("orcaster_worker.main.handle_reserved", new=crash):
        await main_mod._run_reserved(api, settings, task)  # type: ignore[arg-type]
    stderr = _joined(api.events, "stderr")
    assert "handler crashed" in stderr
    assert SECRET not in stderr


async def test_reserved_result_is_redacted_in_idempotency_store(settings: Settings) -> None:
    from orcaster_worker import main as main_mod
    from orcaster_worker.internal_handlers import InternalResult

    async def handler(*args, **kwargs):
        return InternalResult(
            exit_code=0,
            stdout=f"out {SECRET}",
            stderr="",
            result={"echo": SECRET},
        )

    api = _CaptureAPI()
    task = _task(
        command="__noop__", payload={}, secrets={"API_TOKEN": SECRET}, idempotency_key="idem-2"
    )
    with patch("orcaster_worker.main.handle_reserved", new=handler):
        await main_mod._run_reserved(api, settings, task)  # type: ignore[arg-type]
    stored = get_push_result(settings.state_dir, "idem-2")
    assert stored is not None
    assert SECRET not in json.dumps(stored)
    assert SECRET not in json.dumps(api.events)


async def test_dispatch_fails_task_with_malformed_secrets(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed `secrets`: the task fails visibly (exit 2 + diagnostic), same
    as a malformed payload; it does not run without its secrets."""
    from orcaster_worker import main as main_mod

    api = _CaptureAPI()
    task = _task(secrets_error="secrets malformed: expected an object, got list")
    ran: list[bool] = []

    async def fake_run_external(*args, **kwargs):
        ran.append(True)

    caplog.set_level(logging.DEBUG)
    with (
        patch("orcaster_worker.main._verify_poll_task_signature", return_value=True),
        patch("orcaster_worker.main._run_external", new=fake_run_external),
    ):
        await main_mod._dispatch_poll_task(
            api,  # type: ignore[arg-type]
            settings,
            main_mod.BusyState(),
            asyncio.Event(),
            task,
        )
    assert not ran
    assert api.completes == [{"status": "failed", "exit_code": 2, "cancelled_reason": None}]
    assert "secrets malformed" in _joined(api.events, "stderr")
    assert any(
        "task_id=42" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    )


# --- Parts of a composite value --------------------------------------------------


def test_redactor_masks_comma_and_whitespace_separated_parts() -> None:
    tok1 = "ghp_first_token_aaaa"
    tok2 = "ghp_second_token_bbbb"
    r = SecretRedactor([f"{tok1},{tok2} short1 {SHORT}"])
    assert r.redact(f"using {tok2} then {tok1}") == f"using {MASK} then {MASK}"
    # Short parts are not masked on their own.
    assert r.redact("short1 and " + SHORT) == "short1 and " + SHORT
    # The full value is still masked whole (longest first).
    assert r.redact(f"{tok1},{tok2} short1 {SHORT}") == MASK


_PEM_SECRET = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7secretline01\n"
    "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789secretline02abcdefghijklmn\n"
    "-----END PRIVATE KEY-----\n"
)
_PUBLIC_CERT = (
    "-----BEGIN CERTIFICATE-----\n"
    "MIIDdzCCAl+gAwIBAgIEAgAAuTANBgkqhkiG9w0BAQUFADBaMQswCQYDVQQGEwJJ\n"
    "-----END CERTIFICATE-----\n"
)


def test_redactor_pem_secret_does_not_mask_armor_or_unrelated_cert() -> None:
    r = SecretRedactor([_PEM_SECRET])
    # An unrelated public certificate comes out intact: the armor lines
    # and "-----BEGIN"-like fragments are not patterns.
    assert r.redact(_PUBLIC_CERT) == _PUBLIC_CERT
    # Each data line of the secret is masked on its own.
    line = _PEM_SECRET.splitlines()[1]
    assert r.redact(f"leak {line} end") == f"leak {MASK} end"
    # The whole value is masked whole.
    assert r.redact(_PEM_SECRET) == MASK


def test_redactor_json_secret_masks_whole_not_structural_pieces() -> None:
    value = '{"type": "service_account", "client_email": "bot@x.iam.example.com"}'
    r = SecretRedactor([value])
    assert r.redact('"client_email": "someone-else@example.com"') == (
        '"client_email": "someone-else@example.com"'
    )
    assert r.redact(f"cfg={value}") == f"cfg={MASK}"


_SA_PRIVATE_KEY = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDsaline0001\n"
    "ZyXwVuTsRqPoNmLkJiHgFeDcBa9876543210saline0002zyxwvutsrqponm\n"
    "-----END PRIVATE KEY-----\n"
)
_SA_KEY_ID = "0123456789abcdef0123456789abcdef01234567"
_SA_SECRET = json.dumps(
    {
        "type": "service_account",
        "project_id": "demo-project",
        "private_key_id": _SA_KEY_ID,
        "private_key": _SA_PRIVATE_KEY,
        "client_email": "bot@demo-project.iam.gserviceaccount.com",
        "client_id": "123456789012345678901",
        "nested": {"refresh": ["rt_nested_leaf_value_1", "tiny"]},
    },
    indent=2,
)


def test_redactor_json_secret_masks_decoded_string_leaves() -> None:
    """The task parses the JSON and writes individual fields: each string leaf is
    masked, also the decoded PEM and each of its data lines."""
    r = SecretRedactor([_SA_SECRET])
    data = json.loads(_SA_SECRET)
    assert r.redact(f"key id {data['private_key_id']}") == f"key id {MASK}"
    assert r.redact(data["private_key"]) == MASK
    body = data["private_key"].splitlines()
    assert r.redact(f"line {body[1]} end") == f"line {MASK} end"
    assert r.redact(f"line {body[2]} end") == f"line {MASK} end"
    assert r.redact(data["nested"]["refresh"][0]) == MASK
    # Leaves shorter than MIN_MASK_LEN and object keys are not masked.
    assert r.redact("tiny private_key_id") == "tiny private_key_id"
    # The PEM armor alone is not a pattern.
    assert r.redact("-----BEGIN PRIVATE KEY-----") == "-----BEGIN PRIVATE KEY-----"
    for leaked in (_SA_KEY_ID, *body[1:3]):
        assert leaked not in r.redact(json.dumps(data["private_key"]) + _SA_KEY_ID)


def test_redactor_json_secret_masks_escaped_leaf_form() -> None:
    """Re-serializing the leaf (``json.dumps``) leaves literal ``\\n``: that form is also
    masked, with and without non-ASCII escaping."""
    r = SecretRedactor([_SA_SECRET])
    escaped = json.dumps(_SA_PRIVATE_KEY)
    assert r.redact(f"pk={escaped}") == f'pk="{MASK}"'
    accented = json.dumps({"password": "contraseña-secreta-ñ"})
    r2 = SecretRedactor([accented])
    assert r2.redact(json.dumps("contraseña-secreta-ñ")) == f'"{MASK}"'
    assert r2.redact("contraseña-secreta-ñ") == MASK


def test_redactor_single_line_json_array_masks_leaves() -> None:
    r = SecretRedactor(['["ghp_array_leaf_one","ghp_array_leaf_two"]'])
    assert r.redact("use ghp_array_leaf_two now") == f"use {MASK} now"


def test_redactor_deeply_nested_json_falls_back_to_whole_value() -> None:
    value = "[" * 50_000 + '"deep_secret_leaf"' + "]" * 50_000
    r = SecretRedactor([value])
    assert r.redact(value) == MASK


def test_redactor_non_json_braces_value_still_masks_whole() -> None:
    value = "{not json at all secret-xyz"
    r = SecretRedactor([value])
    assert r.redact(f"v={value}") == f"v={MASK}"
    assert r.redact("not json at all") == "not json at all"


# --- Worker local logs ------------------------------------------------------------


def _record(msg: str, *args: Any, exc: BaseException | None = None) -> logging.LogRecord:
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    return logging.LogRecord("t", logging.ERROR, __file__, 1, msg, args, exc_info)


def _raise_and_catch(text: str) -> BaseException:
    try:
        raise RuntimeError(text)
    except RuntimeError as e:
        return e


def test_log_filter_masks_message_args_and_traceback() -> None:
    from orcaster_worker.secret_redaction import SecretLogFilter, active_redactor

    filt = SecretLogFilter()
    handler = logging.Handler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    with active_redactor(SecretRedactor([SECRET])):
        rec = _record("crashed: %s", repr(RuntimeError(SECRET)), exc=_raise_and_catch(SECRET))
        assert filt.filter(rec) is True
        text = handler.format(rec)
    assert SECRET not in text
    assert MASK in text
    assert "Traceback" in text


def test_log_filter_without_active_redactor_is_identity() -> None:
    from orcaster_worker.secret_redaction import SecretLogFilter

    rec = _record("value %s", SECRET)
    assert SecretLogFilter().filter(rec) is True
    assert rec.getMessage() == f"value {SECRET}"


def test_install_log_redaction_is_idempotent_and_masks_root_handlers() -> None:
    from orcaster_worker.secret_redaction import (
        SecretLogFilter,
        active_redactor,
        install_log_redaction,
    )

    root = logging.getLogger()
    stream_lines: list[str] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            stream_lines.append(self.format(record))

    h = _ListHandler()
    root.addHandler(h)
    try:
        install_log_redaction()
        install_log_redaction()
        assert sum(isinstance(f, SecretLogFilter) for f in h.filters) == 1
        with active_redactor(SecretRedactor([SECRET])):
            logging.getLogger("orcaster.worker").error("boom %s", SECRET)
    finally:
        root.removeHandler(h)
    assert stream_lines and SECRET not in stream_lines[-1]


def test_busy_state_activates_and_reset_clears_redactor() -> None:
    from orcaster_worker import secret_redaction
    from orcaster_worker.main import BusyState, _activate_task_redaction

    busy = BusyState()
    _activate_task_redaction(_task(secrets={"API_TOKEN": SECRET}))
    assert secret_redaction.current_redactor() is not None
    busy.reset()
    assert secret_redaction.current_redactor() is None


async def test_reaper_crash_log_is_masked(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """The reaper logs repr(exc) and the traceback: with the filter installed on the
    handler, the value does not reach the log."""
    from orcaster_worker import main as main_mod
    from orcaster_worker.secret_redaction import SecretLogFilter

    caplog.handler.addFilter(SecretLogFilter())
    caplog.set_level(logging.DEBUG)
    task = _task(secrets={"API_TOKEN": SECRET})
    busy = main_mod.BusyState()
    busy.task_id = task.task_id
    busy.lease_token = task.lease_token
    busy.task_payload = task
    main_mod._activate_task_redaction(task)

    async def crash() -> None:
        raise RuntimeError(f"exploded with {SECRET}")

    busy.executor_task = asyncio.create_task(crash())
    with contextlib.suppress(RuntimeError):
        await busy.executor_task
    api = _CaptureAPI()
    await main_mod._reap_executor_task(api, busy, settings)  # type: ignore[arg-type]
    assert any("executor_task crashed" in r.getMessage() for r in caplog.records)
    assert SECRET not in caplog.text


# --- Reserved names: the server value is never applied ---------------------------

RESERVED_SRC = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/orcaster",
    "SSL_CERT_FILE": "/etc/ssl/worker.pem",
    "REQUESTS_CA_BUNDLE": "/etc/ssl/worker-bundle.pem",
    "PIP_INDEX_URL": "https://pypi.worker/simple",
}


@pytest.mark.parametrize(
    "name",
    [
        "PATH",
        "HOME",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "PYTHONPATH",
        "UV_INDEX_URL",
        "PIP_INDEX_URL",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_BUNDLE",
        "BASH_ENV",
        "ENV",
        "IFS",
        "CDPATH",
        "SHELLOPTS",
        "BASHOPTS",
        "PS4",
        "PROMPT_COMMAND",
        "GCONV_PATH",
        "LOCPATH",
        "GLIBC_TUNABLES",
        "glibc_tunables",
        "NODE_OPTIONS",
        "NODE_PATH",
        "JAVA_TOOL_OPTIONS",
        "_JAVA_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "GIT_SSH_COMMAND",
        "GIT_CONFIG_COUNT",
        "GIT_EXEC_PATH",
        "PERL5LIB",
        "PERL_MM_OPT",
        "RUBYOPT",
        "GEM_HOME",
        "BUNDLE_GEMFILE",
        "NPM_CONFIG_REGISTRY",
        "JAVA_HOME",
        "DOTNET_STARTUP_HOOKS",
        "CORECLR_PROFILER",
    ],
)
def test_reserved_name_detection(name: str) -> None:
    from orcaster_worker.task_env import is_reserved_env_name

    assert is_reserved_env_name(name)


@pytest.mark.parametrize("name", ["API_TOKEN", "GITHUB_TOKEN", "MY_PATH", "CA_BUNDLE_X"])
def test_non_reserved_name_detection(name: str) -> None:
    from orcaster_worker.task_env import is_reserved_env_name

    assert not is_reserved_env_name(name)


@pytest.mark.parametrize(
    "name", ["PATH", "HOME", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "PIP_INDEX_URL"]
)
def test_reserved_declared_name_keeps_worker_value(name: str) -> None:
    env = build_scrubbed_base_env([name], source=RESERVED_SRC, secrets={name: SECRET})
    assert env[name] == RESERVED_SRC[name]
    assert SECRET not in env.values()


@pytest.mark.parametrize("name", ["LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONPATH", "UV_X"])
def test_reserved_declared_name_absent_in_worker_stays_absent(name: str) -> None:
    env = build_scrubbed_base_env([name], source=RESERVED_SRC, secrets={name: SECRET})
    assert name not in env
    assert SECRET not in env.values()


@pytest.mark.parametrize("name", ["ld_preload", "Git_Ssh_Command", "https_proxy"])
def test_reserved_lowercase_server_value_is_refused(name: str) -> None:
    """The denylist is not case-sensitive: the server value is not applied."""
    env = build_scrubbed_base_env([name], source={}, secrets={name: SECRET})
    assert name not in env
    sources = describe_env_sources([name], source={}, secrets={name: SECRET})
    assert sources.refused_server == (name,)
    assert sources.server == ()


def test_reserved_server_value_logs_warning_with_name_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="orcaster.worker.task_env")
    build_scrubbed_base_env(["LD_PRELOAD"], source=RESERVED_SRC, secrets={"LD_PRELOAD": SECRET})
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("LD_PRELOAD" in r.getMessage() for r in warnings)
    assert all(SECRET not in r.getMessage() for r in caplog.records)


def test_describe_env_sources_reports_refused_server() -> None:
    sources = describe_env_sources(
        ["HOME", "LD_PRELOAD", "API_TOKEN"],
        source=RESERVED_SRC,
        secrets={
            "HOME": SECRET,
            "LD_PRELOAD": SECRET,
            "API_TOKEN": OTHER,
            "UV_INDEX_URL": SECRET,
        },
    )
    assert sources.refused_server == ("HOME", "LD_PRELOAD", "UV_INDEX_URL")
    assert sources.server == ("API_TOKEN",)
    assert sources.worker_env == ("HOME",)
    assert sources.missing == ("LD_PRELOAD",)
    assert sources.ignored == ()
    line = sources.render()
    assert "refused_server=[HOME,LD_PRELOAD,UV_INDEX_URL]" in line
    assert SECRET not in line


@pytest.mark.parametrize(
    "name",
    [
        "CC",
        "CXX",
        "LDSHARED",
        "MAKEFLAGS",
        "GOFLAGS",
        "RUSTC_WRAPPER",
        "CARGO_HOME",
        "PERLLIB",
        "CLASSPATH",
        "PHPRC",
        "NODE_EXTRA_CA_CERTS",
        "LESSOPEN",
        "PAGER",
        "EDITOR",
        "SHELL",
        "CURL_HOME",
        "WGETRC",
        "HGRCPATH",
        "OPENSSL_CONF",
        "XDG_CONFIG_HOME",
        "cxx",
        "Cargo_Home",
        "openssl_conf",
        "Xdg_Data_Home",
        "visual",
    ],
)
def test_reserved_name_detection_closed_list_additions(name: str) -> None:
    from orcaster_worker.task_env import is_reserved_env_name

    assert is_reserved_env_name(name)


@pytest.mark.parametrize(
    "name",
    ["GITHUB_TOKEN", "API_TOKEN", "GITHUB_TOKENS", "OPENROUTER_API_KEY", "hf_token", "CC_TOKEN"],
)
def test_allowed_names_still_not_reserved(name: str) -> None:
    from orcaster_worker.task_env import is_reserved_env_name

    assert not is_reserved_env_name(name)


@pytest.mark.parametrize("name", ["HF_ENDPOINT", "SSLKEYLOGFILE", "Hf_Endpoint", "SslKeyLogFile"])
def test_issue_680_additions_keep_worker_value(name: str) -> None:
    """The server value for `HF_ENDPOINT` or `SSLKEYLOGFILE` is discarded
    and the worker environment's value rules."""
    worker = {name: "worker-value"}
    env = build_scrubbed_base_env([name], source=worker, secrets={name: SECRET})
    assert env[name] == "worker-value"
    assert SECRET not in env.values()
    sources = describe_env_sources([name], source=worker, secrets={name: SECRET})
    assert sources.refused_server == (name,)
    assert sources.server == ()


@pytest.mark.parametrize("name", ["HOSTALIASES", "NETRC", "LUA_INIT", "LC_MESSAGES", "HF_HOME"])
def test_issue_680_left_out_names_take_server_value(name: str) -> None:
    """Names reviewed and deliberately left out of the reserved ones: the server value rules."""
    env = build_scrubbed_base_env([name], source={name: "worker"}, secrets={name: SECRET})
    assert env[name] == SECRET


@pytest.mark.parametrize("name", ["orch_token", "Orch_X"])
def test_orch_any_case_server_value_is_refused(name: str) -> None:
    """A server secret with `ORCH_` in any letter case is not applied."""
    env = build_scrubbed_base_env([name], source={}, secrets={name: SECRET})
    assert name not in env
    sources = describe_env_sources([name], source={}, secrets={name: SECRET})
    assert sources.refused_server == (name,)
    assert sources.server == ()
    assert sources.denied == ()


@pytest.mark.parametrize("name", ["orch_lower", "Orch_X"])
def test_orch_any_case_declared_name_keeps_worker_value(name: str) -> None:
    """The declared-names denylist remains case-sensitive: the worker environment's
    value is injected just as before."""
    env = build_scrubbed_base_env([name], source={name: "worker"}, secrets=None)
    assert env[name] == "worker"
    sources = describe_env_sources([name], source={name: "worker"})
    assert sources.worker_env == (name,)
    assert sources.denied == ()


def test_describe_env_sources_orch_stays_denied_not_refused() -> None:
    sources = describe_env_sources(["ORCH_API_KEY"], source=SRC, secrets={"ORCH_API_KEY": SECRET})
    assert sources.denied == ("ORCH_API_KEY",)
    assert sources.refused_server == ()
    assert sources.ignored == ("ORCH_API_KEY",)


def test_refused_server_value_is_still_masked() -> None:
    from orcaster_worker.main import _task_redactor

    redactor = _task_redactor(_task(secrets={"LD_PRELOAD": SECRET}))
    assert redactor is not None
    assert redactor.redact(f"leak {SECRET}") == f"leak {MASK}"


async def test_run_external_refused_server_name_logged_and_masked(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from orcaster_worker import main as main_mod

    monkeypatch.setenv("HOME", "/home/orcaster")
    captured: dict[str, Any] = {}

    async def fake_dispatch(
        payload, _settings, *, env=None, cwd=None, abandon_event=None, on_output=None
    ):
        captured["env"] = dict(env or {})
        assert on_output is not None
        await on_output(f"echo {SECRET}\n", None)
        return "ok", {"exit_codes": [0], "failed_at": None}

    api = _CaptureAPI()
    task = _task(
        payload={"requirements": [], "commands": ["echo hi"], "task_env": ["HOME"]},
        secrets={"HOME": SECRET},
    )
    caplog.set_level(logging.DEBUG)
    with (
        patch("orcaster_worker.main.dispatch_run_batch", new=fake_dispatch),
        patch("orcaster_worker.main.ephemeral_venv", new=_fake_venv_factory(captured)),
    ):
        await main_mod._run_external(api, settings, main_mod.BusyState(), asyncio.Event(), task)

    assert captured["env"]["HOME"] == "/home/orcaster"
    assert _joined(api.events, "stdout") == f"echo {MASK}\n"
    line = next(r.getMessage() for r in caplog.records if "task_env sources" in r.getMessage())
    assert "refused_server=[HOME]" in line
    assert any(
        r.levelno == logging.WARNING and "HOME" in r.getMessage() and "refused" in r.getMessage()
        for r in caplog.records
    )
    assert all(SECRET not in r.getMessage() for r in caplog.records)


async def test_run_external_sources_line_never_lists_entry_text_or_forges_a_line(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """Rows already stored with `NAME=value` or newlines. The per-task line
    lists only valid names, counts the rest and escapes whatever is not a name."""
    from orcaster_worker import main as main_mod

    value = "hf_abc123"
    orch_value = "s3cr3t_orch_646"
    forged = "SRV_646\nFORGED line"
    captured: dict[str, Any] = {}

    async def fake_dispatch(
        payload, _settings, *, env=None, cwd=None, abandon_event=None, on_output=None
    ):
        captured["env"] = dict(env or {})
        return "ok", {"exit_codes": [0], "failed_at": None}

    api = _CaptureAPI()
    task = _task(
        payload={
            "requirements": [],
            "commands": ["echo hi"],
            "task_env": [
                f"API_TOKEN={value}",
                "bad name",
                "",
                "NOT_SET_646",
                f"ORCH_X={orch_value}",
                f"ORCH_Y\nFORGED2={orch_value}",
                forged,
            ],
        },
        secrets={forged: SECRET, "UNUSED_646\nFORGED3": OTHER},
    )
    caplog.set_level(logging.DEBUG)
    with (
        patch("orcaster_worker.main.dispatch_run_batch", new=fake_dispatch),
        patch("orcaster_worker.main.ephemeral_venv", new=_fake_venv_factory(captured)),
    ):
        await main_mod._run_external(api, settings, main_mod.BusyState(), asyncio.Event(), task)

    line = next(r.getMessage() for r in caplog.records if "task_env sources" in r.getMessage())
    assert "\n" not in line
    assert f"server=[{forged!r}]" in line
    assert "missing=[NOT_SET_646] invalid_missing=2" in line
    assert "denied=[ORCH_X] invalid_denied=1" in line
    assert f"ignored_server_secrets=[{'UNUSED_646' + chr(10) + 'FORGED3'!r}]" in line
    for text in (value, orch_value, "API_TOKEN", "FORGED2", "bad name", "ORCH_X="):
        assert text not in line
    # Neither the line nor any other record of the execution carries text from an
    # entry, nor leaves a raw newline before a name that forges another line.
    assert value not in caplog.text
    assert orch_value not in caplog.text
    assert SECRET not in caplog.text
    assert OTHER not in caplog.text
    for forged_start in ("\nFORGED", "\nFORGED2", "\nFORGED3"):
        assert forged_start not in caplog.text
