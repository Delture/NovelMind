"""Typed application settings loaded from environment variables."""

import os
from dataclasses import dataclass
from typing import Mapping, Optional

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Raised when an environment variable has an invalid value."""


@dataclass(frozen=True)
class Settings:
    """Runtime settings shared by the backend services."""

    app_name: str
    database_url: str
    deepseek_api_key: Optional[str]
    deepseek_base_url: str
    deepseek_model: str
    deepseek_timeout_seconds: int
    deepseek_max_attempts: int
    ollama_base_url: str
    embedding_model: str
    chroma_path: str

    @property
    def deepseek_configured(self) -> bool:
        """Return whether a non-empty DeepSeek API key is available."""
        return self.deepseek_api_key is not None


def _required_value(
    environ: Mapping[str, str],
    name: str,
    default: str,
) -> str:
    value = environ.get(name, default).strip()
    if not value:
        raise ConfigurationError(f"{name} cannot be blank")
    return value


def _positive_integer(
    environ: Mapping[str, str],
    name: str,
    default: int,
) -> int:
    raw_value = environ.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ConfigurationError(
            f"{name} must be a positive integer"
        ) from error
    if value < 1:
        raise ConfigurationError(f"{name} must be a positive integer")
    return value


def load_settings(environ: Mapping[str, str]) -> Settings:
    """Build and validate settings from an environment mapping."""
    api_key = environ.get("DEEPSEEK_API_KEY", "").strip() or None
    deepseek_base_url = _required_value(
        environ,
        "DEEPSEEK_BASE_URL",
        "https://api.deepseek.com",
    ).rstrip("/")
    ollama_base_url = _required_value(
        environ,
        "OLLAMA_BASE_URL",
        "http://127.0.0.1:11434",
    ).rstrip("/")

    return Settings(
        app_name=_required_value(environ, "APP_NAME", "NovelMind"),
        database_url=_required_value(
            environ,
            "DATABASE_URL",
            "sqlite:///./data/novelmind.db",
        ),
        deepseek_api_key=api_key,
        deepseek_base_url=deepseek_base_url,
        deepseek_model=_required_value(
            environ,
            "DEEPSEEK_MODEL",
            "deepseek-v4-pro",
        ),
        deepseek_timeout_seconds=_positive_integer(
            environ,
            "DEEPSEEK_TIMEOUT_SECONDS",
            660,
        ),
        deepseek_max_attempts=_positive_integer(
            environ,
            "DEEPSEEK_MAX_ATTEMPTS",
            3,
        ),
        ollama_base_url=ollama_base_url,
        embedding_model=_required_value(
            environ,
            "EMBEDDING_MODEL",
            "qwen3-embedding:0.6b",
        ),
        chroma_path=_required_value(
            environ,
            "CHROMA_PATH",
            "./chroma_db",
        ),
    )


load_dotenv()
settings = load_settings(os.environ)


def get_settings() -> Settings:
    """Return the validated process-wide settings object."""
    return settings
