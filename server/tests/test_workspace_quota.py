"""Per-workspace artifact quota and over-commit warning."""

from __future__ import annotations

import logging

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID

GIB = 1024**3


async def _exec(db_url: str, sql: str, params: dict | None = None):
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params or {})
            return result.scalar_one() if result.returns_rows else None
    finally:
        await engine.dispose()


async def _second_workspace(db_url: str, quota: int | None) -> str:
    return await _exec(
        db_url,
        "INSERT INTO workspaces (name, slug, artifact_quota_bytes) "
        "VALUES ('Example', 'example', :q) RETURNING id::text",
        {"q": quota},
    )


async def _set_usage(db_url: str, workspace_id: str, total: int) -> None:
    await _exec(
        db_url,
        "INSERT INTO workspace_artifact_usage (workspace_id, total_bytes) VALUES (:w, :t) "
        "ON CONFLICT (workspace_id) DO UPDATE SET total_bytes = :t",
        {"w": workspace_id, "t": total},
    )


async def _status(db_url: str, workspace_id: str):
    from orcaster_server import storage
    from orcaster_server.config import settings

    engine = create_async_engine(db_url)
    try:
        async with AsyncSession(engine) as session:
            return await storage.storage_status(session, workspace_id, settings=settings)
    finally:
        await engine.dispose()


@pytest.fixture
def _disk(monkeypatch: pytest.MonkeyPatch):
    """Measurable, roomy disk; the caller adjusts free space with `set_free`."""
    from orcaster_server import storage
    from orcaster_server.config import settings

    monkeypatch.setattr(settings, "artifact_disk_free_cache_ttl_s", 0.0)
    monkeypatch.setattr(settings, "artifact_workspace_max_total_bytes", 10 * GIB)
    monkeypatch.setattr(settings, "artifact_disk_min_free_bytes", 2 * GIB)
    free = {"bytes": 50 * GIB}
    monkeypatch.setattr(storage, "disk_free_bytes", lambda _root: free["bytes"])
    monkeypatch.setattr(storage, "disk_total_bytes", lambda _root: 100 * GIB)

    def set_free(n: int) -> None:
        free["bytes"] = n

    return set_free


# --- Effective quota ----------------------------------------------------------


@pytest.mark.asyncio
async def test_null_override_uses_global_default(
    app_client: AsyncClient, migrated_db_url: str, _disk
) -> None:
    st = await _status(migrated_db_url, DEFAULT_WORKSPACE_ID)
    assert st.quota_bytes == 10 * GIB
    assert st.quota_overridden is False


@pytest.mark.asyncio
async def test_two_workspaces_pause_at_their_own_quota(
    app_client: AsyncClient, migrated_db_url: str, _disk
) -> None:
    await _exec(
        migrated_db_url,
        "UPDATE workspaces SET artifact_quota_bytes = :q WHERE id = :w",
        {"q": 3 * GIB, "w": DEFAULT_WORKSPACE_ID},
    )
    other = await _second_workspace(migrated_db_url, 1 * GIB)
    # Same usage in both: 2 GiB. Only the 1 GiB quota one ends up full.
    await _set_usage(migrated_db_url, DEFAULT_WORKSPACE_ID, 2 * GIB)
    await _set_usage(migrated_db_url, other, 2 * GIB)

    mining = await _status(migrated_db_url, DEFAULT_WORKSPACE_ID)
    second = await _status(migrated_db_url, other)
    assert (mining.quota_bytes, mining.quota_overridden) == (3 * GIB, True)
    assert (second.quota_bytes, second.quota_overridden) == (1 * GIB, True)
    assert mining.paused_reason is None
    assert second.paused_reason == "quota"


@pytest.mark.asyncio
async def test_override_must_be_positive(app_client: AsyncClient, migrated_db_url: str) -> None:
    with pytest.raises(IntegrityError):
        await _exec(
            migrated_db_url,
            "UPDATE workspaces SET artifact_quota_bytes = 0 WHERE id = :w",
            {"w": DEFAULT_WORKSPACE_ID},
        )


# --- Total commitment and over-commit warning ----------------------------------


@pytest.mark.asyncio
async def test_quota_commitment_sums_overrides_and_defaults(
    app_client: AsyncClient, migrated_db_url: str, _disk
) -> None:
    from orcaster_server import storage
    from orcaster_server.config import settings

    await _second_workspace(migrated_db_url, 30 * GIB)
    engine = create_async_engine(migrated_db_url)
    try:
        c = await storage.quota_commitment(engine, settings)
    finally:
        await engine.dispose()
    # Default (NULL → 10 GiB) + 30 GiB override.
    assert (c.workspaces, c.overridden) == (2, 1)
    assert c.committed_bytes == 40 * GIB
    assert c.headroom_bytes == 40 * GIB  # no usage, everything is growth headroom
    # Free 50 − floor 2. The filesystem total (100) does not count: the rest is
    # taken by other writers on the same disk.
    assert c.available_bytes == 48 * GIB
    assert c.overcommitted is False


@pytest.mark.asyncio
async def test_quota_commitment_counts_current_artifact_usage(
    app_client: AsyncClient, migrated_db_url: str, _disk
) -> None:
    """What the workspace already uses of its quota is not growth headroom."""
    from orcaster_server import storage
    from orcaster_server.config import settings

    await _set_usage(migrated_db_url, DEFAULT_WORKSPACE_ID, 7 * GIB)
    _disk(5 * GIB)
    engine = create_async_engine(migrated_db_url)
    try:
        c = await storage.quota_commitment(engine, settings)
    finally:
        await engine.dispose()
    assert (c.usage_bytes, c.free_bytes) == (7 * GIB, 5 * GIB)
    assert c.headroom_bytes == 3 * GIB  # 10 quota − 7 used
    assert c.available_bytes == 3 * GIB  # 5 free − 2 floor
    assert c.overcommitted is False  # the headroom fits just above the floor


@pytest.mark.asyncio
async def test_quota_commitment_does_not_credit_usage_above_quota(
    app_client: AsyncClient, migrated_db_url: str, _disk
) -> None:
    """Bytes of a workspace above its quota are not room another one can grow into.

    A: quota 1, usage 5. B: quota 10, usage 0. Free − floor = 8. Summing quotas
    (11) against usage + free − floor (5 + 8 = 13) would say it fits; but B can
    grow by 10 and only 8 sit above the floor, so the shared floor is reachable.
    """
    from orcaster_server import storage
    from orcaster_server.config import settings

    await _exec(
        migrated_db_url,
        "UPDATE workspaces SET artifact_quota_bytes = :q WHERE id = :w",
        {"q": 1 * GIB, "w": DEFAULT_WORKSPACE_ID},
    )
    await _set_usage(migrated_db_url, DEFAULT_WORKSPACE_ID, 5 * GIB)
    await _second_workspace(migrated_db_url, 10 * GIB)
    _disk(10 * GIB)  # free 10 − floor 2 = 8
    engine = create_async_engine(migrated_db_url)
    try:
        c = await storage.quota_commitment(engine, settings)
    finally:
        await engine.dispose()
    assert (c.committed_bytes, c.usage_bytes) == (11 * GIB, 5 * GIB)
    assert c.headroom_bytes == 10 * GIB  # A aporta max(1 − 5, 0) = 0; B aporta 10
    assert c.available_bytes == 8 * GIB
    assert c.overcommitted is True


@pytest.mark.asyncio
async def test_overcommit_warning_logged_once_then_recovery(
    app_client: AsyncClient,
    migrated_db_url: str,
    _disk,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from orcaster_server import housekeeping
    from orcaster_server.config import settings

    housekeeping.reset_quota_commitment_state()
    other = await _second_workspace(migrated_db_url, 30 * GIB)
    _disk(20 * GIB)  # free 20 − floor 2 = 18 GiB < 40 GiB headroom (no usage)
    engine = create_async_engine(migrated_db_url)
    caplog.set_level(logging.INFO, logger="orcaster_server.housekeeping")
    try:
        await housekeeping._check_quota_commitment(engine, settings)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "over-committed" in message
        # Explicit about what it compares: 40 GiB headroom against free 20 − floor 2.
        assert f"can still grow {40 * GIB} bytes" in message
        assert f"only {18 * GIB} bytes are free above the floor" in message
        assert f"free {20 * GIB} - ARTIFACT_DISK_MIN_FREE_BYTES {2 * GIB}" in message

        # No change: the warning is not repeated, even if only free space moved.
        caplog.clear()
        await housekeeping._check_quota_commitment(engine, settings)
        _disk(19 * GIB)
        await housekeeping._check_quota_commitment(engine, settings)
        assert caplog.records == []

        # Lower the override until it fits: one recovery INFO.
        await _exec(
            migrated_db_url,
            "UPDATE workspaces SET artifact_quota_bytes = :q WHERE id = CAST(:w AS uuid)",
            {"q": 1 * GIB, "w": other},
        )
        await housekeeping._check_quota_commitment(engine, settings)
        assert [r.levelno for r in caplog.records] == [logging.INFO]
        assert "fit the disk again" in caplog.records[0].getMessage()
    finally:
        await engine.dispose()
        housekeeping.reset_quota_commitment_state()


@pytest.mark.asyncio
async def test_overcommit_check_warns_when_disk_unmeasurable(
    app_client: AsyncClient,
    migrated_db_url: str,
    _disk,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from orcaster_server import housekeeping, storage
    from orcaster_server.config import settings

    housekeeping.reset_quota_commitment_state()
    _disk(storage._UNLIMITED)  # disk_free_bytes fails open when statvfs fails
    engine = create_async_engine(migrated_db_url)
    caplog.set_level(logging.INFO, logger="orcaster_server.housekeeping")
    try:
        await housekeeping._check_quota_commitment(engine, settings)
    finally:
        await engine.dispose()
        housekeeping.reset_quota_commitment_state()
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "cannot measure" in caplog.records[0].getMessage()


@pytest.mark.asyncio
async def test_no_log_when_quotas_fit(
    app_client: AsyncClient,
    migrated_db_url: str,
    _disk,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from orcaster_server import housekeeping
    from orcaster_server.config import settings

    housekeeping.reset_quota_commitment_state()
    engine = create_async_engine(migrated_db_url)
    caplog.set_level(logging.INFO, logger="orcaster_server.housekeeping")
    try:
        await housekeeping._check_quota_commitment(engine, settings)
    finally:
        await engine.dispose()
        housekeeping.reset_quota_commitment_state()
    assert caplog.records == []


# --- Host script ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1048576", 1024**2), ("20GiB", 20 * GIB), ("512 MiB", 512 * 1024**2), ("1tib", 1024**4)],
)
def test_parse_size_accepts_bytes_and_binary_units(raw: str, expected: int) -> None:
    from orcaster_server.cli.workspace_quota import parse_size

    assert parse_size(raw) == expected


@pytest.mark.parametrize("raw", ["20GB", "0", "0GiB", "-1", "1.5GiB", "lots"])
def test_parse_size_rejects_decimal_units_and_non_positive(raw: str) -> None:
    from orcaster_server.cli.workspace_quota import parse_size

    with pytest.raises(ValueError):
        parse_size(raw)


def test_parse_size_accepts_the_bigint_maximum() -> None:
    from orcaster_server.cli.workspace_quota import MAX_QUOTA_BYTES, parse_size

    assert MAX_QUOTA_BYTES == 2**63 - 1
    assert parse_size(str(MAX_QUOTA_BYTES)) == MAX_QUOTA_BYTES


@pytest.mark.parametrize("raw", [str(2**63), "9000000TiB"])
def test_parse_size_rejects_values_above_bigint(raw: str) -> None:
    from orcaster_server.cli.workspace_quota import parse_size

    with pytest.raises(ValueError, match="exceeds the maximum quota"):
        parse_size(raw)


def test_script_exits_non_zero_on_oversized_quota(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The error comes from argparse (exit 2) before any DB connection is opened."""
    import asyncio
    import sys

    from orcaster_server.cli import workspace_quota

    monkeypatch.setattr(workspace_quota.settings, "database_url", "postgresql+asyncpg://unused")
    monkeypatch.setattr(
        sys, "argv", ["workspace_quota.py", "--workspace", "default", "--set", "9000000TiB"]
    )
    monkeypatch.setattr(
        workspace_quota,
        "create_async_engine",
        lambda *_a, **_k: pytest.fail("must not open the database"),
    )
    with pytest.raises(SystemExit) as exc:
        asyncio.run(workspace_quota.amain())
    assert exc.value.code == 2
    assert "exceeds the maximum quota" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_script_sets_and_clears_override(
    app_client: AsyncClient, migrated_db_url: str, _disk
) -> None:
    from orcaster_server.cli.workspace_quota import list_quotas, set_quota
    from orcaster_server.config import settings

    engine = create_async_engine(migrated_db_url)
    try:
        await set_quota(engine, "default", 20 * GIB)
        (row,) = await list_quotas(engine, settings)
        assert (row.override_bytes, row.effective_bytes) == (20 * GIB, 20 * GIB)

        await set_quota(engine, "default", None)
        (row,) = await list_quotas(engine, settings)
        assert (row.override_bytes, row.effective_bytes) == (None, 10 * GIB)

        with pytest.raises(SystemExit):
            await set_quota(engine, "missing", 1 * GIB)
    finally:
        await engine.dispose()


def test_quota_pause_warning_when_usage_reaches_the_quota() -> None:
    """`--set` below current usage is applied, but warns about the quota pause."""
    from orcaster_server.cli.workspace_quota import WorkspaceQuotaRow, quota_pause_warning

    def row(quota: int, used: int) -> WorkspaceQuotaRow:
        return WorkspaceQuotaRow("example", "Example", quota, quota, used)

    assert quota_pause_warning(row(2 * GIB, 1 * GIB)) is None
    below = quota_pause_warning(row(1 * GIB, 3 * GIB))
    assert below is not None
    assert "WARNING: example already uses 3.00 GiB" in below
    assert "polling for this workspace stays paused" in below
    # Poll pauses at usage >= quota: matching usage also warns.
    assert quota_pause_warning(row(1 * GIB, 1 * GIB)) is not None


@pytest.mark.asyncio
async def test_script_set_below_usage_applies_and_warns(
    app_client: AsyncClient,
    migrated_db_url: str,
    _disk,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import sys

    from orcaster_server.cli import workspace_quota

    await _set_usage(migrated_db_url, DEFAULT_WORKSPACE_ID, 3 * GIB)
    monkeypatch.setattr(workspace_quota.settings, "database_url", migrated_db_url)
    monkeypatch.setattr(
        sys, "argv", ["workspace_quota.py", "--workspace", "default", "--set", "1GiB"]
    )
    await workspace_quota.amain()
    out = capsys.readouterr().out
    assert "default: artifact quota set to 1.00 GiB" in out
    assert "WARNING: default already uses 3.00 GiB" in out
    status = await _status(migrated_db_url, DEFAULT_WORKSPACE_ID)
    assert (status.quota_bytes, status.paused_reason) == (1 * GIB, "quota")


# --- Indicator ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_storage_indicator_shows_effective_quota_and_source(
    authed_admin_client: AsyncClient, migrated_db_url: str, _disk
) -> None:
    r = await authed_admin_client.get("/admin/storage", headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    assert "Server default" in r.text

    await _exec(
        migrated_db_url,
        "UPDATE workspaces SET artifact_quota_bytes = :q WHERE id = :w",
        {"q": 3 * GIB, "w": DEFAULT_WORKSPACE_ID},
    )
    r = await authed_admin_client.get("/admin/storage", headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    assert "Workspace override" in r.text
    assert "/ 3.0 GB" in r.text
