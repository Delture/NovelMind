"""与具体供应商无关的结构化大模型接口和错误分类。

业务流水线只依赖 :class:`LLMProvider` 协议，不直接依赖 OpenAI/DeepSeek SDK。
这叫“依赖倒置”：摘要关心“给提示并得到已校验 Pydantic 对象”，不关心 HTTP
细节。测试可传入假 provider，未来更换兼容服务也无需重写人物或报告算法。

错误按认证、余额、限流、请求、服务不可用和响应不合约分类，因为它们的用户
提示与重试策略不同。切勿把所有异常都吞成一个“模型失败”。
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
    """已通过模式校验的结果，以及不敏感的供应商元数据。"""

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
        """保存安全用户消息、稳定机器错误码和“是否适合稍后重试”标志。"""
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ProviderNotConfiguredError(LLMProviderError):
    """Raised when generation credentials have not been configured."""

    def __init__(self) -> None:
        """构造缺少生成 API 配置错误；调用者无需传参数。"""
        super().__init__(
            "生成模型 API 未配置，请前往“模型与密钥”完成设置"
            "（或设置 DEEPSEEK_API_KEY）。",
            code="provider_not_configured",
        )


class ProviderAuthenticationError(LLMProviderError):
    """Raised when the provider rejects authentication or authorization."""

    def __init__(self) -> None:
        """构造密钥或授权被拒错误；此类错误不应自动重试。"""
        super().__init__(
            "生成模型 API 认证失败，请检查供应商与密钥配置。",
            code="provider_authentication_failed",
        )


class ProviderBalanceError(LLMProviderError):
    """Raised when the provider account has insufficient balance."""

    def __init__(self) -> None:
        """构造账户余额不足错误；不会把供应商原始响应暴露给界面。"""
        super().__init__(
            "生成模型账户余额不足，请处理后重试。",
            code="provider_balance_insufficient",
        )


class ProviderRateLimitError(LLMProviderError):
    """Raised after DeepSeek rate-limit retries are exhausted."""

    def __init__(self) -> None:
        """构造有界重试耗尽后的限流错误，并标记 retryable=True。"""
        super().__init__(
            "生成模型请求频率已达上限，请稍后重试。",
            code="provider_rate_limited",
            retryable=True,
        )


class ProviderRequestError(LLMProviderError):
    """Raised when the provider rejects a non-retryable request."""

    def __init__(self) -> None:
        """构造不可重试的请求拒绝错误，例如参数或输入超出服务要求。"""
        super().__init__(
            "生成模型服务拒绝了本次请求，请检查模型配置或缩短输入。",
            code="provider_request_rejected",
        )


class ProviderUnavailableError(LLMProviderError):
    """Raised when retryable provider failures exhaust their attempts."""

    def __init__(self) -> None:
        """构造连接/服务端故障重试耗尽错误，并允许用户稍后重试。"""
        super().__init__(
            "生成模型服务暂时不可用，请稍后重试。",
            code="provider_unavailable",
            retryable=True,
        )


class ProviderResponseError(LLMProviderError):
    """Raised when a provider response cannot be safely consumed."""

    def __init__(self) -> None:
        """构造模型输出无法通过 JSON/模式校验的安全响应错误。"""
        super().__init__(
            "生成模型返回了无法解析的结果，请重试。",
            code="provider_response_invalid",
        )


@runtime_checkable
class LLMProvider(Protocol):
    """业务层所需的最小生成能力；实现类不必继承它，只需结构匹配。"""

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
