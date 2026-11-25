"""Worker-scoped secrets, worker side.

Covers the parsing of `worker_scoped_secrets` (old server without the key,
malformed shape) and the `task_env sources` line: byte-for-byte identical for the names,
with a separate `server_worker_scoped=[...]` token at the end only when there is
any, so that `grep 'server=[GITHUB_TOKENS]'` keeps matching.
"""

from __future__ import annotations

from typing import Any

import pytest

from orcaster_worker.config import Settings
from orcaster_worker.poll import PollResponse
from orcaster_worker.task_env import describe_env_sources
from tests.test_task_secrets import (  # noqa: F401 - `settings` is a fixture
    _CaptureAPI,
    _fake_venv_factory,
    _task,
    settings,
)

WID = "11111111-1111-1111-1111-111111111111"
VALUE = "ghp_worker1_value_0123456789"


def _wire_task(**extra: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": 7,
        "command": "__run_batch__",
        "payload": {"commands": ["echo hi"], "task_env": ["GITHUB_TOKENS"]},
        "created_at": "2026-11-02T00:00:00Z",
        "delivery_mode": "claim",
        "signed": True,
        "signature": None,
        "idempotency_key": None,
        "worker_id": WID,
        "target_worker_id": None,
        "lease_token": "lease-7",
        "lease_expires_at": "2030-01-01T00:00:00Z",
        "secrets": {"GITHUB_TOKENS": VALUE},
    }
    task.update(extra)
    return task


def test_poll_parses_worker_scoped_secrets() -> None:
    resp = PollResponse.from_wire({"task": _wire_task(worker_scoped_secrets=["GITHUB_TOKENS"])})
    assert resp.task is not None
    assert resp.task.worker_scoped_secrets == ("GITHUB_TOKENS",)
    assert resp.task.worker_scoped_secrets_error is None
    assert resp.task.secrets_error is None


@pytest.mark.parametrize("raw", [None, []], ids=["absent_or_null", "empty"])
def test_poll_without_worker_scoped_secrets_parses_as_empty(raw: Any) -> None:
    """Server older than per-worker scope: without the key, no name belongs to the worker."""
    task = _wire_task() if raw is None else _wire_task(worker_scoped_secrets=raw)
    resp = PollResponse.from_wire({"task": task})
    assert resp.task is not None
    assert resp.task.worker_scoped_secrets == ()
    assert resp.task.worker_scoped_secrets_error is None


def test_poll_worker_scoped_only_keeps_delivered_names() -> None:
    """A name that did not come in `secrets` is not marked as worker-scoped."""
    resp = PollResponse.from_wire(
        {"task": _wire_task(worker_scoped_secrets=["NOT_DELIVERED", "GITHUB_TOKENS"])}
    )
    assert resp.task is not None
    assert resp.task.worker_scoped_secrets == ("GITHUB_TOKENS",)


@pytest.mark.parametrize("bad", ["GITHUB_TOKENS", {"GITHUB_TOKENS": True}, [1]])
def test_poll_malformed_worker_scoped_is_flagged_not_fatal(bad: Any) -> None:
    """Diagnostic only: it does not fail the task or touch `secrets`, but it is not
    discarded silently: `worker_scoped_secrets_error` is set."""
    resp = PollResponse.from_wire({"task": _wire_task(worker_scoped_secrets=bad)})
    assert resp.task is not None
    assert resp.task.worker_scoped_secrets == ()
    assert resp.task.worker_scoped_secrets_error
    assert resp.task.secrets_error is None
    assert dict(resp.task.secrets) == {"GITHUB_TOKENS": VALUE}


def test_sources_line_unchanged_without_worker_scoped() -> None:
    plain = describe_env_sources(["GITHUB_TOKENS"], source={}, secrets={"GITHUB_TOKENS": VALUE})
    scoped_none = describe_env_sources(
        ["GITHUB_TOKENS"], source={}, secrets={"GITHUB_TOKENS": VALUE}, worker_scoped=()
    )
    assert plain.render() == scoped_none.render()
    assert "server_worker_scoped" not in plain.render()


def test_sources_line_appends_worker_scoped_token() -> None:
    sources = describe_env_sources(
        ["GITHUB_TOKENS", "API_TOKEN"],
        source={},
        secrets={"GITHUB_TOKENS": VALUE, "API_TOKEN": "hf_value_0123456789"},
        worker_scoped=("GITHUB_TOKENS",),
    )
    base = describe_env_sources(
        ["GITHUB_TOKENS", "API_TOKEN"],
        source={},
        secrets={"GITHUB_TOKENS": VALUE, "API_TOKEN": "hf_value_0123456789"},
    ).render()
    line = sources.render()
    assert line == base + " server_worker_scoped=[GITHUB_TOKENS]"
    assert VALUE not in line


def test_sources_line_existing_grep_keeps_matching() -> None:
    line = describe_env_sources(
        ["GITHUB_TOKENS"],
        source={},
        secrets={"GITHUB_TOKENS": VALUE},
        worker_scoped=("GITHUB_TOKENS",),
    ).render()
    assert "server=[GITHUB_TOKENS]" in line
    assert line.endswith(" server_worker_scoped=[GITHUB_TOKENS]")


def test_sources_worker_scoped_only_lists_injected_names() -> None:
    """A worker name that is undeclared (or reserved) is not injected, so it does not
    appear in `server_worker_scoped`; legacy does not show it either."""
    sources = describe_env_sources(
        ["API_TOKEN"],
        source={},
        secrets={"GITHUB_TOKENS": VALUE, "API_TOKEN": "hf_value_0123456789"},
        worker_scoped=("GITHUB_TOKENS",),
    )
    assert sources.server_worker_scoped == ()
    assert "server_worker_scoped" not in sources.render()
    legacy = describe_env_sources(
        None, source={}, secrets={"GITHUB_TOKENS": VALUE}, worker_scoped=("GITHUB_TOKENS",)
    )
    assert "server_worker_scoped" not in legacy.render()


# --- Integration in main ------------------------------------------------------


async def test_run_external_logs_worker_scoped_token(
    settings: Settings,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The dispatcher's `task_env sources` line carries the token only with names."""
    import asyncio
    import dataclasses
    import logging
    from unittest.mock import patch

    from orcaster_worker import main as main_mod

    async def fake_dispatch(
        payload, _settings, *, env=None, cwd=None, abandon_event=None, on_output=None
    ):
        return "ok", {"exit_codes": [0], "failed_at": None}

    task = dataclasses.replace(
        _task(
            payload={"requirements": [], "commands": ["echo hi"], "task_env": ["GITHUB_TOKENS"]},
            secrets={"GITHUB_TOKENS": VALUE},
        ),
        worker_scoped_secrets=("GITHUB_TOKENS",),
    )
    caplog.set_level(logging.DEBUG)
    captured: dict[str, Any] = {}
    with (
        patch("orcaster_worker.main.dispatch_run_batch", new=fake_dispatch),
        patch("orcaster_worker.main.ephemeral_venv", new=_fake_venv_factory(captured)),
    ):
        await main_mod._run_external(
            _CaptureAPI(), settings, main_mod.BusyState(), asyncio.Event(), task
        )
    [line] = [r.getMessage() for r in caplog.records if "task_env sources" in r.getMessage()]
    assert "server=[GITHUB_TOKENS]" in line
    assert line.endswith(" server_worker_scoped=[GITHUB_TOKENS]")
    assert all(VALUE not in r.getMessage() for r in caplog.records)
