"""HTTP layer smoke test: runs the real ASGI app (lifespan and background queue included), fully offline."""

import httpx
import pytest


@pytest.mark.asyncio
async def test_end_to_end_offline():
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            # System
            assert (await c.get("/api/v1/system/health")).json()["status"] == "ok"
            caps = (await c.get("/api/v1/system/capabilities")).json()
            assert caps["llm_configured"] is False

            # Authentication: register the single account
            r = await c.post(
                "/api/v1/auth/register",
                json={"email": "a@b.com", "password": "password123", "name": "Ada"},
            )
            assert r.status_code == 201
            tok = r.json()["access_token"]
            H = {"Authorization": f"Bearer {tok}"}

            assert (await c.get("/api/v1/auth/me", headers=H)).json()["email"] == "a@b.com"
            assert (await c.get("/api/v1/auth/me")).status_code == 401
            local_login = await c.post("/api/v1/auth/login", json={"name": "Ada"})
            assert local_login.status_code == 200
            assert local_login.json()["user"]["id"] == r.json()["user"]["id"]
            login = await c.post(
                "/api/v1/auth/login",
                json={"name": "Ada", "email": "a@b.com", "password": "password123"},
            )
            assert login.status_code == 200
            assert login.json()["user"]["id"] == r.json()["user"]["id"]
            dup = await c.post(
                "/api/v1/auth/register", json={"email": "a@b.com", "password": "password123"}
            )
            assert dup.status_code == 409

            # Connectors + sources
            conns = (await c.get("/api/v1/sources/connectors", headers=H)).json()
            assert any(x["kind"] == "file_upload" for x in conns)

            r = await c.post("/api/v1/sources", headers=H, json={"name": "Manual"})
            assert r.status_code == 201
            sid = r.json()["id"]
            assert r.json()["source_type"] == "document"
            # A shared test database -> locate by existence or id rather than an exact count
            def _find(sources):
                return next(s for s in sources if s["id"] == sid)

            assert _find((await c.get("/api/v1/sources", headers=H)).json())["id"] == sid

            # Upload (without waiting for the background job, so a 401 retry does not slow the test)
            up = await c.post(
                f"/api/v1/sources/{sid}/documents",
                headers=H,
                files={"file": ("a.md", b"# T\n\nhello world\n", "text/markdown")},
            )
            assert up.status_code == 201 and up.json()["status"] == "pending"
            assert _find((await c.get("/api/v1/sources", headers=H)).json())["document_count"] == 1

            # The unified write endpoint: pushing a batch of messages -> normalised into a document entering the pipeline
            ing = await c.post(
                f"/api/v1/sources/{sid}/documents/ingest",
                headers=H,
                json={"messages": [{"author": "Alex", "text": "what time is the review tomorrow?", "ts": "2026-07-07T09:00Z"}]},
            )
            assert ing.status_code == 201 and ing.json()["status"] == "pending"
            assert _find((await c.get("/api/v1/sources", headers=H)).json())["document_count"] == 2

            # Global search: offline (no embedding), a single source failure is swallowed and it returns 200 with an empty result
            gs = await c.post("/api/v1/search", headers=H, json={"query": "hello"})
            assert gs.status_code == 200
            body = gs.json()
            assert body["query"] == "hello" and isinstance(body["sections"], list)
            # Narrowed to the given source
            gs2 = await c.post(
                "/api/v1/search", headers=H, json={"query": "hello", "source_ids": [sid]}
            )
            assert gs2.status_code == 200
