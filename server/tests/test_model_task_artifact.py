"""The TaskArtifact model maps the task_artifacts table."""

from orcaster_server.models import TaskArtifact


def test_task_artifact_tablename_and_columns() -> None:
    assert TaskArtifact.__tablename__ == "task_artifacts"
    cols = set(TaskArtifact.__table__.columns.keys())
    assert cols == {
        "id",
        "execution_id",
        "rel_path",
        "size_bytes",
        "sha256",
        "content_type",
        "created_at",
        # Migration 0052: collection evidence.
        "collected_at",
        "collected_note",
    }
