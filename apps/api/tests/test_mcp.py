"""Three MCP concerns, fully offline:

1. A source is an MCP endpoint - list and call search/get_entity/get_chunk through the in-process memory client (an empty database -> a structured result).
2. A remote MCP tool adapted into a sag `Tool` (a namespace prefix + a call_tool round trip).
3. The binding and description endpoints - validating an agent mounting an external MCP, and a source's MCP connection details.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session as connect

from sag_api.mcp.server import MCP_TOOL_DETAILS, MCP_TOOL_NAMES, build_source_mcp, use_scope
from sag_api.tools import mcp as mcp_module
from sag_api.tools import registry
from sag_api.tools.base import Tool, ToolContext, ToolMeta, ToolResult
from sag_api.tools.mcp import (
    MCPTool,
    MCPToolExecutionError,
    _clean_url,
    open_agent_mcp_tools,
    tools_from_session,
)


async def _register(c, email):
    r = await c.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_source_mcp_lists_and_calls_tools_over_engine():
    """The knowledge-base MCP server: a real engine with whole-library scope, where the exploration and search tools all work."""
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Source
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            A = await _register(c, "mcpsrv@t.com")
            src = (await c.post("/api/v1/sources", headers=A, json={"name": "MCP \u6e90"})).json()
            src2 = (await c.post("/api/v1/sources", headers=A, json={"name": "\u7b2c\u4e8c\u4e2a MCP \u6e90"})).json()
            async with SessionLocal() as s:
                sources = tuple(
                    (
                        await s.execute(
                            select(Source)
                            .where(Source.id.in_([src["id"], src2["id"]]))
                            .order_by(Source.created_at, Source.id)
                        )
                    )
                    .scalars()
                    .all()
                )

            mcp = build_source_mcp()
            # The scope must be set before connect (which starts the service task), because the task copies the context carrying the scope
            with use_scope(app.state.engine_manager, sources):
                async with connect(mcp) as client:
                    await client.initialize()
                    listed = await client.list_tools()
                    tools_by_name = {tool.name: tool for tool in listed.tools}
                    names = set(tools_by_name)
                    assert {
                        "search",
                        "get_entity",
                        "get_chunk",
                        "list_sources",
                        "list_documents",
                        "outline",
                        "grep",
                        "read",
                    } <= names
                    for detail in MCP_TOOL_DETAILS:
                        tool = tools_by_name[detail["name"]]
                        assert tool.title == detail["label"]
                        assert tool.description == detail["description"]
                        assert tool.annotations is not None
                        # ask_sub_agent spends an external request; log_agent_task writes
                        # telemetry. Both must say so instead of posing as read-only.
                        assert tool.annotations.readOnlyHint is (
                            detail["name"] not in {"ask_sub_agent", "log_agent_task"}
                        )
                        assert tool.annotations.destructiveHint is False
                    search_properties = tools_by_name["search"].inputSchema["properties"]
                    assert search_properties["query"]["description"]
                    assert search_properties["source_id"]["description"]

                    r_sources = await client.call_tool("list_sources", {})
                    assert "MCP \u6e90" in r_sources.content[0].text
                    assert "\u7b2c\u4e8c\u4e2a MCP \u6e90" in r_sources.content[0].text

                    # Exploration primitives (offline): upload one md first
                    up = await c.post(
                        f"/api/v1/sources/{src['id']}/documents",
                        headers=A,
                        files={"file": ("probe.md", b"# Title\n\nhello mcp world", "text/markdown")},
                    )
                    doc = up.json()
                    r_ls = await client.call_tool("list_documents", {})
                    assert "probe.md" in r_ls.content[0].text
                    r_read = await client.call_tool("read", {"document_id": doc["id"]})
                    assert "hello mcp world" in r_read.content[0].text
                    r_out = await client.call_tool("outline", {"document_id": doc["id"]})
                    assert isinstance(r_out.content[0].text, str)  # still processing -> a placeholder string is fine too
                    r_grep = await client.call_tool("grep", {"pattern": "\u4e0d\u5b58\u5728\u7684\u4e32xyz", "source_id": src["id"]})
                    assert "nothing matched" in r_grep.content[0].text or "chunk_id" in r_grep.content[0].text

                    r_chunk = await client.call_tool(
                        "get_chunk", {"chunk_id": "does-not-exist", "source_id": src["id"]}
                    )
                    assert not r_chunk.isError
                    assert "not found" in r_chunk.content[0].text

                    r_entity = await client.call_tool("get_entity", {"name": "\u67e5\u65e0\u6b64\u5b9e\u4f53", "source_id": src["id"]})
                    assert not r_entity.isError
                    assert "not found" in r_entity.content[0].text

                    # Search goes through the real engine (offline, SAG needs an LLM to extract entities -> a structured error);
                    # what matters is that the tool dispatches correctly and returns a structured MCP response without crashing the server
                    r_search = await client.call_tool("search", {"query": "\u4efb\u610f\u95ee\u9898", "source_id": src["id"]})
                    assert r_search.content and isinstance(r_search.content[0].text, str)


@pytest.mark.asyncio
async def test_mcp_lists_registry_and_executes_sub_agent_with_verified_telemetry(
    monkeypatch: pytest.MonkeyPatch,
):
    from sag_api.core.telemetry import set_agent_event_sink
    from sag_api.services import sub_agent_execution

    events = []

    async def fake_list(_session):
        return [
            {
                "provider": "opencode-zen",
                "display_name": "OpenCode ZEN",
                "provider_name": "",
                "model": "opencode/deepseek-v4-flash-free",
                "credential_set": True,
                "model_verified": True,
                "callable": True,
                "error": None,
            }
        ]

    async def fake_invoke(
        _session,
        provider,
        task,
        *,
        context="",
        actor="unknown",
    ):
        assert provider == "opencode-zen"
        assert task == "Review auth boundary"
        assert context == "source excerpt"
        assert actor == "claude-code"
        return sub_agent_execution.SubAgentResult(
            provider=provider,
            display_name="OpenCode ZEN",
            model="opencode/deepseek-v4-flash-free",
            content="Do not trust the client-provided owner id.",
            input_tokens=20,
            output_tokens=9,
            total_tokens=29,
        )

    async def capture(record):
        events.append(record)

    monkeypatch.setattr(sub_agent_execution, "list_available_sub_agents", fake_list)
    monkeypatch.setattr(sub_agent_execution, "invoke_sub_agent", fake_invoke)
    set_agent_event_sink(capture)
    try:
        mcp = build_source_mcp()
        with use_scope(SimpleNamespace(), (), actor="claude-code", transport="stdio"):
            async with connect(mcp) as client:
                await client.initialize()
                registry = await client.call_tool("list_sub_agents", {})
                delegated = await client.call_tool(
                    "ask_sub_agent",
                    {
                        "provider": "opencode-zen",
                        "task": "Review auth boundary",
                        "context": "source excerpt",
                    },
                )
    finally:
        set_agent_event_sink(None)

    assert "callable=yes" in registry.content[0].text
    assert "Do not trust the client-provided owner id." in delegated.content[0].text
    assert [event.kind for event in events] == ["sub_agent_registry", "delegation"]
    assert events[1].tool == "opencode-zen"
    assert events[1].detail["status"] == "done"
    assert events[1].detail["input_tokens"] == 20


@pytest.mark.asyncio
async def test_remote_mcp_tool_adapted_as_sag_tool():
    """A remote MCP tool -> MCPTool: a namespace prefix and an invoke round trip returning text."""
    stub = FastMCP("stub")

    @stub.tool(description="echo the input")
    async def echo(text: str) -> str:
        return f"echo:{text}"

    async with connect(stub) as client:
        await client.initialize()
        tools = await tools_from_session(client, namespace="stub")
        assert len(tools) == 1
        tool = tools[0]
        assert tool.meta.name == "mcp__stub__echo"
        assert tool.meta.parameters.get("type") == "object"
        result = await tool.invoke({"text": "hi"}, ToolContext(engine_manager=None))
        assert result.content == "echo:hi"
        assert result.data == {"external_references": []}


class _StubCallSession:
    def __init__(self, result):
        self.result = result

    async def call_tool(self, name, args):
        return self.result


def _adapt_stub_result(result) -> MCPTool:
    return MCPTool(
        _StubCallSession(result),
        remote_name="lookup",
        local_name="mcp__stub__lookup",
        description="stub",
        parameters={"type": "object", "properties": {}},
    )


def test_external_reference_url_validation_rejects_unsafe_authority_and_whitespace():
    unsafe_urls = (
        " https://example.com/leading-space",
        "https://example.com/path with space",
        "https://user:secret@example.com/private",
        "https://example.com:not-a-port/result",
        "https:///missing-host",
        "ftp://example.com/file",
    )

    assert all(_clean_url(url) is None for url in unsafe_urls)
    safe_url = "https://example.com:8443/report?q=agent#section"
    assert _clean_url(safe_url) == safe_url


@pytest.mark.asyncio
async def test_remote_mcp_error_result_raises_structured_exception():
    """An MCP isError must take the runtime failure branch and must not pose as a successful text result."""
    tool = _adapt_stub_result(
        SimpleNamespace(
            isError=True,
            content=[SimpleNamespace(text="upstream rejected the query")],
        )
    )

    with pytest.raises(MCPToolExecutionError) as raised:
        await tool.invoke({}, ToolContext(engine_manager=None))

    assert raised.value.to_dict() == {
        "code": "mcp_tool_error",
        "tool_name": "lookup",
        "message": "upstream rejected the query",
    }
    assert "lookup" in str(raised.value)


@pytest.mark.asyncio
async def test_remote_mcp_extracts_and_deduplicates_external_references():
    """Structured content, text JSON and a plain URL can all form a renderable external source."""
    tool = _adapt_stub_result(
        SimpleNamespace(
            isError=False,
            structuredContent={
                "results": [
                    {
                        "url": "https://news.example/a",
                        "title": "Alpha report",
                        "source": "Example News",
                        "snippet": "  Alpha   launch\n details.  ",
                    }
                ]
            },
            structured_content={
                "items": [
                    {
                        "href": "https://docs.example/b",
                        "name": "Beta docs",
                        "publisher": "Example Docs",
                        "description": "Beta documentation summary.",
                    }
                ]
            },
            content=[
                SimpleNamespace(
                    text=(
                        '{"results":[{"link":"https://third.example/c","title":"Gamma",'
                        '"site":"Third","summary":"Gamma release notes."}]}'
                    )
                ),
                SimpleNamespace(text="\u91cd\u590d\u6765\u6e90 https://news.example/a\uff1b\u5ffd\u7565 ftp://files.example/x"),
            ],
        )
    )

    result = await tool.invoke({}, ToolContext(engine_manager=None))

    assert result.data["external_references"] == [
        {
            "url": "https://news.example/a",
            "title": "Alpha report",
            "source": "Example News",
            "snippet": "Alpha launch details.",
        },
        {
            "url": "https://docs.example/b",
            "title": "Beta docs",
            "source": "Example Docs",
            "snippet": "Beta documentation summary.",
        },
        {
            "url": "https://third.example/c",
            "title": "Gamma",
            "source": "Third",
            "snippet": "Gamma release notes.",
        },
    ]


@pytest.mark.asyncio
async def test_remote_mcp_reference_snippets_are_bounded_and_merge_richer_duplicates():
    """A duplicate URL backfills the later metadata; the summary is bounded and never expands a JSON tool payload."""
    long_summary = "  ".join(["detail"] * 100)
    serialized_payload = (
        '{"results":[{"url":"https://nested.example/private",'
        '"content":"complete upstream payload"}]}'
    )
    tool = _adapt_stub_result(
        SimpleNamespace(
            isError=False,
            structuredContent={
                "results": [
                    {"url": "https://merge.example/report"},
                    {
                        "url": "https://safe.example/result",
                        "title": "Safe result",
                        "content": serialized_payload,
                    },
                ]
            },
            structured_content={
                "results": [
                    {
                        "url": "https://merge.example/report",
                        "headline": "Complete report",
                        "provider": "Merge News",
                        "content": long_summary,
                    }
                ]
            },
            content=[
                SimpleNamespace(
                    text=(
                        '{"results":[{"link":"https://text.example/item",'
                        '"title":"Text JSON","description":"  concise\\nsummary  "}]}'
                    )
                )
            ],
        )
    )

    result = await tool.invoke({}, ToolContext(engine_manager=None))
    references = result.data["external_references"]

    assert references[0]["title"] == "Complete report"
    assert references[0]["source"] == "Merge News"
    assert len(references[0]["snippet"]) == 320
    assert references[0]["snippet"].endswith("…")
    assert references[1] == {
        "url": "https://safe.example/result",
        "title": "Safe result",
        "source": "safe.example",
    }
    assert references[2] == {
        "url": "https://text.example/item",
        "title": "Text JSON",
        "source": "text.example",
        "snippet": "concise summary",
    }
    assert all(reference["url"] != "https://nested.example/private" for reference in references)


@pytest.mark.asyncio
async def test_open_agent_mcp_tools_returns_safe_connection_warning(monkeypatch):
    """A connection failure is reported to the caller, but the warning leaks no configuration, credential or full exception."""

    @asynccontextmanager
    async def broken_session(config):
        del config
        raise RuntimeError("Bearer top-secret at https://private.example/mcp")
        yield  # pragma: no cover

    monkeypatch.setattr(mcp_module, "_open_session", broken_session)

    async with open_agent_mcp_tools(
        [("private-search", {"url": "https://private.example/mcp", "token": "top-secret"})]
    ) as bundle:
        assert bundle.tools == []
        assert bundle.warnings == [
            {
                "code": "mcp_connection_failed",
                "server": "private_search",
                "message": "Could not connect to the MCP service; it was skipped this turn.",
            }
        ]
        warning_text = str(bundle.warnings)
        assert "top-secret" not in warning_text
        assert "private.example" not in warning_text


def test_registry_overlay_does_not_pollute_global():
    """The overlay holds the built-in and the MCP tools; the global singleton stays clean."""

    class _Stub(Tool):
        meta = ToolMeta(
            name="mcp__ext__ping",
            description="stub",
            parameters={"type": "object", "properties": {}},
        )

        async def invoke(self, args, ctx):
            return ToolResult(content="pong")

    child = registry.overlay([_Stub()])
    assert child.has("mcp__ext__ping")
    assert child.has("search_context")  # the built-in tools are inherited
    assert not registry.has("mcp__ext__ping")  # the global registry is untouched


@pytest.mark.asyncio
async def test_mcp_binding_validation_and_source_descriptor():
    """Validating an agent mounting an external MCP, plus the source MCP connection description endpoint."""
    from sag_api.main import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            A = await _register(c, "mcpbind@t.com")
            src = (await c.post("/api/v1/sources", headers=A, json={"name": "\u63cf\u8ff0\u6e90"})).json()

            desc = await c.get(f"/api/v1/sources/{src['id']}/mcp", headers=A)
            assert desc.status_code == 200, desc.text
            body = desc.json()
            assert src["id"] in body["http"]["url"]
            assert body["stdio"]["env"]["SAG_MCP_SOURCE_ID"] == src["id"]
            assert set(body["tools"]) == set(MCP_TOOL_NAMES)
            assert body["tool_details"] == list(MCP_TOOL_DETAILS)

            knowledge = await c.get("/api/v1/system/mcp", headers=A)
            assert knowledge.status_code == 200, knowledge.text
            global_body = knowledge.json()
            assert global_body["scope"] == "knowledge_base"
            assert global_body["source_count"] >= 1
            assert "source_id" not in global_body["http"]["url"]
            assert global_body["http"]["url"].endswith("/mcp/")
            assert global_body["stdio"]["env"] == {}
            assert set(global_body["tools"]) == set(MCP_TOOL_NAMES)
            assert global_body["tool_details"] == list(MCP_TOOL_DETAILS)

            unauthorized = await c.get("/mcp/")
            assert unauthorized.status_code == 401

            initialized = await c.post(
                "/mcp/",
                headers={
                    **A,
                    "Host": "192.168.1.20:8000",
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "lan-host-test", "version": "1.0"},
                    },
                },
            )
            assert initialized.status_code == 200, initialized.text

            agent = (await c.post("/api/v1/agents", headers=A, json={"name": "\u6302\u8f7d\u52a9\u624b"})).json()
            ok = await c.post(
                f"/api/v1/agents/{agent['id']}/bindings",
                headers=A,
                json={"target_type": "mcp_server", "config": {"name": "fs", "url": "http://x/mcp"}},
            )
            assert ok.status_code == 201, ok.text
            assert ok.json()["target_type"] == "mcp_server"
            assert ok.json()["config"]["url"] == "http://x/mcp"

            bad = await c.post(
                f"/api/v1/agents/{agent['id']}/bindings",
                headers=A,
                json={"target_type": "mcp_server", "config": {"name": "\u7f3a\u5c11\u8fde\u63a5"}},
            )
            assert bad.status_code == 422, bad.text
