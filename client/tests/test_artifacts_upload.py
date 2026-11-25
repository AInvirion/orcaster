"""api.upload_artifacts: multipart, translation of 409/413."""

from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from orcaster_worker.api import (
    ArtifactsTooLargeError,
    LeaseLostError,
    OrchestratorAPI,
)
from orcaster_worker.artifacts import ArtifactsSpec
from orcaster_worker.main import _finalize_status, _upload_artifacts


def _api(transport=None) -> OrchestratorAPI:
    api = OrchestratorAPI("http://srv", transport=transport)
    api.set_credentials("11111111-1111-1111-1111-111111111111", "k")
    return api


@pytest.mark.asyncio
async def test_upload_artifacts_202(tmp_path) -> None:
    p = tmp_path / "r.json"
    p.write_bytes(b"data")
    async with _api() as api:
        with respx.mock(base_url="http://srv") as mock:
            route = mock.post("/v1/tasks/7/artifacts").mock(
                return_value=httpx.Response(202, json={"accepted": []})
            )
            await api.upload_artifacts(
                7,
                lease_token="tok",
                files=[("r.json", p, "application/json")],
            )
            assert route.called
            req = route.calls.last.request
            assert b'name="lease_token"' in req.content
            assert b"tok" in req.content
            assert b'filename="r.json"' in req.content


@pytest.mark.asyncio
async def test_upload_artifacts_409_lease_lost(tmp_path) -> None:
    p = tmp_path / "r.json"
    p.write_bytes(b"x")
    async with _api() as api:
        with respx.mock(base_url="http://srv") as mock:
            mock.post("/v1/tasks/7/artifacts").mock(
                return_value=httpx.Response(409, json={"detail": "gone"})
            )
            with pytest.raises(LeaseLostError):
                await api.upload_artifacts(
                    7, lease_token="tok", files=[("r.json", p, "application/json")]
                )


@pytest.mark.asyncio
async def test_upload_artifacts_413_too_large(tmp_path) -> None:
    p = tmp_path / "r.json"
    p.write_bytes(b"x")
    async with _api() as api:
        with respx.mock(base_url="http://srv") as mock:
            mock.post("/v1/tasks/7/artifacts").mock(
                return_value=httpx.Response(413, json={"detail": "too big"})
            )
            with pytest.raises(ArtifactsTooLargeError):
                await api.upload_artifacts(
                    7, lease_token="tok", files=[("r.json", p, "application/json")]
                )


def _spec(required: bool = False) -> ArtifactsSpec:
    return ArtifactsSpec(enabled=True, include=["**/*"], exclude=[], required=required)


def _seed(d: Path) -> None:
    (d / "a.json").write_bytes(b"AA")
    (d / "sub").mkdir()
    (d / "sub" / "b.json").write_bytes(b"BBB")


@pytest.mark.asyncio
async def test_upload_artifacts_orchestrator_uploads_all(tmp_path) -> None:
    art_dir = tmp_path / "artifacts"
    art_dir.mkdir()
    _seed(art_dir)
    api = AsyncMock()
    ok = await _upload_artifacts(
        api,
        task_id=7,
        lease_token="tok",
        artifacts_dir=art_dir,
        spec=_spec(),
        max_request_bytes=1024,
    )
    assert ok is True
    uploaded = [f[0] for call in api.upload_artifacts.await_args_list for f in call.kwargs["files"]]
    assert set(uploaded) == {"a.json", "sub/b.json"}


@pytest.mark.asyncio
async def test_upload_artifacts_empty_dir_noop(tmp_path) -> None:
    art_dir = tmp_path / "artifacts"
    art_dir.mkdir()
    api = AsyncMock()
    ok = await _upload_artifacts(
        api,
        task_id=7,
        lease_token="tok",
        artifacts_dir=art_dir,
        spec=_spec(),
        max_request_bytes=1024,
    )
    assert ok is True
    api.upload_artifacts.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_required_fails_on_persistent_413(tmp_path) -> None:
    art_dir = tmp_path / "artifacts"
    art_dir.mkdir()
    _seed(art_dir)
    api = AsyncMock()
    api.upload_artifacts.side_effect = ArtifactsTooLargeError("nope")
    ok = await _upload_artifacts(
        api,
        task_id=7,
        lease_token="tok",
        artifacts_dir=art_dir,
        spec=_spec(required=True),
        max_request_bytes=1024,
    )
    assert ok is False


@pytest.mark.asyncio
async def test_upload_best_effort_swallows_413(tmp_path) -> None:
    art_dir = tmp_path / "artifacts"
    art_dir.mkdir()
    _seed(art_dir)
    api = AsyncMock()
    api.upload_artifacts.side_effect = ArtifactsTooLargeError("nope")
    ok = await _upload_artifacts(
        api,
        task_id=7,
        lease_token="tok",
        artifacts_dir=art_dir,
        spec=_spec(required=False),
        max_request_bytes=1024,
    )
    assert ok is True


@pytest.mark.asyncio
async def test_upload_lease_lost_stops_and_returns_true(tmp_path) -> None:
    art_dir = tmp_path / "artifacts"
    art_dir.mkdir()
    _seed(art_dir)
    api = AsyncMock()
    api.upload_artifacts.side_effect = LeaseLostError("gone")
    ok = await _upload_artifacts(
        api,
        task_id=7,
        lease_token="tok",
        artifacts_dir=art_dir,
        spec=_spec(required=True),
        max_request_bytes=1024,
    )
    assert ok is True


def test_finalize_status_required_failure_becomes_failed() -> None:
    assert _finalize_status("completed", artifacts_ok=False, required=True) == "failed"


def test_finalize_status_required_success_stays() -> None:
    assert _finalize_status("completed", artifacts_ok=True, required=True) == "completed"


def test_finalize_status_best_effort_never_downgrades() -> None:
    assert _finalize_status("completed", artifacts_ok=False, required=False) == "completed"


def test_finalize_status_failed_stays_failed() -> None:
    assert _finalize_status("failed", artifacts_ok=True, required=True) == "failed"
