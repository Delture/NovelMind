"""把环境变量和前端本地覆盖转换为经过校验的运行时配置。

配置模块位于项目底层，必须集中默认值和类型转换、尽早拒绝危险 URL 或非法
数值，并以不可变快照交给后台任务。``.env`` 是安装默认值，
``data/model_settings.json`` 是界面保存的本机覆盖；密钥不会通过状态接口回显。
覆盖文件使用临时文件加原子替换，防止断电留下半截 JSON。
"""

import os
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """表示配置值有问题，而不是网络或业务数据发生错误。"""


@dataclass(frozen=True)
class Settings:
    """一次完整且不可变的运行时配置快照。

    ``frozen=True`` 阻止属性原地改变。切换模型时创建新 Settings，使已启动任务
    继续用旧快照、新请求使用新快照，执行过程不会中途变更供应商。
    """

    app_name: str
    database_url: str
    generation_provider: str
    deepseek_api_key: Optional[str]
    deepseek_base_url: str
    deepseek_model: str
    deepseek_timeout_seconds: int
    deepseek_max_attempts: int
    deepseek_max_tokens: int
    summary_concurrency_mode: str
    summary_max_concurrency: int
    embedding_provider: str
    embedding_api_key: Optional[str]
    embedding_base_url: Optional[str]
    embedding_model: str
    embedding_dimensions: int
    embedding_timeout_seconds: int
    embedding_max_attempts: int
    vector_store_path: str

    @property
    def deepseek_configured(self) -> bool:
        """Return whether a non-empty DeepSeek API key is available."""
        return self.deepseek_api_key is not None

    @property
    def embedding_configured(self) -> bool:
        """Return whether a complete external embedding API is configured."""
        return bool(self.embedding_api_key and self.embedding_base_url)

    @property
    def summary_provider_concurrency_limit(self) -> int:
        """给出已知供应商/模型的请求并发硬上限。"""
        model = self.deepseek_model.casefold()
        if self.generation_provider == "deepseek":
            if "flash" in model:
                return 2_500
            if "pro" in model:
                return 500
        return 256

    @property
    def summary_initial_concurrency(self) -> int:
        """给自动调度器一个保守起点，后续再根据成功或限流调整。"""
        if self.summary_concurrency_mode == "manual":
            return self.summary_effective_concurrency_limit
        model = self.deepseek_model.casefold()
        if self.generation_provider == "deepseek":
            if "flash" in model:
                recommended = 128
            elif "pro" in model:
                recommended = 64
            else:
                recommended = 16
        else:
            recommended = 16
        return min(recommended, self.summary_effective_concurrency_limit)

    @property
    def summary_effective_concurrency_limit(self) -> int:
        """同时服从用户设置和供应商上限，取二者中更小的值。"""
        return min(
            self.summary_max_concurrency,
            self.summary_provider_concurrency_limit,
        )


# 内部稳定标识映射为界面名称。持久化与判断使用英文标识，不使用可翻译文字。
GENERATION_PROVIDERS = {
    "deepseek": "DeepSeek",
    "openai_compatible": "OpenAI 兼容服务",
}
EMBEDDING_PROVIDERS = {
    "siliconflow": "硅基流动",
    "openai_compatible": "OpenAI 兼容服务",
}
SILICONFLOW_EMBEDDING_BASE_URL = "https://api.siliconflow.cn/v1"
SILICONFLOW_EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-4B"
SILICONFLOW_EMBEDDING_DIMENSIONS = 1_024
_MODEL_SETTING_NAMES = frozenset(
    {
        "GENERATION_PROVIDER",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_MODEL",
        "DEEPSEEK_TIMEOUT_SECONDS",
        "DEEPSEEK_MAX_ATTEMPTS",
        "DEEPSEEK_MAX_TOKENS",
        "SUMMARY_CONCURRENCY_MODE",
        "SUMMARY_MAX_CONCURRENCY",
        "EMBEDDING_PROVIDER",
        "EMBEDDING_API_KEY",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIMENSIONS",
        "EMBEDDING_TIMEOUT_SECONDS",
        "EMBEDDING_MAX_ATTEMPTS",
    }
)
# 保护同一进程里的并发读写；原子替换另负责保证磁盘文件不会只写了一半。
_runtime_settings_lock = threading.RLock()


def _required_value(
    environ: Mapping[str, str],
    name: str,
    default: str,
) -> str:
    """读取必填字符串，去除首尾空白，空值时抛 ``ConfigurationError``。

    ``environ`` 是环境变量式映射，``name`` 是键名，``default`` 在键不存在时使用。
    返回保证非空的字符串；集中处理让错误消息始终指出具体变量名。
    """
    value = environ.get(name, default).strip()
    if not value:
        raise ConfigurationError(f"{name} cannot be blank")
    return value


def _positive_integer(
    environ: Mapping[str, str],
    name: str,
    default: int,
) -> int:
    """把环境变量转换为大于零的整数。

    ``default`` 已是整数。无法执行 ``int`` 或结果小于 1 时抛 ConfigurationError；
    返回值可直接用于超时、重试和并发上限。
    """
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
    """读取可选字符串；缺失或去空白后为空则统一返回 ``None``。"""
    return environ.get(name, "").strip() or None


def _choice_value(
    environ: Mapping[str, str],
    name: str,
    default: str,
    choices: frozenset[str],
) -> str:
    """读取小写枚举值并验证其属于 ``choices``。

    ``choices`` 用 frozenset 表示不会被修改。成功返回规范化小写字符串，业务层
    无需反复处理大小写；失败抛出列有允许值的 ConfigurationError。
    """
    value = _required_value(environ, name, default).lower()
    if value not in choices:
        allowed = " or ".join(sorted(choices))
        raise ConfigurationError(f"{name} must be {allowed}")
    return value


def _normalize_http_url(name: str, value: str) -> str:
    """规范化服务根地址，并拒绝容易泄密或产生歧义的 URL 组成部分。"""
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


def _normalize_compatible_base_url(name: str, value: str) -> str:
    """Allow HTTPS endpoints and HTTP only for a local model gateway."""
    normalized = _normalize_http_url(name, value)
    parsed = urlparse(normalized)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ConfigurationError(
            f"{name} must use HTTPS unless it points to localhost"
        )
    return normalized


def _normalize_deepseek_base_url(value: str) -> str:
    """只接受 DeepSeek 官方 HTTPS 根地址或 ``/v1``，返回规范地址。

    这防止 DeepSeek 预设密钥被误发往自定义域名；自定义端点必须显式选择
    ``openai_compatible`` 供应商。
    """
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


def _normalize_siliconflow_base_url(value: str) -> str:
    """Allow only SiliconFlow's official OpenAI-compatible API root."""
    normalized = _normalize_http_url("EMBEDDING_BASE_URL", value)
    parsed = urlparse(normalized)
    try:
        port = parsed.port
    except ValueError as error:
        raise ConfigurationError(
            "EMBEDDING_BASE_URL contains an invalid port"
        ) from error
    if (
        parsed.scheme != "https"
        or port is not None
        or (parsed.hostname or "").lower() != "api.siliconflow.cn"
        or parsed.path != "/v1"
    ):
        raise ConfigurationError(
            "EMBEDDING_BASE_URL must be "
            "https://api.siliconflow.cn/v1 for siliconflow"
        )
    return SILICONFLOW_EMBEDDING_BASE_URL


def load_settings(environ: Mapping[str, str]) -> Settings:
    """Build and validate settings from an environment mapping."""
    generation_provider = _required_value(
        environ,
        "GENERATION_PROVIDER",
        "deepseek",
    ).lower()
    if generation_provider not in GENERATION_PROVIDERS:
        raise ConfigurationError(
            "GENERATION_PROVIDER must be deepseek or openai_compatible"
        )
    api_key = environ.get("DEEPSEEK_API_KEY", "").strip() or None
    raw_generation_base_url = _required_value(
        environ,
        "DEEPSEEK_BASE_URL",
        "https://api.deepseek.com",
    )
    deepseek_base_url = (
        _normalize_deepseek_base_url(raw_generation_base_url)
        if generation_provider == "deepseek"
        else _normalize_compatible_base_url(
            "DEEPSEEK_BASE_URL",
            raw_generation_base_url,
        )
    )
    embedding_provider = _required_value(
        environ,
        "EMBEDDING_PROVIDER",
        "siliconflow",
    ).lower()
    if embedding_provider not in EMBEDDING_PROVIDERS:
        raise ConfigurationError(
            "EMBEDDING_PROVIDER must be siliconflow or openai_compatible"
        )
    embedding_api_key = _optional_value(environ, "EMBEDDING_API_KEY")
    raw_embedding_base_url = _optional_value(environ, "EMBEDDING_BASE_URL")
    if raw_embedding_base_url is None and embedding_provider == "siliconflow":
        raw_embedding_base_url = SILICONFLOW_EMBEDDING_BASE_URL
    embedding_base_url = raw_embedding_base_url
    if embedding_base_url is not None:
        embedding_base_url = (
            _normalize_siliconflow_base_url(embedding_base_url)
            if embedding_provider == "siliconflow"
            else _normalize_compatible_base_url(
                "EMBEDDING_BASE_URL",
                embedding_base_url,
            )
        )
    embedding_model = _required_value(
        environ,
        "EMBEDDING_MODEL",
        SILICONFLOW_EMBEDDING_MODEL,
    )
    if (
        embedding_provider == "siliconflow"
        and embedding_model != SILICONFLOW_EMBEDDING_MODEL
    ):
        raise ConfigurationError(
            "EMBEDDING_MODEL must be Qwen/Qwen3-Embedding-4B for "
            "siliconflow"
        )
    embedding_dimensions = _positive_integer(
        environ,
        "EMBEDDING_DIMENSIONS",
        SILICONFLOW_EMBEDDING_DIMENSIONS,
    )
    if embedding_dimensions > 8_192:
        raise ConfigurationError(
            "EMBEDDING_DIMENSIONS must not exceed 8192"
        )
    if (
        embedding_provider == "siliconflow"
        and embedding_dimensions != SILICONFLOW_EMBEDDING_DIMENSIONS
    ):
        raise ConfigurationError(
            "EMBEDDING_DIMENSIONS must be 1024 for siliconflow"
        )
    if embedding_api_key and not embedding_base_url:
        raise ConfigurationError(
            "EMBEDDING_BASE_URL is required when EMBEDDING_API_KEY is set"
        )
    summary_concurrency_mode = _choice_value(
        environ,
        "SUMMARY_CONCURRENCY_MODE",
        "auto",
        frozenset({"auto", "manual"}),
    )
    summary_max_concurrency = _positive_integer(
        environ,
        "SUMMARY_MAX_CONCURRENCY",
        2_500,
    )
    if summary_max_concurrency > 2_500:
        raise ConfigurationError(
            "SUMMARY_MAX_CONCURRENCY must not exceed 2500"
        )

    return Settings(
        app_name=_required_value(environ, "APP_NAME", "NovelMind"),
        database_url=_required_value(
            environ,
            "DATABASE_URL",
            "sqlite:///./data/novelmind.db",
        ),
        generation_provider=generation_provider,
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
        summary_concurrency_mode=summary_concurrency_mode,
        summary_max_concurrency=summary_max_concurrency,
        embedding_provider=embedding_provider,
        embedding_api_key=embedding_api_key,
        embedding_base_url=embedding_base_url,
        embedding_model=embedding_model,
        embedding_dimensions=embedding_dimensions,
        embedding_timeout_seconds=_positive_integer(
            environ,
            "EMBEDDING_TIMEOUT_SECONDS",
            120,
        ),
        embedding_max_attempts=_positive_integer(
            environ,
            "EMBEDDING_MAX_ATTEMPTS",
            3,
        ),
        vector_store_path=_required_value(
            environ,
            "VECTOR_STORE_PATH",
            environ.get("CHROMA_PATH", "./vector_store"),
        ),
    )


def _runtime_settings_path() -> Path:
    """返回前端覆盖配置文件的绝对 Path，供原子读取和写入使用。"""
    return Path(
        os.environ.get(
            "MODEL_SETTINGS_PATH",
            "./data/model_settings.json",
        )
    )


def _replace_legacy_embedding_settings(
    values: Mapping[str, str],
) -> dict[str, str]:
    """Move the retired embedding preset to unconfigured SiliconFlow safely."""
    migrated = dict(values)
    provider = migrated.get("EMBEDDING_PROVIDER", "").strip().lower()
    base_url = migrated.get("EMBEDDING_BASE_URL", "").strip().lower()
    if (
        provider != "aliyun_model_studio"
        and ".maas.aliyuncs.com" not in base_url
    ):
        return migrated
    migrated.update(
        {
            "EMBEDDING_PROVIDER": "siliconflow",
            # Never send an Alibaba credential to a different provider.
            "EMBEDDING_API_KEY": "",
            "EMBEDDING_BASE_URL": SILICONFLOW_EMBEDDING_BASE_URL,
            "EMBEDDING_MODEL": SILICONFLOW_EMBEDDING_MODEL,
            "EMBEDDING_DIMENSIONS": str(
                SILICONFLOW_EMBEDDING_DIMENSIONS
            ),
        }
    )
    return migrated


def _atomic_write_runtime_payload(
    path: Path,
    payload: Mapping[str, str],
) -> None:
    """Atomically write recognized model settings with restricted access."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as error:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ConfigurationError(
            "The local model settings file cannot be saved"
        ) from error


def _read_runtime_model_settings() -> dict[str, str]:
    """Read only recognized string overrides from the local settings file."""
    path = _runtime_settings_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(
            "The local model settings file cannot be read"
        ) from error
    if not isinstance(payload, dict):
        raise ConfigurationError(
            "The local model settings file must contain an object"
        )
    overrides: dict[str, str] = {}
    for name, value in payload.items():
        if name not in _MODEL_SETTING_NAMES or not isinstance(value, str):
            raise ConfigurationError(
                "The local model settings file contains invalid fields"
            )
        overrides[name] = value
    migrated = _replace_legacy_embedding_settings(overrides)
    if migrated != overrides:
        _atomic_write_runtime_payload(path, migrated)
    return migrated


def _model_settings_environ(value: Settings) -> dict[str, str]:
    """Serialize the editable provider fields without unrelated settings."""
    return {
        "GENERATION_PROVIDER": value.generation_provider,
        "DEEPSEEK_API_KEY": value.deepseek_api_key or "",
        "DEEPSEEK_BASE_URL": value.deepseek_base_url,
        "DEEPSEEK_MODEL": value.deepseek_model,
        "DEEPSEEK_TIMEOUT_SECONDS": str(value.deepseek_timeout_seconds),
        "DEEPSEEK_MAX_ATTEMPTS": str(value.deepseek_max_attempts),
        "DEEPSEEK_MAX_TOKENS": str(value.deepseek_max_tokens),
        "SUMMARY_CONCURRENCY_MODE": value.summary_concurrency_mode,
        "SUMMARY_MAX_CONCURRENCY": str(value.summary_max_concurrency),
        "EMBEDDING_PROVIDER": value.embedding_provider,
        "EMBEDDING_API_KEY": value.embedding_api_key or "",
        "EMBEDDING_BASE_URL": value.embedding_base_url or "",
        "EMBEDDING_MODEL": value.embedding_model,
        "EMBEDDING_DIMENSIONS": str(value.embedding_dimensions),
        "EMBEDDING_TIMEOUT_SECONDS": str(
            value.embedding_timeout_seconds
        ),
        "EMBEDDING_MAX_ATTEMPTS": str(value.embedding_max_attempts),
    }


def save_runtime_model_settings(value: Settings) -> None:
    """Atomically persist validated local provider settings."""
    path = _runtime_settings_path()
    payload = _model_settings_environ(value)
    with _runtime_settings_lock:
        _atomic_write_runtime_payload(path, payload)


def update_runtime_model_settings(
    overrides: Mapping[str, str],
) -> Settings:
    """Validate and persist a partial editable-settings update."""
    unknown = set(overrides).difference(_MODEL_SETTING_NAMES)
    if unknown:
        raise ConfigurationError("Unknown model setting")
    with _runtime_settings_lock:
        current = get_settings()
        environ = dict(os.environ)
        environ.update(_model_settings_environ(current))
        environ.update(overrides)
        updated = load_settings(environ)
        save_runtime_model_settings(updated)
        return updated


def clear_runtime_model_settings() -> None:
    """Remove the local override so environment settings apply again."""
    with _runtime_settings_lock:
        try:
            _runtime_settings_path().unlink(missing_ok=True)
        except OSError as error:
            raise ConfigurationError(
                "The local model settings file cannot be removed"
            ) from error


def runtime_model_settings_exist() -> bool:
    """Return whether the frontend-managed override file exists."""
    return _runtime_settings_path().is_file()


load_dotenv()
settings = load_settings(_replace_legacy_embedding_settings(os.environ))


def get_settings() -> Settings:
    """Return environment settings overlaid by editable local settings."""
    with _runtime_settings_lock:
        environ = _replace_legacy_embedding_settings(os.environ)
        environ.update(_read_runtime_model_settings())
        return load_settings(environ)
