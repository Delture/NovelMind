"""Provider-neutral contracts for structured LLM generation."""

from dataclasses import dataclass
from typing import (
    Callable,
    Generic,
    Optional,
    Protocol,
    Type,
    TypeVar,
    runtime_checkable,
)

from pydantic import BaseModel


T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class JsonGenerationResult(Generic[T]):
    """A validated JSON result plus non-sensitive provider metadata."""

    value: T
    model: str
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int
    request_id: Optional[str]


class LLMProviderError(RuntimeError):
    """Base class for errors whose messages are safe to show to users."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ProviderNotConfiguredError(LLMProviderError):
    """Raised when generation credentials have not been configured."""

    def __init__(self) -> None:
        super().__init__(
            "DeepSeek API 未配置，请在本地 .env 中设置 DEEPSEEK_API_KEY。",
            code="provider_not_configured",
        )


class ProviderAuthenticationError(LLMProviderError):
    """Raised when the provider rejects authentication or authorization."""

    def __init__(self) -> None:
        super().__init__(
            "DeepSeek API 认证失败，请检查本地 API 配置。",
            code="provider_authentication_failed",
        )


class ProviderBalanceError(LLMProviderError):
    """Raised when the provider account has insufficient balance."""

    def __init__(self) -> None:
        super().__init__(
            "DeepSeek API 余额不足，请充值后重试。",
            code="provider_balance_insufficient",
        )


class ProviderRateLimitError(LLMProviderError):
    """Raised after DeepSeek rate-limit retries are exhausted."""

    def __init__(self) -> None:
        super().__init__(
            "DeepSeek 请求频率已达上限，请稍后重试。",
            code="provider_rate_limited",
            retryable=True,
        )


class ProviderRequestError(LLMProviderError):
    """Raised when the provider rejects a non-retryable request."""

    def __init__(self) -> None:
        super().__init__(
            "DeepSeek 拒绝了本次请求，请检查模型配置或缩短输入。",
            code="provider_request_rejected",
        )


class ProviderUnavailableError(LLMProviderError):
    """Raised when retryable provider failures exhaust their attempts."""

    def __init__(self) -> None:
        super().__init__(
            "DeepSeek 服务暂时不可用，请稍后重试。",
            code="provider_unavailable",
            retryable=True,
        )


class ProviderResponseError(LLMProviderError):
    """Raised when a provider response cannot be safely consumed."""

    def __init__(self) -> None:
        super().__init__(
            "DeepSeek 返回了无法解析的结果，请重试。",
            code="provider_response_invalid",
        )


@runtime_checkable
class LLMProvider(Protocol):
    """Generate validated JSON without exposing provider implementation details."""

    @property
    def model(self) -> str:
        """Return the provider model identifier."""
        ...

    def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: Type[T],
        thinking: bool = False,
        reasoning_effort: Optional[str] = None,
        before_attempt: Optional[Callable[[], None]] = None,
    ) -> JsonGenerationResult[T]:
        """Generate JSON, checking ``before_attempt`` before each send."""
        ...

    def close(self) -> None:
        """Release provider-owned network resources."""
        ...
