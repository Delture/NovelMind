
import json
from dataclasses import replace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backend.app.api.system import (
    EmbeddingSettingsUpdate,
    GenerationSettingsUpdate,
    ModelSettingsUpdate,
    get_model_settings,
    get_system_status,
    put_model_settings,
    reset_model_settings,
)
from backend.app.core.config import get_settings, load_settings
from backend.app.main import app


def test_system_status_route_is_registered() -> None:
    assert "get" in app.openapi()["paths"]["/api/system/status"]
    methods = app.openapi()["paths"]["/api/system/model-settings"]
    assert {"get", "put", "delete"} <= set(methods)


def test_system_status_reports_models_without_exposing_secrets() -> None:
    settings = replace(
        load_settings({}),
        deepseek_api_key="super-secret-api-key",
        embedding_api_key="embedding-secret-key",
        embedding_base_url="https://api.siliconflow.cn/v1",
    )

    result = get_system_status(settings=settings)
    payload = result.model_dump()

    assert payload == {
        "status": "ok",
        "generation": {
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "configured": True,
            "summary_concurrency_mode": "auto",
            "summary_initial_concurrency": 64,
            "summary_max_concurrency": 500,
        },
        "embedding": {
            "provider": "siliconflow",
            "model": "Qwen/Qwen3-Embedding-4B",
            "dimensions": 1024,
            "region": "global",
            "configured": True,
        },
    }
    assert "super-secret-api-key" not in json.dumps(payload)
    assert "embedding-secret-key" not in json.dumps(payload)


def _local_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/api/system/model-settings",
            "headers": [],
            "client": ("127.0.0.1", 51_234),
            "server": ("127.0.0.1", 8_000),
            "scheme": "http",
        }
    )


def test_model_settings_can_be_changed_without_returning_keys(
    tmp_path,
    monkeypatch,
) -> None:
    settings_path = tmp_path / "model-settings.json"
    monkeypatch.setenv("MODEL_SETTINGS_PATH", str(settings_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-generation-key")
    monkeypatch.setenv("EMBEDDING_API_KEY", "environment-embedding-key")
    monkeypatch.setenv(
        "EMBEDDING_BASE_URL",
        "https://api.siliconflow.cn/v1",
    )
    monkeypatch.setenv("EMBEDDING_PROVIDER", "siliconflow")
    monkeypatch.setenv(
        "EMBEDDING_MODEL",
        "Qwen/Qwen3-Embedding-4B",
    )

    result = put_model_settings(
        ModelSettingsUpdate(
            generation=GenerationSettingsUpdate(
                provider="openai_compatible",
                model="custom-chat",
                base_url="https://models.example.com/v1",
                api_key="replacement-generation-key",
                summary_concurrency_mode="manual",
                summary_max_concurrency=80,
            ),
            embedding=EmbeddingSettingsUpdate(
                provider="openai_compatible",
                model="custom-embedding",
                base_url="https://embeddings.example.com/v1",
                dimensions=1_536,
                api_key="replacement-embedding-key",
            ),
        ),
        _local_request(),
    )
    payload = result.model_dump()

    assert payload["generation"]["provider"] == "openai_compatible"
    assert payload["embedding"]["dimensions"] == 1_536
    assert payload["generation"]["api_key_set"] is True
    assert payload["generation"]["summary_concurrency_mode"] == "manual"
    assert payload["generation"]["summary_initial_concurrency"] == 80
    assert payload["embedding"]["api_key_set"] is True
    assert payload["local_override"] is True
    serialized = json.dumps(payload)
    assert "replacement-generation-key" not in serialized
    assert "replacement-embedding-key" not in serialized
    assert get_settings().deepseek_api_key == "replacement-generation-key"

    public_payload = get_model_settings(
        settings=get_settings()
    ).model_dump()
    assert "replacement-generation-key" not in json.dumps(public_payload)

    cleared = put_model_settings(
        ModelSettingsUpdate(
            generation=GenerationSettingsUpdate(
                provider="openai_compatible",
                model="custom-chat",
                base_url="https://models.example.com/v1",
                clear_api_key=True,
                summary_concurrency_mode="manual",
                summary_max_concurrency=80,
            ),
            embedding=EmbeddingSettingsUpdate(
                provider="openai_compatible",
                model="custom-embedding",
                base_url="https://embeddings.example.com/v1",
                dimensions=1_536,
                clear_api_key=True,
            ),
        ),
        _local_request(),
    ).model_dump()
    assert cleared["generation"]["configured"] is False
    assert cleared["embedding"]["configured"] is False
    saved = get_settings()
    assert saved.embedding_base_url == "https://embeddings.example.com/v1"
    assert saved.deepseek_base_url == "https://models.example.com/v1"
    assert saved.deepseek_api_key is None
    assert saved.embedding_api_key is None
    assert cleared["generation"]["api_key_set"] is False
    assert cleared["embedding"]["api_key_set"] is False
    for key in ("replacement-generation-key", "generation-secret", "embedding-secret"):
        assert key not in json.dumps(cleared)

    restored = reset_model_settings(_local_request()).model_dump()
    assert restored["generation"]["provider"] == "deepseek"
    assert restored["embedding"]["provider"] == "siliconflow"
    assert restored["local_override"] is False
    assert not settings_path.exists()


@pytest.mark.parametrize("role", ["generation", "embedding"])
def test_switching_provider_rejects_whitespace_only_api_key(
    role: str,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "MODEL_SETTINGS_PATH",
        str(tmp_path / "model-settings.json"),
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "existing-generation-key")
    monkeypatch.setenv("EMBEDDING_API_KEY", "existing-embedding-key")
    monkeypatch.setenv(
        "EMBEDDING_BASE_URL",
        "https://api.siliconflow.cn/v1",
    )
    monkeypatch.setenv("EMBEDDING_PROVIDER", "siliconflow")
    monkeypatch.setenv(
        "EMBEDDING_MODEL",
        "Qwen/Qwen3-Embedding-4B",
    )

    generation_provider = (
        "openai_compatible" if role == "generation" else "deepseek"
    )
    embedding_provider = (
        "openai_compatible"
        if role == "embedding"
        else "siliconflow"
    )
    with pytest.raises(HTTPException) as captured:
        put_model_settings(
            ModelSettingsUpdate(
                generation=GenerationSettingsUpdate(
                    provider=generation_provider,
                    model="custom-chat",
                    base_url="https://models.example.com/v1",
                    api_key="   ",
                ),
                embedding=EmbeddingSettingsUpdate(
                    provider=embedding_provider,
                    model="custom-embedding",
                    base_url="https://embeddings.example.com/v1",
                    dimensions=1_536,
                    api_key="   ",
                ),
            ),
            _local_request(),
        )

    assert captured.value.status_code == 400
    assert not (tmp_path / "model-settings.json").exists()
