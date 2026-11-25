"""POST /v1/tasks/{id}/artifacts: multipart artifact upload."""

import hashlib
import io

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.helpers_artifacts import _running_execution


@pytest.fixture(autouse=True)
def _artifacts_tmp_root(tmp_path, monkeypatch) -> None:
    """Redirect ARTIFACTS_ROOT to a writable tmp (the default lives in
    /var/lib and is not writable in the test environment)."""
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifacts_root", tmp_path / "artifacts")


async def _count_rows(db_url: str, execution_id: str) -> int:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            return (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM task_artifacts WHERE execution_id = CAST(:e AS uuid)"
                    ),
                    {"e": execution_id},
                )
            ).scalar_one()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upload_single_artifact_202(app_client: AsyncClient, migrated_db_url: str) -> None:
    task_id, key, token, eid = await _running_execution(app_client, migrated_db_url)
    content = b'{"model": "x"}' * 100
    r = await app_client.post(
        f"/v1/tasks/{task_id}/artifacts",
        headers={"X-API-Key": key},
        data={"lease_token": str(token)},
        files=[("files", ("results.json", io.BytesIO(content), "application/json"))],
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert len(body["accepted"]) == 1
    a = body["accepted"][0]
    assert a["rel_path"] == "results.json"
    assert a["size_bytes"] == len(content)
    assert a["sha256"] == hashlib.sha256(content).hexdigest()
    assert a["content_type"] == "application/json"
    assert await _count_rows(migrated_db_url, eid) == 1


@pytest.mark.asyncio
async def test_upload_preserves_subdirs(app_client: AsyncClient, migrated_db_url: str) -> None:
    task_id, key, token, eid = await _running_execution(app_client, migrated_db_url)
    r = await app_client.post(
        f"/v1/tasks/{task_id}/artifacts",
        headers={"X-API-Key": key},
        data={"lease_token": str(token)},
        files=[("files", ("nested/dir/a.json", io.BytesIO(b"x"), "application/json"))],
    )
    assert r.status_code == 202
    assert r.json()["accepted"][0]["rel_path"] == "nested/dir/a.json"


@pytest.mark.asyncio
async def test_upload_idempotent_same_sha_noop(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    task_id, key, token, eid = await _running_execution(app_client, migrated_db_url)
    content = b"same-bytes"

    async def _post() -> int:
        r = await app_client.post(
            f"/v1/tasks/{task_id}/artifacts",
            headers={"X-API-Key": key},
            data={"lease_token": str(token)},
            files=[("files", ("r.json", io.BytesIO(content), "application/json"))],
        )
        return r.status_code

    assert await _post() == 202
    assert await _post() == 202  # resend: no-op
    assert await _count_rows(migrated_db_url, eid) == 1


@pytest.mark.asyncio
async def test_upload_bad_lease_409(app_client: AsyncClient, migrated_db_url: str) -> None:
    task_id, key, _token, _eid = await _running_execution(app_client, migrated_db_url)
    r = await app_client.post(
        f"/v1/tasks/{task_id}/artifacts",
        headers={"X-API-Key": key},
        data={"lease_token": "00000000-0000-0000-0000-000000000000"},
        files=[("files", ("r.json", io.BytesIO(b"x"), "application/json"))],
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_upload_rejects_traversal_422(app_client: AsyncClient, migrated_db_url: str) -> None:
    task_id, key, token, _eid = await _running_execution(app_client, migrated_db_url)
    r = await app_client.post(
        f"/v1/tasks/{task_id}/artifacts",
        headers={"X-API-Key": key},
        data={"lease_token": str(token)},
        files=[("files", ("../escape.json", io.BytesIO(b"x"), "application/json"))],
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_upload_per_file_limit_413(
    app_client: AsyncClient, migrated_db_url: str, monkeypatch
) -> None:
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifact_max_file_bytes", 10)
    task_id, key, token, _eid = await _running_execution(app_client, migrated_db_url)
    r = await app_client.post(
        f"/v1/tasks/{task_id}/artifacts",
        headers={"X-API-Key": key},
        data={"lease_token": str(token)},
        files=[("files", ("big.bin", io.BytesIO(b"x" * 50), "application/octet-stream"))],
    )
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_upload_count_limit_413(
    app_client: AsyncClient, migrated_db_url: str, monkeypatch
) -> None:
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifact_max_count", 1)
    task_id, key, token, _eid = await _running_execution(app_client, migrated_db_url)
    r = await app_client.post(
        f"/v1/tasks/{task_id}/artifacts",
        headers={"X-API-Key": key},
        data={"lease_token": str(token)},
        files=[
            ("files", ("a.json", io.BytesIO(b"a"), "application/json")),
            ("files", ("b.json", io.BytesIO(b"b"), "application/json")),
        ],
    )
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_upload_total_size_limit_413(
    app_client: AsyncClient, migrated_db_url: str, monkeypatch
) -> None:
    from orcaster_server.config import settings as app_settings

    # Per-execution total bounded to 10 bytes; two 8-byte files (16 > 10)
    # -> the second one trips the total guard.
    monkeypatch.setattr(app_settings, "artifact_max_total_bytes", 10)
    task_id, key, token, _eid = await _running_execution(app_client, migrated_db_url)
    r = await app_client.post(
        f"/v1/tasks/{task_id}/artifacts",
        headers={"X-API-Key": key},
        data={"lease_token": str(token)},
        files=[
            ("files", ("a.json", io.BytesIO(b"x" * 8), "application/octet-stream")),
            ("files", ("b.json", io.BytesIO(b"y" * 8), "application/octet-stream")),
        ],
    )
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_upload_total_limit_cleans_previous_bytes(
    app_client: AsyncClient, migrated_db_url: str, monkeypatch
) -> None:
    # When the second file of a POST trips 413 on the total, the
    # first file's bytes must NOT remain in ARTIFACTS_ROOT/<eid>/.
    from pathlib import Path

    from orcaster_server.config import settings as app_settings

    # Total bounded to 10 bytes; two 8-byte files (16 > 10).
    monkeypatch.setattr(app_settings, "artifact_max_total_bytes", 10)
    task_id, key, token, eid = await _running_execution(app_client, migrated_db_url)

    r = await app_client.post(
        f"/v1/tasks/{task_id}/artifacts",
        headers={"X-API-Key": key},
        data={"lease_token": str(token)},
        files=[
            ("files", ("first.bin", io.BytesIO(b"x" * 8), "application/octet-stream")),
            ("files", ("second.bin", io.BytesIO(b"y" * 8), "application/octet-stream")),
        ],
    )
    assert r.status_code == 413

    # The first file's bytes must have been cleaned up.
    exec_dir = Path(app_settings.artifacts_root) / eid
    assert not (exec_dir / "first.bin").exists(), (
        "first file bytes must be cleaned up when the batch fails"
    )


@pytest.mark.asyncio
async def test_batch_failure_preserves_existing_path_bytes(
    app_client: AsyncClient, migrated_db_url: str, monkeypatch
) -> None:
    # Regression: when a LATER file in the batch trips 413, the
    # cleanup must NOT delete the bytes of a PRE-EXISTING path whose metadata row
    # was committed in an earlier request (it survives the rollback).
    # Deleting them would leave it orphaned (live row without bytes -> 404 on download).
    from pathlib import Path

    from orcaster_server.config import settings as app_settings

    keep = b"keep-bytes"  # 10 bytes
    task_id, key, token, eid = await _running_execution(app_client, migrated_db_url)

    # Request 1: upload keep.json and verify its bytes stay on disk.
    r1 = await app_client.post(
        f"/v1/tasks/{task_id}/artifacts",
        headers={"X-API-Key": key},
        data={"lease_token": str(token)},
        files=[("files", ("keep.json", io.BytesIO(keep), "application/json"))],
    )
    assert r1.status_code == 202, r1.text
    exec_dir = Path(app_settings.artifacts_root) / eid
    assert (exec_dir / "keep.json").exists()

    # Request 2: keep.json (same content, idempotent re-upload -> no-op) +
    # big.json exceeds the per-file limit (trips 413). The per-file limit
    # trims big.json mid-stream; keep.json was already written.
    monkeypatch.setattr(app_settings, "artifact_max_file_bytes", 16)
    r2 = await app_client.post(
        f"/v1/tasks/{task_id}/artifacts",
        headers={"X-API-Key": key},
        data={"lease_token": str(token)},
        files=[
            ("files", ("keep.json", io.BytesIO(keep), "application/json")),
            ("files", ("big.json", io.BytesIO(b"z" * 64), "application/octet-stream")),
        ],
    )
    assert r2.status_code == 413, r2.text

    # The keep.json row is still alive (committed in the previous request).
    assert await _count_rows(migrated_db_url, eid) >= 1
    # And its bytes were NOT deleted by the cleanup of the failed batch.
    assert (exec_dir / "keep.json").exists(), (
        "bytes of a pre-existing path must NOT be deleted when a later file in the batch fails"
    )


@pytest.mark.asyncio
async def test_reupload_existing_path_not_double_counted_in_total(
    app_client: AsyncClient, migrated_db_url: str, monkeypatch
) -> None:
    # Guard for the new_total bug: re-uploading the SAME rel_path with more content
    # must subtract the old size from the total. With total limit = 20 and a single
    # file that grows from 8 to 16 bytes, the second POST (16 <= 20) must NOT give 413
    # even though 8 + 16 = 24 > 20 if it were counted twice.
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifact_max_total_bytes", 20)
    task_id, key, token, eid = await _running_execution(app_client, migrated_db_url)

    r1 = await app_client.post(
        f"/v1/tasks/{task_id}/artifacts",
        headers={"X-API-Key": key},
        data={"lease_token": str(token)},
        files=[("files", ("r.json", io.BytesIO(b"x" * 8), "application/octet-stream"))],
    )
    assert r1.status_code == 202

    r2 = await app_client.post(
        f"/v1/tasks/{task_id}/artifacts",
        headers={"X-API-Key": key},
        data={"lease_token": str(token)},
        files=[("files", ("r.json", io.BytesIO(b"z" * 16), "application/octet-stream"))],
    )
    assert r2.status_code == 202, r2.text
    # Still a single row (same rel_path, upsert).
    assert await _count_rows(migrated_db_url, eid) == 1
    assert r2.json()["accepted"][0]["size_bytes"] == 16


@pytest.mark.asyncio
async def test_changed_existing_path_not_overwritten_when_batch_trips_limit(
    app_client: AsyncClient, migrated_db_url: str, monkeypatch
) -> None:
    # Re-uploading an EXISTING rel_path with DIFFERENT content in the
    # SAME batch as a LATER file that trips 413 must NOT leave the disk
    # with the new bytes while the DB rolls back to the old metadata
    # (disk/DB inconsistency). The two-phase validation guarantees that if the
    # batch does not pass ALL the limits, not a single byte is written.
    from pathlib import Path

    from orcaster_server.config import settings as app_settings

    old = b"v1-old-bytes"  # 12 bytes
    old_sha = hashlib.sha256(old).hexdigest()
    task_id, key, token, eid = await _running_execution(app_client, migrated_db_url)

    # Request 1: upload r.json with the old content.
    r1 = await app_client.post(
        f"/v1/tasks/{task_id}/artifacts",
        headers={"X-API-Key": key},
        data={"lease_token": str(token)},
        files=[("files", ("r.json", io.BytesIO(old), "application/json"))],
    )
    assert r1.status_code == 202, r1.text
    exec_dir = Path(app_settings.artifacts_root) / eid
    assert (exec_dir / "r.json").read_bytes() == old

    # Request 2: r.json with NEW content (fits the per-file limit) +
    # big.json which exceeds it (trips 413). r.json comes BEFORE big.json in the
    # batch: in a single-pass flow it would be rewritten before big.json failed.
    new = b"v2-new-and-longer-content"  # 25 bytes (<= 32)
    monkeypatch.setattr(app_settings, "artifact_max_file_bytes", 32)
    r2 = await app_client.post(
        f"/v1/tasks/{task_id}/artifacts",
        headers={"X-API-Key": key},
        data={"lease_token": str(token)},
        files=[
            ("files", ("r.json", io.BytesIO(new), "application/json")),
            ("files", ("big.json", io.BytesIO(b"z" * 64), "application/octet-stream")),
        ],
    )
    assert r2.status_code == 413, r2.text

    # Disk: r.json still has the OLD content (it was not rewritten).
    assert (exec_dir / "r.json").read_bytes() == old, (
        "an existing path must NOT be rewritten if the batch trips a limit"
    )
    # big.json must not have been materialized.
    assert not (exec_dir / "big.json").exists()
    # DB: a single row, with the OLD sha and size (consistent with the disk).
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            metarow = (
                await conn.execute(
                    text(
                        "SELECT sha256, size_bytes FROM task_artifacts "
                        "WHERE execution_id = CAST(:e AS uuid)"
                    ),
                    {"e": eid},
                )
            ).first()
    finally:
        await engine.dispose()
    assert metarow is not None
    assert metarow.sha256 == old_sha
    assert metarow.size_bytes == len(old)
    assert await _count_rows(migrated_db_url, eid) == 1


# ---------------------------------------------------------------------------
# Per-workspace counter and ENOSPC->507
# ---------------------------------------------------------------------------

DEFAULT_WS = "00000000-0000-0000-0000-000000000001"


async def _workspace_usage(db_url: str, workspace_id: str) -> int:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT total_bytes FROM workspace_artifact_usage WHERE workspace_id = :w"
                    ),
                    {"w": workspace_id},
                )
            ).first()
            return int(row[0]) if row is not None else 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upload_increments_workspace_usage(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    task_id, key, token, eid = await _running_execution(app_client, migrated_db_url)
    content = b"z" * 4096
    r = await app_client.post(
        f"/v1/tasks/{task_id}/artifacts",
        headers={"X-API-Key": key},
        data={"lease_token": str(token)},
        files=[("files", ("a.bin", io.BytesIO(content), "application/octet-stream"))],
    )
    assert r.status_code == 202, r.text
    assert await _workspace_usage(migrated_db_url, DEFAULT_WS) == 4096

    # Idempotent resend of the SAME file: the counter does not change.
    r2 = await app_client.post(
        f"/v1/tasks/{task_id}/artifacts",
        headers={"X-API-Key": key},
        data={"lease_token": str(token)},
        files=[("files", ("a.bin", io.BytesIO(content), "application/octet-stream"))],
    )
    assert r2.status_code == 202, r2.text
    assert await _workspace_usage(migrated_db_url, DEFAULT_WS) == 4096


@pytest.mark.asyncio
async def test_upload_enospc_returns_507(
    app_client: AsyncClient, migrated_db_url: str, monkeypatch
) -> None:
    from orcaster_server import artifacts_store
    from orcaster_server.artifacts_store import NoSpaceLeft

    task_id, key, token, eid = await _running_execution(app_client, migrated_db_url)

    def _boom(*args, **kwargs):
        # The real layer of stage_artifact_bytes translates ENOSPC to NoSpaceLeft;
        # the mock mirrors the real exception to exercise the right branch.
        raise NoSpaceLeft("No space left on device")

    monkeypatch.setattr(artifacts_store, "stage_artifact_bytes", _boom)
    r = await app_client.post(
        f"/v1/tasks/{task_id}/artifacts",
        headers={"X-API-Key": key},
        data={"lease_token": str(token)},
        files=[("files", ("a.bin", io.BytesIO(b"data"), "application/octet-stream"))],
    )
    assert r.status_code == 507, r.text


# ---------------------------------------------------------------------------
# Per-workspace quota enforcement and disk floor (507)
# ---------------------------------------------------------------------------


async def _set_workspace_usage(db_url: str, workspace_id: str, total: int) -> None:
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO workspace_artifact_usage (workspace_id, total_bytes)
                    VALUES (:w, :t)
                    ON CONFLICT (workspace_id) DO UPDATE SET total_bytes = :t
                    """
                ),
                {"w": workspace_id, "t": total},
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upload_507_when_workspace_quota_exceeded(
    app_client: AsyncClient, migrated_db_url: str, monkeypatch
) -> None:
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifact_workspace_max_total_bytes", 1000)
    task_id, key, token, eid = await _running_execution(app_client, migrated_db_url)
    await _set_workspace_usage(migrated_db_url, DEFAULT_WS, 999)

    r = await app_client.post(
        f"/v1/tasks/{task_id}/artifacts",
        headers={"X-API-Key": key},
        data={"lease_token": str(token)},
        files=[("files", ("a.bin", io.BytesIO(b"x" * 50), "application/octet-stream"))],
    )
    assert r.status_code == 507, r.text
    # No metadata was written.
    assert await _count_rows(migrated_db_url, eid) == 0


async def _set_workspace_quota(db_url: str, workspace_id: str, quota: int | None) -> None:
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE workspaces SET artifact_quota_bytes = :q WHERE id = :w"),
                {"w": workspace_id, "q": quota},
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("global_quota", "override", "expected"),
    [
        # Override LOWER than the default: cuts at the override.
        (10 * 1024**3, 1000, 507),
        # Override HIGHER than the default: the default no longer cuts.
        (1000, 10_000, 202),
    ],
)
async def test_upload_quota_uses_workspace_override(
    app_client: AsyncClient,
    migrated_db_url: str,
    monkeypatch,
    global_quota: int,
    override: int,
    expected: int,
) -> None:
    from orcaster_server.config import settings as app_settings

    monkeypatch.setattr(app_settings, "artifact_workspace_max_total_bytes", global_quota)
    task_id, key, token, eid = await _running_execution(app_client, migrated_db_url)
    await _set_workspace_quota(migrated_db_url, DEFAULT_WS, override)
    await _set_workspace_usage(migrated_db_url, DEFAULT_WS, 999)

    r = await app_client.post(
        f"/v1/tasks/{task_id}/artifacts",
        headers={"X-API-Key": key},
        data={"lease_token": str(token)},
        files=[("files", ("a.bin", io.BytesIO(b"x" * 50), "application/octet-stream"))],
    )
    assert r.status_code == expected, r.text
    assert await _count_rows(migrated_db_url, eid) == (1 if expected == 202 else 0)


@pytest.mark.asyncio
async def test_upload_507_when_disk_below_floor(
    app_client: AsyncClient, migrated_db_url: str, monkeypatch
) -> None:
    from orcaster_server import storage

    task_id, key, token, eid = await _running_execution(app_client, migrated_db_url)
    # Free disk below the floor (default 2 GiB).
    monkeypatch.setattr(storage, "disk_free_bytes", lambda _root: 1024)

    r = await app_client.post(
        f"/v1/tasks/{task_id}/artifacts",
        headers={"X-API-Key": key},
        data={"lease_token": str(token)},
        files=[("files", ("a.bin", io.BytesIO(b"x" * 50), "application/octet-stream"))],
    )
    assert r.status_code == 507, r.text
    assert await _count_rows(migrated_db_url, eid) == 0


@pytest.mark.asyncio
async def test_delete_task_decrements_workspace_usage(
    app_client: AsyncClient, authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Deleting a terminal task must decrement the workspace counter."""
    task_id, key, token, eid = await _running_execution(app_client, migrated_db_url)
    content = b"q" * 2048
    up = await app_client.post(
        f"/v1/tasks/{task_id}/artifacts",
        headers={"X-API-Key": key},
        data={"lease_token": str(token)},
        files=[("files", ("a.bin", io.BytesIO(content), "application/octet-stream"))],
    )
    assert up.status_code == 202, up.text
    assert await _workspace_usage(migrated_db_url, DEFAULT_WS) == 2048

    # Move the task to a terminal state so it can be deleted.
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET status='completed' WHERE id=:i"),
                {"i": task_id},
            )
    finally:
        await engine.dispose()

    # The single handler is DELETE /admin/tasks/{id} -> 204.
    d = await authed_admin_client.delete(f"/admin/tasks/{task_id}")
    assert d.status_code == 204, d.text
    assert await _workspace_usage(migrated_db_url, DEFAULT_WS) == 0


@pytest.mark.asyncio
async def test_upload_duplicate_rel_path_in_batch_last_wins(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    """Two parts with the same rel_path in a batch: per-batch staging materializes
    a single .tmp per (execution_id, rel_path); without dedup, the second commit
    blows up (tmp already renamed) -> 500 + orphaned bytes. Mirroring the
    last-write-wins of the rename in the previous flow, the batch must be accepted (202),
    persist ONE row with the bytes of the last part, and count only once."""
    task_id, key, token, eid = await _running_execution(app_client, migrated_db_url)
    first = b"first-content"
    second = b"second-content-wins" * 8
    r = await app_client.post(
        f"/v1/tasks/{task_id}/artifacts",
        headers={"X-API-Key": key},
        data={"lease_token": str(token)},
        files=[
            ("files", ("dup.bin", io.BytesIO(first), "application/octet-stream")),
            ("files", ("dup.bin", io.BytesIO(second), "application/octet-stream")),
        ],
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert len(body["accepted"]) == 1
    a = body["accepted"][0]
    assert a["rel_path"] == "dup.bin"
    assert a["size_bytes"] == len(second)
    assert a["sha256"] == hashlib.sha256(second).hexdigest()
    # A single metadata row and the counter accounts the rel_path once.
    assert await _count_rows(migrated_db_url, eid) == 1
    assert await _workspace_usage(migrated_db_url, DEFAULT_WS) == len(second)


# --- Artifact collection -----------------------------------------------------


async def _mark_collected(db_url: str, execution_id: str, rel_path: str) -> None:
    """Mark the row as collected, like the operational ops routine."""
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    UPDATE task_artifacts
                       SET collected_at = now(), collected_note = 'purged by ops'
                     WHERE execution_id = CAST(:e AS uuid) AND rel_path = :p
                    """
                ),
                {"e": execution_id, "p": rel_path},
            )
    finally:
        await engine.dispose()


async def _collected_row(db_url: str, execution_id: str, rel_path: str):
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            return (
                (
                    await conn.execute(
                        text(
                            "SELECT collected_at, collected_note, size_bytes "
                            "  FROM task_artifacts "
                            " WHERE execution_id = CAST(:e AS uuid) AND rel_path = :p"
                        ),
                        {"e": execution_id, "p": rel_path},
                    )
                )
                .mappings()
                .first()
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reupload_resets_collected(app_client: AsyncClient, migrated_db_url: str) -> None:
    """If the bytes are re-uploaded, the artifact becomes available again.

    Defensive scenario (the upload requires a live lease and collection operates
    on terminal executions, so in production it should not happen): the
    collection mark must NOT survive an upload that restores the bytes,
    because the download would answer 410 with the file present on disk.
    """
    task_id, key, token, eid = await _running_execution(app_client, migrated_db_url)
    content = b"c" * 1024

    async def _post(data: bytes) -> int:
        r = await app_client.post(
            f"/v1/tasks/{task_id}/artifacts",
            headers={"X-API-Key": key},
            data={"lease_token": str(token)},
            files=[("files", ("a.bin", io.BytesIO(data), "application/octet-stream"))],
        )
        return r.status_code

    assert await _post(content) == 202
    assert await _workspace_usage(migrated_db_url, DEFAULT_WS) == 1024

    # The ops routine purges the bytes and marks the row; the quota counter stops
    # counting it (the script does that; here the decrement is emulated).
    from orcaster_server.config import settings as app_settings

    (app_settings.artifacts_root / eid / "a.bin").unlink()
    await _mark_collected(migrated_db_url, eid, "a.bin")
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE workspace_artifact_usage SET total_bytes = 0 WHERE workspace_id = :w"),
                {"w": DEFAULT_WS},
            )
    finally:
        await engine.dispose()

    # Re-upload of the SAME bytes: the row becomes available again and the quota
    # counts it again (the bytes are on disk once more).
    assert await _post(content) == 202
    row = await _collected_row(migrated_db_url, eid, "a.bin")
    assert row is not None
    assert row["collected_at"] is None
    assert row["collected_note"] is None
    assert await _count_rows(migrated_db_url, eid) == 1
    assert await _workspace_usage(migrated_db_url, DEFAULT_WS) == 1024


@pytest.mark.asyncio
async def test_reupload_different_bytes_resets_collected(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    """Variant with different bytes: the UPSERT DO UPDATE clears the mark."""
    task_id, key, token, eid = await _running_execution(app_client, migrated_db_url)

    async def _post(data: bytes) -> int:
        r = await app_client.post(
            f"/v1/tasks/{task_id}/artifacts",
            headers={"X-API-Key": key},
            data={"lease_token": str(token)},
            files=[("files", ("a.bin", io.BytesIO(data), "application/octet-stream"))],
        )
        return r.status_code

    assert await _post(b"v1") == 202
    await _mark_collected(migrated_db_url, eid, "a.bin")
    assert await _post(b"v2-distinto") == 202

    row = await _collected_row(migrated_db_url, eid, "a.bin")
    assert row is not None
    assert row["collected_at"] is None
    assert row["collected_note"] is None
    assert row["size_bytes"] == len(b"v2-distinto")


@pytest.mark.asyncio
async def test_delete_task_does_not_discount_collected_artifacts(
    app_client: AsyncClient, authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Deleting a task with collected artifacts must not make the counter drift.

    A collected row already left the counter when the ops routine purged its
    bytes; discounting it again when deleting the task would eat into the real usage of the
    workspace's other artifacts. TWO tasks are needed to discriminate: with
    just one, the CHECK clamp to 0 would mask the drift.
    """
    task_a, key_a, token_a, eid_a = await _running_execution(app_client, migrated_db_url)
    up_a = await app_client.post(
        f"/v1/tasks/{task_a}/artifacts",
        headers={"X-API-Key": key_a},
        data={"lease_token": str(token_a)},
        files=[("files", ("a.bin", io.BytesIO(b"a" * 2048), "application/octet-stream"))],
    )
    assert up_a.status_code == 202, up_a.text

    task_b, key_b, token_b, _eid_b = await _running_execution(app_client, migrated_db_url)
    up_b = await app_client.post(
        f"/v1/tasks/{task_b}/artifacts",
        headers={"X-API-Key": key_b},
        data={"lease_token": str(token_b)},
        files=[("files", ("b.bin", io.BytesIO(b"b" * 1024), "application/octet-stream"))],
    )
    assert up_b.status_code == 202, up_b.text
    assert await _workspace_usage(migrated_db_url, DEFAULT_WS) == 3072

    # Ops purges the bytes of a.bin, marks the row and discounts its 2048.
    from orcaster_server.config import settings as app_settings

    (app_settings.artifacts_root / eid_a / "a.bin").unlink()
    await _mark_collected(migrated_db_url, eid_a, "a.bin")
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE workspace_artifact_usage SET total_bytes = 1024 "
                    " WHERE workspace_id = :w"
                ),
                {"w": DEFAULT_WS},
            )
            await conn.execute(
                text("UPDATE tasks SET status='completed' WHERE id=:i"),
                {"i": task_a},
            )
    finally:
        await engine.dispose()

    d = await authed_admin_client.delete(f"/admin/tasks/{task_a}")
    assert d.status_code == 204, d.text
    # Only what was counted is discounted: the 1024 of b.bin survive.
    assert await _workspace_usage(migrated_db_url, DEFAULT_WS) == 1024
