"""运行期模型与知识库配置 —— 以 DB 为唯一事实来源，覆盖 `settings` 单例。

「模型与检索」配置存进 `settings` 表（scope=global, key=model_config）。启动时与保存后
**就地覆盖 `settings` 单例**，端点再重建 `LLMClient` / 重置暖引擎，使改动**无需重启即生效**。

两条重要约定：

1. **LLM 只能在 UI 配置。** 凭据放在 `llm_providers`（优先级链），环境变量 `SAG_LLM_*` 不再
   参与——启动时若 DB 里没有链，扁平凭据会被**清空**，免得出现「.env 里还有一把旧 key
   偷偷生效」这种两个事实来源的局面。
2. **api_key 加密入库**（AES-GCM，见 `core/crypto.py`），读取时只回 `api_key_set` 布尔。
"""

from __future__ import annotations

from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sag_api.core.config import Settings
from sag_api.core.config import settings as _settings
from sag_api.core.crypto import decrypt_secret, encrypt_secret
from sag_api.core.errors import ConfigurationError
from sag_api.core.logging import get_logger
from sag_api.core.model_providers import get_model_provider
from sag_api.db.models import Setting
from sag_api.enums import SEARCH_STRATEGIES, normalize_search_strategy

_SCOPE = "global"
_KEY = "model_config"
_PREFERENCES_KEY = "system_preferences"
log = get_logger("settings")

# 允许运行期覆盖的字段（值已由请求 schema 校验/转型）
_FIELDS = frozenset(
    {
        "llm_providers",
        "llm_temperature",
        "llm_max_tokens",
        "llm_context_window",
        "llm_timeout_ms",
        "llm_max_retries",
        "embedding_model",
        "embedding_base_url",
        "embedding_api_key",
        "embedding_dimensions",
        "document_parser",
        "document_extract_concurrency",
        "document_chunk_max_tokens",
        "document_chunk_mode",
        "search_strategy",
        "search_top_k",
        "sag_language",
    }
)
_SECRET_FIELDS = frozenset({"embedding_api_key"})
_NULLABLE_FIELDS = frozenset({"embedding_base_url", "embedding_dimensions"})

_OPENAI_COMPATIBLE = get_model_provider("openai")

DEFAULT_PRESET = {
    "llm_temperature": _OPENAI_COMPATIBLE.default_temperature,
    "llm_max_tokens": 20_000,
    "llm_context_window": _OPENAI_COMPATIBLE.default_context_window,
    "llm_timeout_ms": 60_000,
    "llm_max_retries": 2,
    "embedding_dimensions": 1024,
    "document_parser": "markitdown",
    "document_extract_concurrency": 5,
    "document_chunk_max_tokens": 1_000,
    "document_chunk_mode": "standard",
    "search_strategy": "vector",
    "search_top_k": 8,
    "sag_language": "en",
}



async def _load_row(session: AsyncSession, key: str = _KEY) -> Setting | None:
    return await session.scalar(select(Setting).where(Setting.scope == _SCOPE, Setting.key == key))


def _normalize_overrides(overrides: dict) -> dict:
    """清理持久化配置，确保已下线或非法策略不会进入运行时。"""
    normalized = dict(overrides)
    strategy = normalized.get("search_strategy")
    if strategy == "atomic":
        normalized["search_strategy"] = normalize_search_strategy(strategy)
        log.warning("旧检索策略 atomic 已迁移为精确模式 multi")
    elif strategy is not None and strategy not in SEARCH_STRATEGIES:
        normalized.pop("search_strategy", None)
        log.warning("忽略非法的持久化检索策略：%s", strategy)
    return normalized


def _provider_entries(overrides: dict) -> list[dict]:
    raw = overrides.get("llm_providers")
    return [dict(entry) for entry in raw] if isinstance(raw, list) else []


def _encrypt_entries(entries: list[dict], previous: list[dict]) -> list[dict]:
    """Mã hoá key của từng entry trước khi ghi DB; entry gửi key rỗng thì giữ key cũ theo `id`."""
    kept = {entry.get("id"): entry.get("api_key") for entry in previous if entry.get("api_key")}
    prepared: list[dict] = []
    for entry in entries:
        item = dict(entry)
        submitted = (item.get("api_key") or "").strip()
        if submitted:
            item["api_key"] = encrypt_secret(submitted, _settings.secret_key)
        else:
            # Không gửi key mới → giữ nguyên ciphertext cũ. Đây là điều kiện để UI có thể
            # sửa nhãn / thứ tự / model mà không phải nhập lại key.
            existing = kept.get(item.get("id"))
            if existing:
                item["api_key"] = existing
            else:
                item.pop("api_key", None)
        prepared.append(item)
    return prepared


def _decrypt_entries(entries: list[dict]) -> list[dict]:
    """Giải mã key để dùng lúc chạy; entry nào không giải được thì **tắt** và nêu lý do."""
    resolved: list[dict] = []
    for entry in entries:
        item = dict(entry)
        stored = item.get("api_key") or ""
        if stored:
            plain = decrypt_secret(stored, _settings.secret_key)
            if plain is None:
                log.error(
                    "Provider %s: không giải mã được API key → tạm tắt, cần nhập lại trên UI",
                    item.get("id"),
                )
                item["enabled"] = False
                item["api_key"] = ""
                item["error"] = "credential_undecryptable"
            else:
                item["api_key"] = plain
        resolved.append(item)
    return resolved


def _masked_entries(entries: list[dict]) -> list[dict]:
    """Bản cho client: không bao giờ trả key (kể cả ciphertext), chỉ trả đã đặt hay chưa."""
    masked: list[dict] = []
    for entry in entries:
        item = {key: value for key, value in entry.items() if key != "api_key"}
        item["api_key_set"] = bool(entry.get("api_key"))
        masked.append(item)
    return masked


def _sync_flat_head(settings: Settings, entries: list[dict]) -> None:
    """Đồng bộ entry ưu tiên cao nhất vào các trường `llm_*` phẳng.

    Nhiều nơi trong ứng dụng chỉ cần biết "đang dùng model nào" (capabilities, litellm policy,
    embedding tái dùng credential). Cho chúng đọc ảnh chiếu của đầu chuỗi thay vì bắt mọi chỗ
    hiểu khái niệm chuỗi. Chuỗi rỗng → **xoá sạch** credential phẳng để env không thể lén tác dụng.
    """
    chain = sorted(
        (e for e in entries if e.get("enabled", True) and e.get("api_key") and e.get("model")),
        key=lambda e: e.get("priority", 100),
    )
    if not chain:
        settings.llm_api_key = None
        settings.llm_model = ""
        settings.llm_base_url = None
        return
    head = chain[0]
    settings.llm_provider = head.get("provider") or _OPENAI_COMPATIBLE.id
    settings.llm_api_key = head.get("api_key")
    settings.llm_model = head.get("model") or ""
    settings.llm_base_url = head.get("base_url") or None
    if head.get("extra_body"):
        settings.llm_extra_body = dict(head["extra_body"])


async def load_overrides(session: AsyncSession) -> dict:
    row = await _load_row(session)
    raw = dict(row.value) if row and isinstance(row.value, dict) else {}
    return _normalize_overrides(raw)


def _same_endpoint(left: str | None, right: str | None) -> bool:
    """Hai base_url có trỏ về cùng một nơi không (bỏ qua khác biệt vô nghĩa)."""
    return (left or "").strip().rstrip("/").casefold() == (right or "").strip().rstrip("/").casefold()


async def stored_provider_key(
    session: AsyncSession,
    provider_id: str,
    *,
    provider: str,
    base_url: str | None,
) -> str | None:
    """Key (đã giải mã) của một provider đã lưu — dùng cho nút Test khi form không nhập lại key.

    Chỉ trả key khi entry đang thử **vẫn trỏ đúng chỗ cũ** (cùng provider, cùng base_url).
    Nếu không có ràng buộc này thì bất cứ ai gọi được API cũng bảo server gửi key đã lưu
    tới một host tuỳ ý — biến "API không bao giờ trả key ra" thành lời hứa suông.
    """
    row = await _load_row(session)
    stored = dict(row.value) if row and isinstance(row.value, dict) else {}
    for entry in _provider_entries(stored):
        if entry.get("id") != provider_id or not entry.get("api_key"):
            continue
        if entry.get("provider") != provider or not _same_endpoint(entry.get("base_url"), base_url):
            log.warning(
                "Từ chối tái dùng key của provider %s: endpoint gửi lên khác endpoint đã lưu",
                provider_id,
            )
            return None
        return decrypt_secret(str(entry["api_key"]), _settings.secret_key)
    return None


async def model_setup_status(session: AsyncSession) -> dict[str, bool]:
    """判断是否需要首次模型配置。

    只看 DB：LLM 只能在 UI 配置，环境变量不再是一条有效路径，所以「已配置」= 库里有
    至少一个启用且带 key 的 provider。
    """
    row = await _load_row(session)
    stored = dict(row.value) if row and isinstance(row.value, dict) else {}
    entries = _provider_entries(stored)
    database_configured = any(
        entry.get("enabled", True) and entry.get("api_key") and entry.get("model") for entry in entries
    )
    return {
        "required": not database_configured,
        "environment_configured": False,
        "database_configured": database_configured,
    }


def apply_overrides(settings: Settings, overrides: dict) -> None:
    """把存储的覆盖值就地写回 settings 单例（请求 schema 已保证类型合法）。

    `llm_providers` 会被解密后写入（运行期需要明文），并同步出扁平的 `llm_*` 头部字段。
    """
    normalized = _normalize_overrides(overrides)
    for key, value in normalized.items():
        if key not in _FIELDS or key == "llm_providers":
            continue
        if key in _SECRET_FIELDS and isinstance(value, str) and value:
            plain = decrypt_secret(value, settings.secret_key)
            if plain is None:
                log.error("Không giải mã được %s → coi như chưa đặt", key)
                setattr(settings, key, None)
                continue
            setattr(settings, key, plain)
            continue
        setattr(settings, key, value)

    entries = _decrypt_entries(_provider_entries(normalized))
    settings.llm_providers = entries
    _sync_flat_head(settings, entries)


async def apply_startup_overrides(session_factory: async_sessionmaker) -> None:
    """启动时：把 DB 里的模型配置覆盖到 settings 单例（在构建 LLMClient 之前调用）。"""
    async with session_factory() as session:
        row = await _load_row(session)
        raw = dict(row.value) if row and isinstance(row.value, dict) else {}
        overrides = _normalize_overrides(raw)
        if row is not None and overrides != raw:
            # JSON 列未使用 MutableDict，必须整体重新赋值才能可靠持久化。
            row.value = overrides
            await session.commit()
        apply_overrides(_settings, overrides)
        preferences = await _load_row(session, _PREFERENCES_KEY)
        preference_values = dict(preferences.value) if preferences and isinstance(preferences.value, dict) else {}
        timezone = preference_values.get("timezone")
        if isinstance(timezone, str):
            # Stored values were validated on write. Settings assignment is kept
            # explicit so model configuration and presentation preferences remain separate.
            try:
                ZoneInfo(timezone)
            except (ZoneInfoNotFoundError, ValueError):
                log.warning("忽略非法的持久化时区：%s", timezone)
            else:
                _settings.timezone = timezone


def effective_model_config() -> dict:
    """当前生效的模型配置（读 settings 单例；密钥脱敏为 *_set 布尔）。"""
    return {
        "llm_providers": _masked_entries(_settings.llm_providers),
        # Ảnh chiếu của entry đầu chuỗi — chỉ để hiển thị "đang dùng gì", không phải nơi cấu hình.
        "llm_active_provider": _settings.llm_provider,
        "llm_active_model": _settings.llm_model,
        "llm_temperature": _settings.llm_temperature,
        "llm_max_tokens": _settings.llm_max_tokens,
        "llm_context_window": _settings.llm_context_window,
        "llm_timeout_ms": _settings.llm_timeout_ms,
        "llm_max_retries": _settings.llm_max_retries,
        "llm_configured": _settings.llm_configured,
        "embedding_model": _settings.embedding_model,
        "embedding_base_url": _settings.embedding_base_url,
        "embedding_dimensions": _settings.embedding_dimensions,
        "embedding_api_key_set": bool(_settings.embedding_api_key),
        "document_parser": _settings.document_parser,
        "effective_document_parser": _settings.effective_document_parser,
        "document_extract_concurrency": _settings.document_extract_concurrency,
        "document_chunk_max_tokens": _settings.document_chunk_max_tokens,
        "document_chunk_mode": _settings.document_chunk_mode,
        "search_strategy": _settings.search_strategy,
        "search_top_k": _settings.search_top_k,
        "sag_language": _settings.sag_language,
    }


def effective_system_preferences() -> dict[str, str]:
    return {"timezone": _settings.timezone}


async def save_system_preferences(session: AsyncSession, patch: dict) -> dict[str, str]:
    row = await _load_row(session, _PREFERENCES_KEY)
    stored = dict(row.value) if row and isinstance(row.value, dict) else {}
    timezone = patch.get("timezone")
    if isinstance(timezone, str):
        stored["timezone"] = timezone

    if row is None:
        session.add(Setting(scope=_SCOPE, key=_PREFERENCES_KEY, value=stored))
    else:
        row.value = stored
    await session.commit()

    if isinstance(stored.get("timezone"), str):
        _settings.timezone = stored["timezone"]
    return effective_system_preferences()


async def save_model_config(session: AsyncSession, patch: dict) -> dict:
    """合并保存模型配置：入库 + 覆盖 settings 单例；返回生效配置（脱敏）。

    约定（配合 `exclude_unset`）：
    - 字段未出现 → 保持不变；
    - 密钥字段值为空 → 忽略（保留原密钥，避免误清空）；空值仅经显式非空覆盖；
    - 可空字段（base_url / dimensions）值为空 → 置 None（清除）。
    """
    row = await _load_row(session)
    raw = dict(row.value) if row and isinstance(row.value, dict) else {}
    stored = _normalize_overrides(raw)
    previous_entries = _provider_entries(stored)

    for key, value in patch.items():
        if key not in _FIELDS:
            continue
        if key == "llm_providers":
            # Danh sách gửi lên **thay thế toàn bộ** (xoá entry = không gửi entry đó nữa).
            # Key rỗng trong entry = giữ key cũ theo id, xem _encrypt_entries.
            stored["llm_providers"] = _encrypt_entries(
                [dict(entry) for entry in (value or [])],
                previous_entries,
            )
            continue
        if key in _SECRET_FIELDS:
            if value:  # 仅非空才更新；空/None 保留原值
                stored[key] = encrypt_secret(str(value), _settings.secret_key)
            continue
        if key in _NULLABLE_FIELDS and (value is None or value == ""):
            stored[key] = None
            continue
        stored[key] = value

    stored = _normalize_overrides(stored)

    if row is None:
        session.add(Setting(scope=_SCOPE, key=_KEY, value=stored))
    else:
        row.value = stored
    await session.commit()

    apply_overrides(_settings, stored)
    return effective_model_config()
