from __future__ import annotations

import copy
from uuid import uuid4

import httpx
import pytest

from sag_api.core.portable_config import open_portable_config, seal_portable_config


def test_portable_config_round_trip_hides_plaintext_secrets():
    config = {
        "llm_providers": [
            {"id": "primary", "provider": "openai", "model": "gpt-test", "api_key": "secret-key"}
        ],
        "embedding_api_key": "embedding-secret",
    }
    bundle = seal_portable_config("alice-model-config", config, "correct horse battery staple")

    assert bundle["contains_secrets"] is True
    assert "secret-key" not in str(bundle)
    assert "embedding-secret" not in str(bundle)
    assert open_portable_config(
        bundle,
        "correct horse battery staple",
        "alice-model-config",
    ) == config


@pytest.mark.parametrize("mutation", ["passphrase", "ciphertext", "kind"])
def test_portable_config_rejects_wrong_password_tampering_or_kind(mutation: str):
    bundle = seal_portable_config(
        "alice-sub-agent-config",
        {"entries": [{"provider": "custom", "credential": "secret"}]},
        "correct horse battery staple",
    )
    candidate = copy.deepcopy(bundle)
    passphrase = "correct horse battery staple"
    expected_kind = "alice-sub-agent-config"
    if mutation == "passphrase":
        passphrase = "wrong horse battery staple"
    elif mutation == "ciphertext":
        candidate["ciphertext"] = "A" + candidate["ciphertext"][1:]
    else:
        expected_kind = "alice-model-config"

    with pytest.raises(ValueError, match="portable_config_decryption_failed"):
        open_portable_config(candidate, passphrase, expected_kind)


@pytest.mark.asyncio
async def test_portable_config_api_moves_model_and_sub_agent_credentials_without_exposing_them():
    from sqlalchemy import delete

    from sag_api.core.config import settings
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Setting
    from sag_api.main import app

    snapshot = {
        key: getattr(settings, key)
        for key in (
            "llm_providers",
            "llm_provider",
            "llm_base_url",
            "llm_model",
            "llm_api_key",
            "embedding_api_key",
        )
    }
    transport = httpx.ASGITransport(app=app)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                registered = await client.post(
                    "/api/v1/auth/register",
                    json={
                        "email": f"portable-config-{uuid4().hex}@t.com",
                        "password": "password123",
                    },
                )
                assert registered.status_code == 201, registered.text
                auth = {"Authorization": f"Bearer {registered.json()['access_token']}"}

                model_saved = await client.put(
                    "/api/v1/system/model-config",
                    headers=auth,
                    json={
                        "llm_providers": [
                            {
                                "id": "portable-primary",
                                "provider": "openai",
                                "model": "portable-model",
                                "api_key": "portable-model-secret",
                                "priority": 1,
                            }
                        ],
                        "embedding_api_key": "portable-embedding-secret",
                    },
                )
                assert model_saved.status_code == 200, model_saved.text
                sub_agent_saved = await client.put(
                    "/api/v1/system/sub-agent-config",
                    headers=auth,
                    json={
                        "entries": [
                            {
                                "provider": "custom",
                                "provider_name": "Portable custom",
                                "model": "portable/sub-agent",
                                "base_url": "https://portable.invalid/v1",
                                "credential": "portable-sub-agent-secret",
                                "enabled": True,
                            }
                        ]
                    },
                )
                assert sub_agent_saved.status_code == 200, sub_agent_saved.text

                exports: dict[str, dict] = {}
                for kind in ("alice-model-config", "alice-sub-agent-config"):
                    response = await client.post(
                        "/api/v1/system/config-transfer/export",
                        headers=auth,
                        json={"kind": kind, "passphrase": "portable test passphrase"},
                    )
                    assert response.status_code == 200, response.text
                    assert "portable-model-secret" not in response.text
                    assert "portable-embedding-secret" not in response.text
                    assert "portable-sub-agent-secret" not in response.text
                    exports[kind] = response.json()

                await client.put(
                    "/api/v1/system/model-config",
                    headers=auth,
                    json={"llm_providers": []},
                )
                await client.put(
                    "/api/v1/system/sub-agent-config",
                    headers=auth,
                    json={"entries": []},
                )

                wrong = await client.post(
                    "/api/v1/system/config-transfer/import",
                    headers=auth,
                    json={
                        "bundle": exports["alice-model-config"],
                        "passphrase": "definitely wrong passphrase",
                    },
                )
                assert wrong.status_code == 422
                assert wrong.json()["error"]["code"] == "portable_config_decryption_failed"

                for kind, bundle in exports.items():
                    imported = await client.post(
                        "/api/v1/system/config-transfer/import",
                        headers=auth,
                        json={"bundle": bundle, "passphrase": "portable test passphrase"},
                    )
                    assert imported.status_code == 200, imported.text
                    assert "portable-model-secret" not in imported.text
                    assert "portable-embedding-secret" not in imported.text
                    assert "portable-sub-agent-secret" not in imported.text
                    if kind == "alice-sub-agent-config":
                        assert imported.json()["config"]["entries"][0]["enabled"] is False
                        assert imported.json()["config"]["entries"][0]["credential_set"] is True

                model = await client.get("/api/v1/system/model-config", headers=auth)
                assert model.json()["llm_providers"][0]["api_key_set"] is True
                assert model.json()["embedding_api_key_set"] is True
                assert settings.llm_providers[0]["api_key"] == "portable-model-secret"
                assert settings.embedding_api_key == "portable-embedding-secret"
    finally:
        async with SessionLocal() as session:
            await session.execute(
                delete(Setting).where(
                    Setting.scope == "global",
                    Setting.key.in_(["model_config", "sub_agent_config"]),
                )
            )
            await session.commit()
        for key, value in snapshot.items():
            setattr(settings, key, value)
