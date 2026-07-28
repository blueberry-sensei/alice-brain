"""Agent HTTP e2e (offline): CRUD, source binding, threads, the ask guard, binding -> source resolution."""

import httpx
import pytest


@pytest.mark.asyncio
async def test_agents_flow_offline():
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Agent
    from sag_api.main import app
    from sag_api.services.agent_domain import resolve_sources

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            tok = (
                await c.post(
                    "/api/v1/auth/register", json={"email": "agent@x.com", "password": "password123"}
                )
            ).json()["access_token"]
            H = {"Authorization": f"Bearer {tok}"}

            src = (await c.post("/api/v1/sources", headers=H, json={"name": "Manual"})).json()
            scoped_src = (
                await c.post("/api/v1/sources", headers=H, json={"name": "Temporary scope"})
            ).json()

            # Create the agent
            r = await c.post(
                "/api/v1/agents",
                headers=H,
                json={"name": "Amo", "avatar": "A", "persona": {"system_prompt": "You are Amo."}},
            )
            assert r.status_code == 201
            agent = r.json()
            assert agent["name"] == "Amo"
            aid = agent["id"]

            # Bind the source
            b = await c.post(
                f"/api/v1/agents/{aid}/bindings",
                headers=H,
                json={"target_type": "source", "target_id": src["id"]},
            )
            assert b.status_code == 201
            assert len((await c.get(f"/api/v1/agents/{aid}/bindings", headers=H)).json()) == 1
            # A duplicate binding -> 409
            assert (
                await c.post(
                    f"/api/v1/agents/{aid}/bindings",
                    headers=H,
                    json={"target_type": "source", "target_id": src["id"]},
                )
            ).status_code == 409

            # Binding -> source resolution
            async with SessionLocal() as s:
                agent_obj = await s.get(Agent, aid)
                resolved = await resolve_sources(s, agent_obj)
                explicitly_scoped = await resolve_sources(s, agent_obj, [scoped_src["id"]])
            assert [x.id for x in resolved] == [src["id"]]
            assert [x.id for x in explicitly_scoped] == [scoped_src["id"]]

            # A configuration error before the run starts uses a standard HTTP error rather than posing as an SSE run.
            th = (await c.post(f"/api/v1/agents/{aid}/threads", headers=H, json={})).json()
            ask = await c.post(
                f"/api/v1/agents/{aid}/threads/{th['id']}/ask", headers=H, json={"query": "hello"}
            )
            assert ask.status_code == 400
            assert ask.json()["error"]["code"] == "configuration_error"

            # List + delete (a shared test database -> assert existence rather than an exact count)
            assert any(a["id"] == aid for a in (await c.get("/api/v1/agents", headers=H)).json())
            assert (await c.delete(f"/api/v1/agents/{aid}", headers=H)).status_code == 200
            assert not any(a["id"] == aid for a in (await c.get("/api/v1/agents", headers=H)).json())
