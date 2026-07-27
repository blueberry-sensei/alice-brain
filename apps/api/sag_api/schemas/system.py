from __future__ import annotations

from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

from sag_api.core.model_providers import ModelProviderId
from sag_api.enums import SearchStrategy


class SystemPreferencesUpdate(BaseModel):
    timezone: str = Field(min_length=1, max_length=100)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        normalized = value.strip()
        try:
            ZoneInfo(normalized)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("必须使用有效的 IANA 时区，例如 Asia/Shanghai") from error
        return normalized


class LLMProviderEntry(BaseModel):
    """Một provider trong chuỗi ưu tiên.

    `api_key` để trống nghĩa là **giữ key đã lưu** của entry cùng `id` — nhờ đó UI sửa được
    nhãn / thứ tự / model mà không phải nhập lại key (server không bao giờ gửi key ra ngoài).
    """

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    provider: ModelProviderId
    model: str = Field(min_length=1, max_length=200)
    label: str = Field(default="", max_length=100)
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    priority: int = Field(default=100, ge=1, le=999)
    enabled: bool = True
    #: Tham số riêng của gateway, ví dụ ép backend OpenRouter:
    #: {"provider": {"order": ["deepinfra/fp4"], "allow_fallbacks": false}}
    extra_body: dict | None = None
    #: Bị 429 thì tạm bỏ qua provider này bao lâu (giây).
    cooldown_seconds: float = Field(default=60.0, ge=0, le=3600)
    #: Ghi đè tham số hành vi ở mức entry (bỏ trống = dùng cấu hình chung bên dưới).
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=32768)
    timeout_ms: int | None = Field(default=None, ge=1_000, le=600_000)
    max_retries: int | None = Field(default=None, ge=0, le=10)


class ModelConfigUpdate(BaseModel):
    """模型与知识库配置的部分更新（未出现的字段保持不变）。

    密钥字段留空表示「保持原值」（不清空）；base_url / dimensions 留空表示清除。
    `llm_providers` 出现时**整体替换**优先级链。
    """

    llm_providers: list[LLMProviderEntry] | None = Field(default=None, max_length=20)
    llm_temperature: float | None = Field(default=None, ge=0, le=2)
    llm_max_tokens: int | None = Field(default=None, ge=1, le=32768)
    llm_context_window: int | None = Field(default=None, ge=1024, le=2_000_000)
    llm_timeout_ms: int | None = Field(default=None, ge=1_000, le=600_000)
    llm_max_retries: int | None = Field(default=None, ge=0, le=10)

    embedding_model: str | None = Field(default=None, min_length=1, max_length=200)
    embedding_base_url: str | None = Field(default=None, max_length=500)
    embedding_api_key: str | None = Field(default=None, max_length=500)
    embedding_dimensions: int | None = Field(default=None, ge=1, le=8192)

    document_parser: Literal["markitdown"] | None = None
    document_extract_concurrency: int | None = Field(default=None, ge=1, le=50)
    document_chunk_max_tokens: int | None = Field(default=None, ge=100, le=100_000)
    document_chunk_mode: Literal["standard", "heading_strict"] | None = None

    search_strategy: SearchStrategy | None = None
    search_top_k: int | None = Field(default=None, ge=1, le=50)
    sag_language: Literal["en", "vi"] | None = None

    @field_validator("document_parser")
    @classmethod
    def reject_null_parser_fields(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("document_parser không được để null")
        return value

    @field_validator("document_extract_concurrency", "document_chunk_max_tokens")
    @classmethod
    def reject_null_document_numbers(cls, value: int | None) -> int:
        if value is None:
            raise ValueError("知识库解析参数不能为 null")
        return value

    @field_validator("document_chunk_mode")
    @classmethod
    def reject_null_chunk_mode(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("切片模式不能为 null")
        return value

    @field_validator("llm_timeout_ms", "llm_max_retries")
    @classmethod
    def reject_null_llm_resilience_fields(cls, value: int | None) -> int:
        if value is None:
            raise ValueError("模型超时与重试次数不能为 null")
        return value

    @field_validator("llm_providers")
    @classmethod
    def validate_provider_chain(cls, value: list[LLMProviderEntry] | None) -> list[LLMProviderEntry] | None:
        """`id` phải duy nhất — nó là khoá để giữ key cũ và để đối chiếu log lỗi."""
        if value is None:
            return value
        seen: set[str] = set()
        for entry in value:
            if entry.id in seen:
                raise ValueError(f"id của provider bị trùng: {entry.id}")
            seen.add(entry.id)
        return value
