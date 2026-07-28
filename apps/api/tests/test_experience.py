"""R4 experience: fallback wording injection, prompt transparency, the OpenAI-compatible endpoint.

Under the agent-first architecture empty_response is no longer a "short circuit that skips the LLM" but a wrap-up
instruction injected into the system prompt; the tests use a stub LLM (a direct answer, no tools) for a deterministic answer, fully offline.
"""

import json

import httpx
import pytest

from sag_agent import ModelChunk


class DirectLLM:
    """Direct-answer stub: the decision round asks for no tool and streams a fixed pair of tokens."""

    @property
    def configured(self):
        return True

    async def stream_turn(self, request, cancellation):
        for token in ["\u4f60\u597d", "\uff01"]:
            cancellation.raise_if_cancelled()
            yield ModelChunk(text_delta=token)
        yield ModelChunk(finish_reason="stop")

    async def complete(self, messages):
        return "summary"


async def _register(c, email="exp@t.com"):
    r = await c.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


EMPTY = "\u62b1\u6b49\uff0c\u6211\u6682\u65f6\u6ca1\u6709\u67e5\u5230\u76f8\u5173\u8d44\u6599\u3002"


async def _make_agent_with_empty_response(c, headers):
    agent = (
        await c.post(
            "/api/v1/agents",
            headers=headers,
            json={"name": "Careful assistant", "persona": {"empty_response": EMPTY}},
        )
    ).json()
    return agent


@pytest.mark.asyncio
async def test_empty_response_injection_and_prompt_preview():
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        app.state.llm = DirectLLM()
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            A = await _register(c)
            agent = await _make_agent_with_empty_response(c, A)
            thread = (await c.post(f"/api/v1/agents/{agent['id']}/threads", headers=A, json={})).json()

            events: list[tuple[str, dict]] = []
            async with c.stream(
                "POST",
                f"/api/v1/agents/{agent['id']}/threads/{thread['id']}/ask",
                headers=A,
                json={"query": "\u516c\u53f8\u7684\u62a5\u9500\u6d41\u7a0b\u662f\u600e\u6837\u7684\uff1f"},
            ) as resp:
                assert resp.status_code == 200
                ev = None
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        ev = line.split(":", 1)[1].strip()
                    elif line.startswith("data:") and ev:
                        events.append((ev, json.loads(line.split(":", 1)[1].strip())))

            kinds = [e for e, _ in events]
            assert "message.delta" in kinds and "run.completed" in kinds
            assert "run.failed" not in kinds
            completed = next(d["payload"] for e, d in events if e == "run.completed")
            assert completed["prompt_preview"]
            assert EMPTY in completed["prompt_preview"]
            assert "\u516c\u53f8\u7684\u62a5\u9500\u6d41\u7a0b\u662f\u600e\u6837\u7684\uff1f" in completed["prompt_preview"]
            assert "\u4f60\u597d\uff01" not in completed["prompt_preview"]
            tokens = "".join(d["payload"]["delta"] for e, d in events if e == "message.delta")
            assert tokens == "\u4f60\u597d\uff01"

            # This round's answer is persisted
            msgs = (await c.get(f"/api/v1/agents/{agent['id']}/threads/{thread['id']}/messages", headers=A)).json()[
                "items"
            ]
            saved = next(m for m in msgs if m["content"] == "\u4f60\u597d\uff01" and m["role"] == "assistant")
            assert saved["prompt_preview"] == completed["prompt_preview"]
            assert saved["content"] not in saved["prompt_preview"]


@pytest.mark.asyncio
async def test_openai_compatible_endpoint():
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        app.state.llm = DirectLLM()
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            A = await _register(c, "oai@t.com")
            agent = await _make_agent_with_empty_response(c, A)

            # Non-streaming: the standard OpenAI ChatCompletion shape
            r = await c.post(
                f"/api/v1/openai/{agent['id']}/chat/completions",
                headers=A,
                json={"messages": [{"role": "user", "content": "introduce the product pricing"}]},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["object"] == "chat.completion"
            assert body["choices"][0]["message"]["content"] == "\u4f60\u597d\uff01"
            assert body["choices"][0]["finish_reason"] == "stop"
            assert "sag" in body  # the extension field: citations

            # Streaming: SSE chunks + [DONE]
            chunks: list[str] = []
            async with c.stream(
                "POST",
                f"/api/v1/openai/{agent['id']}/chat/completions",
                headers=A,
                json={"messages": [{"role": "user", "content": "\u4f60\u597d"}], "stream": True},
            ) as resp:
                assert resp.status_code == 200
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        chunks.append(line[len("data:") :].strip())
            assert chunks[-1] == "[DONE]"
            content = "".join(
                json.loads(ch)["choices"][0]["delta"].get("content", "") for ch in chunks if ch != "[DONE]"
            )
            assert content == "\u4f60\u597d\uff01"

            # A missing user message -> 422
            bad = await c.post(
                f"/api/v1/openai/{agent['id']}/chat/completions",
                headers=A,
                json={"messages": [{"role": "system", "content": "x"}]},
            )
            assert bad.status_code == 422

            # Unauthenticated -> 401
            un = await c.post(
                f"/api/v1/openai/{agent['id']}/chat/completions",
                json={"messages": [{"role": "user", "content": "hi"}]},
            )
            assert un.status_code == 401
