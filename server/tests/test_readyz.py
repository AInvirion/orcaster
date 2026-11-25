"""`/readyz` does not return the DB exception text to an anonymous client.

The text of a driver error may carry the host, the port, the user or the
database name. The route is anonymous: the response carries a fixed reason and
the detail goes only to the server log.

These tests do not use the `app_client` fixture (they need no Postgres): they
talk to the app over ASGI and replace `db.get_engine` and `db.ping`.
"""

import logging

import pytest
from httpx import ASGITransport, AsyncClient

from orcaster_server import db
from orcaster_server.main import app

_DRIVER_ERROR = (
    "connection to server at 'db-internal.example.net' (10.1.2.3), port 5432 failed: "
    'FATAL:  password authentication failed for user "tasktest_admin" database "tasks_prod"'
)
_LEAKED_FRAGMENTS = ("db-internal.example.net", "10.1.2.3", "5432", "tasktest_admin", "tasks_prod")


async def _get_readyz() -> tuple[int, dict[str, str], str]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/readyz")
    return resp.status_code, resp.json(), resp.text


@pytest.mark.asyncio
async def test_readyz_db_error_returns_fixed_reason_without_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed ping returns 503 with a fixed reason, with nothing from the error."""

    async def _boom() -> bool:
        raise RuntimeError(_DRIVER_ERROR)

    monkeypatch.setattr(db, "get_engine", lambda: object())
    monkeypatch.setattr(db, "ping", _boom)

    status, body, raw = await _get_readyz()

    assert status == 503
    assert body == {"status": "not_ready", "reason": "db unreachable"}
    for fragment in _LEAKED_FRAGMENTS:
        assert fragment not in raw


@pytest.mark.asyncio
async def test_readyz_db_error_text_goes_to_the_server_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The detail removed from the response stays in the log, with the traceback."""

    async def _boom() -> bool:
        raise RuntimeError(_DRIVER_ERROR)

    monkeypatch.setattr(db, "get_engine", lambda: object())
    monkeypatch.setattr(db, "ping", _boom)

    with caplog.at_level(logging.ERROR, logger="orcaster_server.main"):
        await _get_readyz()

    records = [r for r in caplog.records if r.levelno >= logging.ERROR and "readyz" in r.message]
    assert records, "readyz did not log the DB error"
    assert records[0].exc_info is not None
    assert _DRIVER_ERROR in "".join(
        str(part) for part in (records[0].getMessage(), records[0].exc_info[1])
    )


@pytest.mark.asyncio
async def test_readyz_ready_when_ping_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the DB up the response does not change."""

    async def _ok() -> bool:
        return True

    monkeypatch.setattr(db, "get_engine", lambda: object())
    monkeypatch.setattr(db, "ping", _ok)

    status, body, _ = await _get_readyz()

    assert status == 200
    assert body == {"status": "ready"}
