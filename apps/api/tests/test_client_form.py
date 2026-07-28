"""The v0.3 client-shape backend: the default agent, recent activity, the document raw-text endpoint. Fully offline."""

import httpx
import pytest

from sag_agent import ModelChunk
from sag_api.branding import DEFAULT_AGENT_AVATAR, DEFAULT_AGENT_NAME


class OfflineLLM:
    @property
    def configured(self):
        return True

    async def stream_turn(self, request, cancellation):
        yield ModelChunk(text_delta="ok", finish_reason="stop")

    async def complete(self, messages):
        return "summary"


async def _register(c, email):
    r = await c.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_default_agent_activity_and_document_file():
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Agent
    from sag_api.main import app
    from sag_api.services.agent_domain import resolve_sources

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            A = await _register(c, "clientform@t.com")

            # Simulate the complete old defaults; the official endpoint must migrate safely to the new identity.
            async with SessionLocal() as s:
                legacy = await s.scalar(select(Agent).where(Agent.is_default.is_(True)))
                legacy.name = "sag"
                legacy.avatar = "s"
                legacy.persona = {
                    "greeting": "\u6211\u5728\u3002\u4e0a\u4f20\u8d44\u6599\u5230\u77e5\u8bc6\u5e93\uff0c\u6216\u76f4\u63a5\u95ee\u6211\u4efb\u4f55\u95ee\u9898\u3002",  # legacy default persona kept as escapes on purpose
                    "system_prompt": "",
                }
                await s.commit()

            # The default agent: the endpoint is an idempotent get-or-create (a stable id), and the old defaults migrate automatically.
            a1 = (await c.get("/api/v1/agents/default", headers=A)).json()
            a2 = (await c.get("/api/v1/agents/default", headers=A)).json()
            assert a1["id"] == a2["id"] and a1["is_default"] is True
            assert a1["name"] == DEFAULT_AGENT_NAME and a1["avatar"] == DEFAULT_AGENT_AVATAR
            assert a1["persona"]["greeting"] == ""
            async with SessionLocal() as s:
                defaults = (
                    (await s.execute(select(Agent).where(Agent.is_default.is_(True))))
                    .scalars()
                    .all()
                )
                assert len(defaults) == 1

            # Knowledge base = every source: a newly created source is resolved without any binding
            src = (await c.post("/api/v1/sources", headers=A, json={"name": "Client source"})).json()
            async with SessionLocal() as s:
                agent = await s.get(Agent, a1["id"])
                sources = await resolve_sources(s, agent)
                assert any(x.id == src["id"] for x in sources)

            # Upload one document (offline: md is parsed and stored)
            up = await c.post(
                f"/api/v1/sources/{src['id']}/documents",
                headers=A,
                files={"file": ("hello.md", b"# T\n\nhello sag", "text/markdown")},
            )
            assert up.status_code in (200, 201), up.text
            doc = up.json()

            # Recent activity: it contains that document; a thread also appears once created
            t = (await c.post(f"/api/v1/agents/{a1['id']}/threads", headers=A, json={})).json()
            acts = (await c.get("/api/v1/activity", headers=A)).json()
            assert all(x["type"] == "document" for x in acts)  # activity = knowledge-base events, threads excluded
            assert any(x["id"] == doc["id"] for x in acts)
            assert acts == sorted(acts, key=lambda x: x["at"], reverse=True)

            scoped_acts = (
                await c.get(
                    "/api/v1/activity",
                    headers=A,
                    params=[("source_ids", src["id"]), ("source_ids", src["id"])],
                )
            ).json()
            assert any(x["id"] == doc["id"] for x in scoped_acts)
            assert all(x["source_id"] == src["id"] for x in scoped_acts)
            empty_acts = (
                await c.get(
                    "/api/v1/activity",
                    headers=A,
                    params={"source_ids": "missing-source"},
                )
            ).json()
            assert empty_acts == []

            # Archiving: PATCH -> it leaves the default list, appears in the archived=true list, and comes back on restore
            arch = await c.patch(
                f"/api/v1/agents/{a1['id']}/threads/{t['id']}",
                headers=A,
                json={"archived": True},
            )
            assert arch.status_code == 200 and arch.json()["archived"] is True
            live = (await c.get(f"/api/v1/agents/{a1['id']}/threads", headers=A)).json()
            assert all(x["id"] != t["id"] for x in live)
            gone = (
                await c.get(f"/api/v1/agents/{a1['id']}/threads?archived=true", headers=A)
            ).json()
            assert any(x["id"] == t["id"] for x in gone)
            back = await c.patch(
                f"/api/v1/agents/{a1['id']}/threads/{t['id']}",
                headers=A,
                json={"archived": False, "title": "Renamed thread"},
            )
            assert back.json()["archived"] is False and back.json()["title"] == "Renamed thread"

            # Image attachments: upload -> retrieve identical; a non-image gives 422; an image alone can also start an Agent run.
            png = bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                "0000000d4944415478da63fcffff3f030005fe02fea7568c4a0000000049454e44ae426082"
            )
            att = await c.post(
                "/api/v1/attachments", headers=A, files={"file": ("dot.png", png, "image/png")}
            )
            assert att.status_code == 201, att.text
            aid = att.json()["id"]
            got = await c.get(f"/api/v1/attachments/{aid}", headers=A)
            assert got.status_code == 200 and got.content == png
            bad = await c.post(
                "/api/v1/attachments", headers=A, files={"file": ("x.txt", b"nope", "text/plain")}
            )
            assert bad.status_code == 422
            app.state.llm = OfflineLLM()
            ask = await c.post(
                f"/api/v1/agents/{a1['id']}/threads/{t['id']}/ask",
                headers=A,
                json={"query": "", "attachments": [aid]},
            )
            assert ask.status_code == 200 and "event: run.completed" in ask.text
            msgs = (
                await c.get(f"/api/v1/agents/{a1['id']}/threads/{t['id']}/messages", headers=A)
            ).json()["items"]
            mine = [m for m in msgs if m["role"] == "user" and m["attachments"]]
            assert mine and mine[0]["attachments"][0]["id"] == aid

            # The scoped question parameters are accepted; the message delete endpoint
            scoped = await c.post(
                f"/api/v1/agents/{a1['id']}/threads/{t['id']}/ask",
                headers=A,
                json={"query": "search only this source", "source_ids": [src["id"]]},
            )
            assert scoped.status_code == 200 and "event: run.completed" in scoped.text
            gone_id = mine[0]["id"]
            rd = await c.delete(
                f"/api/v1/agents/{a1['id']}/threads/{t['id']}/messages/{gone_id}", headers=A
            )
            assert rd.status_code == 200
            after = (
                await c.get(f"/api/v1/agents/{a1['id']}/threads/{t['id']}/messages", headers=A)
            ).json()["items"]
            assert all(m["id"] != gone_id for m in after)

            # The raw-text endpoint: 200 with content matching the upload; a missing id -> 404
            f = await c.get(
                f"/api/v1/sources/{src['id']}/documents/{doc['id']}/file", headers=A
            )
            assert f.status_code == 200 and b"hello sag" in f.content
            preview = await c.get(
                f"/api/v1/sources/{src['id']}/documents/{doc['id']}/preview", headers=A
            )
            assert preview.status_code == 200 and "hello sag" in preview.text
            assert preview.headers["x-muse-source-encoding"] == "utf-8"
            nf = await c.get(
                f"/api/v1/sources/{src['id']}/documents/does-not-exist/file", headers=A
            )
            assert nf.status_code == 404
