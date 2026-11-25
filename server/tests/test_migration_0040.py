"""Migration 0040 extends the output_events.stream CHECK to 'command'."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_output_events_stream_check_allows_command(migrated_db_url: str) -> None:
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
                            "WHERE conname = 'output_events_stream_check'"
                        )
                    )
                )
                .mappings()
                .first()
            )
        assert row is not None, "the output_events_stream_check constraint does not exist"
        assert "command" in row["def"]
        assert "stdout" in row["def"] and "stderr" in row["def"]
    finally:
        await engine.dispose()
