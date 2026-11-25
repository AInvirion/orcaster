"""Setting for the request size limit of the artifact batch."""

from orcaster_worker.config import Settings


def test_artifact_request_max_bytes_default() -> None:
    s = Settings()
    assert s.artifact_request_max_bytes == 32 * 1024 * 1024


def test_artifact_request_max_bytes_env(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_ARTIFACT_REQUEST_MAX_BYTES", "1024")
    s = Settings()
    assert s.artifact_request_max_bytes == 1024
