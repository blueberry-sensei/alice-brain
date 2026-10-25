"""Documents whose events the client extracted: stored, chunked and embedded without any LLM call."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

EVENTS = [
    {
        "title": "D-0002 (ACTIVE): GH stores World config in its DB",
        "content": "D-0002 (ACTIVE): GH stores World config in its DB",
        "line": 1,
        "entities": [{"type": "work", "name": "D-0002"}],
    },
    {
        "title": "GH reads the world endpoint from the `worlds` table",
        "content": "D-0002 › Luật: GH reads the world endpoint from the `worlds` table",
        "line": 4,
        "entities": [{"type": "technology", "name": "worlds"}],
    },
]


def test_extracted_request_rejects_lines_past_the_text_and_blank_entities():
    from sag_api.schemas.document import ExtractedIngestRequest

    base = {"title": "decisions/LOG.md#D-0002", "text": "a\nb\n", "origin_path": "decisions/LOG.md", "origin_line": 10}
    with pytest.raises(ValidationError, match="past the end"):
        ExtractedIngestRequest(**base, events=[{"title": "t", "content": "c", "line": 3}])
    with pytest.raises(ValidationError):
        ExtractedIngestRequest(
            **base,
            events=[{"title": "t", "content": "c", "line": 1, "entities": [{"type": "work", "name": "  x "}]}],
        )
    with pytest.raises(ValidationError):
        ExtractedIngestRequest(**base, events=[])


def test_chunk_start_lines_follow_chunks_that_split_mid_line():
    from sag_api.sag.incremental_processor import chunk_start_lines

    text = "## D-0001 · title\n\n- **Luật:** first sentence. Second sentence\n- **Vì sao:** why\n"
    bodies = ["## D-0001 · title\n\n- **Luật:** first sentence.", "Second sentence\n- **Vì sao:** why"]
    assert chunk_start_lines(bodies, text) == [1, 3]
    with pytest.raises(RuntimeError, match="does not start"):
        chunk_start_lines(["text that is not there"], text)


@pytest.mark.asyncio
async def test_extracted_processor_puts_each_event_on_the_chunk_holding_its_line(monkeypatch, tmp_path):
    from sag_api.sag import incremental_processor as module
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    path = tmp_path / "entry.md"
    path.write_text("x\n" * 8, encoding="utf-8")

    class FakeLoader:
        async def load(self, config):
            assert config.path == str(path) and config.article_id == "article-1"
            return SimpleNamespace(source_id="article-1", chunk_ids=["c2", "c1"])

    saved: dict = {}

    class FakeExtractor:
        def __init__(self, **_kwargs):
            pass

        async def save_events(self, config, items):
            saved.update(items)
            return [SimpleNamespace(id=f"e{index}") for index, _ in enumerate(sum(items.values(), []))]

    async def record(chunk_ids, text):
        assert set(chunk_ids) == {"c1", "c2"} and text == path.read_text(encoding="utf-8")
        return ["c1", "c2"], [1, 5]

    monkeypatch.setattr(module, "DocumentLoader", FakeLoader)
    monkeypatch.setattr(module, "EventExtractor", FakeExtractor)
    engine = SimpleNamespace(_extractor=SimpleNamespace(prompt_manager=None, model_config=None))
    processor = IncrementalDocumentProcessor(engine, "source-config", max_concurrency=1)
    monkeypatch.setattr(processor, "_record_chunk_lines", record)

    events = [dict(EVENTS[0], line=1), dict(EVENTS[1], line=4), dict(EVENTS[1], line=5), dict(EVENTS[1], line=8)]
    outcome = await processor.process_extracted(path, events, article_id="article-1")

    assert [event["line"] for event in saved["c1"]] == [1, 4]
    assert [event["line"] for event in saved["c2"]] == [5, 8]
    assert outcome.chunk_count == 2 and outcome.event_count == 4
    assert outcome.source_id == "article-1" and outcome.token_usage == 0


@pytest.mark.asyncio
async def test_process_document_with_client_events_never_extracts(monkeypatch):
    from sag_api.db.models import Document, Source
    from sag_api.enums import DocumentStatus
    from sag_api.jobs import tasks
    from sag_api.sag.dto import ProcessOutcome

    document = SimpleNamespace(
        id="doc-1",
        source_id="source-1",
        filename="decisions/LOG.md#D-0002",
        storage_path="/uploads/doc-1_entry.md",
        status=DocumentStatus.PENDING,
        error=None,
        chunk_count=0,
        event_count=0,
        progress=0,
        token_usage=0,
        sag_source_id=None,
    )
    source = SimpleNamespace(id="source-1", sag_source_config_id="sag-1", chunk_count=0, event_count=0)
    job = SimpleNamespace(id="job-1", document_id="doc-1", progress=0.0, payload={"extracted_events": EVENTS})

    class FakeSession:
        async def get(self, model, _id):
            return document if model is Document else source if model is Source else None

        async def commit(self):
            pass

        async def execute(self, _statement):
            pass

        async def refresh(self, _instance, attribute_names=None):
            pass

    async def no_prepare(*_args, **_kwargs):
        raise AssertionError("client events must not go through document conversion")

    class FakeEngineManager:
        seen: tuple = ()
        deleted: list = []

        async def process_document(self, *_args, **_kwargs):
            raise AssertionError("client events must never reach LLM extraction")

        async def delete_document_data(self, source_config_id, document_source_id, *, source):
            self.deleted.append(document_source_id)

        async def process_extracted_document(self, source_config_id, path, events, *, article_id, source):
            # The article id is on the document before anything is written under it.
            assert document.sag_source_id == article_id
            self.seen = (source_config_id, path, events)
            return ProcessOutcome(source_id=article_id, chunk_count=1, event_count=len(events))

    monkeypatch.setattr(tasks, "prepare_document", no_prepare)
    engine = FakeEngineManager()
    await tasks.process_document(FakeSession(), job, engine_manager=engine)

    assert engine.seen == ("sag-1", "/uploads/doc-1_entry.md", EVENTS)
    assert engine.deleted == []
    assert document.status == DocumentStatus.READY
    assert document.event_count == 2 and document.token_usage == 0
    first_article = document.sag_source_id

    # A requeued job carries the checkpoint of the attempt before it: that attempt is cleaned first.
    await tasks.process_document(FakeSession(), job, engine_manager=engine)
    assert engine.deleted == [first_article]
    assert document.sag_source_id != first_article


@pytest.mark.asyncio
async def test_reprocess_keeps_client_events_instead_of_falling_back_to_llm():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import reprocess_document

    class FakeQueue:
        async def enqueue(self, _job_id: str):
            pass

    class FakeEngineManager:
        async def delete_document_data(self, *_args, **_kwargs):
            pass

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="extracted-reprocess", sag_source_config_id="extracted-reprocess-config")
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="decisions/LOG.md#D-0002",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/entry.md",
            status=DocumentStatus.READY,
            sag_source_id="article-1",
            origin_path="decisions/LOG.md",
            origin_line=40,
        )
        session.add(document)
        await session.flush()
        session.add(
            Job(
                type=JobType.PROCESS_DOCUMENT,
                status=JobStatus.SUCCEEDED,
                source_id=source.id,
                document_id=document.id,
                payload={"extracted_events": EVENTS},
            )
        )
        await session.commit()

        job = await reprocess_document(
            session, source, document.id, job_queue=FakeQueue(), engine_manager=FakeEngineManager()
        )
        assert job.payload == {"extracted_events": EVENTS}

        await session.delete(source)
        await session.commit()


@pytest.mark.asyncio
async def test_ingest_extracted_endpoint_records_origin_and_moves_it():
    from sag_api.core.deps import get_job_queue
    from sag_api.main import app

    class FakeQueue:
        async def enqueue(self, _job_id: str):
            pass

    app.dependency_overrides[get_job_queue] = lambda: FakeQueue()
    try:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                registered = await client.post(
                    "/api/v1/auth/register", json={"email": "extracted@t.com", "password": "password123"}
                )
                headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
                source = (await client.post("/api/v1/sources", headers=headers, json={"name": "extracted"})).json()
                body = {
                    "title": "decisions/LOG.md#D-0002",
                    "text": "## D-0002 · title\n\n- **Trạng thái:** ACTIVE\n- **Luật:** GH reads `worlds`\n",
                    "origin_path": "decisions/LOG.md",
                    "origin_line": 40,
                    "events": EVENTS,
                }
                created = await client.post(
                    f"/api/v1/sources/{source['id']}/documents/ingest-extracted", headers=headers, json=body
                )
                assert created.status_code == 201, created.text
                document = created.json()
                assert document["filename"] == "decisions/LOG.md#D-0002"
                assert (document["origin_path"], document["origin_line"]) == ("decisions/LOG.md", 40)

                moved = await client.post(
                    f"/api/v1/sources/{source['id']}/documents/origins",
                    headers=headers,
                    json={"items": [{"document_id": document["id"], "origin_line": 52}]},
                )
                assert moved.status_code == 200, moved.text
                current = await client.get(
                    f"/api/v1/sources/{source['id']}/documents/{document['id']}", headers=headers
                )
                assert current.json()["origin_line"] == 52

                plain = await client.post(
                    f"/api/v1/sources/{source['id']}/documents/ingest",
                    headers=headers,
                    json={"title": "note", "text": "hello"},
                )
                refused = await client.post(
                    f"/api/v1/sources/{source['id']}/documents/origins",
                    headers=headers,
                    json={"items": [{"document_id": plain.json()["id"], "origin_line": 3}]},
                )
                assert refused.status_code == 409, refused.text
    finally:
        app.dependency_overrides.pop(get_job_queue, None)


def test_citations_use_the_origin_file_for_synced_knowledge():
    from sag_api.mcp.server import _cite

    synced = SimpleNamespace(id="d1", origin_path="decisions/LOG.md", origin_line=40, filename="decisions/LOG.md#D-0002")
    upload = SimpleNamespace(id="d2", origin_path=None, origin_line=None, filename="book.md")
    assert _cite(synced, 4) == "decisions/LOG.md:43 · id=d1"
    assert _cite(upload, 7) == "book.md:7 · id=d2"
    assert _cite(upload, None) == "book.md · id=d2"
    assert _cite(None, 3) == ""


def test_grep_citation_points_at_the_matching_line(tmp_path):
    from sag_api.mcp.server import _first_line_with, _stored_lines

    stored = tmp_path / "entry.md"
    stored.write_text("## M-0001 · title\n\n- **Lỗi gì:** first\n- **Root cause:** `wait_ready()` ran\n", encoding="utf-8")
    lines = _stored_lines(SimpleNamespace(storage_path=str(stored)))
    assert _first_line_with(lines, 1, "WAIT_READY") == 4
    assert _first_line_with(lines, 1, "not\nthere") == 1
    assert _stored_lines(SimpleNamespace(storage_path=str(tmp_path / "gone.md"))) == []


@pytest.mark.asyncio
async def test_failed_extracted_attempt_is_cleaned_before_the_retry(monkeypatch, tmp_path):
    """Real engine + DB: a load that succeeded but whose events failed must leave nothing behind."""
    import uuid

    from sqlalchemy import func, select
    from alicecore.core.ai import factory
    from alicecore.db import Article, SourceChunk, SourceEvent, get_session_factory
    from alicecore.modules.extract import saver as saver_module

    from sag_api.core.config import settings
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs import tasks
    from sag_api.sag import EngineManager

    failing_title = "Retry probe: this event title fails to embed on the first attempt"

    class FakeEmbedding:
        fail = True
        calls = 0

        async def batch_generate(self, texts):
            self.calls += 1
            if self.fail and failing_title in texts:
                raise RuntimeError("embedding endpoint down")
            return [[float((len(text) + index) % 7 + 1)] + [0.0] * 1023 for index, text in enumerate(texts)]

        async def generate(self, text):
            return (await self.batch_generate([text]))[0]

    embedding = FakeEmbedding()

    async def get_embedding_client(scenario="general"):
        return embedding

    monkeypatch.setattr(factory, "get_embedding_client", get_embedding_client)
    monkeypatch.setattr(saver_module, "get_embedding_client", get_embedding_client)

    text = "## D-0009 · [2026-10-25] Retry probe · #testing\n\n- **Trạng thái:** ACTIVE\n- **Luật:** " + failing_title + "\n"
    stored = tmp_path / "entry.md"
    stored.write_text(text, encoding="utf-8")
    events = [
        {"title": "D-0009 retry probe heading", "content": "D-0009 retry probe heading", "line": 1,
         "entities": [{"type": "work", "name": "D-0009"}]},
        {"title": failing_title, "content": "D-0009 › Luật › " + failing_title, "line": 4,
         "entities": [{"type": "work", "name": "D-0009"}, {"type": "concept", "name": "#testing"}]},
    ]

    await init_db()
    engine = EngineManager(settings)
    config_id = str(uuid.uuid4())
    async with SessionLocal() as session:
        source = Source(name="extracted-retry", sag_source_config_id=config_id)
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="decisions/LOG.md#D-0009",
            content_type="text/markdown",
            size_bytes=len(text),
            storage_path=str(stored),
            status=DocumentStatus.PENDING,
            origin_path="decisions/LOG.md",
            origin_line=10,
        )
        session.add(document)
        await session.flush()
        job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.RUNNING,
            source_id=source.id,
            document_id=document.id,
            payload={"extracted_events": events},
        )
        session.add(job)
        await session.commit()

        try:
            with pytest.raises(Exception, match="embedding|vector|Saving extracted events"):
                await tasks.process_document(session, job, engine_manager=engine)
            first_article = document.sag_source_id
            assert document.status == DocumentStatus.FAILED and first_article

            async with get_session_factory()() as engine_session:
                left = await engine_session.scalar(
                    select(func.count()).select_from(SourceChunk).where(SourceChunk.source_id == first_article)
                )
            assert left >= 1  # the failed attempt really wrote rows under the recorded id

            embedding.fail = False  # the job is requeued (worker restart / retry) with its payload
            await tasks.process_document(session, job, engine_manager=engine)

            assert document.status == DocumentStatus.READY
            assert document.sag_source_id != first_article
            async with get_session_factory()() as engine_session:
                articles = (
                    await engine_session.scalars(select(Article.id).where(Article.source_config_id == config_id))
                ).all()
                stale_chunks = await engine_session.scalar(
                    select(func.count()).select_from(SourceChunk).where(SourceChunk.source_id == first_article)
                )
                live_events = (
                    await engine_session.scalars(
                        select(SourceEvent).where(
                            SourceEvent.source_config_id == config_id, SourceEvent.not_deleted()
                        )
                    )
                ).all()
                chunk_lines = (
                    await engine_session.scalars(
                        select(SourceChunk.extra_data).where(SourceChunk.source_id == document.sag_source_id)
                    )
                ).all()
            assert articles == [document.sag_source_id]  # no orphan, no duplicate
            assert stale_chunks == 0
            assert sorted(event.extra_data["line"] for event in live_events) == [1, 4]
            assert [extra["line"] for extra in chunk_lines] == [1]
        finally:
            await engine.delete_document_data(config_id, document.sag_source_id or "", source=source)
            await session.delete(source)
            await session.commit()
            await engine.aclose_all()


@pytest.mark.asyncio
async def test_memo_embedding_asks_once_per_text_and_forgets_after_a_failed_commit(monkeypatch):
    from alicecore.modules.extract import saver as saver_module
    from alicecore.modules.extract.saver import EventSaver, _MemoEmbedding

    asked: list[list[str]] = []

    class Client:
        async def batch_generate(self, texts):
            asked.append(list(texts))
            return [[float(len(text))] for text in texts]

    memo = _MemoEmbedding(Client())
    assert await memo.batch_generate(["a", "bb", "a"]) == [[1.0], [2.0], [1.0]]
    assert await memo.generate("bb") == [2.0]
    assert asked == [["a", "bb"]]

    async def get_embedding_client(scenario="general"):
        return Client()

    monkeypatch.setattr(saver_module, "get_embedding_client", get_embedding_client)
    saver = EventSaver()

    async def broken_save(_events):
        await saver._embedding_client()
        raise RuntimeError("database down")

    monkeypatch.setattr(saver, "_save_to_database", broken_save)
    with pytest.raises(RuntimeError, match="database down"):
        await saver.commit([SimpleNamespace(id="e1")], SimpleNamespace(enable_event_vector_sync=False))
    assert saver._memo is None


@pytest.mark.asyncio
async def test_ingest_extracted_rejects_unknown_entity_types_before_storing():
    from sag_api.core.deps import get_job_queue
    from sag_api.main import app

    class FakeQueue:
        async def enqueue(self, _job_id: str):
            raise AssertionError("nothing may be queued for a rejected request")

    app.dependency_overrides[get_job_queue] = lambda: FakeQueue()
    try:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                registered = await client.post(
                    "/api/v1/auth/register", json={"email": "extracted-types@t.com", "password": "password123"}
                )
                headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
                source = (await client.post("/api/v1/sources", headers=headers, json={"name": "types"})).json()
                bad = [dict(EVENTS[0], entities=[{"type": "spaceship", "name": "D-0002"}])]
                refused = await client.post(
                    f"/api/v1/sources/{source['id']}/documents/ingest-extracted",
                    headers=headers,
                    json={"title": "x#D-0002", "text": "## D-0002\n", "origin_path": "x", "origin_line": 1, "events": bad},
                )
                assert refused.status_code == 422, refused.text
                assert "spaceship" in refused.text
                listed = await client.get(f"/api/v1/sources/{source['id']}/documents", headers=headers)
                assert listed.json() == []
    finally:
        app.dependency_overrides.pop(get_job_queue, None)



@pytest.mark.asyncio
async def test_llm_path_records_the_article_id_before_loading_and_cleans_a_dead_attempt(monkeypatch):
    from sag_api.db.models import Document, Source
    from sag_api.enums import DocumentStatus
    from sag_api.jobs import tasks
    from sag_api.parsing.service import PreparedDocument
    from sag_api.sag.dto import ProcessCheckpoint, ProcessOutcome

    document = SimpleNamespace(
        id="doc-2", source_id="source-1", filename="upload.md", storage_path="/uploads/upload.md",
        status=DocumentStatus.PENDING, error=None, chunk_count=0, event_count=0, progress=0,
        token_usage=0, sag_source_id=None,
    )
    source = SimpleNamespace(id="source-1", sag_source_config_id="sag-1", chunk_count=0, event_count=0)
    # The previous attempt recorded its id, then died before chunking finished.
    job = SimpleNamespace(id="job-2", document_id="doc-2", progress=0.0,
                          payload={"process_checkpoint": ProcessCheckpoint(source_id="dead-article").model_dump()})

    class FakeSession:
        async def get(self, model, _id):
            return document if model is Document else source if model is Source else None

        async def commit(self):
            pass

        async def execute(self, _statement):
            pass

        async def refresh(self, _instance, attribute_names=None):
            pass

    async def prepare(path, settings, *, state=None, on_state=None):
        return PreparedDocument(path, "original")

    class FakeEngineManager:
        deleted: list = []

        async def delete_document_data(self, source_config_id, document_source_id, *, source):
            self.deleted.append(document_source_id)

        async def process_document(self, source_config_id, path, *, article_id, checkpoint, **_kwargs):
            assert document.sag_source_id == article_id == checkpoint.source_id != "dead-article"
            return ProcessOutcome(source_id=article_id, chunk_count=1, event_count=0)

    monkeypatch.setattr(tasks, "prepare_document", prepare)
    engine = FakeEngineManager()
    await tasks.process_document(FakeSession(), job, engine_manager=engine)
    assert engine.deleted == ["dead-article"]
    assert document.status == DocumentStatus.READY
