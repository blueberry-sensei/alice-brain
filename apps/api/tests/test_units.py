"""\u5feb\u901f\u5355\u5143\u6d4b\u8bd5\uff1a\u65e0\u9700\u7f51\u7edc / \u5f15\u64ce\u3002"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from sag_api.branding import DEFAULT_AGENT_NAME
from sag_api.connectors import registry
from sag_api.core.config import Settings, settings
from sag_api.core.litellm_policy import (
    apply_litellm_completion_policy,
    install_litellm_policy,
    uninstall_litellm_policy,
)
from sag_api.core.model_providers import get_model_provider, model_provider_catalog
from sag_api.core.security import hash_password, verify_password
from sag_api.enums import ConnectorKind
from sag_api.generation.prompt import build_agent_messages, build_citations, build_messages
from sag_api.sag import GraphEventInfo, RetrievedSection
from sag_api.sag.config_builder import build_engine_config


def test_password_hash_roundtrip():
    h = hash_password("password123")
    assert verify_password("password123", h)
    assert not verify_password("wrong", h)


def test_connector_registry():
    conn = registry.get(ConnectorKind.FILE_UPLOAD)
    assert conn.meta.kind == ConnectorKind.FILE_UPLOAD
    assert conn.meta.supports_sync is False
    assert any(c.meta.kind == ConnectorKind.FILE_UPLOAD for c in registry.all())


def test_model_provider_registry_is_the_public_source_of_truth():
    catalog = model_provider_catalog()
    assert [provider["id"] for provider in catalog] == ["openai", "anthropic", "gemini"]
    assert all("litellm_prefix" not in provider for provider in catalog)
    assert get_model_provider("openai").route_model("qwen3.6-flash") == "openai/qwen3.6-flash"
    assert get_model_provider("gemini").route_model("gemini/gemini-3.5-flash") == "gemini/gemini-3.5-flash"


def test_build_engine_config_zero_infra():
    cfg = build_engine_config(settings)
    assert cfg.vector_provider == "lancedb"  # backend vector zero-dependency mặc định
    assert cfg.llm.max_tokens == settings.llm_max_tokens
    assert cfg.llm.provider == "litellm"
    assert cfg.data_dir == settings.data_dir
    # Chưa cấu hình provider: engine vẫn dựng được (đường offline để start() tạo schema),
    # nhưng chuỗi chỉ có placeholder — service layer chặn ingest/search trước khi tới đây.
    assert cfg.llm.providers == [] and cfg.llm.api_key == "not-configured"


def test_build_engine_config_passes_whole_chain():
    """Engine phải nhận CẢ chuỗi: hết quota giữa lúc extract thì nó tự đổi nhà."""
    configured = Settings(
        _env_file=None,
        llm_providers=[
            {
                "id": "openrouter",
                "provider": "openai",
                "model": "deepseek/deepseek-v4-flash",
                "api_key": "sk-or",
                "base_url": "https://openrouter.ai/api/v1",
                "priority": 10,
                "extra_body": {"provider": {"order": ["deepinfra/fp4"]}},
            },
            {
                "id": "gemini",
                "provider": "gemini",
                "model": "gemini-3.5-flash",
                "api_key": "AIza",
                "priority": 20,
            },
        ],
    )

    cfg = build_engine_config(configured)
    chain = cfg.llm.resolved_chain()

    assert [entry.id for entry in chain] == ["openrouter", "gemini"]
    # route_model thêm tiền tố litellm đúng theo provider của từng entry.
    assert [entry.model for entry in chain] == [
        "openai/deepseek/deepseek-v4-flash",
        "gemini/gemini-3.5-flash",
    ]
    assert chain[0].extra_body == {"provider": {"order": ["deepinfra/fp4"]}}
    assert all(entry.provider == "litellm" for entry in chain)


@pytest.mark.parametrize(
    ("provider", "model", "expected_model"),
    [
        ("openai", "qwen3.6-flash", "openai/qwen3.6-flash"),
        ("anthropic", "claude-sonnet-5", "anthropic/claude-sonnet-5"),
        ("gemini", "gemini-3.5-flash", "gemini/gemini-3.5-flash"),
    ],
)
def test_extraction_engine_uses_one_litellm_transport(provider, model, expected_model):
    configured = Settings(
        _env_file=None,
        llm_providers=[
            {
                "id": "primary",
                "provider": provider,
                "model": model,
                "api_key": "provider-key",
                "priority": 10,
            }
        ],
    )

    engine = build_engine_config(configured)

    assert engine.llm.provider == "litellm"
    assert engine.llm.resolved_chain()[0].model == expected_model


@pytest.mark.parametrize(
    ("extra_body", "expected_reasoning", "expect_extra_body"),
    [
        (None, "none", False),
        ({"enable_thinking": False}, "none", True),
        ({"chat_template_kwargs": {"enable_thinking": False}}, "none", True),
        ({"enable_thinking": True}, None, True),
    ],
)
def test_litellm_policy_maps_qwen_thinking_option(extra_body, expected_reasoning, expect_extra_body):
    configured = Settings(
        _env_file=None,
        llm_provider="openai",
        llm_api_key="provider-key",
        llm_extra_body=extra_body,
    )
    request = apply_litellm_completion_policy(
        configured,
        {"model": configured.routed_llm_model, "messages": []},
    )

    assert request.get("reasoning_effort") == expected_reasoning
    assert ("extra_body" in request) is expect_extra_body
    assert ("reasoning_effort" in request.get("allowed_openai_params", [])) is (expected_reasoning is not None)


def test_litellm_policy_preserves_explicit_reasoning_and_allowed_params():
    configured = Settings(_env_file=None, llm_api_key="provider-key")

    request = apply_litellm_completion_policy(
        configured,
        {
            "model": "openai/qwen3.6-flash",
            "messages": [],
            "reasoning_effort": "low",
            "allowed_openai_params": ["seed"],
        },
    )

    assert request["reasoning_effort"] == "low"
    assert request["allowed_openai_params"] == ["seed", "reasoning_effort"]


@pytest.mark.asyncio
async def test_installed_litellm_policy_covers_dependency_owned_calls():
    import litellm

    configured = Settings(_env_file=None, llm_api_key="provider-key")
    previous_callbacks = list(litellm.callbacks)
    callback = install_litellm_policy(configured)
    try:
        request = await callback.async_pre_call_deployment_hook(
            {"model": "openai/qwen3.6-flash", "messages": []},
            SimpleNamespace(value="acompletion"),
        )
        assert request["reasoning_effort"] == "none"
        assert callback in litellm.callbacks
    finally:
        uninstall_litellm_policy(callback)
    assert litellm.callbacks == previous_callbacks


def test_document_output_redacts_database_details():
    from datetime import UTC, datetime

    from sag_api.enums import DocumentStatus
    from sag_api.schemas.document import DocumentOut

    payload = {
        "id": "doc-1",
        "source_id": "source-1",
        "filename": "note.md",
        "content_type": "text/markdown",
        "size_bytes": 12,
        "status": DocumentStatus.FAILED,
        "chunk_count": 0,
        "event_count": 0,
        "progress": 5,
        "token_usage": 0,
        "error": "(sqlite3.IntegrityError) FOREIGN KEY constraint failed [SQL: INSERT]",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    document = DocumentOut.model_validate(payload)
    assert document.error == "The source has not finished initialising, so the document is not stored yet; please retry."

    payload["error"] = "The parsing service is temporarily unavailable"
    document = DocumentOut.model_validate(payload)
    assert document.error == "The parsing service is temporarily unavailable"


@pytest.mark.asyncio
async def test_llm_timeout_and_retries_reach_unified_client(monkeypatch):
    from sag_api.generation import llm as generation_llm

    seen: dict = {}

    async def fake_completion(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="pong"))])

    monkeypatch.setattr(generation_llm, "_litellm_completion", fake_completion)
    configured = Settings(
        _env_file=None,
        llm_providers=[
            {
                "id": "primary",
                "provider": "openai",
                "model": "qwen3.6-flash",
                "api_key": "provider-key",
                "priority": 10,
            }
        ],
        llm_timeout_ms=45_000,
        llm_max_retries=3,
    )

    client = generation_llm.LLMClient(configured)
    assert await client.complete([{"role": "user", "content": "ping"}]) == "pong"
    assert seen["model"] == "openai/qwen3.6-flash"
    assert seen["timeout"] == 45
    # num_retries=0 là CHỦ Ý: retry do ChainRunner làm để mỗi lần thử đều vào
    # ATTEMPT_LOG. Nếu để LiteLLM tự retry thì các lần thử đó im lặng.
    assert seen["num_retries"] == 0
    assert seen["reasoning_effort"] == "none"
    assert "reasoning_effort" in seen["allowed_openai_params"]
    assert "extra_body" not in seen

    engine = build_engine_config(configured)
    assert engine.llm.provider == "litellm"
    # Model nằm ở từng entry của chuỗi, không còn ở field phẳng.
    assert engine.llm.resolved_chain()[0].model == "openai/qwen3.6-flash"
    assert engine.llm.timeout == 45
    assert engine.llm.max_retries == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model", "expected", "expected_temperature"),
    [
        ("openai", "qwen3.6-flash", "openai/qwen3.6-flash", 0.3),
        ("anthropic", "claude-sonnet-5", "anthropic/claude-sonnet-5", 1.0),
        ("gemini", "gemini/gemini-3.5-flash", "gemini/gemini-3.5-flash", 0.3),
    ],
)
async def test_generation_providers_use_one_litellm_route(monkeypatch, provider, model, expected, expected_temperature):
    from sag_api.generation import llm as generation_llm

    seen: dict = {}

    async def fake_completion(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="pong"))])

    monkeypatch.setattr(generation_llm, "_litellm_completion", fake_completion)
    configured = Settings(
        _env_file=None,
        llm_providers=[
            {
                "id": "primary",
                "provider": provider,
                "model": model,
                "api_key": "provider-key",
                "priority": 10,
            }
        ],
        llm_timeout_ms=45_000,
        llm_max_retries=3,
    )

    client = generation_llm.LLMClient(configured)
    assert await client.complete([{"role": "user", "content": "ping"}]) == "pong"
    assert seen["model"] == expected
    assert seen["api_key"] == "provider-key"
    assert seen["temperature"] == expected_temperature
    assert seen["timeout"] == 45
    # num_retries=0 là CHỦ Ý: retry do ChainRunner làm để mỗi lần thử đều vào
    # ATTEMPT_LOG. Nếu để LiteLLM tự retry thì các lần thử đó im lặng.
    assert seen["num_retries"] == 0
    assert "api_base" not in seen


@pytest.mark.asyncio
async def test_native_provider_stream_keeps_text_usage_and_tool_calls(monkeypatch):
    from sag_agent import AgentMessage, CancellationToken, ModelRequest
    from sag_api.generation import llm as generation_llm

    class ProviderStream:
        closed = False

        async def __aiter__(self):
            yield SimpleNamespace(
                usage={"prompt_tokens": 7, "completion_tokens": 3},
                choices=[],
            )
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason=None,
                        delta=SimpleNamespace(content="\u5148\u67e5\u8be2", tool_calls=[]),
                    )
                ]
            )
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="tool_calls",
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="call-1",
                                    function=SimpleNamespace(
                                        name="search_context",
                                        arguments={"query": "SAG"},
                                    ),
                                )
                            ],
                        ),
                    )
                ]
            )

        async def close(self):
            self.closed = True

    stream = ProviderStream()
    seen: dict = {}

    async def fake_completion(**kwargs):
        seen.update(kwargs)
        return stream

    monkeypatch.setattr(generation_llm, "_litellm_completion", fake_completion)
    client = generation_llm.LLMClient(
        Settings(
            _env_file=None,
            llm_providers=[
                {
                    "id": "primary",
                    "provider": "gemini",
                    "model": "gemini-3.5-flash",
                    "api_key": "gemini-key",
                    "priority": 10,
                }
            ],
        )
    )
    request = ModelRequest(
        messages=(AgentMessage(role="user", content="\u67e5\u8be2 SAG"),),
        tools=(
            {
                "type": "function",
                "function": {
                    "name": "search_context",
                    "description": "search",
                    "parameters": {"type": "object"},
                },
            },
        ),
        tool_choice="required",
        turn=2,
    )

    chunks = [chunk async for chunk in client.stream_turn(request, CancellationToken())]
    assert seen["model"] == "gemini/gemini-3.5-flash"
    assert seen["tool_choice"] == "required"
    assert chunks[0].usage is not None and chunks[0].usage.total_tokens == 10
    assert chunks[1].text_delta == "\u5148\u67e5\u8be2"
    assert chunks[-1].finish_reason == "tool_calls"
    assert chunks[-1].tool_calls[0].name == "search_context"
    assert chunks[-1].tool_calls[0].arguments == {"query": "SAG"}
    assert stream.closed is True


def test_native_generation_key_is_not_reused_for_openai_embeddings():
    configured = Settings(
        _env_file=None,
        llm_providers=[
            {
                "id": "primary",
                "provider": "anthropic",
                "model": "claude-sonnet-5",
                "api_key": "anthropic-secret",
                "priority": 10,
            }
        ],
        # Provider native (Anthropic/Gemini) không dùng chung định dạng embedding,
        # nên credential sinh văn bản KHÔNG được mượn cho embedding.
        llm_provider="anthropic",
        llm_api_key="anthropic-secret",
        embedding_api_key=None,
    )

    assert configured.effective_embedding_api_key is None
    engine = build_engine_config(configured)
    assert engine.llm.provider == "litellm"
    assert engine.llm.resolved_chain()[0].model == "anthropic/claude-sonnet-5"
    assert engine.llm.temperature == 1.0
    assert engine.embedding.api_key == "not-configured"


def test_retrieved_section_from_dict():
    s = RetrievedSection.from_section({"chunk_id": "c1", "heading": "H", "content": "text", "score": 0.9, "rank": 2})
    assert s.chunk_id == "c1" and s.heading == "H" and s.score == 0.9 and s.rank == 2


def test_prompt_and_citations():
    sections = [
        RetrievedSection(
            chunk_id="c1",
            heading="\u521b\u7acb",
            content="Acme \u7531\u5f20\u4e09\u521b\u7acb\u3002\u8fd9\u662f\u7528\u4e8e\u5f15\u7528\u9884\u89c8\u7684\u8865\u5145\u6b63\u6587\u3002",
            score=0.8,
            rank=0,
            source_config_id="sc-1",
        )
    ]
    msgs = build_messages("Who founded Acme?", sections, language="en")
    assert msgs[0]["role"] == "system"
    assert DEFAULT_AGENT_NAME in msgs[0]["content"]
    assert "[1]" in msgs[-1]["content"] and "Acme" in msgs[-1]["content"]
    cites = build_citations(
        sections,
        events=[
            GraphEventInfo(
                id="event-1",
                source_id="doc-1",
                source_config_id="sc-1",
                chunk_id="c1",
                title="Acme \u5ba3\u5e03\u521b\u7acb",
                summary="\u5f20\u4e09\u5b8c\u6210\u4e86 Acme \u7684\u521b\u7acb\u3002",
                content="\u5f20\u4e09\u5b8c\u6210\u516c\u53f8\u6ce8\u518c\uff0c\u5e76\u6b63\u5f0f\u5ba3\u5e03 Acme \u6210\u7acb\u3002",
                category="\u516c\u53f8\u4e8b\u4ef6",
                start_time=datetime(2026, 7, 21, tzinfo=UTC),
            )
        ],
    )
    assert cites[0]["n"] == 1 and cites[0]["heading"] == "\u521b\u7acb"
    assert cites[0]["snippet"] == "Acme \u7531\u5f20\u4e09\u521b\u7acb\u3002\u8fd9\u662f\u7528\u4e8e\u5f15\u7528\u9884\u89c8\u7684\u8865\u5145\u6b63\u6587\u3002"
    assert "summary" not in cites[0]
    assert cites[0]["event_refs"] == [
        {
            "id": "event-1",
            "title": "Acme \u5ba3\u5e03\u521b\u7acb",
            "summary": "\u5f20\u4e09\u5b8c\u6210\u4e86 Acme \u7684\u521b\u7acb\u3002",
            "content": "\u5f20\u4e09\u5b8c\u6210\u516c\u53f8\u6ce8\u518c\uff0c\u5e76\u6b63\u5f0f\u5ba3\u5e03 Acme \u6210\u7acb\u3002",
            "category": "\u516c\u53f8\u4e8b\u4ef6",
            "start_time": "2026-07-21T00:00:00+00:00",
        }
    ]


def test_citation_events_use_source_and_chunk_composite_key_and_are_bounded():
    sections = [
        RetrievedSection(chunk_id="same", source_config_id="source-a", content="A"),
        RetrievedSection(chunk_id="same", source_config_id="source-b", content="B"),
    ]
    events = [
        GraphEventInfo(
            id=f"a-{index}",
            source_id="doc-a",
            source_config_id="source-a",
            chunk_id="same",
            title=f"A \u4e8b\u4ef6 {index}",
        )
        for index in range(4)
    ] + [
        GraphEventInfo(
            id="b-1",
            source_id="doc-b",
            source_config_id="source-b",
            chunk_id="same",
            title="B \u4e8b\u4ef6",
        )
    ]

    citations = build_citations(sections, events=events)

    assert [item["id"] for item in citations[0]["event_refs"]] == ["a-0", "a-1", "a-2"]
    assert [item["id"] for item in citations[1]["event_refs"]] == ["b-1"]
    assert citations[1]["event_refs"][0]["summary"] == ""
    assert citations[1]["event_refs"][0]["category"] == ""


@pytest.mark.asyncio
async def test_engine_extract_compat_repairs_missing_meta():
    from alicecore.modules.extract.processor import EventProcessor

    from sag_api.sag.compat import install_engine_extract_compat

    class FakeLLM:
        async def chat_with_schema(self, _messages, response_schema):
            data_schema = response_schema["properties"]["data"]
            meta_schema = data_schema["properties"]["meta"]
            assert "meta" not in data_schema.get("required", [])
            assert "reason" not in meta_schema.get("required", [])
            return {"type": "response", "data": {"items": []}}

    install_engine_extract_compat()

    schema = {
        "type": "object",
        "required": ["type", "data"],
        "properties": {
            "type": {"const": "response"},
            "data": {
                "type": "object",
                "required": ["items", "meta"],
                "properties": {
                    "items": {"type": "array"},
                    "meta": {
                        "type": "object",
                        "required": ["reason"],
                        "properties": {"reason": {"type": "string"}},
                    },
                },
            },
        },
    }
    fake_processor = SimpleNamespace(llm_client=FakeLLM())

    result = await EventProcessor._call_llm_with_retry(fake_processor, [], schema)

    assert result["data"]["items"] == []
    assert result["data"]["meta"]["reason"]


@pytest.mark.asyncio
async def test_engine_extract_compat_repairs_missing_is_valid():
    from alicecore.modules.extract.processor import EventProcessor

    from sag_api.sag.compat import install_engine_extract_compat

    class FakeLLM:
        async def chat_with_schema(self, _messages, response_schema):
            event_schema = response_schema["definitions"]["event"]
            assert "is_valid" not in event_schema.get("required", [])
            return {
                "type": "response",
                "data": {
                    "meta": {"reason": "ok"},
                    "items": [
                        {
                            "title": "\u9876\u5c42\u4e8b\u9879",
                            "content": "\u4e8b\u9879\u5185\u5bb9",
                            "references": [1],
                            "children": [
                                {
                                    "title": "\u5b50\u4e8b\u9879",
                                    "content": "\u5b50\u4e8b\u9879\u5185\u5bb9",
                                    "references": [1],
                                }
                            ],
                        }
                    ],
                },
            }

    install_engine_extract_compat()

    schema = {
        "type": "object",
        "required": ["type", "data"],
        "properties": {
            "type": {"const": "response"},
            "data": {
                "type": "object",
                "required": ["items", "meta"],
                "properties": {
                    "items": {"type": "array", "items": {"$ref": "#/definitions/event"}},
                    "meta": {
                        "type": "object",
                        "required": ["reason"],
                        "properties": {"reason": {"type": "string"}},
                    },
                },
            },
        },
        "definitions": {
            "event": {
                "type": "object",
                "required": ["title", "content", "references", "is_valid"],
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "references": {"type": "array", "items": {"type": "integer"}},
                    "is_valid": {"type": "boolean"},
                    "children": {"type": "array", "items": {"$ref": "#/definitions/event"}},
                },
            },
        },
    }
    fake_processor = SimpleNamespace(llm_client=FakeLLM())

    result = await EventProcessor._call_llm_with_retry(fake_processor, [], schema)

    item = result["data"]["items"][0]
    assert item["is_valid"] is True
    assert item["children"][0]["is_valid"] is True


def test_agent_name_is_injected_into_prompt():
    messages = build_agent_messages(
        "Alice",
        {"system_prompt": "Luôn nghiêm túc."},
        "Bạn tên gì?",
        language="vi",
    )
    system = messages[0]["content"]
    assert "Tên của bạn là Alice" in system
    assert "Luôn nghiêm túc." in system
    assert "sag" not in system.lower()


@pytest.mark.asyncio
async def test_chain_runner_retries_transient_and_records_every_attempt(monkeypatch):
    """Tắt retry của LiteLLM không làm mất retry — ChainRunner làm, và ghi lại từng lần."""
    from sag_api.core import llm_routing
    from sag_api.generation import llm as generation_llm

    calls = {"n": 0}

    async def flaky_completion(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Error code: 503 - upstream unavailable")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="pong"))])

    monkeypatch.setattr(generation_llm, "_litellm_completion", flaky_completion)
    llm_routing.clear_attempts()
    configured = Settings(
        _env_file=None,
        llm_providers=[
            {
                "id": "primary",
                "provider": "openai",
                "model": "qwen3.6-flash",
                "api_key": "provider-key",
                "priority": 10,
                "max_retries": 2,
            }
        ],
    )
    client = generation_llm.LLMClient(
        configured, llm_routing.ChainRunner(retry_delay=0.001, max_delay=0.002)
    )

    assert await client.complete([{"role": "user", "content": "ping"}]) == "pong"
    assert calls["n"] == 2  # lỗi 5xx -> thử lại cùng provider

    recorded = llm_routing.recent_attempts(10)
    assert [entry["action"] for entry in recorded] == ["ok", "retry"]  # mới nhất lên đầu
    assert recorded[1]["kind"] == "transient"
    assert "503" in recorded[1]["error"]


@pytest.mark.asyncio
async def test_chain_runner_switches_provider_on_rate_limit(monkeypatch):
    """429 ở nhà đầu -> nhà thứ hai trả lời, và lý do đổi nhà nằm trong log."""
    from sag_api.core import llm_routing
    from sag_api.generation import llm as generation_llm

    seen_keys: list[str] = []

    async def quota_then_ok(**kwargs):
        seen_keys.append(kwargs["api_key"])
        if kwargs["api_key"] == "first-key":
            raise RuntimeError("Error code: 429 - quota exceeded")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="pong"))])

    monkeypatch.setattr(generation_llm, "_litellm_completion", quota_then_ok)
    llm_routing.clear_attempts()
    configured = Settings(
        _env_file=None,
        llm_providers=[
            {"id": "first", "provider": "openai", "model": "m1", "api_key": "first-key", "priority": 10},
            {"id": "second", "provider": "openai", "model": "m2", "api_key": "second-key", "priority": 20},
        ],
    )
    client = generation_llm.LLMClient(
        configured, llm_routing.ChainRunner(retry_delay=0.001, max_delay=0.002)
    )

    assert await client.complete([{"role": "user", "content": "ping"}]) == "pong"
    assert seen_keys == ["first-key", "second-key"]  # không thử lại nhà đã hết quota

    recorded = llm_routing.recent_attempts(10)
    assert recorded[-1]["kind"] == "rate_limit" and recorded[-1]["action"] == "failover"
    assert recorded[0]["ok"] is True and recorded[0]["provider_id"] == "second"
