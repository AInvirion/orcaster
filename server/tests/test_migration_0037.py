"""Migration 0037 creates the supporting indexes for the artifact filter."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def _indexdefs(db_url: str, table: str) -> str:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text("SELECT indexdef FROM pg_indexes WHERE tablename = :t"),
                        {"t": table},
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()
    return " ".join(rows)


@pytest.mark.asyncio
async def test_artifact_filter_indexes_exist(migrated_db_url: str) -> None:
    arts = await _indexdefs(migrated_db_url, "task_artifacts")
    # GIN trigram on rel_path (ILIKE with a wildcard in any position).
    assert "gin" in arts.lower() and "rel_path" in arts and "trgm" in arts.lower()
    # btree on content_type (exact equality).
    assert "task_artifacts_content_type_idx" in arts
    # btree on sha256 (equality + prefix).
    assert "task_artifacts_sha256_idx" in arts


@pytest.mark.asyncio
async def test_pg_trgm_extension_present(migrated_db_url: str) -> None:
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            present = (
                await conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'"))
            ).scalar_one_or_none()
    finally:
        await engine.dispose()
    assert present == 1
