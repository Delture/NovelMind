"""业务流水线只依赖 LLMProvider 协议，不依赖具体 SDK。
错误按认证、余额、限流、请求、服务不可用和响应不合约分类，因为用户提示与重试策略不同。
"""

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
    def __init__(self) -> None:
        super().__init__(
            "生成模型 API 未配置，请前往“模型与密钥”完成设置"
            "（或设置 DEEPSEEK_API_KEY）。",
            code="provider_not_configured",
        )


class ProviderAuthenticationError(LLMProviderError):
    def __init__(self) -> None:
        super().__init__(
            "生成模型 API 认证失败，请检查供应商与密钥配置。",
            code="provider_authentication_failed",
        )


class ProviderBalanceError(LLMProviderError):
    def __init__(self) -> None:
        super().__init__(
            "生成模型账户余额不足，请处理后重试。",
            code="provider_balance_insufficient",
        )


class ProviderRateLimitError(LLMProviderError):
    def __init__(self) -> None:
        super().__init__(
            "生成模型请求频率已达上限，请稍后重试。",
            code="provider_rate_limited",
            retryable=True,
        )


class ProviderRequestError(LLMProviderError):
    def __init__(self) -> None:
        super().__init__(
            "生成模型服务拒绝了本次请求，请检查模型配置或缩短输入。",
            code="provider_request_rejected",
        )


class ProviderUnavailableError(LLMProviderError):
    def __init__(self) -> None:
        super().__init__(
            "生成模型服务暂时不可用，请稍后重试。",
            code="provider_unavailable",
            retryable=True,
        )


class ProviderResponseError(LLMProviderError):
    def __init__(self, *, category: str = "invalid") -> None:
        if category == "length":
            message = "生成结果达到输出上限，未完成。"
        else:
            message = "生成模型返回了无法解析的结果，请重试。"
        super().__init__(
            message,
            code="provider_response_invalid",
        )
        self.category = category


@runtime_checkable
class LLMProvider(Protocol):
    @property
    def model(self) -> str:
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
        ...

    def close(self) -> None:
        ...
