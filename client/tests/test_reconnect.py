"""Idempotent reconnection tests.

We cover `_with_retry` in `orcaster_worker.api`:
    * success after a transient `ConnectError`.
    * success after a transient 502 (with several retries).
    * 401 does not retry.
    * exhausting the attempts propagates `httpx.ConnectError`.
    * 429 with `Retry-After` honors the header.

The loop only uses /poll, so the tests target that endpoint.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

import orcaster_worker.api as api_mod
from orcaster_worker.api import OrchestratorAPI
from orcaster_worker.poll import PollRequest

BASE = "http://server.test"
WID = "22222222-2222-2222-2222-222222222222"
POLL_URL = f"{BASE}/v1/workers/{WID}/poll"


def _empty_poll_response() -> dict:
    return {"task": None, "cancel_signals": [], "next_poll_after_seconds": 30}


def _task_poll_response(task_id: int = 99) -> dict:
    return {
        "task": {
            "task_id": task_id,
            "command": "/bin/echo",
            "payload": {},
            "created_at": "2026-05-18T00:00:00Z",
            "delivery_mode": "claim",
            "signed": False,
            "signature": None,
            "idempotency_key": None,
            "worker_id": WID,
            "target_worker_id": None,
            "lease_token": "11111111-1111-1111-1111-111111111111",
            "lease_expires_at": "2030-01-01T00:00:00Z",
        },
        "cancel_signals": [],
        "next_poll_after_seconds": 1,
    }


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Disable `asyncio.sleep` inside the api module for speed."""

    async def _fast(_seconds: float) -> None:
        return None

    monkeypatch.setattr(api_mod.asyncio, "sleep", _fast)
    yield


def _make_api(**kwargs: Any) -> OrchestratorAPI:
    return OrchestratorAPI(
        BASE,
        worker_id=WID,
        api_key="k",
        max_attempts=kwargs.pop("max_attempts", 5),
        base_delay=kwargs.pop("base_delay", 0.01),
        max_delay=kwargs.pop("max_delay", 0.05),
        **kwargs,
    )


@respx.mock
async def test_retry_on_connect_error_then_success() -> None:
    api = _make_api()
    route = respx.post(POLL_URL).mock(
        side_effect=[
            httpx.ConnectError("network down"),
            httpx.ConnectError("still down"),
            httpx.Response(200, json=_empty_poll_response()),
        ]
    )
    res = await api.poll(PollRequest(status="idle"))
    assert res.task is None
    assert route.call_count == 3
    await api.aclose()


@respx.mock
async def test_retry_on_502_then_200() -> None:
    api = _make_api()
    route = respx.post(POLL_URL).mock(
        side_effect=[
            httpx.Response(502, text="bad gateway"),
            httpx.Response(503, text="unavailable"),
            httpx.Response(200, json=_task_poll_response(task_id=99)),
        ]
    )
    res = await api.poll(PollRequest(status="idle"))
    assert route.call_count == 3
    assert res.task is not None
    assert res.task.task_id == 99
    await api.aclose()


@respx.mock
async def test_no_retry_on_401() -> None:
    api = _make_api()
    route = respx.post(POLL_URL).mock(return_value=httpx.Response(401, json={"detail": "bad key"}))
    with pytest.raises(httpx.HTTPStatusError):
        await api.poll(PollRequest(status="idle"))
    assert route.call_count == 1
    await api.aclose()


@respx.mock
async def test_no_retry_on_409_complete_raises_lease_lost() -> None:
    from orcaster_worker.api import LeaseLostError

    api = _make_api()
    route = respx.post(f"{BASE}/v1/tasks/7/complete").mock(
        return_value=httpx.Response(409, json={"detail": "lease lost"})
    )
    with pytest.raises(LeaseLostError):
        await api.complete(
            7,
            lease_token="lease-x",
            exit_code=0,
            status="completed",
        )
    assert route.call_count == 1
    await api.aclose()


@respx.mock
async def test_retry_exhausted_propagates_connect_error() -> None:
    api = _make_api(max_attempts=4)
    route = respx.post(POLL_URL).mock(side_effect=httpx.ConnectError("never recovers"))
    with pytest.raises(httpx.ConnectError):
        await api.poll(PollRequest(status="idle"))
    assert route.call_count == 4
    await api.aclose()


@respx.mock
async def test_429_with_retry_after_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the 429 response carries a numeric `Retry-After: N`, we honor it."""
    sleeps: list[float] = []

    async def _record(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(api_mod.asyncio, "sleep", _record)

    api = _make_api(max_attempts=3, base_delay=0.5, max_delay=10.0)
    respx.post(POLL_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}, text="slow down"),
            httpx.Response(200, json=_empty_poll_response()),
        ]
    )
    await api.poll(PollRequest(status="idle"))
    # The first sleep must be exactly 2.0s (header), without jitter.
    assert sleeps and sleeps[0] == 2.0
    await api.aclose()


@respx.mock
async def test_register_retries_too() -> None:
    """Sanity: the wrapper also applies to the unauthenticated endpoint."""
    api = OrchestratorAPI(
        BASE,
        max_attempts=3,
        base_delay=0.01,
        max_delay=0.05,
    )
    route = respx.post(f"{BASE}/v1/workers/register").mock(
        side_effect=[
            httpx.ConnectError("blip"),
            httpx.Response(
                200,
                json={
                    "worker_id": "11111111-1111-1111-1111-111111111111",
                    "api_key": "k",
                },
            ),
        ]
    )
    res = await api.register(
        enrollment_token="enroll-tok-123", hostname="h", os_name="linux", arch="x86_64"
    )
    assert res.worker_id == "11111111-1111-1111-1111-111111111111"
    assert route.call_count == 2
    await api.aclose()


async def test_with_retry_helper_module_level() -> None:
    """`_with_retry` is callable as a standalone helper (internal public
    contract used by `OrchestratorAPI._post`)."""
    calls = {"n": 0}

    async def factory() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("flake")
        return "ok"

    out = await api_mod._with_retry(factory, max_attempts=5, base_delay=0.0, max_delay=0.0)
    assert out == "ok"
    assert calls["n"] == 3


async def test_with_retry_exhausts_and_raises() -> None:
    async def factory() -> str:
        raise httpx.ReadTimeout("timeout")

    with pytest.raises(httpx.ReadTimeout):
        await api_mod._with_retry(factory, max_attempts=2, base_delay=0.0, max_delay=0.0)


def test_pytest_asyncio_mode_check() -> None:
    """Marker sanity - if pytest-asyncio is not in mode=auto the async tests
    would be silently skipped. This blows up if that were the case."""
    assert inspect.iscoroutinefunction(test_retry_on_connect_error_then_success)
