"""Agentic plumbing: default tools, global evidence numbering, history compaction, token estimation. Fully offline."""

import re

import pytest

from sag_agent import AgentTool, ToolResult, ToolSpec
from sag_api.generation.prompt import build_agent_messages, build_prompt_preview, estimate_tokens
from sag_api.sag import RetrievedSection, SearchOutcome
from sag_api.services.agent_domain import compress_history
from sag_api.services.agent_service import (
    _append_current_scene,
    _build_external_citations,
    _enabled_tool_names,
    _finalize_answer_citations,
    _initial_tool_choice,
)
from sag_api.tools.base import ToolContext
from sag_api.tools.builtin import SearchContextTool


class _A:
    def __init__(self, is_default=False, tools=None):
        self.is_default = is_default
        self.persona = {"tools": tools} if tools is not None else {}


def test_default_agent_gets_builtin_tools():
    assert _enabled_tool_names(_A(is_default=True)) == [
        "get_time",
        "search_context",
        "get_entity",
        "web_search",
        "open_webpage",
    ]
    assert _enabled_tool_names(_A(is_default=False)) == [
        "get_time",
        "web_search",
        "open_webpage",
    ]
    assert _enabled_tool_names(_A(is_default=True, tools=["echo"])) == [
        "get_time",
        "search_context",
        "get_entity",
        "web_search",
        "open_webpage",
        "echo",
    ]
    assert _enabled_tool_names(_A(is_default=True), knowledge_only=True) == [
        "get_time",
        "search_context",
        "get_entity",
    ]


def test_estimate_tokens_cjk_aware():
    assert estimate_tokens("\u4f60\u597d\u4e16\u754c") == 4  # CJK escapes on purpose: 1 token per CJK character
    assert estimate_tokens("abcdefgh") == 2  # ASCII: 1 token per 4 characters
    assert estimate_tokens("") == 0


def test_agent_prompt_uses_static_timezone_rule_and_time_tool_guidance():
    messages = build_agent_messages(
        "Trợ lý thử nghiệm",
        {},
        "Bây giờ là mấy giờ?",
        language="vi",
        timezone="Asia/Ho_Chi_Minh",
    )
    system = messages[0]["content"]
    assert "Asia/Ho_Chi_Minh" in system
    assert "get_time" in system
    assert "Ngày giờ hiện tại là dữ kiện động" in system
    assert "ngày tuyệt đối, khoảng thời gian phù hợp" in system
    assert "hội thoại cũ" in system


def test_agent_prompt_guides_clarification_progress_and_delivery_in_both_languages():
    vi = build_agent_messages(
        "Trợ lý thử nghiệm",
        {},
        "Hãy đề xuất",
        language="vi",
        timezone="UTC",
    )[0]["content"]
    en = build_agent_messages(
        "Test Assistant",
        {},
        "Recommend something",
        language="en",
        timezone="UTC",
    )[0]["content"]

    assert "thay đổi đáng kể kết luận hoặc sản phẩm bàn giao" in vi
    assert "giả định hợp lý rồi tiếp tục" in vi
    assert "kết quả có thể dùng ngay" in vi
    assert "không trình bày dài dòng quá trình suy luận ẩn" in vi
    assert "thông báo chính thức, tài liệu sản phẩm, dữ liệu gốc" in vi
    assert "ít nhất hai nguồn độc lập" in vi
    assert "liên kết Markdown" in vi
    assert "get_entity để phân giải thực thể" in vi
    assert "Trả lời trực tiếp lời chào, cảm ơn, tạm biệt" in vi
    assert "Tìm kiếm không thay thế việc làm rõ" in vi
    assert "Luôn trả lời bằng tiếng Việt" in vi
    assert "materially change the conclusion or deliverable" in en
    assert "reasonable assumptions and proceed" in en
    assert "directly usable result" in en
    assert "Do not expose lengthy hidden reasoning" in en
    assert "first-party announcements, product documentation, original data" in en
    assert "at least two independent sources" in en
    assert "clickable direct source near each key external claim" in en
    assert "get_entity only to disambiguate entities" in en
    assert "Answer greetings, thanks, farewells, and identity questions directly" in en
    assert "search is not a substitute for clarification" in en
    assert re.search(r"[\u3400-\u9fff]", vi) is None
    assert re.search(r"[\u3400-\u9fff]", en) is None


def test_search_context_description_has_explicit_non_retrieval_boundary():
    description = SearchContextTool.meta.description

    assert "only when the answer depends on facts" in description
    assert "Do not use it for greetings, thanks, identity questions, pure creation" in description
    assert "retrieval cannot replace clarification" in description
    assert re.search(r"[\u3400-\u9fff]", description) is None


def test_agent_messages_keep_system_history_and_current_user_separate():
    messages = build_agent_messages(
        "Trợ lý thử nghiệm",
        {},
        "Câu hỏi hiện tại",
        language="vi",
        history=[
            {"role": "user", "content": "Câu hỏi trước"},
            {"role": "assistant", "content": "Câu trả lời trước"},
        ],
    )

    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert "Câu trả lời trước" not in messages[0]["content"]
    preview = build_prompt_preview(messages, language="vi")
    assert "\u3010Chỉ dẫn hệ thống\u3011" in preview
    assert "\u3010Lịch sử · Trợ lý\u3011\nCâu trả lời trước" in preview
    assert "\u3010Câu hỏi hiện tại\u3011\nCâu hỏi hiện tại" in preview


def test_dynamic_scene_stays_inside_single_system_message():
    messages = build_agent_messages(
        "Test Assistant",
        {},
        "Search the knowledge base",
        history=[{"role": "assistant", "content": "Earlier answer"}],
    )
    with_scene = _append_current_scene(messages, ["Use local knowledge", "Use product sources"])

    assert [message["role"] for message in with_scene] == ["system", "assistant", "user"]
    assert sum(message["role"] == "system" for message in with_scene) == 1
    assert "[Current context]" in with_scene[0]["content"]
    assert "Use local knowledge" in with_scene[0]["content"]
    assert with_scene[1]["content"] == "Earlier answer"
    assert messages[0]["content"] != with_scene[0]["content"]


def test_initial_tool_policy_anchors_time_and_preserves_clarification():
    async def execute(arguments, context):
        return ToolResult(content="ok")

    search = AgentTool(
        ToolSpec(name="search_context", description="Search the knowledge base"),
        execute,
    )
    clock = AgentTool(ToolSpec(name="get_time", description="Get the time"), execute)
    tools = (clock, search)

    named_time = {"type": "function", "function": {"name": "get_time"}}
    named_search = {"type": "function", "function": {"name": "search_context"}}

    assert (
        _initial_tool_choice(
            "What are the latest updates on Agent?",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == named_time
    )
    assert (
        _initial_tool_choice(
            "What are the latest updates to ChatGPT?",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == named_time
    )
    assert (
        _initial_tool_choice(
            "What updates did ChatGPT get in the past three months?",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == named_time
    )
    assert (
        _initial_tool_choice(
            "What changed last week?",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == named_time
    )
    assert (
        _initial_tool_choice(
            "Hello",
            tools,
            knowledge_only=True,
            scoped=False,
        )
        == "none"
    )
    assert (
        _initial_tool_choice(
            "Recommend",
            tools,
            knowledge_only=True,
            scoped=False,
        )
        == "none"
    )
    assert (
        _initial_tool_choice(
            "Recently how are things?",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == "none"
    )
    assert (
        _initial_tool_choice(
            "Last week how are things?",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == "none"
    )
    assert (
        _initial_tool_choice(
            "(2 + 3) * 4 = ?",
            tools,
            knowledge_only=True,
            scoped=False,
        )
        == "none"
    )
    assert (
        _initial_tool_choice(
            "Summarize the release process in the knowledge base",
            tools,
            knowledge_only=True,
            scoped=False,
        )
        == named_search
    )
    assert (
        _initial_tool_choice(
            "Please search and verify this data",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == "required"
    )
    assert (
        _initial_tool_choice(
            "Hello",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == "none"
    )
    assert (
        _initial_tool_choice(
            "Polish this paragraph",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == "auto"
    )


@pytest.mark.parametrize(
    "query",
    ["Hello!", "Hi", "Thanks", "Thank you", "Who are you?", "What's your name?"],
)
def test_high_confidence_social_intents_disable_tools(query):
    async def execute(arguments, context):
        return ToolResult(content="ok")

    tools = (
        AgentTool(ToolSpec(name="get_time", description="Get the time"), execute),
        AgentTool(ToolSpec(name="search_context", description="Search the knowledge base"), execute),
    )

    assert (
        _initial_tool_choice(query, tools, knowledge_only=True, scoped=False)
        == "none"
    )


def test_answer_citations_are_canonical_and_traceable():
    citations = [
        {"n": 1, "chunk_id": "chunk-1", "source_id": "source-1", "heading": "One"},
        {"n": 2, "chunk_id": None, "source_id": "source-1", "heading": "Not openable"},
        {"n": 3, "chunk_id": "chunk-3", "source_id": "source-3", "heading": "Three"},
    ]

    answer, used = _finalize_answer_citations("Conclusion [1], invented [9], bad citation [2].", citations)
    assert answer == "Conclusion [1], invented, bad citation."
    assert [citation["n"] for citation in used] == [1]
    assert used[0]["kind"] == "internal"
    assert used[0]["mapped"] is True
    assert used[0]["claim_level"] == "claim"

    uncited, fallback = _finalize_answer_citations("The model forgot to cite.", citations)
    assert uncited == "The model forgot to cite."
    assert [citation["n"] for citation in fallback] == [1, 3]
    assert all(citation["kind"] == "internal" for citation in fallback)
    assert all(citation["mapped"] is False for citation in fallback)
    assert all(citation["claim_level"] == "run" for citation in fallback)

    external_link = "External source [9](https://example.com/release)."
    preserved, none = _finalize_answer_citations(external_link, [])
    assert preserved == external_link
    assert none == []


def test_external_citations_are_safe_deduplicated_bounded_and_mapping_aware():
    references = [
        {
            "title": "Official release",
            "url": "HTTPS://Example.COM/release#details",
            "source": "OpenAI",
            "description": "  Product   update details.  ",
        },
        {"title": "duplicate", "url": "https://example.com/release"},
        {"title": "bad scheme", "url": "javascript:alert(1)"},
        {"title": "credentials", "url": "https://user:secret@example.com/private"},
        {"title": "whitespace", "url": "https://example.com/a b"},
        *[
            {"title": f"Result {index}", "url": f"https://source{index}.example/article"}
            for index in range(20)
        ],
    ]

    citations = _build_external_citations(
        "See https://example.com/release for the conclusion.",
        references,
        start_n=6,
    )

    assert len(citations) == 12
    assert citations[0] == {
        "kind": "external",
        "n": 6,
        "url": "https://example.com/release",
        "title": "Official release",
        "source": "OpenAI",
        "mapped": True,
        "claim_level": "claim",
        "summary": "Product update details.",
        "snippet": "Product update details.",
    }
    assert citations[1]["n"] == 7
    assert citations[1]["mapped"] is False
    assert citations[1]["claim_level"] == "run"
    assert not any("javascript:" in citation["url"] for citation in citations)
    assert not any("secret" in citation["url"] for citation in citations)


@pytest.mark.asyncio
async def test_search_tool_uses_global_citation_offset():
    class _EM:
        async def search_many(self, targets, query, strategy=None, top_k=None):
            return SearchOutcome(
                query=query,
                sections=[
                    RetrievedSection(
                        heading="Heading",
                        content="Content",
                        chunk_id="c1",
                        source_config_id="scid",
                        score=0.9,
                    )
                ],
            )

    class _Src:
        sag_source_config_id = "scid"
        id = "sid"
        name = "Source"

    ctx = ToolContext(engine_manager=_EM(), sources=[_Src()], citation_offset=3)
    result = await SearchContextTool().invoke({"query": "q"}, ctx)
    assert "[4]" in result.content  # numbering starts at offset+1
    assert result.citations[0]["n"] == 4


@pytest.mark.asyncio
async def test_compress_history_trims_without_llm():
    history = [{"role": "user", "content": "x" * 800} for _ in range(10)]
    out = await compress_history(history, llm=None, budget_tokens=500)
    assert out and out == history[-len(out) :]  # the tail is kept
    assert sum(estimate_tokens(m["content"]) for m in out) <= 500 + 200
    # untouched when within budget
    same = await compress_history(history[:2], llm=None, budget_tokens=10_000)
    assert same == history[:2]
