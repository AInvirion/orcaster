"""Migration 0035 creates task_artifacts with the expected shape."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_task_artifacts_table_exists(migrated_db_url: str) -> None:
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            cols = (
                (
                    await conn.execute(
                        text(
                            """
                        SELECT column_name, data_type, is_nullable
                          FROM information_schema.columns
                         WHERE table_name = 'task_artifacts'
                         ORDER BY ordinal_position
                        """
                        )
                    )
                )
                .mappings()
                .all()
            )
        by_name = {c["column_name"]: c for c in cols}
        assert set(by_name) == {
            "id",
            "execution_id",
            "rel_path",
            "size_bytes",
            "sha256",
            "content_type",
            "created_at",
            # Migration 0052: collection evidence. Asserted here
            # because this test runs against head, not against 0035.
            "collected_at",
            "collected_note",
        }
        assert by_name["execution_id"]["data_type"] == "uuid"
        assert by_name["execution_id"]["is_nullable"] == "NO"
        assert by_name["rel_path"]["data_type"] == "text"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_task_artifacts_unique_execution_relpath(migrated_db_url: str) -> None:
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            """
                        SELECT indexdef FROM pg_indexes
                         WHERE tablename = 'task_artifacts'
                        """
                        )
                    )
                )
                .scalars()
                .all()
            )
        joined = " ".join(rows)
        assert "UNIQUE" in joined
        assert "execution_id" in joined and "rel_path" in joined
    finally:
        await engine.dispose()
