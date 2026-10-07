"""Tests for application configuration loading and validation."""

import pytest

from backend.app.core.config import (
    ConfigurationError,
    SILICONFLOW_EMBEDDING_BASE_URL,
    SILICONFLOW_EMBEDDING_MODEL,
    _replace_legacy_embedding_settings,
    load_settings,
)


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
    assert settings.summary_concurrency_mode == "auto"
    assert settings.summary_max_concurrency == 2_500
    assert settings.summary_provider_concurrency_limit == 500
    assert settings.summary_initial_concurrency == 64
    assert settings.embedding_api_key is None
    assert settings.embedding_provider == "siliconflow"
    assert settings.embedding_base_url == SILICONFLOW_EMBEDDING_BASE_URL
    assert settings.embedding_model == SILICONFLOW_EMBEDDING_MODEL
    assert settings.embedding_dimensions == 1_024
    assert settings.embedding_timeout_seconds == 120
    assert settings.embedding_max_attempts == 3
    assert settings.vector_store_path == "./vector_store"
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
            "SUMMARY_CONCURRENCY_MODE": "manual",
            "SUMMARY_MAX_CONCURRENCY": "80",
            "EMBEDDING_API_KEY": " embedding-secret ",
            "EMBEDDING_BASE_URL": "https://api.siliconflow.cn/v1/",
            "EMBEDDING_MODEL": "Qwen/Qwen3-Embedding-4B",
            "EMBEDDING_DIMENSIONS": "1024",
            "EMBEDDING_TIMEOUT_SECONDS": "180",
            "EMBEDDING_MAX_ATTEMPTS": "4",
            "VECTOR_STORE_PATH": "./data/vectors",
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
    assert settings.summary_concurrency_mode == "manual"
    assert settings.summary_initial_concurrency == 80
    assert settings.embedding_api_key == "embedding-secret"
    assert settings.embedding_base_url == SILICONFLOW_EMBEDDING_BASE_URL
    assert settings.embedding_model == SILICONFLOW_EMBEDDING_MODEL
    assert settings.embedding_dimensions == 1_024
    assert settings.embedding_timeout_seconds == 180
    assert settings.embedding_max_attempts == 4
    assert settings.vector_store_path == "./data/vectors"
    assert settings.deepseek_configured is True
    assert settings.embedding_configured is True


def test_legacy_chroma_path_remains_a_read_only_configuration_alias() -> None:
    settings = load_settings({"CHROMA_PATH": "./legacy-vectors"})

    assert settings.vector_store_path == "./legacy-vectors"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DEEPSEEK_TIMEOUT_SECONDS", "0"),
        ("DEEPSEEK_TIMEOUT_SECONDS", "not-a-number"),
        ("DEEPSEEK_MAX_ATTEMPTS", "0"),
        ("DEEPSEEK_MAX_ATTEMPTS", "2.5"),
        ("DEEPSEEK_MAX_TOKENS", "0"),
        ("DEEPSEEK_MAX_TOKENS", "2.5"),
        ("SUMMARY_MAX_CONCURRENCY", "0"),
        ("SUMMARY_MAX_CONCURRENCY", "2501"),
        ("EMBEDDING_TIMEOUT_SECONDS", "0"),
        ("EMBEDDING_MAX_ATTEMPTS", "not-a-number"),
        ("EMBEDDING_DIMENSIONS", "0"),
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
        "VECTOR_STORE_PATH",
    ],
)
def test_load_settings_rejects_blank_required_values(name: str) -> None:
    with pytest.raises(ConfigurationError, match=name):
        load_settings({name: "   "})


def test_custom_embedding_key_requires_a_base_url() -> None:
    with pytest.raises(ConfigurationError, match="EMBEDDING"):
        load_settings(
            {
                "EMBEDDING_PROVIDER": "openai_compatible",
                "EMBEDDING_API_KEY": "key",
                "EMBEDDING_MODEL": "custom-embedding",
            }
        )


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
                "EMBEDDING_BASE_URL": "api.siliconflow.cn/v1",
                "EMBEDDING_MODEL": "Qwen/Qwen3-Embedding-4B",
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
        "http://api.siliconflow.cn/v1",
        "http://127.0.0.1:8080/compatible-mode/v1",
        "http://localhost:8080/compatible-mode/v1",
    ],
)
def test_embedding_base_url_requires_https(base_url: str) -> None:
    with pytest.raises(ConfigurationError, match="EMBEDDING_BASE_URL"):
        load_settings(
            {
                "EMBEDDING_API_KEY": "key",
                "EMBEDDING_BASE_URL": base_url,
                "EMBEDDING_MODEL": "Qwen/Qwen3-Embedding-4B",
            }
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "https://siliconflow.cn/v1",
        "https://api.siliconflow.cn",
        "https://api.siliconflow.cn/embeddings",
        "https://api.siliconflow.cn:bad/v1",
    ],
)
def test_embedding_base_url_rejects_non_official_siliconflow_origins(
    base_url: str,
) -> None:
    with pytest.raises(ConfigurationError, match="EMBEDDING_BASE_URL"):
        load_settings(
            {
                "EMBEDDING_API_KEY": "key",
                "EMBEDDING_BASE_URL": base_url,
            }
        )


@pytest.mark.parametrize(
    "environ",
    [
        {"EMBEDDING_MODEL": "Qwen/Qwen3-Embedding-0.6B"},
        {"EMBEDDING_DIMENSIONS": "768"},
        {"EMBEDDING_DIMENSIONS": "2048"},
    ],
)
def test_embedding_model_and_dimensions_are_fixed(
    environ: dict[str, str],
) -> None:
    with pytest.raises(ConfigurationError, match="EMBEDDING_"):
        load_settings(environ)


def test_legacy_bailian_settings_are_migrated_without_reusing_key() -> None:
    migrated = _replace_legacy_embedding_settings(
        {
            "EMBEDDING_PROVIDER": "aliyun_model_studio",
            "EMBEDDING_API_KEY": "legacy-bailian-secret",
            "EMBEDDING_BASE_URL": (
                "https://workspace.cn-beijing.maas.aliyuncs.com/"
                "compatible-mode/v1"
            ),
            "EMBEDDING_MODEL": "text-embedding-v4",
            "EMBEDDING_DIMENSIONS": "1024",
        }
    )

    assert migrated["EMBEDDING_PROVIDER"] == "siliconflow"
    assert migrated["EMBEDDING_API_KEY"] == ""
    assert migrated["EMBEDDING_BASE_URL"] == SILICONFLOW_EMBEDDING_BASE_URL
    assert migrated["EMBEDDING_MODEL"] == SILICONFLOW_EMBEDDING_MODEL


def test_openai_compatible_providers_allow_custom_models_and_dimensions() -> None:
    settings = load_settings(
        {
            "GENERATION_PROVIDER": "openai_compatible",
            "DEEPSEEK_BASE_URL": "https://models.example.com/v1",
            "DEEPSEEK_MODEL": "custom-chat",
            "EMBEDDING_PROVIDER": "openai_compatible",
            "EMBEDDING_API_KEY": "embedding-key",
            "EMBEDDING_BASE_URL": "https://embeddings.example.com/v1",
            "EMBEDDING_MODEL": "custom-embedding",
            "EMBEDDING_DIMENSIONS": "1536",
        }
    )

    assert settings.generation_provider == "openai_compatible"
    assert settings.deepseek_model == "custom-chat"
    assert settings.embedding_provider == "openai_compatible"
    assert settings.embedding_model == "custom-embedding"
    assert settings.embedding_dimensions == 1_536
    assert settings.summary_provider_concurrency_limit == 256


def test_summary_concurrency_profiles_follow_known_deepseek_models() -> None:
    flash = load_settings({"DEEPSEEK_MODEL": "deepseek-v4-flash"})
    pro = load_settings({"DEEPSEEK_MODEL": "deepseek-v4-pro"})

    assert flash.summary_provider_concurrency_limit == 2_500
    assert flash.summary_initial_concurrency == 128
    assert pro.summary_provider_concurrency_limit == 500
    assert pro.summary_initial_concurrency == 64

    with pytest.raises(ConfigurationError, match="SUMMARY_CONCURRENCY_MODE"):
        load_settings({"SUMMARY_CONCURRENCY_MODE": "turbo"})


def test_compatible_http_endpoints_are_limited_to_localhost() -> None:
    with pytest.raises(ConfigurationError, match="HTTPS"):
        load_settings(
            {
                "GENERATION_PROVIDER": "openai_compatible",
                "DEEPSEEK_BASE_URL": "http://models.example.com/v1",
            }
        )

    settings = load_settings(
        {
            "GENERATION_PROVIDER": "openai_compatible",
            "DEEPSEEK_BASE_URL": "http://127.0.0.1:11434/v1",
        }
    )
    assert settings.deepseek_base_url == "http://127.0.0.1:11434/v1"


@pytest.mark.parametrize("role", ["generation", "embedding"])
def test_official_default_https_port_is_equivalent(role):
    if role == "generation":
        settings = load_settings({"DEEPSEEK_BASE_URL": "https://API.DEEPSEEK.COM:443/", "DEEPSEEK_API_KEY": "fake-only"})
        assert settings.deepseek_base_url == "https://api.deepseek.com"
        assert settings.deepseek_api_key == "fake-only"
    else:
        settings = load_settings({"EMBEDDING_BASE_URL": "https://API.SILICONFLOW.CN:443/v1/", "EMBEDDING_API_KEY": "fake-only"})
        assert settings.embedding_base_url == "https://api.siliconflow.cn/v1"
        assert settings.embedding_api_key == "fake-only"
