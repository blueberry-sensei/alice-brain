"""Retrieval answers may only see evidence that survives query-aware reranking."""

import re

import pytest

from sag_api.core.config import settings
from sag_api.sag import RetrievedSection
from sag_api.services.retrieval_service import (
    fallback_search_answer,
    rerank_sections,
    synthesize_search_answer,
)


def section(chunk_id: str, heading: str, content: str, score: float) -> RetrievedSection:
    return RetrievedSection(
        chunk_id=chunk_id,
        heading=heading,
        content=content,
        score=score,
        source_config_id="source-1",
    )


def test_rerank_prefers_direct_query_evidence_and_filters_unrelated_candidates():
    result = rerank_sections(
        "What charity work has Alex Rivers done recently",
        [
            section("noise", "Site home", "Trending sports coverage on the front page.", 0.96),
            section("answer", "Alex Rivers charity drive", "Alex Rivers built music classrooms for rural children.", 0.74),
            section("other", "Another singer", "A different singer released a new album.", 0.7),
        ],
        limit=8,
    )

    assert [item.chunk_id for item in result.sections] == ["answer"]
    assert result.filtered_count == 2


def test_rerank_uses_semantic_floor_when_no_lexical_signal_exists():
    result = rerank_sections(
        "How can protections for delivery workers be improved",
        [
            section("strong", "Labour research", "The report discusses working hours, skills and income.", 0.82),
            section("weak", "Unrelated appendix", "Page footer and copyright notice.", 0.12),
        ],
        limit=8,
    )

    assert [item.chunk_id for item in result.sections] == ["strong"]


def test_fallback_answer_cites_only_selected_sections():
    selected = [
        section("one", "Charity drive", "Alex Rivers built music classrooms for rural children.", 0.9),
        section("two", "Disaster relief", "The team donated supplies to the affected area.", 0.8),
    ]

    answer = fallback_search_answer("What charity work has Alex Rivers done", selected)

    assert "Alex Rivers built music classrooms for rural children" in answer
    assert "[1]" in answer and "[2]" in answer
    assert "[3]" not in answer


@pytest.mark.asyncio
async def test_invalid_llm_citation_falls_back_to_selected_evidence():
    class InvalidCitationLLM:
        configured = True

        async def complete(self, _messages):
            return "The model cited evidence that does not exist [9]"

    selected = [section("one", "Relevant evidence", "The fact that was actually selected.", 0.9)]
    answer = await synthesize_search_answer(
        "question",
        selected,
        llm=InvalidCitationLLM(),
    )

    assert "The fact that was actually selected" in answer
    assert "[1]" in answer
    assert "[9]" not in answer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "answer", "required"),
    [
        ("vi", "Câu trả lời dựa trên bằng chứng [1]", "hoàn toàn bằng tiếng Việt"),
        ("en", "An evidence-grounded answer [1]", "entirely in English"),
    ],
)
async def test_search_answer_prompt_follows_configured_language(
    monkeypatch,
    language,
    answer,
    required,
):
    class RecordingLLM:
        configured = True
        messages = None

        async def complete(self, messages):
            self.messages = messages
            return answer

    monkeypatch.setattr(settings, "sag_language", language)
    llm = RecordingLLM()
    selected = [section("one", "Project status", "The release is ready.", 0.9)]

    result = await synthesize_search_answer("What is the status?", selected, llm=llm)

    assert result == answer
    assert required in llm.messages[0]["content"]
    assert re.search(r"[\u3400-\u9fff]", llm.messages[0]["content"]) is None
