"""Typed application settings loaded from environment variables."""

import os
from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import urlparse

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
    deepseek_max_tokens: int
    embedding_api_key: Optional[str]
    embedding_base_url: Optional[str]
    embedding_model: Optional[str]
    chroma_path: str

    @property
    def deepseek_configured(self) -> bool:
        """Return whether a non-empty DeepSeek API key is available."""
        return self.deepseek_api_key is not None

    @property
    def embedding_configured(self) -> bool:
        """Return whether a complete external embedding API is configured."""
        return all(
            (
                self.embedding_api_key,
                self.embedding_base_url,
                self.embedding_model,
            )
        )


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


def _optional_value(
    environ: Mapping[str, str],
    name: str,
) -> Optional[str]:
    return environ.get(name, "").strip() or None


def _normalize_http_url(name: str, value: str) -> str:
    normalized = value.rstrip("/")
    parsed = urlparse(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(
            f"{name} must be an absolute HTTP(S) URL without credentials"
        )
    return normalized


def _normalize_deepseek_base_url(value: str) -> str:
    normalized = _normalize_http_url("DEEPSEEK_BASE_URL", value)
    parsed = urlparse(normalized)
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "api.deepseek.com"
        or parsed.path not in {"", "/v1"}
    ):
        raise ConfigurationError(
            "DEEPSEEK_BASE_URL must be https://api.deepseek.com "
            "with no path or /v1"
        )
    return f"https://api.deepseek.com{parsed.path}"


def _normalize_embedding_base_url(value: str) -> str:
    """Require encrypted transport for external embedding credentials."""
    normalized = _normalize_http_url("EMBEDDING_BASE_URL", value)
    if urlparse(normalized).scheme != "https":
        raise ConfigurationError(
            "EMBEDDING_BASE_URL must use HTTPS"
        )
    return normalized


def load_settings(environ: Mapping[str, str]) -> Settings:
    """Build and validate settings from an environment mapping."""
    api_key = environ.get("DEEPSEEK_API_KEY", "").strip() or None
    deepseek_base_url = _normalize_deepseek_base_url(
        _required_value(
            environ,
            "DEEPSEEK_BASE_URL",
            "https://api.deepseek.com",
        ),
    )
    embedding_api_key = _optional_value(environ, "EMBEDDING_API_KEY")
    embedding_base_url = _optional_value(
        environ,
        "EMBEDDING_BASE_URL",
    )
    if embedding_base_url is not None:
        embedding_base_url = _normalize_embedding_base_url(
            embedding_base_url,
        )
    embedding_model = _optional_value(environ, "EMBEDDING_MODEL")
    embedding_values = (
        embedding_api_key,
        embedding_base_url,
        embedding_model,
    )
    if any(embedding_values) and not all(embedding_values):
        raise ConfigurationError(
            "EMBEDDING_API_KEY, EMBEDDING_BASE_URL, and "
            "EMBEDDING_MODEL must be configured together"
        )

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
        deepseek_max_tokens=_positive_integer(
            environ,
            "DEEPSEEK_MAX_TOKENS",
            8_192,
        ),
        embedding_api_key=embedding_api_key,
        embedding_base_url=embedding_base_url,
        embedding_model=embedding_model,
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
