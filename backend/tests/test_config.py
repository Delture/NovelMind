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
    assert settings.deepseek_max_tokens == 8_192
    assert settings.embedding_api_key is None
    assert settings.embedding_base_url is None
    assert settings.embedding_model is None
    assert settings.chroma_path == "./chroma_db"
    assert settings.deepseek_configured is False
    assert settings.embedding_configured is False


def test_load_settings_normalizes_explicit_values() -> None:
    settings = load_settings(
        {
            "APP_NAME": " NovelMind Local ",
            "DATABASE_URL": "sqlite:///custom.db",
            "DEEPSEEK_API_KEY": " secret-key ",
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1/",
            "DEEPSEEK_MODEL": "custom-model",
            "DEEPSEEK_TIMEOUT_SECONDS": "720",
            "DEEPSEEK_MAX_ATTEMPTS": "4",
            "DEEPSEEK_MAX_TOKENS": "4096",
            "EMBEDDING_API_KEY": " embedding-secret ",
            "EMBEDDING_BASE_URL": "https://embedding.example/v1/",
            "EMBEDDING_MODEL": "embedding-model",
            "CHROMA_PATH": "./data/vectors",
        }
    )

    assert settings.app_name == "NovelMind Local"
    assert settings.database_url == "sqlite:///custom.db"
    assert settings.deepseek_api_key == "secret-key"
    assert settings.deepseek_base_url == "https://api.deepseek.com/v1"
    assert settings.deepseek_model == "custom-model"
    assert settings.deepseek_timeout_seconds == 720
    assert settings.deepseek_max_attempts == 4
    assert settings.deepseek_max_tokens == 4_096
    assert settings.embedding_api_key == "embedding-secret"
    assert settings.embedding_base_url == "https://embedding.example/v1"
    assert settings.embedding_model == "embedding-model"
    assert settings.chroma_path == "./data/vectors"
    assert settings.deepseek_configured is True
    assert settings.embedding_configured is True


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DEEPSEEK_TIMEOUT_SECONDS", "0"),
        ("DEEPSEEK_TIMEOUT_SECONDS", "not-a-number"),
        ("DEEPSEEK_MAX_ATTEMPTS", "0"),
        ("DEEPSEEK_MAX_ATTEMPTS", "2.5"),
        ("DEEPSEEK_MAX_TOKENS", "0"),
        ("DEEPSEEK_MAX_TOKENS", "2.5"),
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
        "CHROMA_PATH",
    ],
)
def test_load_settings_rejects_blank_required_values(name: str) -> None:
    with pytest.raises(ConfigurationError, match=name):
        load_settings({name: "   "})


@pytest.mark.parametrize(
    "environ",
    [
        {"EMBEDDING_API_KEY": "key"},
        {"EMBEDDING_BASE_URL": "https://embedding.example/v1"},
        {"EMBEDDING_MODEL": "embedding-model"},
        {
            "EMBEDDING_API_KEY": "key",
            "EMBEDDING_MODEL": "embedding-model",
        },
    ],
)
def test_embedding_api_configuration_must_be_complete(
    environ: dict[str, str],
) -> None:
    with pytest.raises(ConfigurationError, match="EMBEDDING"):
        load_settings(environ)


@pytest.mark.parametrize(
    ("name", "environ"),
    [
        (
            "DEEPSEEK_BASE_URL",
            {"DEEPSEEK_BASE_URL": "api.deepseek.com"},
        ),
        (
            "EMBEDDING_BASE_URL",
            {
                "EMBEDDING_API_KEY": "key",
                "EMBEDDING_BASE_URL": "embedding.example/v1",
                "EMBEDDING_MODEL": "embedding-model",
            },
        ),
    ],
)
def test_api_base_urls_must_be_absolute_http_urls(
    name: str,
    environ: dict[str, str],
) -> None:
    with pytest.raises(ConfigurationError, match=name):
        load_settings(environ)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.deepseek.com",
        "https://gateway.example/v1",
        "https://api.deepseek.com/chat",
        "https://api.deepseek.com:443",
    ],
)
def test_deepseek_base_url_rejects_non_official_origins_and_paths(
    base_url: str,
) -> None:
    with pytest.raises(ConfigurationError, match="DEEPSEEK_BASE_URL"):
        load_settings({"DEEPSEEK_BASE_URL": base_url})


@pytest.mark.parametrize(
    "base_url",
    [
        "http://embedding.example/v1",
        "http://127.0.0.1:8080/v1",
        "http://localhost:8080/v1",
    ],
)
def test_embedding_base_url_requires_https(base_url: str) -> None:
    with pytest.raises(ConfigurationError, match="EMBEDDING_BASE_URL"):
        load_settings(
            {
                "EMBEDDING_API_KEY": "key",
                "EMBEDDING_BASE_URL": base_url,
                "EMBEDDING_MODEL": "embedding-model",
            }
        )
