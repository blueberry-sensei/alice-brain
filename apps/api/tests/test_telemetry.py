"""Telemetry: mỗi request LLM và mỗi lần agent chạm tri thức đều để lại dấu vết.

Toàn bộ chạy offline: LiteLLM có `mock_response` nên đo được đường ghi thật mà không gọi
nhà cung cấp nào; phần MCP dùng client trong bộ nhớ.
"""

from __future__ import annotations

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connect

from sag_api.core.telemetry import STAGE_EXTRACTION, use_context
from sag_api.core.telemetry_litellm import _record_from
from sag_api.mcp.server import build_source_mcp, use_scope


async def _register(client: httpx.AsyncClient, email: str) -> dict:
    response = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": "password123"}
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _wait_for_calls(client: httpx.AsyncClient, headers: dict, *, timeout: float = 10.0) -> dict:
    import asyncio
    import time

    deadline = time.monotonic() + timeout
    while True:
        body = (await client.get("/api/v1/telemetry/llm-calls", headers=headers)).json()
        # Chờ theo `items`, không theo `total`: hai truy vấn chạy nối nhau nên có nhịp
        # đếm được 1 mà danh sách còn rỗng — chờ nhầm tín hiệu là test đỏ ngẫu nhiên.
        if body["items"] or time.monotonic() >= deadline:
            return body
        await asyncio.sleep(0.2)


def _payload(**overrides) -> dict:
    payload = {
        "call_type": "acompletion",
        "model": "gpt-4o-mini",
        "custom_llm_provider": "openai",
        "response_cost": 0.000135,
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
        "response_time": 0.25,
        "litellm_call_id": "call-1",
        "api_base": "https://api.openai.com/v1",
    }
    payload.update(overrides)
    return {"standard_logging_object": payload}


def test_litellm_payload_becomes_a_record():
    record = _record_from(_payload(), None, None, ok=True)
    assert (record.model, record.provider) == ("gpt-4o-mini", "openai")
    assert (record.input_tokens, record.output_tokens, record.total_tokens) == (10, 20, 30)
    assert record.cost_usd == pytest.approx(0.000135)
    assert record.cost_source == "litellm"
    assert record.latency_ms == 250


def test_unknown_price_is_recorded_as_unknown_not_free():
    """Model lạ (gateway tự host) LiteLLM trả cost 0.0 — phải ghi None, đừng ghi 0."""
    record = _record_from(_payload(response_cost=0.0, model="my-local-model"), None, None, ok=True)
    assert record.cost_usd is None and record.cost_source == "unknown"


def test_failure_is_recorded_with_its_kind():
    record = _record_from(
        _payload(error_str="litellm.RateLimitError: 429 Too Many Requests"),
        None,
        None,
        ok=False,
    )
    assert record.ok is False
    assert record.failure_kind == "rate_limit"
    assert record.cost_usd is None


def test_context_marks_which_document_paid_for_the_call():
    """Ngữ cảnh ingest phải bám vào bản ghi, nếu không thì không biết tài liệu nào tốn tiền."""
    from sag_api.core.telemetry import _apply_context

    with use_context(stage=STAGE_EXTRACTION, actor="ingest", document_id="doc-1", job_id="job-1"):
        record = _apply_context(_record_from(_payload(), None, None, ok=True))
    assert (record.stage, record.document_id, record.job_id) == (STAGE_EXTRACTION, "doc-1", "job-1")


@pytest.mark.asyncio
async def test_llm_call_is_persisted_and_summarised():
    """Một lời gọi LiteLLM giả lập phải đi hết đường: callback → DB → API summary."""
    import litellm

    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            headers = await _register(client, "telemetry@t.com")
            await client.request("DELETE", "/api/v1/telemetry", headers=headers)

            with use_context(stage=STAGE_EXTRACTION, actor="ingest", document_id="doc-42"):
                await litellm.acompletion(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": "hi"}],
                    mock_response="hello",
                    api_key="sk-not-a-real-key",
                )

            # LiteLLM chạy callback logging ở task nền, nên bản ghi tới SAU khi acompletion
            # trả về. Chờ có kết quả thay vì đọc ngay (đọc ngay là đo nhầm nhịp, không phải
            # bằng chứng "không ghi").
            calls = await _wait_for_calls(client, headers)
            assert calls["total"] >= 1
            row = calls["items"][0]
            assert row["stage"] == STAGE_EXTRACTION
            assert row["document_id"] == "doc-42"
            assert row["actor"] == "ingest"
            assert row["total_tokens"] > 0

            summary = (await client.get("/api/v1/telemetry/summary?days=1", headers=headers)).json()
            assert summary["totals"]["calls"] >= 1
            assert summary["totals"]["total_tokens"] > 0
            assert any(bucket["key"] == STAGE_EXTRACTION for bucket in summary["by_stage"])


@pytest.mark.asyncio
async def test_embedding_usage_reaches_telemetry():
    """Embedding đi bằng SDK openai, KHÔNG qua LiteLLM — nên nó có cầu nối riêng.

    Bỏ sót nhánh này là bảng chi phí thiếu đúng phần chạy nhiều nhất lúc ingest.
    """
    import asyncio
    from types import SimpleNamespace

    from alicecore.core.ai.embedding import EmbeddingClient

    from sag_api.core.telemetry import LLMCallRecord, set_llm_call_sink
    from sag_api.sag.embedding_telemetry import (
        embedding_telemetry_supported,
        install_engine_embedding_telemetry,
        uninstall_engine_embedding_telemetry,
    )

    if not embedding_telemetry_supported():
        pytest.skip("alicecore build without an embedding usage sink")

    captured: list[LLMCallRecord] = []

    async def sink(record: LLMCallRecord) -> None:
        captured.append(record)

    set_llm_call_sink(sink)
    install_engine_embedding_telemetry()
    try:
        client = EmbeddingClient(model="bge-m3", base_url="http://embedding:11434/v1", api_key="x")

        async def fake_create(**_kwargs):
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[0.1, 0.2])],
                usage=SimpleNamespace(prompt_tokens=7, total_tokens=7),
            )

        client.client = SimpleNamespace(embeddings=SimpleNamespace(create=fake_create))
        with use_context(document_id="doc-emb", actor="ingest"):
            assert await client.generate("hello") == [0.1, 0.2]
            await asyncio.sleep(0.05)  # cầu nối ghi ở task nền
    finally:
        uninstall_engine_embedding_telemetry()
        set_llm_call_sink(None)

    assert len(captured) == 1
    record = captured[0]
    assert record.stage == "embedding"
    assert (record.input_tokens, record.total_tokens) == (7, 7)
    assert record.document_id == "doc-emb"
    # Endpoint embedding tự cấu hình không có bảng giá → "chưa biết", không phải miễn phí.
    assert record.cost_usd is None


def test_embedding_telemetry_gracefully_handles_older_engine(monkeypatch):
    from sag_api.sag import embedding_telemetry

    warnings: list[str] = []
    monkeypatch.setattr(embedding_telemetry, "set_embedding_usage_sink", None)
    monkeypatch.setattr(
        embedding_telemetry.log,
        "warning",
        lambda message, *_args, **_kwargs: warnings.append(message),
    )

    assert embedding_telemetry.embedding_telemetry_supported() is False
    embedding_telemetry.install_engine_embedding_telemetry()
    embedding_telemetry.uninstall_engine_embedding_telemetry()

    assert warnings == [
        "This alicecore build does not report embedding usage; embedding cost stays out of telemetry"
    ]


@pytest.mark.asyncio
async def test_telemetry_endpoints_require_authentication():
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            for path in ("/api/v1/telemetry/summary", "/api/v1/telemetry/llm-calls"):
                assert (await client.get(path)).status_code == 401


@pytest.mark.asyncio
async def test_knowledge_calls_and_delegation_are_logged(monkeypatch):
    """MCP ghi được retrieval, registry, delegation tự động và lượt CLI do agent khai."""
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Source
    from sag_api.main import app
    from sag_api.services import sub_agent_execution

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

    async def fake_invoke(_session, provider, task, *, context="", actor="unknown"):
        assert (provider, task, context, actor) == (
            "opencode-zen",
            "review the auth boundary",
            "source excerpt",
            "claude-code",
        )
        return sub_agent_execution.SubAgentResult(
            provider=provider,
            display_name="OpenCode ZEN",
            model="opencode/deepseek-v4-flash-free",
            content="Keep ownership server-trusted.",
            input_tokens=18,
            output_tokens=6,
            total_tokens=24,
        )

    monkeypatch.setattr(sub_agent_execution, "list_available_sub_agents", fake_list)
    monkeypatch.setattr(sub_agent_execution, "invoke_sub_agent", fake_invoke)

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            headers = await _register(client, "telemetry-mcp@t.com")
            source = (await client.post("/api/v1/sources", headers=headers, json={"name": "T"})).json()
            async with SessionLocal() as session:
                sources = tuple(
                    (await session.execute(select(Source).where(Source.id == source["id"])))
                    .scalars()
                    .all()
                )

            mcp = build_source_mcp()
            with use_scope(app.state.engine_manager, sources, actor="claude-code", transport="stdio"):
                async with connect(mcp) as mcp_client:
                    await mcp_client.initialize()
                    await mcp_client.call_tool("list_sources", {})
                    await mcp_client.call_tool("list_sub_agents", {})
                    await mcp_client.call_tool(
                        "ask_sub_agent",
                        {
                            "provider": "opencode-zen",
                            "task": "review the auth boundary",
                            "context": "source excerpt",
                        },
                    )
                    await mcp_client.call_tool(
                        "log_agent_task",
                        {
                            "agent": "opencode-go",
                            "task": "refactor the settings form",
                            "status": "done",
                            "model": "grok-code",
                        },
                    )

            events = (await client.get("/api/v1/telemetry/agent-events", headers=headers)).json()
            kinds = {item["kind"] for item in events["items"]}
            assert {"knowledge_call", "sub_agent_registry", "delegation"} <= kinds

            knowledge = next(item for item in events["items"] if item["kind"] == "knowledge_call")
            assert knowledge["tool"] == "list_sources"
            assert knowledge["actor"] == "claude-code"
            assert knowledge["transport"] == "stdio"
            assert knowledge["result_chars"] > 0

            registry = next(
                item for item in events["items"] if item["kind"] == "sub_agent_registry"
            )
            assert registry["tool"] == "list_sub_agents"
            assert registry["result_count"] == 1

            managed = next(
                item
                for item in events["items"]
                if item["kind"] == "delegation" and item["tool"] == "opencode-zen"
            )
            assert managed["model"] == "opencode/deepseek-v4-flash-free"
            assert managed["detail"]["execution_source"] == "brain"
            assert managed["detail"]["note"] == "Keep ownership server-trusted."

            delegation = next(
                item
                for item in events["items"]
                if item["kind"] == "delegation" and item["tool"] == "opencode-go"
            )
            assert delegation["model"] == "grok-code"
            assert delegation["detail"]["status"] == "done"
            # Token/chi phí của sub-agent là do agent KHAI, không phải brain đo.
            assert delegation["detail"]["cost_source"] == "reported"


@pytest.mark.asyncio
async def test_stdio_server_installs_its_own_telemetry_store(monkeypatch):
    """Bridge stdio là process riêng, nên không được dựa vào lifespan FastAPI để cắm sink."""
    from sqlalchemy import select

    import sag_api.mcp.server as mcp_server
    import sag_api.sag as sag_module
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.telemetry import AgentEventRecord, emit_agent_event
    from sag_api.db.models import AgentEvent

    class FakeEngineManager:
        def __init__(self, _settings):
            self.closed = False

        async def aclose_all(self):
            self.closed = True

    class FakeMCP:
        async def run_stdio_async(self):
            await emit_agent_event(
                AgentEventRecord(
                    kind="knowledge_call",
                    actor="stdio-regression",
                    transport="stdio",
                    tool="search",
                    query="where is the contract?",
                    result_count=1,
                    result_chars=42,
                    detail={"preview": "the contract is in source"},
                )
            )
            await emit_agent_event(
                AgentEventRecord(
                    kind="delegation",
                    actor="stdio-regression",
                    transport="stdio",
                    tool="codex",
                    query="verify the contract",
                    model="gpt-test",
                    detail={"status": "done", "note": "targeted tests passed"},
                )
            )

    await init_db()
    monkeypatch.setattr(sag_module, "EngineManager", FakeEngineManager)
    monkeypatch.setattr(mcp_server, "get_source_mcp", lambda: FakeMCP())
    monkeypatch.setenv("SAG_MCP_ACTOR", "stdio-regression")

    await mcp_server.serve_stdio()

    async with SessionLocal() as session:
        events = (
            await session.execute(
                select(AgentEvent)
                .where(AgentEvent.actor == "stdio-regression")
                .order_by(AgentEvent.created_at.desc())
            )
        ).scalars().all()
    assert len(events) == 2
    knowledge = next(event for event in events if event.kind == "knowledge_call")
    delegation = next(event for event in events if event.kind == "delegation")
    assert (knowledge.transport, knowledge.tool) == ("stdio", "search")
    assert knowledge.query == "where is the contract?"
    assert knowledge.detail["preview"] == "the contract is in source"
    assert (delegation.transport, delegation.tool, delegation.model) == ("stdio", "codex", "gpt-test")
    assert delegation.detail["status"] == "done"


@pytest.mark.asyncio
async def test_delegation_can_be_logged_over_http():
    from sag_api.main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            headers = await _register(client, "telemetry-http@t.com")
            created = await client.post(
                "/api/v1/telemetry/agent-events",
                headers=headers,
                json={"agent": "codex", "task": "write migration", "status": "failed"},
            )
            assert created.status_code == 201
            events = (
                await client.get("/api/v1/telemetry/agent-events?kind=delegation", headers=headers)
            ).json()
            assert any(item["tool"] == "codex" and item["ok"] is False for item in events["items"])
