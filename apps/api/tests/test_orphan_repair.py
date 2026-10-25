"""`python -m sag_api.maintenance orphans`: engine rows no document owns are counted, then removed."""

from __future__ import annotations

import uuid

import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connect
from sqlalchemy import delete, func, select


async def _seed(config_id: str):
    """One live document (owner ``live``) and the leftovers of a dead attempt (owner ``orphan``)."""
    from alicecore.core.storage.client import get_vector_client
    from alicecore.db import Article, SourceChunk, SourceConfig, SourceEvent, get_session_factory

    from sag_api.core.config import settings
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Source
    from sag_api.enums import DocumentStatus
    from sag_api.sag import EngineManager

    await init_db()
    engine = EngineManager(settings)
    live, orphan = str(uuid.uuid4()), str(uuid.uuid4())
    async with SessionLocal() as session:
        source = Source(name=f"orphan-{config_id[:8]}", sag_source_config_id=config_id)
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id, filename="wiki/a.md#World", content_type="text/markdown", size_bytes=1,
            storage_path="/tmp/a.md", status=DocumentStatus.READY, sag_source_id=live,
            origin_path="wiki/a.md", origin_line=40,
        )
        session.add(document)
        await session.commit()
        source_id, document_id = source.id, document.id
    await engine.provision(config_id, source)

    chunk_ids = {live: [], orphan: []}
    rows = []
    for owner, count in ((live, 1), (orphan, 3)):
        rows.append(Article(id=owner, source_config_id=config_id, title="t", status="COMPLETED"))
        for rank in range(count):
            chunk_id = str(uuid.uuid4())
            chunk_ids[owner].append(chunk_id)
            rows.append(SourceChunk(id=chunk_id, source_config_id=config_id, source_type="ARTICLE", source_id=owner,
                                    article_id=owner, content=f"resident_count kept by {owner[:8]}", rank=rank,
                                    extra_data={"line": 1}))
            rows.append(SourceEvent(id=str(uuid.uuid4()), source_config_id=config_id, source_type="ARTICLE",
                                    type="ARTICLE", source_id=owner, article_id=owner, chunk_id=chunk_id,
                                    title="e", summary="e", content="e"))
    # An event that only names its owner through source_id (no article) must still be counted.
    rows.append(SourceEvent(id=str(uuid.uuid4()), source_config_id=config_id, source_type="ARTICLE", type="ARTICLE",
                            source_id=orphan, article_id=None, title="e", summary="e", content="e"))
    async with get_session_factory()() as engine_session:
        engine_session.add_all(rows)
        await engine_session.commit()
    vectors = get_vector_client()
    await vectors.bulk_index(
        "source_chunks",
        [{"id": chunk_id, "source_config_id": config_id, "content": "x", "vector": [0.1] * 1024}
         for chunk_id in chunk_ids[live] + chunk_ids[orphan]],
    )

    async def cleanup():
        await engine.delete_document_data(config_id, live, source=source)
        async with SessionLocal() as session:
            await session.execute(delete(Source).where(Source.id == source_id))
            await session.commit()
        async with get_session_factory()() as engine_session:
            await engine_session.execute(delete(SourceConfig).where(SourceConfig.id == config_id))
            await engine_session.commit()
        await engine.aclose_all()

    return engine, source_id, document_id, live, orphan, chunk_ids, cleanup


async def _engine_count(model, owner: str) -> int:
    from alicecore.db import Article, SourceEvent, get_session_factory

    column = model.id if model is Article else model.source_id
    if model is SourceEvent:
        column = func.coalesce(SourceEvent.article_id, SourceEvent.source_id)
    async with get_session_factory()() as session:
        return await session.scalar(select(func.count()).select_from(model).where(column == owner))


async def _vector_count(chunk_ids: list[str]) -> int:
    from alicecore.core.storage.client import get_vector_client

    table = await get_vector_client()._open_table("source_chunks")
    values = ", ".join(f"'{chunk_id}'" for chunk_id in chunk_ids)
    return await table.count_rows(f"id IN ({values})")


async def _grep(engine, source_id: str, pattern: str) -> str:
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Source
    from sag_api.mcp.server import build_source_mcp, use_scope

    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
    with use_scope(engine, (source,)):
        async with connect(build_source_mcp()) as client:
            await client.initialize()
            result = await client.call_tool("grep", {"pattern": pattern, "limit": 10})
    return result.content[0].text


@pytest.mark.asyncio
async def test_dry_run_counts_then_apply_removes_only_unowned_rows_and_vectors():
    from alicecore.db import Article, SourceChunk, SourceEvent

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Document
    from sag_api.enums import DocumentStatus
    from sag_api.maintenance import repair_orphans, report

    config_id = str(uuid.uuid4())
    engine, source_id, document_id, live, orphan, chunk_ids, cleanup = await _seed(config_id)
    try:
        before = await _grep(engine, source_id, "resident_count")
        assert before.count("chunk_id=") == 4 and before.count("Cite:") == 1  # orphans answer without a citation

        dry = await repair_orphans(source_id, apply=False, engine_manager=engine)
        assert (dry.owners, dry.articles, dry.chunks, dry.events) == ((orphan,), 1, 3, 4)
        assert f"owner {orphan}" in report(dry)
        assert await _engine_count(SourceChunk, orphan) == 3

        with pytest.raises(SystemExit, match="expected 2"):
            await repair_orphans(source_id, apply=True, engine_manager=engine, expect_owners=2)
        async with SessionLocal() as session:
            document = await session.get(Document, document_id)
            document.status = DocumentStatus.LOADING
            await session.commit()
            with pytest.raises(SystemExit, match="still processing"):
                await repair_orphans(source_id, apply=True, engine_manager=engine, expect_owners=1)
            document.status = DocumentStatus.READY
            await session.commit()

        applied = await repair_orphans(source_id, apply=True, engine_manager=engine, expect_owners=1)
        assert applied.applied and applied.skipped == []
        assert [await _engine_count(model, orphan) for model in (Article, SourceChunk, SourceEvent)] == [0, 0, 0]
        assert [await _engine_count(model, live) for model in (Article, SourceChunk, SourceEvent)] == [1, 1, 1]
        assert await _vector_count(chunk_ids[orphan]) == 0
        assert await _vector_count(chunk_ids[live]) == 1

        after = await _grep(engine, source_id, "resident_count")
        assert after.count("chunk_id=") == 1 and "Cite: wiki/a.md:40" in after
        assert (await repair_orphans(source_id, apply=False, engine_manager=engine)).owners == ()
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_a_vector_store_failure_deletes_nothing_and_a_rerun_finishes(monkeypatch):
    from alicecore.db import Article, SourceChunk, SourceEvent

    from sag_api import maintenance
    from sag_api.sag import document_cleanup

    config_id = str(uuid.uuid4())
    engine, source_id, _document_id, _live, orphan, chunk_ids, cleanup = await _seed(config_id)
    real_delete = document_cleanup._delete_vector_ids

    async def broken(client, index, ids):
        if index == "event_vectors":
            raise RuntimeError("vector store down")
        await real_delete(client, index, ids)

    try:
        monkeypatch.setattr(document_cleanup, "_delete_vector_ids", broken)
        with pytest.raises(Exception, match="vector store down"):
            await maintenance.repair_orphans(source_id, apply=True, engine_manager=engine, expect_owners=1)
        # Nothing relational was deleted, so the next run still sees every id.
        assert [await _engine_count(model, orphan) for model in (Article, SourceChunk, SourceEvent)] == [1, 3, 4]
        assert (await maintenance.repair_orphans(source_id, apply=False, engine_manager=engine)).owners == (orphan,)

        monkeypatch.setattr(document_cleanup, "_delete_vector_ids", real_delete)
        await maintenance.repair_orphans(source_id, apply=True, engine_manager=engine, expect_owners=1)
        assert [await _engine_count(model, orphan) for model in (Article, SourceChunk, SourceEvent)] == [0, 0, 0]
        assert await _vector_count(chunk_ids[orphan]) == 0
    finally:
        await cleanup()


def test_the_command_runs_the_real_repair_and_exits_1_when_vectors_fail(monkeypatch):
    """`main` end to end: a vector store failure is an uncaught error, a rerun then finishes the job."""
    import asyncio

    from alicecore.db import SourceChunk

    from sag_api import maintenance
    from sag_api.sag import document_cleanup

    config_id = str(uuid.uuid4())
    _engine, source_id, _document_id, _live, orphan, _chunks, cleanup = asyncio.run(_seed(config_id))
    real_delete = document_cleanup._delete_vector_ids

    async def broken(client, index, ids):
        raise RuntimeError("vector store down")

    try:
        monkeypatch.setattr(document_cleanup, "_delete_vector_ids", broken)
        with pytest.raises(Exception, match="vector store down"):  # uncaught -> exit status 1
            maintenance.main(["orphans", "--source-id", source_id, "--apply", "--expect-owners", "1"])
        assert asyncio.run(_engine_count(SourceChunk, orphan)) == 3
        monkeypatch.setattr(document_cleanup, "_delete_vector_ids", real_delete)
        assert maintenance.main(["orphans", "--source-id", source_id, "--apply", "--expect-owners", "1"]) == 0
        assert asyncio.run(_engine_count(SourceChunk, orphan)) == 0
    finally:
        asyncio.run(cleanup())


@pytest.mark.asyncio
async def test_repair_never_removes_a_live_article_bound_to_an_orphan_id():
    from alicecore.db import Article, get_session_factory

    from sag_api.maintenance import repair_orphans

    config_id = str(uuid.uuid4())
    engine, source_id, _document_id, live, orphan, _chunks, cleanup = await _seed(config_id)
    try:
        async with get_session_factory()() as session:
            article = await session.get(Article, live)
            article.source_id = orphan  # an external binding that happens to equal the orphan's id
            await session.commit()
        await repair_orphans(source_id, apply=True, engine_manager=engine, expect_owners=1)
        assert await _engine_count(Article, live) == 1
        assert await _engine_count(Article, orphan) == 0
    finally:
        await cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["delete", "reprocess"])
async def test_document_delete_and_reprocess_keep_everything_when_vectors_fail(monkeypatch, action):
    from alicecore.db import Article, SourceChunk, SourceEvent

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Document, Source
    from sag_api.enums import DocumentStatus
    from sag_api.sag import document_cleanup
    from sag_api.services.document_service import delete_document, reprocess_document

    class FakeQueue:
        async def enqueue(self, _job_id):
            raise AssertionError("nothing may be queued when the cleanup failed")

    async def broken(client, index, ids):
        raise RuntimeError("vector store down")

    config_id = str(uuid.uuid4())
    engine, source_id, document_id, live, _orphan, chunk_ids, cleanup = await _seed(config_id)
    try:
        monkeypatch.setattr(document_cleanup, "_delete_vector_ids", broken)
        async with SessionLocal() as session:
            source = await session.get(Source, source_id)
            with pytest.raises(Exception, match="vector store down"):
                if action == "delete":
                    await delete_document(session, source, document_id, engine_manager=engine, job_queue=FakeQueue())
                else:
                    await reprocess_document(session, source, document_id, job_queue=FakeQueue(), engine_manager=engine)
        async with SessionLocal() as session:
            document = await session.get(Document, document_id)
            assert document is not None and document.status == DocumentStatus.READY
            assert document.sag_source_id == live
        assert [await _engine_count(model, live) for model in (Article, SourceChunk, SourceEvent)] == [1, 1, 1]
        assert await _vector_count(chunk_ids[live]) == 1
    finally:
        monkeypatch.undo()
        await cleanup()
