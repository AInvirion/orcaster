"""Request/response serialization tests against the API contract."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import httpx
import pytest
import respx

from orcaster_worker import api as api_module
from orcaster_worker.api import LeaseLostError, OrchestratorAPI

BASE = "http://server.test"


@pytest.fixture
def api():
    return OrchestratorAPI(BASE, worker_id=None, api_key=None)


@respx.mock
async def test_register_persists_credentials(api: OrchestratorAPI):
    route = respx.post(f"{BASE}/v1/workers/register").mock(
        return_value=httpx.Response(
            200,
            json={
                "worker_id": "11111111-1111-1111-1111-111111111111",
                "api_key": "secret-abc",
            },
        )
    )
    res = await api.register(
        enrollment_token="enroll-tok-123",
        hostname="host-1",
        os_name="linux",
        arch="x86_64",
        version="0.1.0",
    )
    assert route.called
    sent = route.calls.last.request
    # X-API-Key must NOT be sent on register (unauthenticated).
    assert "X-API-Key" not in sent.headers
    # The enrollment token travels in the body; the server requires it.
    body = sent.read().decode()
    assert '"enrollment_token":"enroll-tok-123"' in body or (
        '"enrollment_token": "enroll-tok-123"' in body
    )
    assert res.worker_id == "11111111-1111-1111-1111-111111111111"
    assert res.api_key == "secret-abc"
    await api.aclose()


@respx.mock
async def test_poll_returns_no_task(api: OrchestratorAPI):
    from orcaster_worker.poll import PollRequest

    wid = "22222222-2222-2222-2222-222222222222"
    api.set_credentials(wid, "k")
    respx.post(f"{BASE}/v1/workers/{wid}/poll").mock(
        return_value=httpx.Response(
            200,
            json={"task": None, "cancel_signals": [], "next_poll_after_seconds": 30},
        )
    )
    resp = await api.poll(PollRequest(status="idle"))
    assert resp.task is None
    assert resp.cancel_signals == []
    assert resp.next_poll_after_seconds == 30
    await api.aclose()


@respx.mock
async def test_poll_parses_task(api: OrchestratorAPI):
    from orcaster_worker.poll import PollRequest

    wid = "22222222-2222-2222-2222-222222222222"
    api.set_credentials(wid, "k")
    route = respx.post(f"{BASE}/v1/workers/{wid}/poll").mock(
        return_value=httpx.Response(
            200,
            json={
                "task": {
                    "task_id": 7,
                    "command": "/bin/echo",
                    "payload": {"args": ["hi"]},
                    "created_at": "2026-05-18T00:00:00Z",
                    "delivery_mode": "claim",
                    "signed": False,
                    "signature": None,
                    "idempotency_key": None,
                    "worker_id": wid,
                    "target_worker_id": None,
                    "lease_token": "33333333-3333-3333-3333-333333333333",
                    "lease_expires_at": "2030-01-01T00:00:00Z",
                },
                "cancel_signals": [],
                "next_poll_after_seconds": 1,
            },
        )
    )
    resp = await api.poll(PollRequest(status="idle"))
    assert route.called
    assert route.calls.last.request.headers["X-API-Key"] == "k"
    assert resp.task is not None
    assert resp.task.task_id == 7
    assert resp.task.command == "/bin/echo"
    assert resp.task.payload == {"args": ["hi"]}
    assert resp.task.delivery_mode == "claim"
    assert resp.task.signed is False
    await api.aclose()


@respx.mock
async def test_complete_409_raises_lease_lost(api: OrchestratorAPI):
    api.set_credentials("22222222-2222-2222-2222-222222222222", "k")
    respx.post(f"{BASE}/v1/tasks/7/complete").mock(
        return_value=httpx.Response(409, json={"detail": "execution no longer owned"})
    )
    with pytest.raises(LeaseLostError):
        await api.complete(
            7,
            lease_token="33333333-3333-3333-3333-333333333333",
            exit_code=0,
            status="completed",
        )
    await api.aclose()


@respx.mock
async def test_output_sends_lease_and_streams(api: OrchestratorAPI):
    api.set_credentials("22222222-2222-2222-2222-222222222222", "k")
    route = respx.post(f"{BASE}/v1/tasks/7/output").mock(return_value=httpx.Response(202))
    await api.output(
        7,
        lease_token="33333333-3333-3333-3333-333333333333",
        events=[
            {"seq": 1, "ts": "2026-06-03T00:00:00+00:00", "stream": "stdout", "text": "chunk-1"},
        ],
    )
    body = route.calls.last.request.read().decode()
    assert "33333333" in body
    assert "chunk-1" in body
    assert '"events"' in body
    await api.aclose()


@respx.mock
async def test_output_409_raises_lease_lost(api: OrchestratorAPI) -> None:
    """409 on /output = the attempt is no longer ours -> LeaseLostError so that
    the streamer's sender aborts the backlog (reliable streaming)."""
    api.set_credentials("22222222-2222-2222-2222-222222222222", "k")
    respx.post(f"{BASE}/v1/tasks/7/output").mock(return_value=httpx.Response(409))
    with pytest.raises(LeaseLostError):
        await api.output(
            7,
            lease_token="tok",
            events=[{"seq": 1, "ts": "2026-06-03T00:00:00+00:00", "stream": "stdout", "text": "x"}],
        )


@respx.mock
async def test_complete_body_has_no_stdout_stderr(api: OrchestratorAPI) -> None:
    """Output travels exclusively as events; /complete no longer sends
    stdout/stderr (the server ignores them since output travels as events)."""
    api.set_credentials("22222222-2222-2222-2222-222222222222", "k")
    route = respx.post(f"{BASE}/v1/tasks/7/complete").mock(
        return_value=httpx.Response(200, json={})
    )
    await api.complete(7, lease_token="tok", exit_code=0, status="completed")
    body = json.loads(route.calls.last.request.content)
    assert "stdout" not in body and "stderr" not in body


@respx.mock
async def test_poll_sends_telemetry(api: OrchestratorAPI):
    from orcaster_worker.poll import PollRequest

    wid = "22222222-2222-2222-2222-222222222222"
    api.set_credentials(wid, "k")
    route = respx.post(f"{BASE}/v1/workers/{wid}/poll").mock(
        return_value=httpx.Response(
            200, json={"task": None, "cancel_signals": [], "next_poll_after_seconds": 30}
        )
    )
    await api.poll(
        PollRequest(
            status="idle",
            ram_mb=2048,
            ram_used_mb=128,
            disk_mb=10000,
            disk_used_mb=500,
        )
    )
    body = route.calls.last.request.read().decode()
    assert '"ram_mb":2048' in body or '"ram_mb": 2048' in body
    assert '"disk_used_mb":500' in body or '"disk_used_mb": 500' in body
    await api.aclose()


def test_detect_hostname_prefers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOST_HOSTNAME", "real-host.example")
    # Even with the file present, env wins.
    monkeypatch.setattr(api_module, "_HOST_HOSTNAME_FILE", Path("/nonexistent"))
    assert api_module._detect_hostname() == "real-host.example"


def test_detect_hostname_falls_back_to_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HOST_HOSTNAME", raising=False)
    hostfile = tmp_path / "host_hostname"
    hostfile.write_text("mounted-host\n", encoding="utf-8")
    monkeypatch.setattr(api_module, "_HOST_HOSTNAME_FILE", hostfile)
    assert api_module._detect_hostname() == "mounted-host"


def test_detect_hostname_socket_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HOST_HOSTNAME", raising=False)
    monkeypatch.setattr(api_module, "_HOST_HOSTNAME_FILE", tmp_path / "absent")
    monkeypatch.setattr(socket, "gethostname", lambda: "container-1a2b3c")
    assert api_module._detect_hostname() == "container-1a2b3c"


def test_detect_hostname_ignores_empty_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`-e HOST_HOSTNAME=` must not leak an empty string."""
    monkeypatch.setenv("HOST_HOSTNAME", "   ")
    monkeypatch.setattr(api_module, "_HOST_HOSTNAME_FILE", tmp_path / "absent")
    monkeypatch.setattr(socket, "gethostname", lambda: "fallback-host")
    assert api_module._detect_hostname() == "fallback-host"


@respx.mock
async def test_register_uses_detect_hostname_when_omitted(
    monkeypatch: pytest.MonkeyPatch, api: OrchestratorAPI
):
    monkeypatch.setenv("HOST_HOSTNAME", "host-from-env")
    route = respx.post(f"{BASE}/v1/workers/register").mock(
        return_value=httpx.Response(
            200,
            json={
                "worker_id": "11111111-1111-1111-1111-111111111111",
                "api_key": "k",
            },
        )
    )
    await api.register(enrollment_token="enroll-tok-123", os_name="linux", arch="x86_64")
    body = route.calls.last.request.read().decode()
    assert '"hostname":"host-from-env"' in body or '"hostname": "host-from-env"' in body
    await api.aclose()


@respx.mock
async def test_register_sends_name_in_body(api: OrchestratorAPI):
    route = respx.post(f"{BASE}/v1/workers/register").mock(
        return_value=httpx.Response(
            200,
            json={
                "worker_id": "11111111-1111-1111-1111-111111111111",
                "api_key": "k",
            },
        )
    )
    await api.register(
        enrollment_token="enroll-tok-123",
        hostname="host-1",
        os_name="linux",
        arch="x86_64",
        name="prod-eu-1",
    )
    body = route.calls.last.request.read().decode()
    assert '"name":"prod-eu-1"' in body or '"name": "prod-eu-1"' in body
    await api.aclose()


@respx.mock
async def test_poll_sends_name_in_body(api: OrchestratorAPI):
    from orcaster_worker.poll import PollRequest

    wid = "22222222-2222-2222-2222-222222222222"
    api.set_credentials(wid, "k")
    route = respx.post(f"{BASE}/v1/workers/{wid}/poll").mock(
        return_value=httpx.Response(
            200, json={"task": None, "cancel_signals": [], "next_poll_after_seconds": 30}
        )
    )
    await api.poll(PollRequest(status="idle", name="prod-eu-1"))
    body = route.calls.last.request.read().decode()
    assert '"name":"prod-eu-1"' in body or '"name": "prod-eu-1"' in body
    await api.aclose()


@respx.mock
async def test_goodbye_calls_endpoint(api: OrchestratorAPI):
    api.set_credentials("22222222-2222-2222-2222-222222222222", "k")
    route = respx.post(f"{BASE}/v1/workers/22222222-2222-2222-2222-222222222222/goodbye").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    await api.goodbye()
    assert route.called
    await api.aclose()
