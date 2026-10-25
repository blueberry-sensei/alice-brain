"""Lexical grep ranks chunks by how many query terms they hold; chunk rank alone orders nothing."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete


@pytest.mark.asyncio
async def test_lexical_grep_prefers_chunks_holding_more_query_terms():
    from alicecore.db import SourceChunk, SourceConfig, get_session_factory

    from sag_api.core.config import settings
    from sag_api.core.db import init_db
    from sag_api.sag import EngineManager

    await init_db()
    engine = EngineManager(settings)
    config_id = str(uuid.uuid4())
    await engine.provision(config_id)
    ids = [f"{config_id[:8]}-{index}" for index in range(4)]
    contents = ["resident_count only", "resident_count and capacity", "resident_count, capacity", "nothing"]
    async with get_session_factory()() as session:
        for chunk_id, content in zip(ids, contents, strict=True):
            session.add(SourceChunk(id=chunk_id, source_config_id=config_id, source_type="ARTICLE",
                                    source_id=chunk_id, content=content, rank=0))
        await session.commit()
    try:
        rows = await engine.grep_chunks(config_id, "resident_count", limit=2, also=("capacity", "admission"))
        assert [row["chunk_id"] for row in rows] == [ids[1], ids[2]]  # equal matches tie-break on id
        assert len(await engine.grep_chunks(config_id, "resident_count", limit=5)) == 3
    finally:
        async with get_session_factory()() as session:
            await session.execute(delete(SourceChunk).where(SourceChunk.source_config_id == config_id))
            await session.execute(delete(SourceConfig).where(SourceConfig.id == config_id))
            await session.commit()
        await engine.aclose_all()
