"""Tests for the public system configuration status."""

import json
from dataclasses import replace

from backend.app.api.system import get_system_status
from backend.app.core.config import load_settings
from backend.app.main import app


def test_system_status_route_is_registered() -> None:
    assert "get" in app.openapi()["paths"]["/api/system/status"]


def test_system_status_reports_models_without_exposing_secrets() -> None:
    settings = replace(
        load_settings({}),
        deepseek_api_key="super-secret-api-key",
        embedding_api_key="embedding-secret-key",
        embedding_base_url="https://embedding.example/v1",
        embedding_model="embedding-model",
    )

    result = get_system_status(settings=settings)
    payload = result.model_dump()

    assert payload == {
        "status": "ok",
        "generation": {
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "configured": True,
        },
        "embedding": {
            "provider": "external_api",
            "model": "embedding-model",
            "configured": True,
        },
    }
    assert "super-secret-api-key" not in json.dumps(payload)
    assert "embedding-secret-key" not in json.dumps(payload)
