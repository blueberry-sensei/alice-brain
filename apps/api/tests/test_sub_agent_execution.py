from __future__ import annotations

from typing import Any

import httpx
import pytest

from sag_api.core.errors import ConfigurationError
from sag_api.core.telemetry import set_llm_call_sink
from sag_api.services import sub_agent_execution


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "entry", "payload", "expected_url", "expected_model", "expected_text"),
    [
        (
            "opencode-zen",
            {
                "provider": "opencode-zen",
                "model": "opencode/deepseek-v4-flash-free",
                "credential": "zen-secret",
                "model_verified": True,
            },
            {
                "choices": [{"message": {"content": "ZEN_OK"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
            },
            "https://opencode.ai/zen/v1/chat/completions",
            "deepseek-v4-flash-free",
            "ZEN_OK",
        ),
        (
            "gemini-cli",
            {
                "provider": "gemini-cli",
                "model": "gemini-3.5-flash-lite",
                "credential": "gemini-secret",
                "model_verified": True,
            },
            {
                "candidates": [{"content": {"parts": [{"text": "GEMINI_OK"}]}}],
                "usageMetadata": {
                    "promptTokenCount": 9,
                    "candidatesTokenCount": 2,
                    "totalTokenCount": 11,
                },
            },
            ("https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent"),
            None,
            "GEMINI_OK",
        ),
        (
            "custom",
            {
                "provider": "custom",
                "provider_name": "OpenRouter Free",
                "model": "google/gemma-4-31b-it:free",
                "base_url": "https://openrouter.ai/api/v1",
                "credential": "openrouter-secret",
            },
            {
                "choices": [{"message": {"content": "OPENROUTER_OK"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11},
            },
            "https://openrouter.ai/api/v1/chat/completions",
            "google/gemma-4-31b-it:free",
            "OPENROUTER_OK",
        ),
    ],
)
async def test_invoke_sub_agent_uses_brain_credential_and_provider_contract(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    entry: dict[str, Any],
    payload: dict[str, Any],
    expected_url: str,
    expected_model: str | None,
    expected_text: str,
):
    calls: list[dict[str, Any]] = []
    telemetry = []

    async def fake_entry(_session, requested_provider: str):
        assert requested_provider == provider
        return dict(entry)

    async def capture(record):
        telemetry.append(record)

    async def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        calls.append({"url": str(request.url), "headers": request.headers, "body": body})
        assert entry["credential"] not in request.content.decode()
        if provider == "gemini-cli":
            assert request.headers["x-goog-api-key"] == entry["credential"]
        else:
            assert request.headers["authorization"] == f"Bearer {entry['credential']}"
        if expected_model is not None:
            assert body["model"] == expected_model
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        sub_agent_execution.settings_service,
        "load_sub_agent_for_execution",
        fake_entry,
    )
    set_llm_call_sink(capture)
    try:
        result = await sub_agent_execution.invoke_sub_agent(
            object(),
            provider,
            "Review this function",
            context="def example(): pass",
            actor="claude-code",
            transport=httpx.MockTransport(handler),
        )
    finally:
        set_llm_call_sink(None)

    assert result.content == expected_text
    assert calls[0]["url"] == expected_url
    assert telemetry[0].call_type == "sub_agent"
    assert telemetry[0].provider == provider
    assert telemetry[0].actor == "claude-code"
    assert telemetry[0].ok is True
    assert entry["credential"] not in repr(telemetry[0])


@pytest.mark.asyncio
async def test_invoke_sub_agent_rejects_disabled_slot_before_network(
    monkeypatch: pytest.MonkeyPatch,
):
    async def missing(_session, _provider: str):
        return None

    monkeypatch.setattr(
        sub_agent_execution.settings_service,
        "load_sub_agent_for_execution",
        missing,
    )

    with pytest.raises(ConfigurationError) as raised:
        await sub_agent_execution.invoke_sub_agent(
            object(),
            "opencode-zen",
            "Review this function",
            transport=httpx.MockTransport(lambda _request: pytest.fail("network must not be called")),
        )

    assert raised.value.code == "sub_agent_not_enabled"


@pytest.mark.asyncio
async def test_list_available_sub_agents_reports_registry_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
):
    async def config(_session):
        return {
            "providers": [],
            "entries": [
                {
                    "provider": "opencode-zen",
                    "model": "opencode/deepseek-v4-flash-free",
                    "enabled": True,
                    "credential_set": True,
                    "model_verified": True,
                },
                {
                    "provider": "custom",
                    "provider_name": "OpenRouter Free",
                    "model": "google/gemma-4-31b-it:free",
                    "base_url": "https://openrouter.ai/api/v1",
                    "enabled": True,
                    "credential_set": True,
                },
            ],
        }

    monkeypatch.setattr(
        sub_agent_execution.settings_service,
        "get_sub_agent_config",
        config,
    )

    entries = await sub_agent_execution.list_available_sub_agents(object())

    assert [entry["provider"] for entry in entries] == ["opencode-zen", "custom"]
    assert all(entry["callable"] for entry in entries)
    assert all("credential" not in entry for entry in entries)
