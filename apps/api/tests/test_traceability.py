"""Citation provenance: the chunk raw-text endpoint plus the sag source_id semantics of citations."""

import uuid

import httpx
import pytest


@pytest.mark.asyncio
async def test_chunk_endpoint_and_citation_refs():
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Source
    from sag_api.generation.prompt import build_citations
    from sag_api.main import app
    from sag_api.sag.dto import RetrievedSection

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            tok = (
                await c.post(
                    "/api/v1/auth/register",
                    json={"email": "trace@x.com", "password": "password123"},
                )
            ).json()["access_token"]
            H = {"Authorization": f"Bearer {tok}"}

            src = (await c.post("/api/v1/sources", headers=H, json={"name": "\u624b\u518c"})).json()
            sid = src["id"]
            async with SessionLocal() as s:
                scid = (await s.get(Source, sid)).sag_source_config_id

            # Inject one chunk (simulating the ingest output)
            await app.state.engine_manager.provision(scid)
            from alicecore.db import get_session_factory
            from alicecore.db.models import SourceChunk, SourceConfig

            chunk_id = uuid.uuid4().hex
            full_text = "\u5bfc\u51fa\u652f\u6301 Markdown / PDF / JSON\u3002" * 30  # \u8fdc\u8d85\u5f15\u7528\u9884\u89c8\u4e0a\u9650
            sf = get_session_factory()
            async with sf() as s:
                await s.merge(SourceConfig(id=scid, name="\u624b\u518c"))
                s.add(
                    SourceChunk(
                        id=chunk_id,
                        source_config_id=scid,
                        source_type="doc",
                        source_id="d1",
                        heading="\u5bfc\u51fa\u4e0e\u5907\u4efd",
                        content=full_text,
                    )
                )
                await s.commit()

            # The raw-text endpoint: the full content plus the sag source identity
            r = await c.get(f"/api/v1/sources/{sid}/chunks/{chunk_id}", headers=H)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["content"] == full_text and len(body["content"]) > 240
            assert body["heading"] == "\u5bfc\u51fa\u4e0e\u5907\u4efd"
            assert body["source_id"] == sid and body["source_name"] == "\u624b\u518c"

            # A missing id or a cross-source access -> 404
            assert (await c.get(f"/api/v1/sources/{sid}/chunks/{uuid.uuid4().hex}", headers=H)).status_code == 404

            # citations: source_id must be the sag source id (not the engine-internal one)
            section = RetrievedSection(
                chunk_id=chunk_id,
                heading="\u5bfc\u51fa\u4e0e\u5907\u4efd",
                content=full_text,
                score=0.9,
                source_id="engine-internal-id",
                source_config_id=scid,
            )
            cites = build_citations([section], {scid: {"id": sid, "name": "\u624b\u518c"}})
            assert cites[0]["source_id"] == sid
            assert cites[0]["source_name"] == "\u624b\u518c"
            assert cites[0]["snippet"].endswith("…") and len(cites[0]["snippet"]) <= 722
            assert "summary" not in cites[0]
