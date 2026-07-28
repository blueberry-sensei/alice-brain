from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal, get_session
from sag_api.core.deps import get_current_user
from sag_api.core.errors import ApiError, ConfigurationError, ConflictError, ValidationError
from sag_api.core.llm_routing import ChainRunner, recent_attempts
from sag_api.core.logging import get_logger
from sag_api.core.model_providers import model_provider_catalog
from sag_api.db.models import Source, User
from sag_api.generation import LLMClient
from sag_api.mcp.server import MCP_TOOL_DETAILS, MCP_TOOL_NAMES
from sag_api.schemas.system import (
    LLMProviderEntry,
    ModelConfigUpdate,
    SubAgentConfigUpdate,
    SubAgentModelDiscoveryRequest,
    SystemPreferencesUpdate,
)
from sag_api.services import settings_service
from sag_api.services.sub_agent_discovery import discover_sub_agent_models

router = APIRouter(prefix="/system", tags=["system"])
log = get_logger("system")


def _capabilities() -> dict:
    return {
        "llm_configured": settings.llm_configured,
        # Provider đang ở đầu chuỗi (nơi mọi lời gọi bắt đầu). Số lượng cho biết còn mấy nhà dự bị.
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "llm_provider_count": len(settings.llm_chain),
        "context_window": settings.llm_context_window,
        "embedding_model": settings.embedding_model,
        "document_parser": settings.document_parser,
        "effective_document_parser": settings.effective_document_parser,
        "vector_provider": settings.sag_vector_provider,
        "language": settings.sag_language,
        "search_strategy": settings.search_strategy,
        "timezone": settings.timezone,
        "max_upload_mb": settings.max_upload_mb,
        "allowed_upload_exts": sorted(settings.allowed_upload_exts),
    }


@router.get("/health")
async def health() -> dict:
    """Liveness probe: 200 while the process runs (no dependency is touched)."""
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> JSONResponse:
    """Readiness probe: 200 only when the database is reachable, otherwise 503 (for the compose/K8s health check)."""
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        log.warning("The readiness check failed: %s", e)
        return JSONResponse(status_code=503, content={"status": "unavailable", "db": False})
    return JSONResponse(content={"status": "ready", "db": True})


@router.get("/capabilities")
async def capabilities() -> dict:
    """Capability probe: lets the frontend tell whether an LLM is configured, which engine backend is in use and so on."""
    return _capabilities()


@router.get("/model-config")
async def get_model_config(
    _user: User = Depends(get_current_user),
) -> dict:
    """The model and retrieval configuration in effect (secrets masked to *_set booleans)."""
    return settings_service.effective_model_config()


@router.get("/model-providers")
async def get_model_providers(
    _user: User = Depends(get_current_user),
) -> list[dict[str, object]]:
    """The model connectivity capabilities and technical defaults shared by the frontend and backend."""
    return model_provider_catalog()


@router.get("/preferences")
async def get_system_preferences(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str | bool]:
    """Presentation preferences shared by this local-first installation."""
    return await settings_service.get_system_preferences(session)


@router.put("/preferences")
async def update_system_preferences(
    body: SystemPreferencesUpdate,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str | bool]:
    return await settings_service.save_system_preferences(
        session,
        body.model_dump(exclude_unset=True),
    )


@router.get("/sub-agent-config")
async def get_sub_agent_config(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Registry provider/model của sub-agent; credential chỉ trả trạng thái đã đặt."""
    return await settings_service.get_sub_agent_config(session)


@router.put("/sub-agent-config")
async def update_sub_agent_config(
    body: SubAgentConfigUpdate,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    discovered: dict[str, set[str]] = {}
    for entry in body.entries:
        if entry.provider == "custom":
            continue
        # Key mới luôn phải được provider xác nhận trước khi lưu. Slot đang bật cũng luôn
        # được đối chiếu live để model đã bị gỡ không tiếp tục mang nhãn "đã verify".
        if not entry.credential and not entry.enabled:
            continue
        credential = entry.credential or await settings_service.stored_sub_agent_credential(
            session,
            entry.provider,
        )
        if not credential:
            raise ConfigurationError(
                f"{entry.provider} cần API key để lấy danh sách model",
                code="sub_agent_credential_required",
            )
        models = await discover_sub_agent_models(entry.provider, credential)
        discovered[entry.provider] = set(models)
        if entry.model and entry.model not in discovered[entry.provider]:
            raise ValidationError(
                f"Model {entry.model!r} không có trong danh sách live của {entry.provider}",
                code="sub_agent_model_not_available",
            )
    return await settings_service.save_sub_agent_config(
        session,
        [entry.model_dump() for entry in body.entries],
        discovered_models=discovered,
    )


@router.post("/sub-agent-config/models")
async def get_sub_agent_models(
    body: SubAgentModelDiscoveryRequest,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Xác thực API key rồi lấy danh sách model live; không lưu key được gửi trong request."""
    credential = body.credential or await settings_service.stored_sub_agent_credential(
        session,
        body.provider,
    )
    if not credential:
        raise ConfigurationError(
            f"{body.provider} cần API key để lấy danh sách model",
            code="sub_agent_credential_required",
        )
    models = await discover_sub_agent_models(body.provider, credential)
    return {"provider": body.provider, "models": models}


@router.get("/model-setup")
async def get_model_setup_status(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """On first entry, decide whether the quick model configuration should be shown."""
    return await settings_service.model_setup_status(session)


@router.get("/mcp")
async def knowledge_mcp_descriptor(
    request: Request,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return the details for mounting the whole SAG knowledge base in an external MCP host."""
    source_count = await session.scalar(select(func.count(Source.id))) or 0
    base = str(request.base_url).rstrip("/")
    return {
        "name": "SAG knowledge base",
        "scope": "knowledge_base",
        "source_count": source_count,
        "tools": list(MCP_TOOL_NAMES),
        "tool_details": list(MCP_TOOL_DETAILS),
        "http": {
            "transport": "streamable-http",
            "url": f"{base}/mcp/",
            "headers": {"Authorization": "Bearer <SAG_TOKEN>"},
            "note": (
                "Every source is exposed by default; hosts such as Dify should use the streamable_http / Streamable HTTP transport, "
                "and ?source_id=<id> can be added to the URL to narrow it to one source temporarily."
            ),
        },
        "stdio": {
            "command": "python",
            "args": ["-m", "sag_api.mcp.server"],
            "env": {},
            "note": "Every source is exposed by default; set SAG_MCP_SOURCE_ID to narrow it to one source.",
        },
    }


@router.put("/model-config")
async def update_model_config(
    body: ModelConfigUpdate,
    request: Request,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Save the runtime configuration; the engine is only rebuilt when the model/vector configuration really changed."""
    patch = body.model_dump(exclude_unset=True)
    before = settings_service.effective_model_config()
    config = await settings_service.save_model_config(session, patch)

    # Saving the parser or retrieval parameters need not disturb a warm engine; only a real engine configuration change forces a safe rebuild.
    engine_fields = {
        "llm_providers",
        "llm_temperature",
        "llm_max_tokens",
        "llm_timeout_ms",
        "llm_max_retries",
        "embedding_model",
        "embedding_base_url",
        "embedding_dimensions",
        "sag_language",
    }
    engine_changed = any(before.get(key) != config.get(key) for key in engine_fields)
    engine_changed = engine_changed or bool(patch.get("embedding_api_key"))
    if engine_changed:
        await request.app.state.engine_manager.aclose_all()
        # Provider vừa bị tắt vì sai key đáng được thử lại với key mới → xoá trạng thái cũ.
        request.app.state.llm.runner.reset()
    return {"config": config, "capabilities": _capabilities()}


@router.get("/model-config/attempts")
async def get_provider_attempts(
    request: Request,
    limit: int = 50,
    _user: User = Depends(get_current_user),
) -> dict:
    """Lịch sử gọi provider gần đây + tình trạng từng provider trong chuỗi.

    Đây là chỗ để thấy **vì sao** một provider bị bỏ qua (429 / sai key / model không có),
    thay vì chỉ thấy câu trả lời im lặng đến từ nhà khác.
    """
    return {
        "attempts": recent_attempts(max(1, min(limit, 200))),
        "health": request.app.state.llm.health(),
    }


async def _probe(llm: LLMClient, label: str) -> tuple[bool, str]:
    """Thử đúng một lượt chat — đó là toàn bộ thứ engine dùng.

    Không thử structured output nữa: engine không gửi `response_format` (alicecore
    `core/ai/base.py`), JSON lấy theo prompt rồi bóc + kiểm schema + thử lại.
    """
    try:
        await llm.complete([{"role": "user", "content": "ping"}])
    except ApiError as e:
        return False, f"Thất bại · {label} · {e.message}"
    except Exception as e:  # noqa: BLE001
        return False, f"Thất bại · {label} · {e}"
    return True, f"Chạy được · {label}"


@router.post("/model-config/test")
async def test_model_config(
    request: Request,
    body: LLMProviderEntry | None = None,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Thử **một** provider. Không lưu, không chạm vào singleton đang chạy.

    Gửi entry đang soạn trên form để thử trước khi lưu. Nếu `api_key` để trống mà `id` đã có
    trong DB thì dùng lại key đã lưu — người dùng không phải dán lại key chỉ để bấm Test.
    Việc tái dùng đó **chỉ xảy ra khi entry vẫn trỏ đúng endpoint đã lưu**: đổi `base_url`
    hay `provider` là phải nhập lại key, kẻo endpoint này thành đường gửi key ra host lạ.
    Không truyền body = thử provider đầu chuỗi hiện hành.
    """
    if body is None:
        llm = request.app.state.llm
        if not llm.configured:
            return {"ok": False, "message": "Chưa cấu hình provider nào"}
        ok, message = await _probe(llm, f"{settings.llm_provider} / {settings.llm_model}")
        return {"ok": ok, "message": message}

    entry = body.model_dump()
    if not entry.get("api_key"):
        entry["api_key"] = await settings_service.stored_provider_key(
            session,
            body.id,
            provider=body.provider,
            base_url=body.base_url,
        )
    if not entry.get("api_key"):
        return {
            "ok": False,
            "message": "Chưa có API key dùng được cho provider này (đổi endpoint thì phải nhập lại key)",
        }

    # Chuỗi chỉ gồm đúng entry đang thử, runner riêng → không làm bẩn cooldown của bản đang chạy.
    probe = LLMClient(settings.model_copy(update={"llm_providers": [entry]}), ChainRunner())
    ok, message = await _probe(probe, f"{entry['provider']} / {entry['model']}")
    return {"ok": ok, "message": message}
