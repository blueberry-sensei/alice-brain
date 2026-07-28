"""Agent tool loop: a stub LLM triggers tool_call -> dispatch the tool -> aggregate citations -> wrap up.

FakeLLM (configured=True + a scripted stream_turn) drives the loop deterministically offline; a test echo tool
is registered to verify "dispatch + citation backfill". It goes through the **stateless OpenAI endpoint** (thread_id=None) to
avoid the DB contention a background memory task (engine build) would cause. Backwards compatibility is covered by test_agents/test_experience.
"""

import json
from types import SimpleNamespace

import httpx
import pytest

from sag_agent import ModelChunk, ToolCall
from sag_api.sag import GraphEventInfo, RetrievedSection, SearchOutcome, SourceGraphInfo
from sag_api.services.agent_service import _adapt_tool, _enabled_tool_names
from sag_api.tools import registry
from sag_api.tools.base import Tool, ToolContext, ToolMeta, ToolResult
from sag_api.tools.builtin import GetTimeTool, OpenWebPageTool, SearchContextTool, WebSearchTool

ECHO_CITATION = {
    "n": 1,
    "chunk_id": "c1",
    "heading": "H",
    "snippet": "S",
    "score": 0.9,
    "source_id": "src",
    "source_name": "Echo source",
}


class EchoTool(Tool):
    meta = ToolMeta(
        name="echo",
        description="Test tool: echo the arguments and attach one citation.",
        parameters={"type": "object", "properties": {"q": {"type": "string"}}},
    )

    async def invoke(self, args, ctx):
        return ToolResult(content=f"echoed:{args.get('q', '')}", citations=[ECHO_CITATION])


registry.register(EchoTool())


class ExternalEvidenceTool(Tool):
    meta = ToolMeta(
        name="external_evidence",
        description="Test tool: search external material and return a traceable web source.",
        parameters={"type": "object", "properties": {}},
    )

    async def invoke(self, args, ctx):
        del args, ctx
        return ToolResult(
            content="The official release confirmed the update.",
            data={
                "external_references": [
                    {
                        "title": "Official release",
                        "url": "https://example.com/official-release",
                        "source": "example.com",
                        "snippet": "The official release confirms the update.",
                    },
                    {
                        "title": "Duplicate release",
                        "url": "https://example.com/official-release#summary",
                        "source": "reader",
                    },
                    {
                        "title": "Unsafe result",
                        "url": "javascript:alert(1)",
                        "source": "untrusted",
                    },
                ]
            },
        )


registry.register(ExternalEvidenceTool())


class StubWebSearchTool(Tool):
    meta = ToolMeta(
        name="web_search",
        description="Test tool: return one internet search result.",
        parameters={"type": "object", "properties": {}},
    )

    async def invoke(self, args, ctx):
        del args, ctx
        return ToolResult(
            content="Page 1: Guangzhou weather forecast",
            data={"section_count": 1},
        )


@pytest.mark.asyncio
async def test_search_tool_prefers_exact_body_window_over_semantic_boilerplate():
    class HybridEngine:
        graph_calls = 0

        async def search_many(self, targets, query, *, strategy=None, top_k=None):
            assert strategy == "vector"
            return SearchOutcome(
                query=query,
                sections=[
                    RetrievedSection(
                        chunk_id="nav",
                        heading="\u7248\u6743\u58f0\u660e",
                        content="\u65b0\u6d6a\u9996\u9875 \u9605\u8bfb\u6392\u884c\u699c \u8bc4\u8bba\u6392\u884c\u699c",
                        score=0.64,
                        source_config_id="sc-1",
                    )
                ],
            )

        async def grep_chunks(self, source_config_id, pattern, *, source=None, limit=20):
            assert pattern == "\u6797\u4fca\u6770"
            return [
                {
                    "chunk_id": "body",
                    "heading": "\u6797\u4fca\u6770\u5b98\u5ba3\u604b\u60c5",
                    "snippet": "12\u670829\u65e5\u665a\u6797\u4fca\u6770\u5b98\u5ba3\u604b\u60c5\uff0c\u4e0e\u5973\u53cb\u4e03\u4e03\u76f8\u5dee21\u5c81\u3002",
                }
            ]

        async def graph_for_sections(self, sections, sources_by_config, **kwargs):
            self.graph_calls += 1
            assert kwargs["event_limit"] == max(12, len(sections))
            assert sources_by_config["sc-1"].id == "source-1"
            return SimpleNamespace(
                events=[
                    SimpleNamespace(
                        id="event-1",
                        source_config_id="sc-1",
                        chunk_id=sections[0].chunk_id,
                        title="\u6797\u4fca\u6770\u5b98\u5ba3\u604b\u60c5",
                        summary="\u6797\u4fca\u6770\u4e8e 12 \u6708 29 \u65e5\u516c\u5f00\u604b\u60c5\u3002",
                        content="12 \u6708 29 \u65e5\uff0c\u6797\u4fca\u6770\u516c\u5f00\u786e\u8ba4\u604b\u60c5\uff0c\u5e76\u4ecb\u7ecd\u53cc\u65b9\u4ea4\u5f80\u60c5\u51b5\u3002",
                        category="\u5a31\u4e50",
                        score=0.95,
                    )
                ],
                entities=[],
                associations=[],
            )

    engine = HybridEngine()
    source = SimpleNamespace(id="source-1", name="\u5a31\u4e50\u65b0\u95fb", sag_source_config_id="sc-1")
    host_context = ToolContext(engine_manager=engine, sources=[source])
    result = await SearchContextTool().invoke(
        {"query": "\u5173\u4e8e\u6797\u4fca\u6770\u6700\u65b0\u52a8\u6001 2024 2025", "top_k": 4},
        host_context,
    )

    assert result.citations[0]["chunk_id"] == "body"
    assert result.citations[0]["event_refs"][0]["title"] == "\u6797\u4fca\u6770\u5b98\u5ba3\u604b\u60c5"
    assert result.citations[0]["event_refs"][0]["content"].startswith("12 \u6708 29 \u65e5")
    assert "summary" not in result.citations[0]
    assert "12\u670829\u65e5\u665a" in result.content
    assert result.data["lexical_count"] == 1
    assert result.data["section_count"] == 1
    assert result.data["_graph"] is not None
    assert result.data["_graph"].events[0].id == "event-1"
    assert engine.graph_calls == 1

    # The runtime adapter must reuse SearchContextTool's graph result instead
    # of issuing a second graph query while constructing universe artifacts.
    collected_citations: list[dict] = []
    adapter_engine = HybridEngine()
    adapter_context = ToolContext(engine_manager=adapter_engine, sources=[source])
    adapted = _adapt_tool(SearchContextTool(), adapter_context, collected_citations)
    runtime_result = await adapted.execute(
        {"query": "\u5173\u4e8e\u6797\u4fca\u6770\u6700\u65b0\u52a8\u6001 2024 2025", "top_k": 4},
        SimpleNamespace(
            cancellation=SimpleNamespace(raise_if_cancelled=lambda: None),
        ),
    )

    assert adapter_engine.graph_calls == 1
    assert collected_citations[0]["event_refs"][0]["id"] == "event-1"
    assert runtime_result.details["sources"] == [{"id": "source-1", "name": "\u5a31\u4e50\u65b0\u95fb"}]
    assert runtime_result.artifacts["citations"][0]["event_refs"][0]["summary"] == ("\u6797\u4fca\u6770\u4e8e 12 \u6708 29 \u65e5\u516c\u5f00\u604b\u60c5\u3002")
    assert runtime_result.artifacts["citations"][0]["event_refs"][0]["content"].startswith(
        "12 \u6708 29 \u65e5"
    )
    assert runtime_result.details["matches"][0]["event_refs"][0]["category"] == "\u5a31\u4e50"


@pytest.mark.asyncio
async def test_web_search_trace_uses_internet_scope_instead_of_mounted_knowledge_sources():
    mounted_sources = [
        SimpleNamespace(id="source-1", name="\u897f\u6e38\u8bb0"),
        SimpleNamespace(id="source-2", name="SAG"),
    ]
    adapted = _adapt_tool(
        StubWebSearchTool(),
        ToolContext(engine_manager=SimpleNamespace(), sources=mounted_sources),
        [],
    )

    result = await adapted.execute(
        {"query": "\u5e7f\u5dde\u660e\u5929\u5929\u6c14"},
        SimpleNamespace(cancellation=SimpleNamespace(raise_if_cancelled=lambda: None)),
    )

    assert result.details["scope"] == "internet"
    assert "sources" not in result.details


@pytest.mark.asyncio
async def test_search_tool_graph_capacity_covers_every_returned_section():
    class ManySectionEngine:
        graph_calls = 0
        event_limit = 0

        async def search_many(self, targets, query, *, strategy=None, top_k=None):
            source_config_id = targets[0][0]
            return SearchOutcome(
                query=query,
                sections=[
                    RetrievedSection(
                        chunk_id=f"chunk-{index}",
                        heading=f"\u5171\u540c\u4e3b\u9898 {index}",
                        content=f"\u5171\u540c\u4e3b\u9898\u7684\u53ef\u6838\u9a8c\u8bc1\u636e {index}",
                        score=1.0 - index / 100,
                        source_config_id=source_config_id,
                    )
                    for index in range(20)
                ],
            )

        async def graph_for_sections(self, sections, sources_by_config, **kwargs):
            self.graph_calls += 1
            self.event_limit = kwargs["event_limit"]
            return SourceGraphInfo(
                events=[
                    GraphEventInfo(
                        id=f"event-{index}",
                        source_id="document-1",
                        source_config_id=section.source_config_id or "",
                        chunk_id=section.chunk_id,
                        title=f"\u771f\u5b9e\u4e8b\u4ef6 {index}",
                        summary=f"\u771f\u5b9e\u4e8b\u4ef6\u6458\u8981 {index}",
                        category="\u6d4b\u8bd5",
                    )
                    for index, section in enumerate(sections)
                ]
            )

    engine = ManySectionEngine()
    source = SimpleNamespace(id="source-1", name="\u6d4b\u8bd5\u8d44\u6599", sag_source_config_id="sc-1")
    result = await SearchContextTool().invoke(
        {"query": "\u5171\u540c\u4e3b\u9898", "top_k": 20},
        ToolContext(engine_manager=engine, sources=[source]),
    )

    assert len(result.citations) == 20
    assert engine.graph_calls == 1
    assert engine.event_limit == 20
    assert all(len(citation["event_refs"]) == 1 for citation in result.citations)


@pytest.mark.asyncio
async def test_search_tool_reuses_direct_event_recall_and_loads_traceable_evidence():
    class SparseEventEngine:
        event_score_calls = 0
        chunk_reads: list[tuple[str, str]] = []

        async def search_many(self, targets, query, *, strategy=None, top_k=None):
            return SearchOutcome(
                query=query,
                sections=[
                    RetrievedSection(
                        chunk_id="nearby-chunk",
                        heading="\u5386\u53f2",
                        content="\u5e1d\u56fd\u53d1\u5c55\u76f8\u5173\u7684\u80cc\u666f\u8d44\u6599\uff0c\u4f46\u8fd9\u4e2a\u5206\u5757\u672c\u8eab\u6ca1\u6709\u62bd\u53d6\u4e8b\u9879\u3002",
                        score=0.81,
                        source_config_id=targets[0][0],
                    )
                ],
            )

        async def search_event_scores(self, query, sources_by_config, *, limit=None):
            self.event_score_calls += 1
            assert query == "\u5e1d\u56fd\u53d1\u5c55"
            assert sources_by_config["sc-1"].id == "source-1"
            assert limit == 2
            return {("sc-1", "event-direct"): 0.97}

        async def graph_for_sections(self, sections, sources_by_config, **kwargs):
            assert kwargs["event_scores"] == {("sc-1", "event-direct"): 0.97}
            return SourceGraphInfo(
                events=[
                    GraphEventInfo(
                        id="event-direct",
                        source_id="document-1",
                        source_config_id="sc-1",
                        chunk_id="event-chunk",
                        title="\u5e1d\u56fd\u8fd0\u8f6c\u7684\u4fe1\u606f\u9700\u6c42\u4e0e\u5927\u8111\u5b58\u50a8\u5c40\u9650",
                        summary="\u5e1d\u56fd\u4f9d\u8d56\u5927\u89c4\u6a21\u4fe1\u606f\u5904\u7406\u4f53\u7cfb\u7ef4\u6301\u6269\u5f20\u4e0e\u6cbb\u7406\u3002",
                        category="\u5386\u53f2",
                        score=0.97,
                    )
                ]
            )

        async def get_chunk(self, source_config_id, chunk_id, *, source=None):
            self.chunk_reads.append((source_config_id, chunk_id))
            assert source.id == "source-1"
            return SimpleNamespace(
                chunk_id=chunk_id,
                heading="\u5386\u53f2",
                content="\u7ef4\u6301\u590d\u6742\u793e\u4f1a\u79e9\u5e8f\u9700\u8981\u5b58\u50a8\u5e76\u5904\u7406\u5927\u91cf\u884c\u653f\u4fe1\u606f\u3002",
                rank=12,
            )

    engine = SparseEventEngine()
    source = SimpleNamespace(id="source-1", name="\u4eba\u7c7b\u7b80\u53f2", sag_source_config_id="sc-1")
    result = await SearchContextTool().invoke(
        {"query": "\u5e1d\u56fd\u53d1\u5c55", "top_k": 2},
        ToolContext(engine_manager=engine, sources=[source]),
    )

    assert engine.event_score_calls == 1
    assert engine.chunk_reads == [("sc-1", "event-chunk")]
    assert result.data["event_candidates"] == 1
    assert result.data["event_count"] == 1
    assert result.citations[0]["chunk_id"] == "event-chunk"
    assert result.citations[0]["source_id"] == "source-1"
    assert result.citations[0]["event_refs"] == [
        {
            "id": "event-direct",
            "title": "\u5e1d\u56fd\u8fd0\u8f6c\u7684\u4fe1\u606f\u9700\u6c42\u4e0e\u5927\u8111\u5b58\u50a8\u5c40\u9650",
            "summary": "\u5e1d\u56fd\u4f9d\u8d56\u5927\u89c4\u6a21\u4fe1\u606f\u5904\u7406\u4f53\u7cfb\u7ef4\u6301\u6269\u5f20\u4e0e\u6cbb\u7406\u3002",
            "category": "\u5386\u53f2",
        }
    ]
    assert result.content.startswith("[1] Event: ")
    assert "Summary: " in result.content
    assert "Source evidence:\n" in result.content


def test_visible_sources_mount_builtin_knowledge_tools():
    agent = SimpleNamespace(persona={}, is_default=False)
    assert _enabled_tool_names(agent, has_sources=True) == [
        "get_time",
        "search_context",
        "get_entity",
        "web_search",
        "open_webpage",
    ]


@pytest.mark.asyncio
async def test_web_search_is_unavailable_without_a_search_provider(monkeypatch):
    """Không còn provider search nào được gắn cứng: tool luôn báo chưa cấu hình.

    Kể cả khi LLM đã có key đầy đủ, `_web_search_endpoint()` trả None nên tool
    phải tự tắt thay vì gọi ra một dịch vụ bên thứ ba nào đó.
    """
    from sag_api.core.config import settings

    monkeypatch.setattr(settings, "llm_base_url", "https://api.openai.com/v1")
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")

    assert WebSearchTool.configured() is False
    result = await WebSearchTool().invoke(
        {"query": "\u6700\u65b0\u6d88\u606f"},
        ToolContext(engine_manager=SimpleNamespace()),
    )
    assert result.data["section_count"] == 0
    assert "No web search provider is configured" in result.content


@pytest.mark.asyncio
async def test_open_web_page_extracts_public_html_as_traceable_evidence(monkeypatch):
    from sag_api.tools import builtin

    original_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://weather.example/guangzhou"
        assert request.headers["user-agent"].startswith("alice-bot/")
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                "<html><head><title>\u5e7f\u5dde\u5929\u6c14\u9884\u62a5</title></head>"
                "<body><main><h1>7\u670815\u65e5</h1><p>\u5e7f\u5dde\u6709\u96f7\u9635\u96e8\uff0c\u6700\u9ad8\u6e29 32℃\u3002</p></main></body></html>"
            ),
        )

    async def allow_test_url(url: str) -> str:
        return url

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(builtin, "_validated_public_web_url", allow_test_url)
    monkeypatch.setattr(
        builtin.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )

    result = await OpenWebPageTool().invoke(
        {"url": "https://weather.example/guangzhou"},
        ToolContext(engine_manager=SimpleNamespace()),
    )

    assert "\u5e7f\u5dde\u6709\u96f7\u9635\u96e8" in result.content
    assert result.data["section_count"] == 1
    assert result.data["external_references"][0]["url"] == ("https://weather.example/guangzhou")


@pytest.mark.asyncio
async def test_open_web_page_rejects_private_network_targets():
    with pytest.raises(RuntimeError, match="public web addresses"):
        await OpenWebPageTool().invoke(
            {"url": "http://127.0.0.1:8000/api/v1/system/health"},
            ToolContext(engine_manager=SimpleNamespace()),
        )


@pytest.mark.asyncio
async def test_get_time_uses_system_timezone_and_returns_utc_instant(monkeypatch):
    from sag_api.core.config import settings

    monkeypatch.setattr(settings, "timezone", "Asia/Shanghai")
    result = await GetTimeTool().invoke(
        {},
        ToolContext(engine_manager=SimpleNamespace(), sources=[]),
    )
    assert result.data["ok"] is True
    assert result.data["timezone"] == "Asia/Shanghai"
    assert result.data["utc_offset"] == "+08:00"
    assert result.data["local_iso"].endswith("+08:00")
    assert result.data["utc_iso"].endswith("+00:00")


class FakeLLM:
    """Scripted: the first round asks for the echo tool, the second wraps up; the final answer streams in two tokens."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def configured(self) -> bool:
        return True

    async def stream_turn(self, request, cancellation):
        self.calls += 1
        has_echo = any(tool.get("function", {}).get("name") == "echo" for tool in request.tools)
        if self.calls == 1 and has_echo:
            yield ModelChunk(
                tool_calls=(ToolCall(id="call_1", name="echo", arguments={"q": "hi"}),),
                finish_reason="tool_calls",
            )
            return
        for token in ["final ", "answer"]:
            cancellation.raise_if_cancelled()
            yield ModelChunk(text_delta=token)
        yield ModelChunk(finish_reason="stop")


class ExternalEvidenceLLM:
    """Call an external tool, then omit its URL to exercise run-level mapping."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def configured(self) -> bool:
        return True

    async def stream_turn(self, request, cancellation):
        self.calls += 1
        if self.calls == 1:
            yield ModelChunk(
                tool_calls=(ToolCall(id="external-1", name="external_evidence", arguments={}),),
                finish_reason="tool_calls",
            )
            return
        cancellation.raise_if_cancelled()
        yield ModelChunk(text_delta="Verified update.", finish_reason="stop")


async def _register(c, email):
    r = await c.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_agent_tool_loop_dispatch_and_citations():
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        app.state.llm = FakeLLM()  # replaced by the stub (for this case only)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            A = await _register(c, "agenttools@t.com")
            # Enable the extra echo tool; no source is bound -> the engine is not touched and the loop still runs
            agent = (
                await c.post(
                    "/api/v1/agents",
                    headers=A,
                    json={"name": "Tool assistant", "persona": {"tools": ["echo"]}},
                )
            ).json()

            r = await c.post(
                f"/api/v1/openai/{agent['id']}/chat/completions",
                headers=A,
                json={"messages": [{"role": "user", "content": "hello"}]},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            # The tool loop finished -> the final answer
            assert body["choices"][0]["message"]["content"] == "final answer"
            # The tool was dispatched (echo ran) -> its citation is aggregated into sag.citations
            assert any(c.get("source_name") == "Echo source" for c in body["sag"]["citations"])
            # The unified provider protocol takes exactly two rounds (the tool decision and the wrap-up)
            assert app.state.llm.calls == 2


@pytest.mark.asyncio
async def test_agent_external_tool_returns_structured_citation_when_model_omits_url():
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        app.state.llm = ExternalEvidenceLLM()
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            A = await _register(c, "externalrefs@t.com")
            agent = (
                await c.post(
                    "/api/v1/agents",
                    headers=A,
                    json={"name": "External material assistant", "persona": {"tools": ["external_evidence"]}},
                )
            ).json()

            response = await c.post(
                f"/api/v1/openai/{agent['id']}/chat/completions",
                headers=A,
                json={"messages": [{"role": "user", "content": "please search and verify the update"}]},
            )

            assert response.status_code == 200, response.text
            content = response.json()["choices"][0]["message"]["content"]
            assert content == "Verified update."
            assert response.json()["sag"]["citations"] == [
                {
                    "kind": "external",
                    "n": 1,
                    "url": "https://example.com/official-release",
                    "title": "Official release",
                    "source": "example.com",
                    "mapped": False,
                    "claim_level": "run",
                    "summary": "The official release confirms the update.",
                    "snippet": "The official release confirms the update.",
                }
            ]

            app.state.llm = ExternalEvidenceLLM()
            chunks: list[str] = []
            async with c.stream(
                "POST",
                f"/api/v1/openai/{agent['id']}/chat/completions",
                headers=A,
                json={
                    "messages": [{"role": "user", "content": "please search and verify the update"}],
                    "stream": True,
                },
            ) as streamed:
                assert streamed.status_code == 200
                async for line in streamed.aiter_lines():
                    if line.startswith("data:"):
                        chunks.append(line[len("data:") :].strip())
            streamed_content = "".join(
                json.loads(item)["choices"][0]["delta"].get("content", "") for item in chunks if item != "[DONE]"
            )
            assert streamed_content == "Verified update."

            # Stateful SSE exposes the same structured citation and persists it
            # for history playback without patching a source footer into prose.
            thread = (await c.post(f"/api/v1/agents/{agent['id']}/threads", headers=A, json={})).json()
            app.state.llm = ExternalEvidenceLLM()
            ask = await c.post(
                f"/api/v1/agents/{agent['id']}/threads/{thread['id']}/ask",
                headers=A,
                json={"query": "please search and verify the update", "web_enabled": True},
            )
            assert ask.status_code == 200, ask.text
            event_name = ""
            completed = None
            for line in ask.text.splitlines():
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and event_name == "run.completed":
                    completed = json.loads(line.split(":", 1)[1].strip())["payload"]
            assert completed is not None
            assert completed["citations"] == response.json()["sag"]["citations"]

            messages = (
                await c.get(
                    f"/api/v1/agents/{agent['id']}/threads/{thread['id']}/messages",
                    headers=A,
                )
            ).json()["items"]
            saved = next(message for message in messages if message["role"] == "assistant")
            assert saved["citations"] == completed["citations"]
            assert saved["citations"][0]["kind"] == "external"
            assert "javascript:" not in json.dumps(saved["citations"])


@pytest.mark.asyncio
async def test_no_tools_agent_uses_one_model_turn():
    """No-tool agents use the same provider protocol with exactly one model turn."""
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        fake = FakeLLM()
        app.state.llm = fake
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            A = await _register(c, "plainagent@t.com")
            agent = (await c.post("/api/v1/agents", headers=A, json={"name": "Plain assistant"})).json()
            r = await c.post(
                f"/api/v1/openai/{agent['id']}/chat/completions",
                headers=A,
                json={"messages": [{"role": "user", "content": "are you there"}]},
            )
            assert r.status_code == 200, r.text
            assert r.json()["choices"][0]["message"]["content"] == "final answer"
            assert fake.calls == 1
