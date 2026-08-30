import json

import httpx
import pytest

from sag_api.core.errors import ServiceUnavailableError, UpstreamError, ValidationError
from sag_api.services.sub_agent_discovery import discover_sub_agent_models


@pytest.mark.asyncio
async def test_live_model_discovery_uses_provider_contracts():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://api.anthropic.com/v1/models"):
            assert request.headers["x-api-key"] == "anthropic-key"
            assert request.headers["anthropic-version"] == "2023-06-01"
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "claude-live"}],
                    "has_more": False,
                    "last_id": "claude-live",
                },
            )
        if url == "https://api.openai.com/v1/models":
            assert request.headers["authorization"] == "Bearer openai-key"
            return httpx.Response(200, json={"data": [{"id": "gpt-live"}]})
        if url.startswith("https://generativelanguage.googleapis.com/v1beta/models"):
            assert request.headers["x-goog-api-key"] == "gemini-key"
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-live",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                        {
                            "name": "models/embedding-only",
                            "supportedGenerationMethods": ["embedContent"],
                        },
                    ]
                },
            )
        if url == "https://opencode.ai/zen/go/v1/chat/completions":
            assert request.headers["authorization"] == "Bearer go-key"
            # Probe phải là request inference hợp lệ; chỉ 200 mới chứng minh key đúng.
            assert json.loads(request.content)["model"] == "go-live"
            return httpx.Response(200, json={"choices": []})
        if url == "https://opencode.ai/zen/go/v1/models":
            return httpx.Response(200, json={"data": [{"id": "go-live"}]})
        if url == "https://opencode.ai/zen/v1/chat/completions":
            assert request.headers["authorization"] == "Bearer zen-key"
            assert json.loads(request.content)["max_tokens"] == 1
            return httpx.Response(200, json={"choices": []})
        if url == "https://opencode.ai/zen/v1/models":
            return httpx.Response(200, json={"data": [{"id": "zen-live"}]})
        raise AssertionError(f"Unexpected request: {request.method} {url}")

    transport = httpx.MockTransport(handler)
    assert await discover_sub_agent_models(
        "claude", "anthropic-key", transport=transport
    ) == ["claude-live"]
    assert await discover_sub_agent_models(
        "codex", "openai-key", transport=transport
    ) == ["gpt-live"]
    assert await discover_sub_agent_models(
        "gemini-cli", "gemini-key", transport=transport
    ) == ["gemini-live"]
    assert await discover_sub_agent_models(
        "opencode-go", "go-key", transport=transport
    ) == ["opencode-go/go-live"]
    assert await discover_sub_agent_models(
        "opencode-zen", "zen-key", transport=transport
    ) == ["opencode/zen-live"]


@pytest.mark.asyncio
async def test_opencode_public_catalog_does_not_bypass_key_validation():
    """401 AuthError (key sai thật) vẫn phải bị từ chối — không để lọt vì catalog công khai."""
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/chat/completions"):
            return httpx.Response(
                401,
                json={"error": {"type": "AuthError", "message": "Invalid API key."}},
            )
        return httpx.Response(200, json={"data": [{"id": "must-not-leak"}]})

    with pytest.raises(ValidationError) as error:
        await discover_sub_agent_models(
            "opencode-go",
            "invalid-key",
            transport=httpx.MockTransport(handler),
        )
    assert error.value.code == "sub_agent_credential_invalid"


@pytest.mark.asyncio
async def test_opencode_modelerror_is_not_auth_error():
    """401 ModelError (model không nằm trong plan) không được coi là key sai.

    Plan GO không truy cập được model ZEN, nhưng key vẫn đúng. Probe phải thử model
    khác thay vì raise ValidationError ngay.
    """
    probe_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal probe_count
        if str(request.url).endswith("/chat/completions"):
            probe_count += 1
            model = json.loads(request.content)["model"]
            if model == "go-model":
                return httpx.Response(200, json={"choices": []})
            return httpx.Response(
                401,
                json={"error": {"type": "ModelError", "message": f"Model {model} is not supported"}},
            )
        return httpx.Response(
            200,
            json={"data": [{"id": "zen-only-free"}, {"id": "go-model"}]},
        )

    result = await discover_sub_agent_models(
        "opencode-go",
        "good-key",
        transport=httpx.MockTransport(handler),
    )
    # "zen-only-free" rẻ hơn nên probe trước → ModelError → thử tiếp "go-model" → 200
    assert result == ["opencode-go/zen-only-free", "opencode-go/go-model"]
    assert probe_count >= 2


@pytest.mark.asyncio
async def test_opencode_ambiguous_gateway_error_is_not_reported_as_bad_key():
    """Lỗi không phải auth thì phải giữ nguyên lời gateway, không quy sang "key sai".

    Bản trước probe bằng body rỗng và tin 401 = key sai; gateway lại trả 401 ``ModelError``
    cho **mọi** body rỗng, nên key đúng cũng bị đánh trượt.
    """
    probed_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/chat/completions"):
            probed_models.append(json.loads(request.content)["model"])
            return httpx.Response(500, json={"error": {"message": "Internal server error"}})
        return httpx.Response(
            200,
            json={"data": [{"id": "expensive-pro"}, {"id": "tiny-free"}, {"id": "mid-flash"}]},
        )

    with pytest.raises(UpstreamError) as error:
        await discover_sub_agent_models(
            "opencode-zen",
            "some-key",
            transport=httpx.MockTransport(handler),
        )
    assert error.value.code == "sub_agent_credential_check_failed"
    assert "500" in str(error.value)
    assert "Internal server error" in str(error.value)
    # Bậc rẻ đi trước, và thử hơn một model để plan thiếu đúng một model không thành "key sai".
    assert probed_models[0] == "tiny-free"
    assert probed_models[1] == "mid-flash"
    assert len(probed_models) == 3


@pytest.mark.asyncio
async def test_opencode_returns_full_catalog_after_one_model_verifies():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/chat/completions"):
            model = json.loads(request.content)["model"]
            if model == "cheap-mini":
                return httpx.Response(503, text="upstream busy")
            return httpx.Response(200, json={"choices": []})
        return httpx.Response(
            200, json={"data": [{"id": "cheap-mini"}, {"id": "solid-pro"}]}
        )

    assert await discover_sub_agent_models(
        "opencode-zen",
        "good-key",
        transport=httpx.MockTransport(handler),
    ) == ["opencode/cheap-mini", "opencode/solid-pro"]


@pytest.mark.asyncio
async def test_per_minute_rate_limit_is_waited_out_not_reported_as_exhausted():
    """429 kèm `Retry-After` ngắn là hạn mức theo PHÚT: chờ rồi thử lại, không bỏ cuộc.

    Bản trước bỏ cuộc ngay ở lần 429 đầu tiên, nên một trần RPM mười giây bị người dùng đọc
    thành "provider hết quota" và không bao giờ chọn được model.
    """
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"}, text="rate limit")
        return httpx.Response(200, json={"data": [{"id": "gpt-live"}]})

    assert await discover_sub_agent_models(
        "codex", "openai-key", transport=httpx.MockTransport(handler)
    ) == ["gpt-live"]
    assert calls == 2


@pytest.mark.asyncio
async def test_long_rate_limit_reports_how_long_to_wait():
    """`Retry-After` dài hơn ngân sách chờ là hạn mức theo giờ/ngày: trả lỗi kèm con số thật."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "3600"}, text="quota exceeded")

    with pytest.raises(ServiceUnavailableError) as error:
        await discover_sub_agent_models(
            "codex", "openai-key", transport=httpx.MockTransport(handler)
        )
    assert error.value.code == "sub_agent_provider_rate_limited"
    # Người dùng phải đọc được "60 phút" chứ không phải "hãy thử lại sau".
    assert "60 phút" in str(error.value)


@pytest.mark.asyncio
async def test_opencode_probe_treats_rate_limit_as_a_valid_key():
    """Gateway chỉ đếm hạn mức SAU khi nhận key, nên 429 ở probe nghĩa là key đã qua auth."""
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/chat/completions"):
            return httpx.Response(429, headers={"Retry-After": "3600"}, text="too many requests")
        return httpx.Response(200, json={"data": [{"id": "zen-live"}]})

    assert await discover_sub_agent_models(
        "opencode-zen", "good-key", transport=httpx.MockTransport(handler)
    ) == ["opencode/zen-live"]
