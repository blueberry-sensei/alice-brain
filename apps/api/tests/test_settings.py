"""The model configuration endpoints: a masked GET, a PUT that persists and takes effect, secret retention, 422 on an invalid value, the connection test (offline).

Fully offline and **leaving no global side effect**: `finally` deletes the settings table rows and restores the `settings` singleton fields that were changed,
so nothing leaks across tests (the endpoint overrides the process-level singleton in place). The connection test only exercises the "not configured" branch (no network).
"""

import httpx
import pytest

from sag_api.core.config import Settings, settings

_RESTORE = (
    "llm_providers",
    "llm_provider",
    "llm_base_url",
    "llm_model",
    "llm_temperature",
    "llm_timeout_ms",
    "llm_max_retries",
    "search_strategy",
    "document_chunk_max_tokens",
    "document_chunk_mode",
    "search_top_k",
    "sag_language",
    "llm_api_key",
    "document_parser",
    "timezone",
    "document_extract_concurrency",
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://localhost:3000", ["http://localhost:3000"]),
        (
            "http://localhost:3000,https://sag.example.com",
            ["http://localhost:3000", "https://sag.example.com"],
        ),
        ('["http://localhost:3000"]', ["http://localhost:3000"]),
    ],
)
def test_cors_origins_env_formats(monkeypatch, raw, expected):
    monkeypatch.setenv("SAG_CORS_ORIGINS", raw)
    assert Settings(_env_file=None).cors_origins == expected


def test_legacy_atomic_env_strategy_maps_to_precise(monkeypatch):
    monkeypatch.setenv("SAG_SEARCH_STRATEGY", "atomic")
    assert Settings(_env_file=None).search_strategy == "multi"


def test_timezone_defaults_to_utc_and_rejects_invalid(monkeypatch):
    monkeypatch.delenv("SAG_TIMEZONE", raising=False)
    assert Settings(_env_file=None).timezone == "UTC"
    monkeypatch.setenv("SAG_TIMEZONE", "UTC")
    assert Settings(_env_file=None).timezone == "UTC"
    monkeypatch.setenv("SAG_TIMEZONE", "Mars/Olympus")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


@pytest.mark.asyncio
async def test_system_preferences_persist_timezone_and_report_configuration():
    from sqlalchemy import delete

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Setting
    from sag_api.services.settings_service import (
        get_system_preferences,
        save_system_preferences,
    )

    await init_db()
    previous = settings.timezone
    try:
        async with SessionLocal() as session:
            await session.execute(
                delete(Setting).where(
                    Setting.scope == "global",
                    Setting.key == "system_preferences",
                )
            )
            await session.commit()
            settings.timezone = "UTC"

            initial = await get_system_preferences(session)
            assert initial == {"timezone": "UTC", "timezone_configured": False}

            saved = await save_system_preferences(
                session,
                {"timezone": "Asia/Ho_Chi_Minh"},
            )
            assert saved == {
                "timezone": "Asia/Ho_Chi_Minh",
                "timezone_configured": True,
            }
            assert await get_system_preferences(session) == saved
    finally:
        async with SessionLocal() as session:
            await session.execute(
                delete(Setting).where(
                    Setting.scope == "global",
                    Setting.key == "system_preferences",
                )
            )
            await session.commit()
        settings.timezone = previous


def test_provider_base_urls_have_no_third_party_default(monkeypatch):
    """Không gateway bên thứ ba nào được cấu hình sẵn — người dùng tự khai báo endpoint."""
    for name in ("SAG_LLM_BASE_URL", "SAG_EMBEDDING_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    configured = Settings(_env_file=None)
    assert configured.llm_provider == "openai"
    assert configured.llm_base_url is None
    assert configured.embedding_base_url is None


def test_default_model_output_limit_is_20000(monkeypatch):
    monkeypatch.delenv("SAG_LLM_MAX_TOKENS", raising=False)
    assert Settings(_env_file=None).llm_max_tokens == 20_000


@pytest.mark.asyncio
async def test_legacy_atomic_db_strategy_is_migrated():
    from sqlalchemy import delete, select

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Setting
    from sag_api.services.settings_service import apply_startup_overrides

    await init_db()
    previous = {
        field: getattr(settings, field)
        for field in (
            "search_strategy",
        )
    }
    try:
        async with SessionLocal() as session:
            await session.execute(delete(Setting).where(Setting.scope == "global", Setting.key == "model_config"))
            session.add(
                Setting(
                    scope="global",
                    key="model_config",
                    value={"search_strategy": "atomic"},
                )
            )
            await session.commit()

        await apply_startup_overrides(SessionLocal)
        assert settings.search_strategy == "multi"

        async with SessionLocal() as session:
            row = await session.scalar(select(Setting).where(Setting.scope == "global", Setting.key == "model_config"))
            assert row is not None
            assert row.value["search_strategy"] == "multi"
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Setting).where(Setting.scope == "global", Setting.key == "model_config"))
            await session.commit()
        for field, value in previous.items():
            setattr(settings, field, value)


async def _register(c, email):
    r = await c.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    assert r.json()["user"]["created_at"].endswith(("Z", "+00:00"))
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_sub_agent_config_live_models_and_encrypted_credentials(
    monkeypatch: pytest.MonkeyPatch,
):
    from sqlalchemy import delete, select

    from sag_api.api.v1 import system as system_api
    from sag_api.core.errors import ValidationError
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Setting
    from sag_api.main import app

    observed: list[tuple[str, str]] = []

    async def fake_discovery(provider: str, credential: str) -> list[str]:
        observed.append((provider, credential))
        if credential == "bad-key":
            raise ValidationError("invalid", code="sub_agent_credential_invalid")
        return {
            "claude": ["claude-live-a", "claude-live-b"],
            "codex": ["gpt-live"],
            "opencode-go": ["opencode-go/live"],
            "opencode-zen": ["opencode/live"],
            "gemini-cli": ["gemini-live"],
        }[provider]

    monkeypatch.setattr(system_api, "discover_sub_agent_models", fake_discovery)
    transport = httpx.ASGITransport(app=app)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                auth = await _register(c, "subagents@t.com")

                initial = await c.get("/api/v1/system/sub-agent-config", headers=auth)
                assert initial.status_code == 200
                assert [item["id"] for item in initial.json()["providers"]] == [
                    "claude",
                    "codex",
                    "opencode-go",
                    "opencode-zen",
                    "gemini-cli",
                    "custom",
                ]
                assert all("models" not in item for item in initial.json()["providers"])
                assert all("default_model" not in item for item in initial.json()["providers"])
                assert initial.json()["entries"] == []

                missing_key = await c.post(
                    "/api/v1/system/sub-agent-config/models",
                    headers=auth,
                    json={"provider": "claude"},
                )
                assert missing_key.status_code == 400
                assert missing_key.json()["error"]["code"] == "sub_agent_credential_required"

                invalid_key = await c.post(
                    "/api/v1/system/sub-agent-config/models",
                    headers=auth,
                    json={"provider": "claude", "credential": "bad-key"},
                )
                assert invalid_key.status_code == 422
                assert invalid_key.json()["error"]["code"] == "sub_agent_credential_invalid"

                live = await c.post(
                    "/api/v1/system/sub-agent-config/models",
                    headers=auth,
                    json={"provider": "claude", "credential": "claude-secret"},
                )
                assert live.status_code == 200
                assert live.json()["models"] == ["claude-live-a", "claude-live-b"]

                custom_discovery = await c.post(
                    "/api/v1/system/sub-agent-config/models",
                    headers=auth,
                    json={"provider": "custom", "credential": "custom-secret"},
                )
                assert custom_discovery.status_code == 422

                unavailable_model = await c.put(
                    "/api/v1/system/sub-agent-config",
                    headers=auth,
                    json={
                        "entries": [
                            {
                                "provider": "claude",
                                "model": "model-tu-nhap",
                                "credential": "claude-secret",
                                "enabled": True,
                            }
                        ]
                    },
                )
                assert unavailable_model.status_code == 422
                assert (
                    unavailable_model.json()["error"]["code"]
                    == "sub_agent_model_not_available"
                )

                saved = await c.put(
                    "/api/v1/system/sub-agent-config",
                    headers=auth,
                    json={
                        "entries": [
                            {
                                "provider": "claude",
                                "model": "claude-live-a",
                                "credential": "claude-secret",
                                "enabled": True,
                            },
                            {
                                "provider": "custom",
                                "provider_name": "Internal coding agent",
                                "model": "private-code-model",
                                "base_url": "https://models.example/v1",
                                "credential": "custom-secret",
                                "enabled": True,
                            },
                        ]
                    },
                )
                assert saved.status_code == 200, saved.text
                entries = saved.json()["entries"]
                assert all(entry["credential_set"] is True for entry in entries)
                assert next(
                    entry for entry in entries if entry["provider"] == "claude"
                )["model_verified"] is True
                assert all("credential" not in entry for entry in entries)
                assert "claude-secret" not in saved.text
                assert "custom-secret" not in saved.text

                async with SessionLocal() as probe:
                    row = await probe.scalar(
                        select(Setting).where(
                            Setting.scope == "global",
                            Setting.key == "sub_agent_config",
                        )
                    )
                assert row is not None
                stored = row.value["entries"]
                assert all(entry["credential"].startswith("enc:v1:") for entry in stored)
                assert all("secret" not in entry["credential"] for entry in stored)

                reused = await c.post(
                    "/api/v1/system/sub-agent-config/models",
                    headers=auth,
                    json={"provider": "claude"},
                )
                assert reused.status_code == 200
                assert observed[-1] == ("claude", "claude-secret")

                kept = await c.put(
                    "/api/v1/system/sub-agent-config",
                    headers=auth,
                    json={
                        "entries": [
                            {
                                "provider": "claude",
                                "model": "claude-live-b",
                                "credential": "",
                                "enabled": True,
                            },
                            {
                                "provider": "custom",
                                "provider_name": "Internal coding agent",
                                "model": "private-code-model-v2",
                                "base_url": "https://other.example/v1",
                                "credential": "",
                                "enabled": True,
                            },
                        ]
                    },
                )
                assert kept.status_code == 200
                by_provider = {
                    entry["provider"]: entry for entry in kept.json()["entries"]
                }
                assert by_provider["claude"]["credential_set"] is True
                assert by_provider["claude"]["model_verified"] is True
                # Custom đổi endpoint mà không nhập key mới: không được gửi key cũ sang host mới.
                assert by_provider["custom"]["credential_set"] is False
    finally:
        async with SessionLocal() as session:
            await session.execute(
                delete(Setting).where(
                    Setting.scope == "global",
                    Setting.key == "sub_agent_config",
                )
            )
            await session.commit()


@pytest.mark.asyncio
async def test_model_config_crud_masking_and_test(monkeypatch: pytest.MonkeyPatch):
    from sqlalchemy import delete

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Setting
    from sag_api.main import app

    snapshot = {k: getattr(settings, k) for k in _RESTORE}
    transport = httpx.ASGITransport(app=app)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                A = await _register(c, "modelcfg@t.com")

                preferences = (await c.get("/api/v1/system/preferences", headers=A)).json()
                assert preferences["timezone"] == snapshot["timezone"]
                assert preferences["timezone_configured"] is False
                changed = await c.put(
                    "/api/v1/system/preferences",
                    headers=A,
                    json={"timezone": "UTC"},
                )
                assert changed.status_code == 200
                assert changed.json()["timezone"] == "UTC"
                assert changed.json()["timezone_configured"] is True
                assert settings.timezone == "UTC"
                assert (await c.get("/api/v1/system/capabilities")).json()["timezone"] == "UTC"
                invalid_timezone = await c.put(
                    "/api/v1/system/preferences",
                    headers=A,
                    json={"timezone": "Mars/Olympus"},
                )
                assert invalid_timezone.status_code == 422

                # GET: chưa có provider nào -> chuỗi rỗng, LLM coi như chưa cấu hình.
                body = (await c.get("/api/v1/system/model-config", headers=A)).json()
                assert body["llm_providers"] == []
                assert body["llm_configured"] is False
                assert "llm_api_key" not in body
                assert "mineru_api_key" not in body and "mineru_api_key_set" not in body
                assert body["effective_document_parser"] == "markitdown"
                assert body["document_extract_concurrency"] == 5
                assert body["document_chunk_max_tokens"] == 1_000
                assert body["document_chunk_mode"] == "standard"
                assert body["llm_timeout_ms"] == 60_000
                assert body["llm_max_retries"] == 2
                assert "search_top_k" in body and "sag_language" in body

                providers = (await c.get("/api/v1/system/model-providers", headers=A)).json()
                assert [provider["id"] for provider in providers] == [
                    "openai",
                    "anthropic",
                    "gemini",
                ]
                assert "litellm_prefix" not in providers[0]

                # The connection test (not configured) -> ok False immediately, no network
                t = (await c.post("/api/v1/system/model-config/test", headers=A)).json()
                assert t["ok"] is False and "message" in t

                # Thử một entry đang soạn: phải dùng đúng key/model chưa lưu, và **không**
                # được ghi gì vào cấu hình đang chạy.
                from sag_api.generation.llm import LLMClient

                observed: dict = {}

                # Nút Test chạy HAI bước: chat thường, rồi structured output
                # (`response_format`) — đúng thứ đường trích xuất dùng. Fake phải nhận kwarg
                # đó, nếu không là test xanh trong khi bước thứ hai chưa từng chạy.
                async def fake_complete(client, _messages, *, response_format=None):
                    entry = client._settings.llm_chain[0]
                    observed["provider"] = entry["provider"]
                    observed["model"] = entry["model"]
                    observed["key"] = entry["api_key"]
                    observed.setdefault("formats", []).append(response_format)
                    return "pong"

                monkeypatch.setattr(LLMClient, "complete", fake_complete)
                draft = await c.post(
                    "/api/v1/system/model-config/test",
                    headers=A,
                    json={
                        "id": "draft-gemini",
                        "provider": "gemini",
                        "model": "gemini-3.5-flash",
                        "api_key": "draft-secret",
                    },
                )
                assert draft.status_code == 200
                assert draft.json()["ok"] is True
                assert "gemini-3.5-flash" in draft.json()["message"]
                assert observed["provider"] == "gemini"
                assert observed["model"] == "gemini-3.5-flash"
                assert observed["key"] == "draft-secret"
                # Bước 1 không có schema, bước 2 PHẢI có — bằng chứng Test thật sự thử
                # structured output chứ không chỉ "ping".
                assert observed["formats"][0] is None
                assert observed["formats"][1]["type"] == "json_schema"
                assert "draft-secret" not in draft.text
                assert settings.llm_providers == snapshot["llm_providers"]

                # PUT chuỗi provider → persist + hiệu lực ngay + capabilities phản ánh
                r = await c.put(
                    "/api/v1/system/model-config",
                    headers=A,
                    json={
                        "llm_providers": [
                            {
                                "id": "anthropic-main",
                                "provider": "anthropic",
                                "model": "test-model-x",
                                "api_key": "sk-ant-fake",
                                "priority": 10,
                            },
                            {
                                "id": "gemini-backup",
                                "provider": "gemini",
                                "model": "gemini-3.5-flash",
                                "api_key": "AIza-fake",
                                "priority": 20,
                            },
                        ],
                        "llm_timeout_ms": 45_000,
                        "llm_max_retries": 3,
                        "document_chunk_max_tokens": 1_600,
                        "document_chunk_mode": "heading_strict",
                        "search_top_k": 5,
                        "sag_language": "en",
                    },
                )
                assert r.status_code == 200, r.text
                config = r.json()["config"]
                # Entry ưu tiên cao nhất trở thành "đang dùng"; key không bao giờ được trả về.
                assert [e["id"] for e in config["llm_providers"]] == ["anthropic-main", "gemini-backup"]
                assert all("api_key" not in e for e in config["llm_providers"])
                assert all(e["api_key_set"] is True for e in config["llm_providers"])
                assert config["llm_active_provider"] == "anthropic"
                assert config["llm_active_model"] == "test-model-x"
                assert config["llm_configured"] is True
                assert "sk-ant-fake" not in r.text and "AIza-fake" not in r.text
                assert r.json()["capabilities"]["llm_provider"] == "anthropic"
                assert r.json()["capabilities"]["llm_model"] == "test-model-x"
                assert r.json()["capabilities"]["llm_provider_count"] == 2
                assert settings.llm_model == "test-model-x"  # singleton hiệu lực ngay
                assert settings.llm_provider == "anthropic"
                assert settings.llm_timeout_ms == 45_000
                assert settings.llm_max_retries == 3
                assert settings.document_chunk_max_tokens == 1_600
                assert settings.document_chunk_mode == "heading_strict"
                g = (await c.get("/api/v1/system/model-config", headers=A)).json()
                assert g["llm_active_model"] == "test-model-x" and g["search_top_k"] == 5
                assert g["sag_language"] == "en"
                assert g["llm_timeout_ms"] == 45_000 and g["llm_max_retries"] == 3
                assert g["document_chunk_max_tokens"] == 1_600
                assert g["document_chunk_mode"] == "heading_strict"

                # Key phải được MÃ HOÁ trong DB, không plaintext.
                async with SessionLocal() as probe:
                    from sqlalchemy import select as _select

                    stored = await probe.scalar(
                        _select(Setting).where(
                            Setting.scope == "global", Setting.key == "model_config"
                        )
                    )
                assert stored is not None
                stored_entries = stored.value["llm_providers"]
                assert all(e["api_key"].startswith("enc:v1:") for e in stored_entries)
                assert all("sk-ant-fake" not in e["api_key"] for e in stored_entries)

                # Endpoint cau hinh parser cua ben thu ba da bi go han.
                r = await c.post("/api/v1/system/model-config/mineru/302", headers=A)
                assert r.status_code == 404

                # Gửi lại chuỗi với api_key rỗng → GIỮ key cũ, chỉ đổi field khác.
                r = await c.put(
                    "/api/v1/system/model-config",
                    headers=A,
                    json={
                        "llm_providers": [
                            {
                                "id": "anthropic-main",
                                "provider": "anthropic",
                                "model": "m2",
                                "api_key": "",
                                "priority": 10,
                            },
                        ],
                    },
                )
                kept = r.json()["config"]["llm_providers"]
                assert len(kept) == 1  # entry không gửi lên là bị xoá thật
                assert kept[0]["api_key_set"] is True and kept[0]["model"] == "m2"
                assert settings.llm_model == "m2"

                # Test không nhập key → tái dùng key đã lưu, nhưng CHỈ khi vẫn đúng endpoint cũ.
                observed.clear()
                reuse = await c.post(
                    "/api/v1/system/model-config/test",
                    headers=A,
                    json={"id": "anthropic-main", "provider": "anthropic", "model": "m2"},
                )
                assert reuse.json()["ok"] is True
                assert observed["key"] == "sk-ant-fake"

                # Đổi base_url sang host lạ → TỪ CHỐI đưa key đã lưu (chống rút key ra ngoài).
                observed.clear()
                redirected = await c.post(
                    "/api/v1/system/model-config/test",
                    headers=A,
                    json={
                        "id": "anthropic-main",
                        "provider": "anthropic",
                        "model": "m2",
                        "base_url": "https://attacker.example/v1",
                    },
                )
                assert redirected.json()["ok"] is False
                assert observed == {}
                assert "sk-ant-fake" not in redirected.text

                # Đổi luôn provider cũng không được mượn key.
                observed.clear()
                swapped = await c.post(
                    "/api/v1/system/model-config/test",
                    headers=A,
                    json={"id": "anthropic-main", "provider": "openai", "model": "m2"},
                )
                assert swapped.json()["ok"] is False
                assert observed == {}

                # Chuỗi rỗng → chưa cấu hình, và credential phẳng bị xoá sạch
                # (đây là thứ vô hiệu hoá key cũ còn sót trong .env).
                r = await c.put("/api/v1/system/model-config", headers=A, json={"llm_providers": []})
                assert r.json()["config"]["llm_configured"] is False
                assert r.json()["capabilities"]["llm_configured"] is False
                assert settings.llm_api_key is None
                assert (
                    await c.post("/api/v1/system/model-config/test", headers=A)
                ).json()["ok"] is False

                # The document parsing configuration and the secrets persist, mask and take effect the same way.
                r = await c.put(
                    "/api/v1/system/model-config",
                    headers=A,
                    json={
                        "document_parser": "markitdown",
                        "document_extract_concurrency": 7,
                    },
                )
                parser_config = r.json()["config"]
                assert parser_config["effective_document_parser"] == "markitdown"
                assert parser_config["document_extract_concurrency"] == 7

                # Parser khong hop le -> 422 (Literal chi nhan 'markitdown')
                assert (
                    await c.put(
                        "/api/v1/system/model-config", headers=A, json={"document_parser": "mineru"}
                    )
                ).status_code == 422

                # An invalid value -> 422 (Literal / out of range)
                assert (
                    await c.put("/api/v1/system/model-config", headers=A, json={"search_strategy": "nope"})
                ).status_code == 422
                # Provider lạ / id trùng / thiếu model -> 422 ngay ở schema.
                for invalid_chain in (
                    [{"id": "x", "provider": "nope", "model": "m", "api_key": "k"}],
                    [
                        {"id": "dup", "provider": "openai", "model": "m", "api_key": "k"},
                        {"id": "dup", "provider": "gemini", "model": "m2", "api_key": "k2"},
                    ],
                    [{"id": "x", "provider": "openai", "model": "", "api_key": "k"}],
                    [{"id": "bad id", "provider": "openai", "model": "m", "api_key": "k"}],
                    [{"id": "x", "provider": "openai", "model": "m", "priority": 0}],
                ):
                    assert (
                        await c.put(
                            "/api/v1/system/model-config",
                            headers=A,
                            json={"llm_providers": invalid_chain},
                        )
                    ).status_code == 422, invalid_chain
                assert (
                    await c.put("/api/v1/system/model-config", headers=A, json={"search_strategy": "atomic"})
                ).status_code == 422
                assert (
                    await c.put("/api/v1/system/model-config", headers=A, json={"search_top_k": 999})
                ).status_code == 422
                assert (
                    await c.put("/api/v1/system/model-config", headers=A, json={"document_parser": None})
                ).status_code == 422
                assert (
                    await c.put(
                        "/api/v1/system/model-config",
                        headers=A,
                        json={"document_extract_concurrency": 0},
                    )
                ).status_code == 422
                for invalid in (
                    {"llm_timeout_ms": 999},
                    {"llm_timeout_ms": None},
                    {"llm_max_retries": 11},
                    {"llm_max_retries": None},
                    {"document_chunk_max_tokens": 99},
                    {"document_chunk_max_tokens": None},
                    {"document_chunk_mode": "overlap"},
                    {"document_chunk_mode": None},
                ):
                    assert (await c.put("/api/v1/system/model-config", headers=A, json=invalid)).status_code == 422
                assert (
                    await c.put(
                        "/api/v1/system/model-config",
                        headers=A,
                        json={"document_extract_concurrency": None},
                    )
                ).status_code == 422
    finally:
        async with SessionLocal() as s:
            await s.execute(
                delete(Setting).where(
                    Setting.scope == "global",
                    Setting.key.in_(["model_config", "system_preferences"]),
                )
            )
            await s.commit()
        for key, value in snapshot.items():
            setattr(settings, key, value)


@pytest.mark.asyncio
async def test_clearing_a_nullable_override_falls_back_to_the_environment():
    """Xoá override trong UI phải trả field về giá trị của môi trường, không ép nó thành None.

    Ghi `None` biến "tôi không muốn ghi đè nữa" thành "ép rỗng, kể cả khi `.env` có giá trị", và
    khi đó không còn đường nào từ UI quay về mặc định của bản triển khai — người dùng bấm xoá rồi
    ngồi đợi một thay đổi không bao giờ tới.
    """
    from sqlalchemy import delete, select

    from sag_api.core.config import ENV_BASELINE
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Setting
    from sag_api.services import settings_service

    await init_db()
    baseline = "http://embedding:11434/v1"
    previous_baseline = ENV_BASELINE.get("embedding_base_url")
    previous_value = settings.embedding_base_url
    ENV_BASELINE["embedding_base_url"] = baseline
    try:
        async with SessionLocal() as session:
            await settings_service.save_model_config(
                session, {"embedding_base_url": "https://somewhere-else.example/v1"}
            )
            assert settings.embedding_base_url == "https://somewhere-else.example/v1"

            # Xoá override -> quay về giá trị môi trường, NGAY trong process này.
            await settings_service.save_model_config(session, {"embedding_base_url": ""})
            assert settings.embedding_base_url == baseline

            stored = await settings_service.load_overrides(session)
            assert "embedding_base_url" not in stored

            # Bản cũ đã ghi `None` vào DB: phải được đọc như "không ghi đè", không phải "ép rỗng".
            row = await session.scalar(
                select(Setting).where(Setting.scope == "global", Setting.key == "model_config")
            )
            row.value = {**dict(row.value or {}), "embedding_base_url": None}
            await session.commit()
            settings_service.apply_overrides(settings, await settings_service.load_overrides(session))
            assert settings.embedding_base_url == baseline
    finally:
        if previous_baseline is None:
            ENV_BASELINE.pop("embedding_base_url", None)
        else:
            ENV_BASELINE["embedding_base_url"] = previous_baseline
        settings.embedding_base_url = previous_value
        async with SessionLocal() as s:
            await s.execute(
                delete(Setting).where(Setting.scope == "global", Setting.key == "model_config")
            )
            await s.commit()
