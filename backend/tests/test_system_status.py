"""Tests for the public system configuration status."""

import json
from dataclasses import replace

from backend.app.api.system import get_system_status
from backend.app.core.config import load_settings
from backend.app.main import app


def test_system_status_route_is_registered() -> None:
    assert any(
        route.path == "/api/system/status"
        and "GET" in getattr(route, "methods", set())
        for route in app.routes
    )


def test_system_status_reports_models_without_exposing_secrets() -> None:
    settings = replace(
        load_settings({}),
        deepseek_api_key="super-secret-api-key",
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
            "provider": "ollama",
            "model": "qwen3-embedding:0.6b",
        },
    }
    assert "super-secret-api-key" not in json.dumps(payload)
