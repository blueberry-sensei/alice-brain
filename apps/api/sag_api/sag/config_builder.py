"""由 sag 配置装配 alicecore 的 `EngineConfig`。

支持信源级覆盖（`overrides`）——目前支持 `language`，未来可扩展 `entity_types` 等。
"""

from __future__ import annotations

from typing import Any

from alicecore import EngineConfig
from alicecore.config import EmbeddingConfig, LLMConfig, LLMProviderConfig, RelationalConfig

from sag_api.core.config import Settings
from sag_api.core.model_providers import get_model_provider

# LLM 未配置时的占位符：允许 EngineConfig 构造 / start() 建 schema（离线路径），
# 真正的 ingest / extract / search 会在运行时因缺少凭证而报错（服务层已前置守卫）。
_PLACEHOLDER = "not-configured"


def _build_llm_config(settings: Settings) -> LLMConfig:
    """Chuyển chuỗi provider của sag thành `LLMConfig` của alicecore.

    Engine tự lo chuyện chuyển nhà khi 429/hết quota, nên ở đây đưa **cả chuỗi** xuống,
    không phải chỉ entry đang thắng — nếu chỉ đưa một entry thì lúc extract giữa dòng mà
    hết quota là job chết, dù còn provider khác rảnh.
    """
    chain = settings.llm_chain
    shared = {
        "temperature": settings.effective_llm_temperature,
        "max_tokens": settings.llm_max_tokens,
        "timeout": max(1, (settings.llm_timeout_ms + 999) // 1000),
        "max_retries": settings.llm_max_retries,
    }
    if not chain:
        # Chưa cấu hình LLM: vẫn phải dựng được EngineConfig để start() tạo schema
        # (đường offline). Ingest/extract/search sẽ bị service layer chặn trước đó.
        return LLMConfig(api_key=_PLACEHOLDER, model=_PLACEHOLDER, provider="litellm", **shared)

    providers = [
        LLMProviderConfig(
            id=str(entry.get("id") or f"provider-{index}"),
            label=str(entry.get("label") or ""),
            api_key=str(entry["api_key"]),
            # route_model() thêm tiền tố litellm (gemini/…, anthropic/…) đúng theo provider.
            model=get_model_provider(entry.get("provider") or "openai").route_model(str(entry["model"])),
            provider="litellm",
            base_url=entry.get("base_url") or None,
            priority=int(entry.get("priority", 100)),
            enabled=True,
            extra_body=entry.get("extra_body") or None,
            cooldown_seconds=float(entry.get("cooldown_seconds", 60.0)),
            temperature=entry.get("temperature"),
            max_tokens=entry.get("max_tokens"),
            timeout=(
                max(1, (int(entry["timeout_ms"]) + 999) // 1000) if entry.get("timeout_ms") else None
            ),
            max_retries=entry.get("max_retries"),
        )
        for index, entry in enumerate(chain)
    ]
    return LLMConfig(providers=providers, provider="litellm", **shared)


def build_engine_config(settings: Settings, *, overrides: dict[str, Any] | None = None) -> EngineConfig:
    overrides = overrides or {}

    llm = _build_llm_config(settings)
    embedding = EmbeddingConfig(
        model=settings.embedding_model,
        base_url=settings.effective_embedding_base_url,
        api_key=settings.effective_embedding_api_key or _PLACEHOLDER,
        dimensions=settings.embedding_dimensions,
        max_retries=settings.embedding_max_retries,
    )

    kwargs: dict[str, Any] = {
        "llm": llm,
        "embedding": embedding,
        "data_dir": settings.data_dir,
        "language": overrides.get("language", settings.sag_language),
        "vector_provider": settings.sag_vector_provider,
    }

    # 生产：切到关系型后端（如 Postgres），与 pgvector 单库统一
    if settings.sag_relational_provider:
        kwargs["relational"] = RelationalConfig(
            provider=settings.sag_relational_provider,
            host=settings.sag_pg_host,
            port=settings.sag_pg_port,
            user=settings.sag_pg_user,
            password=settings.sag_pg_password,
            database=settings.sag_pg_database,
        )

    return EngineConfig(**kwargs)
