"""The "About" line in the user menu: ONLY the server version.

Covers the runtime version accessor (explicit fallback, never raises) and the
rendering of the About line in a full page: `v{server_version}` and nothing else.
There is no fleet drift summary here: each worker's version lives in its own
component (the deck and the worker detail), not as an aggregate in "About".
"""

from __future__ import annotations

import importlib.metadata

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from orcaster_server import VERSION_UNKNOWN, package_version
from orcaster_server import db as app_db
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID

_WS = DEFAULT_WORKSPACE_ID


async def _seed_worker(*, hostname: str, version: str | None, status: str = "online") -> None:
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        await s.execute(
            text(
                """
                INSERT INTO workers (workspace_id, api_key_hash, hostname, os, arch,
                                     status, version, registered_at, last_heartbeat_at)
                VALUES (:ws, :hash, :host, 'linux', 'x86_64', :status, :version,
                        now(), now())
                """
            ),
            {
                "ws": _WS,
                "hash": f"about-{hostname}",
                "host": hostname,
                "status": status,
                "version": version,
            },
        )
        await s.commit()


# --- Version accessor --------------------------------------------------------


def test_package_version_returns_installed_number() -> None:
    v = package_version()
    assert isinstance(v, str)
    assert v  # never empty
    # In the test environment the package is installed with its metadata.
    assert v == importlib.metadata.version("orcaster-server")


def test_package_version_fallback_when_not_installed(monkeypatch) -> None:
    def _raise(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(importlib.metadata, "version", _raise)
    assert package_version() == VERSION_UNKNOWN


def test_package_version_fallback_when_metadata_truncated(monkeypatch) -> None:
    # `.dist-info` present but without METADATA: version() returns None.
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: None)
    assert package_version() == VERSION_UNKNOWN


# --- About line rendering ----------------------------------------------------


@pytest.mark.asyncio
async def test_about_line_shows_server_version(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.get("/admin/", headers={"HX-Request": "false"})
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "About" in body
    assert f"v{package_version()}" in body


@pytest.mark.asyncio
async def test_about_line_has_no_fleet_summary(authed_admin_client: AsyncClient) -> None:
    # "About" does not mix the server version with a fleet aggregate.
    # Even with misaligned workers, the line says NOTHING about the fleet.
    v = package_version()
    await _seed_worker(hostname="w1", version=v)
    await _seed_worker(hostname="w2", version="0.0.1")

    resp = await authed_admin_client.get("/admin/", headers={"HX-Request": "false"})
    body = resp.text
    assert "workers outdated" not in body
    assert "Fleet aligned" not in body
    assert "Fleet status unavailable" not in body
    assert "No active workers" not in body
    # The server version is still present.
    assert f"v{v}" in body


def test_package_version_reads_only_orcaster_server(monkeypatch) -> None:
    # Without `orcaster-server` metadata the version is unknown; no other name is tried.
    seen: list[str] = []

    def _absent(name: str) -> str:
        seen.append(name)
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _absent)
    assert package_version() == VERSION_UNKNOWN
    assert seen == ["orcaster-server"]
