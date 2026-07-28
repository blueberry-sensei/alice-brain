"""The entity read path: inject an event-entity graph, then verify the entities endpoint (offline)."""

import uuid

import httpx
import pytest


@pytest.mark.asyncio
async def test_entity_read_path():
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Document, Source
    from sag_api.enums import DocumentStatus
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            tok = (
                await c.post("/api/v1/auth/register", json={"email": "book@x.com", "password": "password123"})
            ).json()["access_token"]
            H = {"Authorization": f"Bearer {tok}"}

            src = (await c.post("/api/v1/sources", headers=H, json={"name": "\u4e09\u56fd\u6f14\u4e49"})).json()
            sid = src["id"]
            async with SessionLocal() as s:
                source = await s.get(Source, sid)
                scid = source.sag_source_config_id
                document_id = uuid.uuid4().hex
                s.add(
                    Document(
                        id=document_id,
                        source_id=sid,
                        filename="\u4e09\u56fd\u6f14\u4e49.md",
                        content_type="text/markdown",
                        size_bytes=128,
                        storage_path="/tmp/three-kingdoms.md",
                        status=DocumentStatus.READY,
                        chunk_count=1,
                        event_count=2,
                        sag_source_id="d1",
                    )
                )
                source.document_count = 1
                source.chunk_count = 1
                # Simulate the brief window where the extraction checkpoint has written the document statistics but the source aggregate is not settled yet.
                # The graph must use the document statistics as the total, and must not report the current slice as everything.
                source.event_count = 0
                await s.commit()

            # Inject the event-entity graph (simulating the extract output)
            from alicecore.db import get_session_factory
            from alicecore.db.models import (
                Article,
                ArticleParseStatus,
                Entity,
                EntityType,
                EventEntity,
                SourceChunk,
                SourceConfig,
                SourceEvent,
            )

            sf = get_session_factory()
            async with sf() as s:
                await s.merge(SourceConfig(id=scid, name="\u4e09\u56fd\u6f14\u4e49"))
                s.add(
                    Article(
                        id="d1",
                        source_config_id=scid,
                        source_id="d1",
                        title="\u4e09\u56fd\u6f14\u4e49",
                        content="# \u4e09\u56fd\u6f14\u4e49\n\n\u5173\u7fbd\u8fc7\u4e94\u5173\u65a9\u516d\u5c06\u3002\n",
                        status="COMPLETED",
                        parse_status=ArticleParseStatus.COMPLETED,
                    )
                )
                et = EntityType(id=uuid.uuid4().hex, type="person", name="\u4eba\u7269")
                s.add(et)
                await s.flush()
                ent = Entity(
                    id=uuid.uuid4().hex,
                    source_config_id=scid,
                    entity_type_id=et.id,
                    type="person",
                    name="\u5173\u7fbd",
                    normalized_name="\u5173\u7fbd",
                    description="\u8700\u6c49\u540d\u5c06",
                )
                s.add(ent)
                await s.flush()
                ev = SourceEvent(
                    id=uuid.uuid4().hex,
                    source_config_id=scid,
                    source_type="doc",
                    source_id="d1",
                    title="\u8fc7\u4e94\u5173\u65a9\u516d\u5c06",
                    summary="\u5173\u7fbd\u5343\u91cc\u8d70\u5355\u9a91\uff0c\u62a4\u9001\u4e8c\u5ac2\u5bfb\u5144\u3002",
                    content="\u5173\u7fbd\u8fc7\u4e94\u5173\u65a9\u516d\u5c06\u3002",
                    chunk_id="chunk-1",
                )
                s.add(ev)
                await s.flush()
                event_id = ev.id
                hidden_event = SourceEvent(
                    id=uuid.uuid4().hex,
                    source_config_id=scid,
                    source_type="doc",
                    source_id="d1",
                    title="\u6843\u56ed\u7ed3\u4e49",
                    summary="\u5218\u5907\u3001\u5173\u7fbd\u3001\u5f20\u98de\u7ed3\u4e3a\u5144\u5f1f\u3002",
                    content="\u6843\u56ed\u4e09\u7ed3\u4e49\u3002",
                    chunk_id="chunk-2",
                    rank=1,
                    status="DELETED",
                )
                s.add(hidden_event)
                await s.flush()
                hidden_event_id = hidden_event.id
                entity_id = ent.id
                s.add(
                    SourceChunk(
                        id="chunk-1",
                        source_config_id=scid,
                        source_type="ARTICLE",
                        source_id="d1",
                        article_id="d1",
                        heading="\u4e09\u56fd\u6f14\u4e49",
                        content="\u5173\u7fbd\u8fc7\u4e94\u5173\u65a9\u516d\u5c06\u3002",
                    )
                )
                s.add(
                    SourceChunk(
                        id="chunk-2",
                        source_config_id=scid,
                        source_type="ARTICLE",
                        source_id="d1",
                        article_id="d1",
                        heading="\u6843\u56ed\u7ed3\u4e49",
                        content="\u5218\u5907\u3001\u5173\u7fbd\u3001\u5f20\u98de\u6843\u56ed\u7ed3\u4e49\u3002",
                    )
                )
                s.add(EventEntity(id=uuid.uuid4().hex, event_id=ev.id, entity_id=ent.id, weight=1.0))
                await s.commit()

            parsed = await c.get(
                f"/api/v1/sources/{sid}/documents/{document_id}/parsed",
                headers=H,
            )
            assert parsed.status_code == 200
            assert parsed.headers["content-type"].startswith("text/markdown")
            assert parsed.text == "# \u4e09\u56fd\u6f14\u4e49\n\n\u5173\u7fbd\u8fc7\u4e94\u5173\u65a9\u516d\u5c06\u3002\n"

            # Read the entities (with their heat)
            ents = (await c.get(f"/api/v1/sources/{sid}/entities?types=person", headers=H)).json()
            assert any(e["name"] == "\u5173\u7fbd" and e["heat"] >= 1 for e in ents)

            # \u56fe\u8c31\u8bfb\u8def\u5f84\u4fdd\u7559\u771f\u5b9e\u6587\u6863—\u4e8b\u4ef6—\u5b9e\u4f53\u5173\u7cfb\uff0c\u5e76\u8fd4\u56de\u5c55\u793a/\u603b\u91cf\u4fe1\u606f\u3002
            response = await c.get(f"/api/v1/sources/{sid}/graph", headers=H)
            assert response.status_code == 200
            graph = response.json()
            assert graph["documents"][0]["id"] == document_id
            assert {event["title"] for event in graph["events"]} == {
                "\u8fc7\u4e94\u5173\u65a9\u516d\u5c06",
                "\u6843\u56ed\u7ed3\u4e49",
            }
            assert all(event["document_id"] == document_id for event in graph["events"])
            assert graph["entities"][0]["name"] == "\u5173\u7fbd"
            assert {relation["kind"] for relation in graph["relations"]} == {
                "contains",
                "mentions",
            }
            assert graph["counts"]["events"] == 2
            assert graph["counts"]["shown_events"] == len(graph["events"]) == 2
            assert graph["counts"]["shown_relations"] == 3
            assert graph["truncated"] is False
            async with sf() as s:
                assert (await s.get(SourceEvent, hidden_event_id)).status == "COMPLETED"

            # \u9875\u9762\u9009\u62e9\u8f83\u5c0f\u9884\u7b97\u65f6\uff0c\u5c55\u793a\u6570\u53ef\u4ee5\u7f29\u5c0f\uff0c\u4f46\u603b\u91cf\u4ecd\u987b\u4e0e\u6587\u6863\u7684\u4e8b\u9879\u7edf\u8ba1\u4e00\u81f4\uff1b
            # \u5426\u5219 3D \u56fe\u8c31\u4f1a\u628a“1 / 2”\u9519\u8bef\u663e\u793a\u6210“1 / 1”\uff0c\u770b\u8d77\u6765\u50cf\u6f0f\u6389\u4e86\u4e8b\u9879\u3002
            limited = (
                await c.get(
                    f"/api/v1/sources/{sid}/graph?event_limit=1&entity_limit=1000",
                    headers=H,
                )
            ).json()
            assert len(limited["events"]) == 1
            assert limited["counts"]["shown_events"] == 1
            assert limited["counts"]["events"] == 2
            assert limited["truncated"] is True

            # \u641c\u7d22\u547d\u4e2d\u5206\u5757\u53ef\u7a33\u5b9a\u6620\u5c04\u56de\u4e8b\u9879\u6807\u9898\u3001\u6b63\u6587\u53ca\u771f\u5b9e\u4e8b\u9879—\u5b9e\u4f53\u5173\u7cfb\u3002
            from sag_api.sag import RetrievedSection

            search_graph = await app.state.engine_manager.graph_for_sections(
                [
                    RetrievedSection(
                        chunk_id="chunk-1",
                        score=0.91,
                        source_config_id=scid,
                    )
                ],
                {scid: source},
            )
            assert search_graph.events[0].title == "\u8fc7\u4e94\u5173\u65a9\u516d\u5c06"
            assert search_graph.events[0].summary.startswith("\u5173\u7fbd\u5343\u91cc\u8d70\u5355\u9a91")
            assert search_graph.events[0].content == "\u5173\u7fbd\u8fc7\u4e94\u5173\u65a9\u516d\u5c06\u3002"
            assert search_graph.events[0].score == pytest.approx(0.91)
            assert search_graph.entities[0].name == "\u5173\u7fbd"
            assert search_graph.associations[0].event_id == search_graph.events[0].id

            # \u4e8b\u9879\u5411\u91cf\u4e0e\u5757\u5411\u91cf\u5e76\u884c\u53ec\u56de\uff1a\u5373\u4f7f\u4e8b\u9879\u6240\u5728 chunk-2 \u6ca1\u8fdb\u5165\u5757 top-k\uff0c
            # \u4e5f\u5fc5\u987b\u80fd\u901a\u8fc7\u4e8b\u9879 id \u76f4\u63a5\u56de\u8868\uff0c\u5e76\u4f18\u5148\u4f7f\u7528\u4e8b\u9879\u76f8\u4f3c\u5ea6\u6392\u5e8f\u3002
            direct_event_graph = await app.state.engine_manager.graph_for_sections(
                [
                    RetrievedSection(
                        chunk_id="chunk-1",
                        score=0.91,
                        source_config_id=scid,
                    )
                ],
                {scid: source},
                event_scores={(scid, hidden_event_id): 0.97},
            )
            assert direct_event_graph.events[0].title == "\u6843\u56ed\u7ed3\u4e49"
            assert direct_event_graph.events[0].chunk_id == "chunk-2"
            assert direct_event_graph.events[0].score == pytest.approx(0.97)

            # \u56fe\u8c31\u5141\u8bb8\u754c\u9762\u6309\u6027\u80fd\u9009\u62e9\u66f4\u5927\u7684\u5c55\u793a\u91cf\uff0c\u540c\u65f6\u4fdd\u7559\u9632\u6b62\u8bef\u8bf7\u6c42\u7684\u4e0a\u9650\u3002
            large = await c.get(
                f"/api/v1/sources/{sid}/graph?document_limit=2000&event_limit=2000&entity_limit=2000",
                headers=H,
            )
            assert large.status_code == 200
            invalid = await c.get(f"/api/v1/sources/{sid}/graph?event_limit=10001", headers=H)
            assert invalid.status_code == 422

            # \u5220\u9664\u6587\u6863\u5fc5\u987b\u540c\u6b65\u6e05\u7406\u7edf\u8ba1\u4e0e\u5f15\u64ce\u4e2d\u7684\u5757\u3001\u4e8b\u4ef6\u53ca\u5b64\u7acb\u5b9e\u4f53\u3002
            deleted = await c.delete(
                f"/api/v1/sources/{sid}/documents/{document_id}",
                headers=H,
            )
            assert deleted.status_code == 200
            source_after_delete = (await c.get(f"/api/v1/sources/{sid}", headers=H)).json()
            assert source_after_delete["document_count"] == 0
            assert source_after_delete["chunk_count"] == 0
            assert source_after_delete["event_count"] == 0
            async with sf() as s:
                assert await s.get(Article, "d1") is None
                assert await s.get(SourceChunk, "chunk-1") is None
                assert await s.get(SourceChunk, "chunk-2") is None
                assert await s.get(SourceEvent, event_id) is None
                assert await s.get(SourceEvent, hidden_event_id) is None
                assert await s.get(Entity, entity_id) is None

            empty_source = (await c.post("/api/v1/sources", headers=H, json={"name": "\u7a7a\u4fe1\u6e90"})).json()
            empty_graph = (await c.get(f"/api/v1/sources/{empty_source['id']}/graph", headers=H)).json()
            assert empty_graph["documents"] == []
            assert empty_graph["events"] == []
            assert empty_graph["entities"] == []
            assert empty_graph["relations"] == []
            assert empty_graph["truncated"] is False


@pytest.mark.asyncio
async def test_source_graph_can_filter_one_or_multiple_documents(monkeypatch):
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Document, Source
    from sag_api.enums import DocumentStatus
    from sag_api.main import app
    from sag_api.sag import GraphEventInfo, SourceGraphInfo

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            token = (
                await client.post(
                    "/api/v1/auth/register",
                    json={
                        "email": f"graph-filter-{uuid.uuid4().hex}@x.com",
                        "password": "password123",
                    },
                )
            ).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            source_payload = (
                await client.post(
                    "/api/v1/sources",
                    headers=headers,
                    json={"name": "\u6587\u6863\u7b5b\u9009"},
                )
            ).json()
            source_id = source_payload["id"]

            async with SessionLocal() as session:
                source = await session.get(Source, source_id)
                assert source is not None
                first = Document(
                    source_id=source_id,
                    filename="\u7b2c\u4e00\u7bc7.md",
                    content_type="text/markdown",
                    size_bytes=10,
                    storage_path="/tmp/first.md",
                    status=DocumentStatus.READY,
                    chunk_count=1,
                    event_count=2,
                    sag_source_id="engine-first",
                )
                second = Document(
                    source_id=source_id,
                    filename="\u7b2c\u4e8c\u7bc7.md",
                    content_type="text/markdown",
                    size_bytes=10,
                    storage_path="/tmp/second.md",
                    status=DocumentStatus.READY,
                    chunk_count=1,
                    event_count=3,
                    sag_source_id="engine-second",
                )
                session.add_all([first, second])
                source.document_count = 2
                source.chunk_count = 2
                source.event_count = 5
                await session.commit()
                first_id, second_id = first.id, second.id

            seen_source_ids: list[list[str]] = []

            async def fake_source_graph(
                _source_config_id,
                source_ids,
                **_kwargs,
            ):
                seen_source_ids.append(list(source_ids))
                return SourceGraphInfo(
                    events=[
                        GraphEventInfo(
                            id=f"event-{engine_source_id}",
                            source_id=engine_source_id,
                            title=f"\u4e8b\u9879 {engine_source_id}",
                        )
                        for engine_source_id in source_ids
                    ]
                )

            monkeypatch.setattr(
                app.state.engine_manager,
                "source_graph",
                fake_source_graph,
            )

            single = await client.get(
                f"/api/v1/sources/{source_id}/graph",
                headers=headers,
                params=[("document_ids", second_id)],
            )
            assert single.status_code == 200
            single_graph = single.json()
            assert [document["id"] for document in single_graph["documents"]] == [second_id]
            assert single_graph["events"][0]["document_id"] == second_id
            assert single_graph["counts"]["documents"] == 1
            assert single_graph["counts"]["events"] == 3
            assert seen_source_ids[-1] == ["engine-second"]

            multiple = await client.get(
                f"/api/v1/sources/{source_id}/graph",
                headers=headers,
                params=[
                    ("document_ids", first_id),
                    ("document_ids", second_id),
                    ("document_ids", second_id),
                ],
            )
            assert multiple.status_code == 200
            multiple_graph = multiple.json()
            assert {document["id"] for document in multiple_graph["documents"]} == {
                first_id,
                second_id,
            }
            assert multiple_graph["counts"]["documents"] == 2
            assert multiple_graph["counts"]["events"] == 5
            assert set(seen_source_ids[-1]) == {"engine-first", "engine-second"}

            empty = await client.get(
                f"/api/v1/sources/{source_id}/graph",
                headers=headers,
                params=[("document_ids", "")],
            )
            assert empty.status_code == 200
            assert empty.json()["documents"] == []
            assert empty.json()["events"] == []
            assert empty.json()["counts"]["documents"] == 0
            assert empty.json()["counts"]["events"] == 0
            assert seen_source_ids[-1] == []

            all_documents = await client.get(
                f"/api/v1/sources/{source_id}/graph",
                headers=headers,
            )
            assert all_documents.status_code == 200
            assert all_documents.json()["counts"]["documents"] == 2
            assert all_documents.json()["counts"]["events"] == 5

            async with SessionLocal() as session:
                source = await session.get(Source, source_id)
                assert source is not None
                await session.delete(source)
                await session.commit()
