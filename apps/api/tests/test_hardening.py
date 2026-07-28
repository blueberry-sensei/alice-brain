"""R3 stability hardening: the readiness/liveness probes, the upload allowlist, job backoff retries, engine LRU eviction."""

import asyncio

import httpx
import pytest


async def _register(c, email="hard@t.com"):
    r = await c.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_health_ready_and_upload_whitelist():
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            assert (await c.get("/api/v1/system/health")).json()["status"] == "ok"
            ready = await c.get("/api/v1/system/ready")
            assert ready.status_code == 200 and ready.json()["db"] is True

            caps = (await c.get("/api/v1/system/capabilities")).json()
            assert ".md" in caps["allowed_upload_exts"]

            A = await _register(c)
            sid = (await c.post("/api/v1/sources", headers=A, json={"name": "Allowlist"})).json()["id"]

            # Creating a source must create the engine parent record at the same time; the incremental loader writes an article directly,
            # and without that record it triggers FOREIGN KEY constraint failed.
            from alicecore.db import SourceConfig, get_session_factory

            from sag_api.core.db import SessionLocal
            from sag_api.db.models import Source

            async with SessionLocal() as session:
                source = await session.get(Source, sid)
                assert source is not None
                source_config_id = source.sag_source_config_id
            engine_session_factory = get_session_factory()
            async with engine_session_factory() as engine_session:
                parent = await engine_session.get(SourceConfig, source_config_id)
                assert parent is not None
                assert parent.name == "Allowlist"

            # An unsupported extension -> 422 (validation failure), and the frontend shows a clear message
            bad = await c.post(
                f"/api/v1/sources/{sid}/documents",
                headers=A,
                files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
            )
            assert bad.status_code == 422
            assert "Unsupported file type" in bad.json()["error"]["message"]

            # An allowed extension -> 201
            ok = await c.post(
                f"/api/v1/sources/{sid}/documents",
                headers=A,
                files={"file": ("note.md", b"# hi", "text/markdown")},
            )
            assert ok.status_code == 201


@pytest.mark.asyncio
async def test_job_retry_backoff(monkeypatch):
    """A retryable failure is rescheduled with backoff until it succeeds; a non-retryable failure goes FAILED immediately."""
    from sqlalchemy import delete

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import ServiceUnavailableError, ValidationError
    from sag_api.db.models import Job
    from sag_api.enums import JobStatus, JobType
    from sag_api.jobs import inproc
    from sag_api.jobs.tasks import TASK_HANDLERS

    monkeypatch.setattr(inproc, "_BACKOFF_BASE_SECONDS", 0.01)
    await init_db()
    # Clear the jobs other cases left behind, so _recover does not run them with our stub handler and pollute the counts
    async with SessionLocal() as s:
        await s.execute(delete(Job))
        await s.commit()

    calls = {"n": 0}

    async def flaky_handler(session, job, **_):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ServiceUnavailableError("The upstream is temporarily unavailable")

    monkeypatch.setitem(TASK_HANDLERS, JobType.PROCESS_DOCUMENT, flaky_handler)

    queue = inproc.InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)
    await queue.start()
    try:
        async with SessionLocal() as s:
            job = Job(type=JobType.PROCESS_DOCUMENT, status=JobStatus.QUEUED)
            s.add(job)
            await s.commit()
            jid = job.id
        await queue.enqueue(jid)

        # Poll until a terminal state
        for _ in range(200):
            await asyncio.sleep(0.05)
            async with SessionLocal() as s:
                st = (await s.get(Job, jid)).status
            if st in (JobStatus.SUCCEEDED, JobStatus.FAILED):
                break
        async with SessionLocal() as s:
            done = await s.get(Job, jid)
        assert done.status == JobStatus.SUCCEEDED
        assert done.attempts == 3  # two failures + one success
        assert calls["n"] == 3

        # Not retryable: it fails on the first attempt
        calls["n"] = 0

        async def bad_handler(session, job, **_):
            calls["n"] += 1
            raise ValidationError("bad input")

        monkeypatch.setitem(TASK_HANDLERS, JobType.PROCESS_DOCUMENT, bad_handler)
        async with SessionLocal() as s:
            job2 = Job(type=JobType.PROCESS_DOCUMENT, status=JobStatus.QUEUED)
            s.add(job2)
            await s.commit()
            jid2 = job2.id
        await queue.enqueue(jid2)
        for _ in range(100):
            await asyncio.sleep(0.05)
            async with SessionLocal() as s:
                st = (await s.get(Job, jid2)).status
            if st in (JobStatus.SUCCEEDED, JobStatus.FAILED):
                break
        async with SessionLocal() as s:
            done2 = await s.get(Job, jid2)
        assert done2.status == JobStatus.FAILED
        assert done2.attempts == 1 and calls["n"] == 1
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_engine_lru_eviction():
    """Exceeding the cache cap evicts the least recently used; a slot holding its lock is never evicted."""
    from sag_api.core.config import settings
    from sag_api.sag.engine_manager import EngineManager, _Slot

    class FakeEngine:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    mgr = EngineManager(settings)
    mgr._cache_size = 3
    engines = {}
    for i in range(3):
        eng = FakeEngine()
        engines[f"s{i}"] = eng
        mgr._slots[f"s{i}"] = _Slot(engine=eng, last_used=float(i))

    # s0 is the least recently used, but it is locked first -> it must be skipped and the next oldest s1 evicted
    await mgr._slots["s0"].lock.acquire()
    try:
        newest = FakeEngine()
        mgr._slots["s_new"] = _Slot(engine=newest, last_used=100.0)  # now 4 > 3
        await mgr._evict_lru(keep="s_new")
    finally:
        mgr._slots["s0"].lock.release()

    assert "s1" not in mgr._slots and engines["s1"].closed is True
    assert "s0" in mgr._slots  # skipped because it holds its lock
    assert "s_new" in mgr._slots and "s2" in mgr._slots


@pytest.mark.asyncio
async def test_engine_close_all_waits_for_inflight_use():
    """Saving the configuration at runtime must not close an engine that is ingesting or searching."""
    from sag_api.core.config import settings
    from sag_api.sag.engine_manager import EngineManager, _Slot

    class FakeEngine:
        closed = False

        async def aclose(self):
            self.closed = True

    engine = FakeEngine()
    manager = EngineManager(settings)
    manager._slots["source"] = _Slot(engine=engine)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def in_flight():
        async with manager.use("source"):
            entered.set()
            await release.wait()

    use_task = asyncio.create_task(in_flight())
    await entered.wait()
    close_task = asyncio.create_task(manager.aclose_all())
    await asyncio.sleep(0)
    assert engine.closed is False
    release.set()
    await use_task
    await close_task
    assert engine.closed is True


@pytest.mark.asyncio
async def test_engine_lifecycle_reset_waits_for_inflight_operations():
    """Before a new engine resets the shared resources, it must wait for the existing engine operations to exit."""
    from sag_api.sag.engine_manager import _EngineLifecycleGate

    gate = _EngineLifecycleGate()
    reader_entered = asyncio.Event()
    release_reader = asyncio.Event()
    writer_entered = asyncio.Event()

    async def hold_operation():
        async with gate.read():
            reader_entered.set()
            await release_reader.wait()

    async def reset_runtime():
        async with gate.write():
            writer_entered.set()

    reader = asyncio.create_task(hold_operation())
    await reader_entered.wait()
    writer = asyncio.create_task(reset_runtime())
    await asyncio.sleep(0.02)
    assert writer_entered.is_set() is False

    release_reader.set()
    await asyncio.gather(reader, writer)
    assert writer_entered.is_set() is True


@pytest.mark.asyncio
async def test_engine_allows_same_source_document_concurrency():
    """Document processing no longer holds the per-source serial lock, so one source can process several documents at once."""
    from sag_api.core.config import settings
    from sag_api.sag.engine_manager import EngineManager, _Slot

    class FakeEngine:
        async def aclose(self):
            pass

    manager = EngineManager(settings)
    manager._slots["source"] = _Slot(engine=FakeEngine())
    entered = 0
    both_entered = asyncio.Event()
    release = asyncio.Event()

    async def in_flight():
        nonlocal entered
        async with manager.use_concurrently("source"):
            entered += 1
            if entered == 2:
                both_entered.set()
            await release.wait()

    first = asyncio.create_task(in_flight())
    second = asyncio.create_task(in_flight())
    await asyncio.wait_for(both_entered.wait(), timeout=1)
    assert manager._slots["source"].concurrent_users == 2
    release.set()
    await asyncio.gather(first, second)
    assert manager._slots["source"].concurrent_users == 0


@pytest.mark.asyncio
async def test_document_cleanup_drains_and_blocks_same_source_processing(monkeypatch):
    """While the derived data is being cleaned, no new document processing may start on the same source."""
    from sag_api.core.config import settings
    from sag_api.sag import document_cleanup
    from sag_api.sag.engine_manager import EngineManager, _Slot

    class FakeEngine:
        async def aclose(self):
            pass

    class Deleted:
        chunk_ids = ()
        event_ids = ()
        relation_ids = ()
        entity_ids = ()

    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def fake_delete_records(source_config_id, document_source_id):
        assert (source_config_id, document_source_id) == ("source", "document")
        cleanup_entered.set()
        await release_cleanup.wait()
        return Deleted()

    monkeypatch.setattr(document_cleanup, "delete_document_records", fake_delete_records)
    manager = EngineManager(settings)
    slot = _Slot(engine=FakeEngine())
    manager._slots["source"] = slot

    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()
    release_second = asyncio.Event()

    async def first_processing():
        async with manager.use_concurrently("source"):
            first_entered.set()
            await release_first.wait()

    async def second_processing():
        async with manager.use_concurrently("source"):
            second_entered.set()
            await release_second.wait()

    first = asyncio.create_task(first_processing())
    await first_entered.wait()
    cleanup = asyncio.create_task(manager.delete_document_data("source", "document"))
    for _ in range(20):
        if not slot.concurrent_allowed.is_set():
            break
        await asyncio.sleep(0)
    assert slot.concurrent_allowed.is_set() is False

    second = asyncio.create_task(second_processing())
    release_first.set()
    await cleanup_entered.wait()
    await asyncio.sleep(0)
    assert second_entered.is_set() is False

    release_cleanup.set()
    await cleanup
    await asyncio.wait_for(second_entered.wait(), timeout=1)
    release_second.set()
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_search_falls_back_to_vector():
    """A failed or empty multi falls back to vector automatically; a failing vector no longer falls back."""
    from sag_api.core.config import settings
    from sag_api.sag.engine_manager import EngineManager, _Slot

    class FakeEngine:
        def __init__(self, fail_multi: bool, empty_multi: bool = False):
            self.fail_multi = fail_multi
            self.empty_multi = empty_multi
            self.calls: list[str] = []

        async def search(self, query, strategy=None, top_k=None):
            self.calls.append(strategy)
            if strategy == "multi":
                if self.fail_multi:
                    raise RuntimeError("boom")
                if self.empty_multi:
                    return type("R", (), {"query": query, "sections": [], "stats": {}})()
            return type(
                "R",
                (),
                {
                    "query": query,
                    "sections": [
                        {"chunk_id": "c1", "source_id": "s", "source_config_id": "scid",
                         "heading": "h", "content": "x", "rank": 1, "score": 0.9, "weight": 1}
                    ],
                    "stats": {},
                },
            )()

        async def aclose(self):
            pass

    async def run_case(engine):
        em = EngineManager(settings)
        em._slots["scid"] = _Slot(engine=engine)
        return await em.search("scid", "q", strategy="multi")

    # Fallback on failure
    eng = FakeEngine(fail_multi=True)
    out = await run_case(eng)
    assert eng.calls == ["multi", "vector"] and len(out.sections) == 1
    # Fallback on an empty result
    eng2 = FakeEngine(fail_multi=False, empty_multi=True)
    out2 = await run_case(eng2)
    assert eng2.calls == ["multi", "vector"] and len(out2.sections) == 1
    # A direct vector failure does not fall back
    class FailVector(FakeEngine):
        async def search(self, query, strategy=None, top_k=None):
            self.calls.append(strategy)
            raise RuntimeError("vector down")

    em = EngineManager(settings)
    fv = FailVector(fail_multi=False)
    em._slots["scid"] = _Slot(engine=fv)
    with pytest.raises(RuntimeError, match="vector down"):
        await em.search("scid", "q", strategy="vector")
    assert fv.calls == ["vector"]
