"""Aggregate universe overview and bounded exploration stay strictly separated."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import delete


def test_universe_evidence_lookup_declares_composite_index():
    from sag_api.db.models import Document

    index = next(
        item
        for item in Document.__table__.indexes
        if item.name == "ix_documents_source_sag_source"
    )
    assert tuple(column.name for column in index.columns) == ("source_id", "sag_source_id")


def test_universe_v2_response_models_reject_malformed_or_stalled_pages():
    from sag_api.schemas.universe import (
        UniverseGraphPatchOut,
        UniverseTimelineIn,
        UniverseTimelineSliceOut,
    )

    now = datetime(2026, 7, 12, tzinfo=UTC)
    node = {
        "id": "event-1",
        "kind": "event",
        "source_id": "source-1",
        "related_count": 1,
    }
    entity = {
        "id": "entity-1",
        "kind": "entity",
        "source_id": "source-1",
    }
    relation = {
        "source_id": "source-1",
        "from_id": "event-1",
        "to_id": "missing-entity",
        "kind": "mentions",
    }
    with pytest.raises(PydanticValidationError, match="must carry a cursor"):
        UniverseTimelineIn.model_validate(
            {
                "epoch": 1,
                "source_id": "source-1",
                "direction": "newer",
            }
        )
    with pytest.raises(PydanticValidationError, match="relation endpoint"):
        UniverseGraphPatchOut.model_validate(
            {
                "schema_version": 2,
                "epoch": 1,
                "source_id": "source-1",
                "source_revision": "revision-1",
                "snapshot_id": "snapshot-1",
                "request_cursor": None,
                "page_id": "page-1",
                "bundle_id": "bundle-1",
                "anchor": node,
                "nodes": [entity],
                "relations": [relation],
                "page": {"returned": 1, "has_more": False, "next_cursor": None},
                "as_of": now,
            }
        )

    def slice_payload(**overrides):
        payload = {
            "schema_version": 3,
            "epoch": 1,
            "source_id": "source-1",
            "source_revision": "revision-1",
            "snapshot_id": "snapshot-1",
            "request_direction": "older",
            "request_cursor": "same-cursor",
            "page_id": "page-1",
            "bundles": [
                {
                    "bundle_id": "bundle-1",
                    "ordinal": 0,
                    "event": node,
                    "nodes": [entity],
                    "relations": [
                        {
                            **relation,
                            "to_id": "entity-1",
                        }
                    ],
                    "neighbor_page": {
                        "total_unique": 1,
                        "returned_unique": 1,
                        "complete": True,
                        "next_cursor": None,
                    },
                    "cursor_before": None,
                    "cursor_after": "same-cursor",
                }
            ],
            "total_events": 3,
            "page": {
                "returned_bundles": 1,
                "returned_unique_nodes": 2,
                "returned_relations": 1,
                "direction": "older",
                "has_newer": False,
                "newer_cursor": None,
                "has_older": True,
                "older_cursor": "same-cursor",
                "has_more": True,
                "next_cursor": "same-cursor",
            },
            "as_of": now,
        }
        payload.update(overrides)
        return payload

    with pytest.raises(PydanticValidationError, match="did not advance"):
        UniverseTimelineSliceOut.model_validate(slice_payload())

    second_bundle = {
        "bundle_id": "bundle-2",
        "ordinal": 0,
        "event": {**node, "id": "event-2", "related_count": 0},
        "nodes": [],
        "relations": [],
        "neighbor_page": {
            "total_unique": 0,
            "returned_unique": 0,
            "complete": True,
            "next_cursor": None,
        },
        "cursor_before": "cursor-1",
        "cursor_after": "same-cursor",
    }

    def two_bundle_payload(second_ordinal: int):
        payload = slice_payload(request_cursor=None)
        first = {
            **payload["bundles"][0],
            "cursor_after": "cursor-1",
            "ordinal": 1,
        }
        payload["bundles"] = [first, {**second_bundle, "ordinal": second_ordinal}]
        payload["page"] = {
            **payload["page"],
            "returned_bundles": 2,
            "returned_unique_nodes": 3,
        }
        return payload

    # The counting axis hangs off these two invariants: ordinals march strictly
    # older within a page, and never reach past the snapshot's event total.
    with pytest.raises(PydanticValidationError, match="must strictly increase"):
        UniverseTimelineSliceOut.model_validate(two_bundle_payload(1))
    with pytest.raises(PydanticValidationError, match="exceeds the source total"):
        UniverseTimelineSliceOut.model_validate(two_bundle_payload(3))


@pytest.mark.asyncio
async def test_universe_overview_expand_detail_and_reset_contract():
    from sag_api.core.db import SessionLocal
    from sag_api.core.deps import get_engine_manager
    from sag_api.db.models import Document, Source, UniverseOverview
    from sag_api.enums import DocumentStatus
    from sag_api.main import app
    from sag_api.sag.dto import (
        EntityInfo,
        GraphAssociationInfo,
        GraphEventInfo,
        RetrievedSection,
        SearchOutcome,
        SourceGraphInfo,
        UniverseExpansionInfo,
        UniverseSourceStatsInfo,
        UniverseTimeBucketInfo,
    )

    class UniverseEngine:
        overview_calls = 0
        source_ref_by_config: dict[str, str] = {}

        async def provision(self, *_args):
            return None

        async def universe_overview_stats(self, source_config_id, **_kwargs):
            self.overview_calls += 1
            now = datetime(2026, 7, 12, tzinfo=UTC)
            return UniverseSourceStatsInfo(
                event_count=1,
                entity_count=1,
                relation_count=1,
                category_counts={"\u4ea7\u54c1\u8bbe\u8ba1": 1},
                time_buckets=[
                    UniverseTimeBucketInfo(
                        start=now - timedelta(days=30),
                        end=now,
                        count=1,
                    )
                ],
            )

        async def universe_expand(
            self, source_config_id, node_kind, node_id, *, limit=20, cursor=None, **_kwargs
        ):
            event_id = f"event-{source_config_id}"
            entity_id = f"entity-{source_config_id}"
            if node_kind == "event" and node_id == event_id:
                return UniverseExpansionInfo(
                    anchor={
                        "id": event_id,
                        "kind": "event",
                        "label": "\u77e5\u8bc6\u5b87\u5b99\u5f00\u59cb\u53d1\u5149",
                        "description": "\u4e8b\u4ef6\u8fd4\u56de\u4e00\u4e2a\u6709\u754c\u5b9e\u4f53\u90bb\u57df\u3002",
                        "related_count": 1,
                    },
                    neighbors=[
                        {
                            "id": entity_id,
                            "kind": "entity",
                            "label": "\u77e5\u8bc6\u5b87\u5b99",
                            "category": "\u4ea7\u54c1\u6982\u5ff5",
                            "weight": 1.0,
                        }
                    ][:limit],
                    relations=[
                        {
                            "from_id": event_id,
                            "to_id": entity_id,
                            "kind": "mentions",
                            "weight": 1.0,
                        }
                    ],
                    returned=1,
                    snapshot_id="test-source-read-snapshot",
                    as_of=datetime(2026, 7, 12, tzinfo=UTC),
                )
            if node_kind == "entity" and node_id == entity_id:
                return UniverseExpansionInfo(
                    anchor={
                        "id": entity_id,
                        "kind": "entity",
                        "label": "\u77e5\u8bc6\u5b87\u5b99",
                        "description": "\u5b9e\u4f53\u53ea\u5c55\u5f00\u6700\u65b0\u7684\u6709\u754c\u4e8b\u4ef6\u3002",
                        "related_count": 1,
                    },
                    neighbors=[
                        {
                            "id": event_id,
                            "kind": "event",
                            "label": "\u77e5\u8bc6\u5b87\u5b99\u5f00\u59cb\u53d1\u5149",
                            "category": "\u4ea7\u54c1\u8bbe\u8ba1",
                            "weight": 1.0,
                        }
                    ][:limit],
                    relations=[
                        {
                            "from_id": event_id,
                            "to_id": entity_id,
                            "kind": "mentions",
                            "weight": 1.0,
                        }
                    ],
                    returned=1,
                    snapshot_id="test-source-read-snapshot",
                    as_of=datetime(2026, 7, 12, tzinfo=UTC),
                )
            return None

        async def universe_node_detail(
            self, source_config_id, node_kind, node_id, **_kwargs
        ):
            event_id = f"event-{source_config_id}"
            entity_id = f"entity-{source_config_id}"
            if node_id not in {event_id, entity_id}:
                return None
            if node_kind == "event":
                return {
                    "label": "\u77e5\u8bc6\u5b87\u5b99\u5f00\u59cb\u53d1\u5149",
                    "description": "\u4e8b\u4ef6\u4e0d\u9700\u8981\u9884\u5148\u5b58\u5728\u4e8e\u4efb\u4f55\u5e03\u5c40\u8868\u3002",
                    "category": "\u4ea7\u54c1\u8bbe\u8ba1",
                    "chunk_id": f"chunk-{source_config_id}",
                    "source_ref_id": self.source_ref_by_config[source_config_id],
                }
            return {
                "label": "\u77e5\u8bc6\u5b87\u5b99",
                "description": "\u672c\u5730\u77e5\u8bc6\u5e93\u7684\u52a8\u6001\u5206\u533a\u6295\u5f71",
                "category": "\u4ea7\u54c1\u6982\u5ff5",
            }

        async def get_chunk(self, source_config_id, chunk_id, **_kwargs):
            return RetrievedSection(
                chunk_id=chunk_id,
                heading="\u77e5\u8bc6\u5b87\u5b99\u65b9\u6848",
                content="\u6240\u6709\u661f\u70b9\u90fd\u80fd\u56de\u5230\u8fd9\u6bb5\u771f\u5b9e\u539f\u6587\u3002",
                score=1.0,
                source_config_id=source_config_id,
            )

        async def search_many(self, targets, query, **_kwargs):
            source_config_id = targets[0][0]
            return SearchOutcome(
                query=query,
                sections=[
                    RetrievedSection(
                        chunk_id=f"chunk-{source_config_id}",
                        heading="\u77e5\u8bc6\u5b87\u5b99\u65b9\u6848",
                        content="\u6240\u6709\u661f\u70b9\u90fd\u80fd\u56de\u5230\u8fd9\u6bb5\u771f\u5b9e\u539f\u6587\u3002",
                        score=0.92,
                        source_config_id=source_config_id,
                    )
                ],
                stats={"retrievals": 1},
            )

        async def graph_for_sections(self, sections, _sources_by_config, **_kwargs):
            source_config_id = sections[0].source_config_id
            source_ref = self.source_ref_by_config[source_config_id]
            event_id = f"event-{source_config_id}"
            entity_id = f"entity-{source_config_id}"
            return SourceGraphInfo(
                events=[
                    GraphEventInfo(
                        id=event_id,
                        source_config_id=source_config_id,
                        source_id=source_ref,
                        chunk_id=f"chunk-{source_config_id}",
                        title="\u77e5\u8bc6\u5b87\u5b99\u5f00\u59cb\u53d1\u5149",
                        summary="\u641c\u7d22\u53ea\u805a\u7126\u5f53\u524d\u771f\u5b9e\u5de5\u4f5c\u96c6\u3002",
                        category="\u4ea7\u54c1\u8bbe\u8ba1",
                        score=0.92,
                    )
                ],
                entities=[
                    EntityInfo(
                        id=entity_id,
                        name="\u77e5\u8bc6\u5b87\u5b99",
                        type="\u4ea7\u54c1\u6982\u5ff5",
                        heat=1,
                    )
                ],
                associations=[
                    GraphAssociationInfo(
                        event_id=event_id,
                        entity_id=entity_id,
                        weight=1.0,
                    )
                ],
            )

    engine = UniverseEngine()
    app.dependency_overrides[get_engine_manager] = lambda: engine
    try:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            # Rebuilds now run inside the queue, outside FastAPI dependency overrides.
            app.state.job_queue._engine_manager = engine
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                registered = await client.post(
                    "/api/v1/auth/register",
                    json={
                        "email": f"universe-{uuid.uuid4().hex}@t.com",
                        "password": "password123",
                    },
                )
                assert registered.status_code == 201, registered.text
                headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

                async def wait_for_job(job_id: str) -> dict:
                    for _ in range(300):
                        response = await client.get(f"/api/v1/jobs/{job_id}", headers=headers)
                        assert response.status_code == 200, response.text
                        body = response.json()
                        if body["status"] in {"succeeded", "failed"}:
                            return body
                        await asyncio.sleep(0.01)
                    pytest.fail(f"job did not finish: {job_id}")

                created = await client.post(
                    "/api/v1/sources",
                    headers=headers,
                    json={"name": "\u5b87\u5b99\u6d4b\u8bd5\u6e90"},
                )
                assert created.status_code == 201, created.text
                source_id = created.json()["id"]
                async with SessionLocal() as session:
                    source = await session.get(Source, source_id)
                    assert source is not None
                    source_config_id = source.sag_source_config_id
                    engine.source_ref_by_config[source_config_id] = "universe-document"
                    session.add(
                        Document(
                            id=uuid.uuid4().hex,
                            source_id=source_id,
                            filename="universe.md",
                            content_type="text/markdown",
                            size_bytes=64,
                            storage_path="/tmp/universe.md",
                            status=DocumentStatus.READY,
                            chunk_count=1,
                            event_count=1,
                            sag_source_id="universe-document",
                        )
                    )
                    source.document_count = 1
                    source.chunk_count = 1
                    source.event_count = 1
                    await session.commit()

                # GET manifest never opens the graph store or materializes real nodes.
                initial = await client.get("/api/v1/universe/manifest", headers=headers)
                assert initial.status_code == 200, initial.text
                assert initial.json()["status"] == "stale"
                assert initial.json()["version"] is None
                initial_partition = next(
                    item
                    for item in initial.json()["partitions"]
                    if item["source_id"] == source_id and item["kind"] == "source"
                )
                assert initial_partition["event_count"] == 1
                assert engine.overview_calls == 0

                rebuilt = await client.post("/api/v1/universe/rebuild", headers=headers)
                assert rebuilt.status_code == 202, rebuilt.text
                rebuild_job = rebuilt.json()
                assert rebuild_job["type"] == "index_universe"
                assert (await wait_for_job(rebuild_job["id"]))["status"] == "succeeded"
                manifest_response = await client.get("/api/v1/universe/manifest", headers=headers)
                assert manifest_response.status_code == 200, manifest_response.text
                manifest = manifest_response.json()
                assert manifest["status"] == "ready"
                counts = manifest["counts"]
                assert counts["sources"] >= 1
                assert counts["partitions"] >= counts["sources"]
                assert counts["events"] >= 1
                assert counts["entities"] >= 1
                assert counts["nodes"] == counts["events"] + counts["entities"]
                assert counts["relations"] >= 1
                partition = next(
                    item
                    for item in manifest["partitions"]
                    if item["source_id"] == source_id and item["kind"] == "source"
                )
                assert partition["event_count"] == 1
                assert partition["entity_count"] == 1
                assert partition["relation_count"] == 1
                assert partition["time_buckets"][0]["count"] == 1
                assert all(key in partition for key in ("x", "y", "z", "density"))

                # \u5feb\u7167\u4e8b\u4ef6\u6570\u4e0e\u4fe1\u6e90\u5b8c\u6210\u6570\u4e0d\u4e00\u81f4\u65f6\uff0c\u5373\u4f7f\u810f\u6807\u8bb0\u9057\u6f0f\u4e5f\u5fc5\u987b\u89e6\u53d1\u91cd\u5efa\u3002
                async with SessionLocal() as session:
                    source = await session.get(Source, source_id)
                    assert source is not None
                    source.event_count = 2
                    await session.commit()
                drifted = await client.get("/api/v1/universe/manifest", headers=headers)
                assert drifted.status_code == 200
                assert drifted.json()["status"] == "stale"
                assert drifted.json()["stale"] is True
                async with SessionLocal() as session:
                    source = await session.get(Source, source_id)
                    assert source is not None
                    source.event_count = 1
                    await session.commit()

                second = await client.post("/api/v1/universe/rebuild", headers=headers)
                assert second.status_code == 202, second.text
                assert (await wait_for_job(second.json()["id"]))["status"] == "succeeded"
                second_manifest = await client.get("/api/v1/universe/manifest", headers=headers)
                second_partition = next(
                    item
                    for item in second_manifest.json()["partitions"]
                    if item["source_id"] == source_id and item["kind"] == "source"
                )
                assert (partition["x"], partition["y"], partition["z"]) == (
                    second_partition["x"],
                    second_partition["y"],
                    second_partition["z"],
                )

                event_id = f"event-{source_config_id}"
                entity_id = f"entity-{source_config_id}"
                too_large = await client.post(
                    "/api/v1/universe/expand",
                    headers=headers,
                    json={
                        "epoch": 7,
                        "source_id": source_id,
                        "node_kind": "event",
                        "node_id": event_id,
                        "limit": 9,
                    },
                )
                assert too_large.status_code == 422

                expanded = await client.post(
                    "/api/v1/universe/expand",
                    headers=headers,
                    json={
                        "epoch": 7,
                        "source_id": source_id,
                        "node_kind": "event",
                        "node_id": event_id,
                        "limit": 1,
                    },
                )
                assert expanded.status_code == 200, expanded.text
                patch = expanded.json()
                assert patch["epoch"] == 7
                assert patch["nodes"][0]["id"] == entity_id
                assert patch["page"] == {
                    "returned": 1,
                    "has_more": False,
                    "next_cursor": None,
                }

                missing_source = await client.get(
                    f"/api/v1/universe/nodes/event/{event_id}", headers=headers
                )
                assert missing_source.status_code == 422
                detail = await client.get(
                    f"/api/v1/universe/nodes/event/{event_id}?source_id={source_id}",
                    headers=headers,
                )
                assert detail.status_code == 200, detail.text
                assert detail.json()["evidence"]["content"] == "\u6240\u6709\u661f\u70b9\u90fd\u80fd\u56de\u5230\u8fd9\u6bb5\u771f\u5b9e\u539f\u6587\u3002"
                assert "related_nodes" not in detail.json()

                # Losing a snapshot returns the cheap source outline; GET never rebuilds.
                async with SessionLocal() as session:
                    await session.execute(delete(UniverseOverview))
                    await session.commit()
                calls_before = engine.overview_calls
                fallback = await client.get("/api/v1/universe/manifest", headers=headers)
                assert fallback.status_code == 200
                assert fallback.json()["status"] == "stale"
                assert fallback.json()["version"] is None
                assert engine.overview_calls == calls_before
    finally:
        app.dependency_overrides.pop(get_engine_manager, None)
