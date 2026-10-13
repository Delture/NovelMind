"""不在本地运行模型。某些兼容端点不接受 dimensions，只在明确 HTTP 400 时有限回退一次。"""

import logging
import math
import random as random_module
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Protocol, Sequence, runtime_checkable

import httpx
import openai
from openai import OpenAI

from backend.app.core.config import Settings


MAX_EMBEDDING_BATCH_SIZE = 10
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 429})
_MAX_BACKOFF_SECONDS = 30.0
_SENSITIVE_SDK_LOGGERS = (
    "openai",
    "openai._base_client",
    "httpx",
    "httpcore",
)


class EmbeddingProviderError(RuntimeError):
    """Base class for embedding errors safe to display to a user."""

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


class EmbeddingNotConfiguredError(EmbeddingProviderError):
    def __init__(self) -> None:
        super().__init__(
            "向量模型 API 未配置，请前往“模型与密钥”完成设置"
            "（或设置 EMBEDDING_API_KEY 与 EMBEDDING_BASE_URL）。",
            code="embedding_not_configured",
        )


class EmbeddingAuthenticationError(EmbeddingProviderError):
    def __init__(self) -> None:
        super().__init__(
            "向量模型 API 认证失败，请检查供应商、密钥与端点配置。",
            code="embedding_authentication_failed",
        )


class EmbeddingBalanceError(EmbeddingProviderError):
    def __init__(self) -> None:
        super().__init__(
            "向量模型账户额度或余额不足，请处理后重试。",
            code="embedding_balance_insufficient",
        )


class EmbeddingRateLimitError(EmbeddingProviderError):
    def __init__(self) -> None:
        super().__init__(
            "向量模型请求频率已达上限，请稍后重试。",
            code="embedding_rate_limited",
            retryable=True,
        )


class EmbeddingRequestError(EmbeddingProviderError):
    def __init__(self) -> None:
        super().__init__(
            "向量模型服务拒绝了本次请求，请检查配置或输入。",
            code="embedding_request_rejected",
        )


class EmbeddingUnavailableError(EmbeddingProviderError):
    def __init__(self) -> None:
        super().__init__(
            "向量模型服务暂时不可用，请稍后重试。",
            code="embedding_unavailable",
            retryable=True,
        )


class EmbeddingResponseError(EmbeddingProviderError):
    def __init__(self) -> None:
        super().__init__(
            "向量模型返回了无法安全使用的结果，请重试。",
            code="embedding_response_invalid",
        )


@dataclass(frozen=True)
class EmbeddingBatchResult:
    vectors: tuple[tuple[float, ...], ...]
    model: str
    prompt_tokens: int
    request_id: Optional[str]


@runtime_checkable
class EmbeddingProvider(Protocol):
    @property
    def model(self) -> str:
        ...

    @property
    def dimensions(self) -> int:
        ...

    def embed(
        self,
        texts: Sequence[str],
        *,
        before_attempt: Optional[Callable[[], None]] = None,
    ) -> EmbeddingBatchResult:
        ...

    def close(self) -> None:
        ...


class OpenAIEmbeddingProvider:
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: Optional[httpx.Client] = None,
        sleep: Callable[[float], None] = time.sleep,
        random: Callable[[], float] = random_module.random,
    ) -> None:
        """配置不完整立即抛 NotConfigured，不发送请求。"""
        if not settings.embedding_configured:
            raise EmbeddingNotConfiguredError()

        self._model = settings.embedding_model
        self._provider = settings.embedding_provider
        self._dimensions = settings.embedding_dimensions
        self._max_attempts = settings.embedding_max_attempts
        self._sleep = sleep
        self._random = random
        self._closed = False
        timeout_seconds = float(settings.embedding_timeout_seconds)
        timeout = httpx.Timeout(
            timeout_seconds,
            connect=10.0,
            read=timeout_seconds,
            write=60.0,
            pool=10.0,
        )
        client_options: Dict[str, Any] = {
            "api_key": settings.embedding_api_key,
            "base_url": settings.embedding_base_url,
            "timeout": timeout,
            "max_retries": 0,
        }
        if http_client is not None:
            client_options["http_client"] = http_client
        self._client = OpenAI(**client_options)

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider_id(self) -> str:
        return self._provider

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def close(self) -> None:
        if self._closed:
            return
        self._client.close()
        self._closed = True

    def embed(
        self,
        texts: Sequence[str],
        *,
        before_attempt: Optional[Callable[[], None]] = None,
    ) -> EmbeddingBatchResult:
        """Embed at most ten non-empty texts and strictly validate output."""
        normalized_texts = list(texts)
        if (
            not normalized_texts
            or len(normalized_texts) > MAX_EMBEDDING_BATCH_SIZE
            or any(
                not isinstance(text, str) or not text.strip()
                for text in normalized_texts
            )
        ):
            raise ValueError(
                "texts must contain 1 to 10 non-empty strings"
            )

        for attempt in range(1, self._max_attempts + 1):
            if before_attempt is not None:
                before_attempt()
            try:
                self._suppress_sensitive_sdk_debug_logs()
                request: Dict[str, Any] = {
                    "model": self._model,
                    "input": normalized_texts,
                    "dimensions": self._dimensions,
                    "encoding_format": "float",
                }
                response = self._client.embeddings.create(
                    **request,
                )
            except openai.APIConnectionError:
                if attempt < self._max_attempts:
                    self._wait_before_retry(attempt)
                    continue
                raise EmbeddingUnavailableError() from None
            except openai.APIStatusError as error:
                if (
                    self._provider == "openai_compatible"
                    and error.status_code == 400
                ):
                    # Some compatible services reject the OpenAI dimensions
                    # extension. Negotiate once without it while retaining
                    # strict response-width validation below.
                    if before_attempt is not None:
                        before_attempt()
                    fallback_request = dict(request)
                    fallback_request.pop("dimensions", None)
                    try:
                        self._suppress_sensitive_sdk_debug_logs()
                        response = self._client.embeddings.create(
                            **fallback_request,
                        )
                    except openai.APIConnectionError:
                        if attempt < self._max_attempts:
                            self._wait_before_retry(attempt)
                            continue
                        raise EmbeddingUnavailableError() from None
                    except openai.APIStatusError as fallback_error:
                        error = fallback_error
                    except openai.APIError:
                        raise EmbeddingUnavailableError() from None
                    else:
                        return self._validate_response(
                            response,
                            expected_count=len(normalized_texts),
                        )
                status_code = error.status_code
                if self._is_retryable_status(status_code):
                    if attempt < self._max_attempts:
                        self._wait_before_retry(attempt)
                        continue
                    if status_code == 429:
                        raise EmbeddingRateLimitError() from None
                    raise EmbeddingUnavailableError() from None
                self._raise_terminal_status(status_code)
            except openai.APIError:
                raise EmbeddingUnavailableError() from None

            return self._validate_response(
                response,
                expected_count=len(normalized_texts),
            )

        raise EmbeddingUnavailableError()

    def _validate_response(
        self,
        response: object,
        *,
        expected_count: int,
    ) -> EmbeddingBatchResult:
        """校验模型名、索引连续性、条数、维度和有限数值；任一不符抛 EmbeddingResponseError。"""
        response_model = getattr(response, "model", None)
        if (
            self._provider == "siliconflow"
            and response_model != self._model
        ):
            raise EmbeddingResponseError()

        data = getattr(response, "data", None)
        if not isinstance(data, list) or len(data) != expected_count:
            raise EmbeddingResponseError()

        ordered: list[Optional[tuple[float, ...]]] = [
            None
            for _ in range(expected_count)
        ]
        for item in data:
            index = getattr(item, "index", None)
            raw_vector = getattr(item, "embedding", None)
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or index < 0
                or index >= expected_count
                or ordered[index] is not None
                or not isinstance(raw_vector, list)
                or len(raw_vector) != self._dimensions
            ):
                raise EmbeddingResponseError()
            vector: list[float] = []
            for value in raw_vector:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                ):
                    raise EmbeddingResponseError()
                vector.append(float(value))
            if not any(value != 0.0 for value in vector):
                raise EmbeddingResponseError()
            ordered[index] = tuple(vector)

        if any(vector is None for vector in ordered):
            raise EmbeddingResponseError()

        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        if prompt_tokens is None:
            prompt_tokens = getattr(usage, "total_tokens", 0)
        if (
            not isinstance(prompt_tokens, int)
            or isinstance(prompt_tokens, bool)
            or prompt_tokens < 0
        ):
            raise EmbeddingResponseError()

        request_id = getattr(response, "_request_id", None)
        if request_id is not None and not isinstance(request_id, str):
            request_id = None
        return EmbeddingBatchResult(
            vectors=tuple(
                vector
                for vector in ordered
                if vector is not None
            ),
            model=self._model,
            prompt_tokens=prompt_tokens,
            request_id=request_id,
        )

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        """408/409/429 和所有 5xx 返回 True，其余客户端错误不可重试。"""
        return (
            status_code in _RETRYABLE_STATUS_CODES
            or 500 <= status_code <= 599
        )

    @staticmethod
    def _raise_terminal_status(status_code: int) -> None:
        if status_code in {401, 403}:
            raise EmbeddingAuthenticationError()
        if status_code == 402:
            raise EmbeddingBalanceError()
        raise EmbeddingRequestError()

    def _wait_before_retry(self, failed_attempt: int) -> None:
        delay = min(
            _MAX_BACKOFF_SECONDS,
            float(2 ** (failed_attempt - 1)) + self._random(),
        )
        self._sleep(delay)

    @staticmethod
    def _suppress_sensitive_sdk_debug_logs() -> None:
        """将可能打印请求头/正文的 SDK logger 最低提高到 WARNING。"""
        for logger_name in _SENSITIVE_SDK_LOGGERS:
            logger = logging.getLogger(logger_name)
            if logger.level == logging.NOTSET or logger.level < logging.WARNING:
                logger.setLevel(logging.WARNING)
