from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal, get_session
from sag_api.core.deps import get_current_user
from sag_api.core.errors import ApiError, ConflictError
from sag_api.core.llm_routing import ChainRunner, recent_attempts
from sag_api.core.logging import get_logger
from sag_api.core.model_providers import model_provider_catalog
from sag_api.db.models import Source, User
from sag_api.generation import LLMClient
from sag_api.mcp.server import MCP_TOOL_DETAILS, MCP_TOOL_NAMES
from sag_api.schemas.system import (
    LLMProviderEntry,
    ModelConfigUpdate,
    SystemPreferencesUpdate,
)
from sag_api.services import settings_service

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
    """存活探针：进程在跑即 200（不触碰依赖）。"""
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> JSONResponse:
    """就绪探针：数据库可连通才 200，否则 503（供 compose/K8s 健康检查）。"""
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        log.warning("就绪检查失败：%s", e)
        return JSONResponse(status_code=503, content={"status": "unavailable", "db": False})
    return JSONResponse(content={"status": "ready", "db": True})


@router.get("/capabilities")
async def capabilities() -> dict:
    """能力探测：供前端判断是否已配置 LLM、当前引擎后端等。"""
    return _capabilities()


@router.get("/model-config")
async def get_model_config(
    _user: User = Depends(get_current_user),
) -> dict:
    """当前生效的模型与检索配置（密钥脱敏为 *_set 布尔）。"""
    return settings_service.effective_model_config()


@router.get("/model-providers")
async def get_model_providers(
    _user: User = Depends(get_current_user),
) -> list[dict[str, object]]:
    """前后端共享的模型接入能力与技术默认值。"""
    return model_provider_catalog()


@router.get("/preferences")
async def get_system_preferences(
    _user: User = Depends(get_current_user),
) -> dict[str, str]:
    """Presentation preferences shared by this local-first installation."""
    return settings_service.effective_system_preferences()


@router.put("/preferences")
async def update_system_preferences(
    body: SystemPreferencesUpdate,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    return await settings_service.save_system_preferences(
        session,
        body.model_dump(exclude_unset=True),
    )


@router.get("/model-setup")
async def get_model_setup_status(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """首次进入时判断是否需要展示快捷模型配置。"""
    return await settings_service.model_setup_status(session)


@router.get("/mcp")
async def knowledge_mcp_descriptor(
    request: Request,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """返回将整个 SAG 知识库挂入外部 MCP 宿主的连接信息。"""
    source_count = await session.scalar(select(func.count(Source.id))) or 0
    base = str(request.base_url).rstrip("/")
    return {
        "name": "SAG 知识库",
        "scope": "knowledge_base",
        "source_count": source_count,
        "tools": list(MCP_TOOL_NAMES),
        "tool_details": list(MCP_TOOL_DETAILS),
        "http": {
            "transport": "streamable-http",
            "url": f"{base}/mcp/",
            "headers": {"Authorization": "Bearer <SAG_TOKEN>"},
            "note": (
                "默认开放全部信源；Dify 等宿主请使用 streamable_http/Streamable HTTP 传输，"
                "可在 URL 添加 ?source_id=<id> 临时限定单个信源。"
            ),
        },
        "stdio": {
            "command": "python",
            "args": ["-m", "sag_api.mcp.server"],
            "env": {},
            "note": "默认开放全部信源；设置 SAG_MCP_SOURCE_ID 可限定单个信源。",
        },
    }


@router.put("/model-config")
async def update_model_config(
    body: ModelConfigUpdate,
    request: Request,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """保存运行期配置；仅在模型/向量配置实际变化时安全重建引擎。"""
    patch = body.model_dump(exclude_unset=True)
    before = settings_service.effective_model_config()
    config = await settings_service.save_model_config(session, patch)

    # 解析器/检索参数保存无需打断暖引擎；只有引擎配置真的变化才安全重建。
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
        try:
            await llm.complete([{"role": "user", "content": "ping"}])
        except ApiError as e:
            return {"ok": False, "message": e.message}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}
        return {"ok": True, "message": f"Kết nối được · {settings.llm_provider} / {settings.llm_model}"}

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
    try:
        await probe.complete([{"role": "user", "content": "ping"}])
    except ApiError as e:
        return {"ok": False, "message": e.message}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e)}
    return {"ok": True, "message": f"Kết nối được · {entry['provider']} / {entry['model']}"}
