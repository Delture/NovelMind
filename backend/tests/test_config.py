"""Tests for application configuration loading and validation."""

import pytest

from backend.app.core.config import ConfigurationError, load_settings


def test_load_settings_uses_safe_local_defaults() -> None:
    settings = load_settings({})

    assert settings.app_name == "NovelMind"
    assert settings.database_url == "sqlite:///./data/novelmind.db"
    assert settings.deepseek_api_key is None
    assert settings.deepseek_base_url == "https://api.deepseek.com"
    assert settings.deepseek_model == "deepseek-v4-pro"
    assert settings.deepseek_timeout_seconds == 660
    assert settings.deepseek_max_attempts == 3
    assert settings.ollama_base_url == "http://127.0.0.1:11434"
    assert settings.embedding_model == "qwen3-embedding:0.6b"
    assert settings.chroma_path == "./chroma_db"
    assert settings.deepseek_configured is False


def test_load_settings_normalizes_explicit_values() -> None:
    settings = load_settings(
        {
            "APP_NAME": " NovelMind Local ",
            "DATABASE_URL": "sqlite:///custom.db",
            "DEEPSEEK_API_KEY": " secret-key ",
            "DEEPSEEK_BASE_URL": "https://gateway.example/v1/",
            "DEEPSEEK_MODEL": "custom-model",
            "DEEPSEEK_TIMEOUT_SECONDS": "720",
            "DEEPSEEK_MAX_ATTEMPTS": "4",
            "OLLAMA_BASE_URL": "http://localhost:11434/",
            "EMBEDDING_MODEL": "embedding-model",
            "CHROMA_PATH": "./data/vectors",
        }
    )

    assert settings.app_name == "NovelMind Local"
    assert settings.database_url == "sqlite:///custom.db"
    assert settings.deepseek_api_key == "secret-key"
    assert settings.deepseek_base_url == "https://gateway.example/v1"
    assert settings.deepseek_model == "custom-model"
    assert settings.deepseek_timeout_seconds == 720
    assert settings.deepseek_max_attempts == 4
    assert settings.ollama_base_url == "http://localhost:11434"
    assert settings.embedding_model == "embedding-model"
    assert settings.chroma_path == "./data/vectors"
    assert settings.deepseek_configured is True


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DEEPSEEK_TIMEOUT_SECONDS", "0"),
        ("DEEPSEEK_TIMEOUT_SECONDS", "not-a-number"),
        ("DEEPSEEK_MAX_ATTEMPTS", "0"),
        ("DEEPSEEK_MAX_ATTEMPTS", "2.5"),
    ],
)
def test_load_settings_rejects_invalid_positive_integers(
    name: str,
    value: str,
) -> None:
    with pytest.raises(ConfigurationError, match=name):
        load_settings({name: value})


@pytest.mark.parametrize(
    "name",
    [
        "APP_NAME",
        "DATABASE_URL",
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_MODEL",
        "OLLAMA_BASE_URL",
        "EMBEDDING_MODEL",
        "CHROMA_PATH",
    ],
)
def test_load_settings_rejects_blank_required_values(name: str) -> None:
    with pytest.raises(ConfigurationError, match=name):
        load_settings({name: "   "})
