"""OpenAI 兼容结构化生成客户端。网络重试与 JSON 语义纠错分别计数，避免无上限付费；
日志绝不记录提示全文、密钥或模型推理。
"""

import asyncio
import json
import logging
import random as random_module
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Type

import httpx
import openai
from openai import AsyncOpenAI, OpenAI
from pydantic import ValidationError

from backend.app.analyzer.llm import (
    JsonGenerationResult,
    ProviderAuthenticationError,
    ProviderBalanceError,
    ProviderNotConfiguredError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderUnavailableError,
    T,
)
from backend.app.core.config import Settings


_JSON_INSTRUCTION = (
    "\n\n请仅输出一个有效的 JSON 对象，不要包含 Markdown 代码块、注释、"
    "前后说明或思维过程。严格遵循当前任务要求的字段名、字段类型、数量"
    "限制与 null 规则，不得增加字段。"
    'JSON 字面对象示例：{"field":"value"}；示例只说明语法，不是输出模板。'
)
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 429})
_TERMINAL_FINISH_REASONS = frozenset(
    {"length", "content_filter", "tool_calls"}
)
MAX_JSON_GENERATION_ATTEMPTS = 5
_MAX_BACKOFF_SECONDS = 30.0
_SENSITIVE_SDK_LOGGERS = (
    "openai",
    "openai._base_client",
    "httpx",
    "httpcore",
)
_SAFE_FINISH_REASONS = frozenset(
    {"length", "content_filter", "tool_calls", "stop", "other"}
)
logger = logging.getLogger(__name__)


def _strip_json_fence(content: str) -> str:
    candidate = content.strip().lstrip("\ufeff")
    if not candidate.startswith("```") or not candidate.endswith("```"):
        return candidate
    lines = candidate.splitlines()
    if len(lines) < 3 or lines[-1].strip() != "```":
        return candidate
    opening = lines[0].strip().lower()
    if opening not in {"```", "```json"}:
        return candidate
    return "\n".join(lines[1:-1]).strip()


def _validation_feedback(error: ValidationError) -> str:
    """不把原始敏感输入再次拼入提示。"""
    issues: list[str] = []
    for item in error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )[:8]:
        location = ".".join(str(part) for part in item.get("loc", ()))
        issue_type = str(item.get("type", "invalid"))
        issues.append(f"{location or '<root>'}:{issue_type}")
    return "；".join(issues) or "字段或类型不符合约定"


def _validate_json_content(
    content: object,
    response_model: Type[T],
) -> tuple[Optional[T], str, Optional[str]]:
    if not isinstance(content, str) or not content.strip():
        return None, "上一次输出为空。", "empty"
    try:
        decoded = json.loads(_strip_json_fence(content))
    except (json.JSONDecodeError, TypeError):
        return None, "上一次输出不是有效的 JSON 对象。", "json"
    try:
        return response_model.model_validate(decoded), "", None
    except ValidationError as error:
        return (
            None,
            "上一次输出未通过字段校验（"
            f"{_validation_feedback(error)}）。",
            "pydantic",
        )


def _safe_finish_reason(finish_reason: object) -> str:
    if isinstance(finish_reason, str) and finish_reason in _SAFE_FINISH_REASONS:
        return finish_reason
    return "other"


def _append_semantic_retry_feedback(
    messages: list[dict[str, str]],
    feedback: str,
) -> None:
    """Change the next attempt without reflecting invalid output content."""
    messages.append(
        {
            "role": "user",
            "content": (
                f"{feedback}请重新完成同一任务，从头输出一个完整 JSON 对象；"
                "不要解释，不要省略必填字段，不要沿用上一版错误结构。"
            ),
        }
    )


class DeepSeekProvider:
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: Optional[httpx.Client] = None,
        sleep: Callable[[float], None] = time.sleep,
        random: Callable[[], float] = random_module.random,
    ) -> None:
        """SDK 自带重试已关闭，由本类统一分类、计数和退避。"""
        if not settings.deepseek_api_key:
            raise ProviderNotConfiguredError()

        self._model = settings.deepseek_model
        self._provider = settings.generation_provider
        self._max_attempts = settings.deepseek_max_attempts
        self._max_tokens = settings.deepseek_max_tokens
        self._sleep = sleep
        self._random = random
        self._closed = False
        timeout_seconds = float(settings.deepseek_timeout_seconds)
        timeout = httpx.Timeout(
            timeout_seconds,
            connect=10.0,
            read=timeout_seconds,
            write=60.0,
            pool=10.0,
        )
        client_options: Dict[str, Any] = {
            "api_key": settings.deepseek_api_key,
            "base_url": settings.deepseek_base_url,
            "timeout": timeout,
            "max_retries": 0,
        }
        if http_client is not None:
            client_options["http_client"] = http_client
        self._client = OpenAI(**client_options)

    @property
    def model(self) -> str:
        return self._model

    def close(self) -> None:
        if self._closed:
            return
        self._client.close()
        self._closed = True

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
        self._validate_reasoning_options(thinking, reasoning_effort)
        messages = [
            {
                "role": "system",
                "content": f"{system_prompt.rstrip()}{_JSON_INSTRUCTION}",
            },
            {"role": "user", "content": user_prompt},
        ]

        for semantic_attempt in range(MAX_JSON_GENERATION_ATTEMPTS):
            response = self._create_completion(
                messages=messages,
                thinking=thinking,
                reasoning_effort=reasoning_effort,
                before_attempt=before_attempt,
            )
            choice = self._first_choice(response)
            finish_reason = choice.finish_reason
            if finish_reason == "length":
                self._log_json_failure(
                    category="length",
                    semantic_attempt=semantic_attempt + 1,
                    finish_reason=finish_reason,
                    response=response,
                )
                raise ProviderResponseError(category="length")
            if finish_reason in _TERMINAL_FINISH_REASONS or finish_reason != "stop":
                self._log_json_failure(
                    category="finish",
                    semantic_attempt=semantic_attempt + 1,
                    finish_reason=finish_reason,
                    response=response,
                )
                raise ProviderResponseError(category="finish")

            value, feedback, failure_category = _validate_json_content(
                choice.message.content,
                response_model,
            )
            if value is not None:
                return self._build_result(response, value)

            assert failure_category is not None
            self._log_json_failure(
                category=failure_category,
                semantic_attempt=semantic_attempt + 1,
                finish_reason=finish_reason,
                response=response,
            )
            if semantic_attempt == MAX_JSON_GENERATION_ATTEMPTS - 1:
                raise ProviderResponseError(category=failure_category)
            _append_semantic_retry_feedback(messages, feedback)

        raise ProviderResponseError()

    def _create_completion(
        self,
        *,
        messages: list,
        thinking: bool,
        reasoning_effort: Optional[str],
        before_attempt: Optional[Callable[[], None]],
    ):
        request_options: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "max_tokens": self._max_tokens,
            "response_format": {"type": "json_object"},
        }
        if self._provider == "deepseek":
            request_options["extra_body"] = {
                "thinking": {
                    "type": "enabled" if thinking else "disabled",
                }
            }
        if reasoning_effort is not None and self._provider == "deepseek":
            request_options["reasoning_effort"] = reasoning_effort

        for attempt in range(1, self._max_attempts + 1):
            if before_attempt is not None:
                before_attempt()
            try:
                self._suppress_sensitive_sdk_debug_logs()
                response = self._client.chat.completions.create(
                    **request_options
                )
            except openai.APIConnectionError:
                if attempt < self._max_attempts:
                    self._wait_before_retry(attempt)
                    continue
                raise ProviderUnavailableError() from None
            except openai.APIStatusError as error:
                status_code = error.status_code
                if self._is_retryable_status(status_code):
                    if attempt < self._max_attempts:
                        self._wait_before_retry(attempt)
                        continue
                    if status_code == 429:
                        raise ProviderRateLimitError() from None
                    raise ProviderUnavailableError() from None
                self._raise_terminal_status(status_code)
            except openai.APIError:
                raise ProviderUnavailableError() from None

            choice = self._first_choice(response)
            if choice.finish_reason == "insufficient_system_resource":
                if attempt < self._max_attempts:
                    self._wait_before_retry(attempt)
                    continue
                raise ProviderUnavailableError()
            return response

        raise ProviderUnavailableError()

    @staticmethod
    def _validate_reasoning_options(
        thinking: bool,
        reasoning_effort: Optional[str],
    ) -> None:
        if reasoning_effort is not None and reasoning_effort not in {
            "high",
            "max",
        }:
            raise ValueError("reasoning_effort must be 'high' or 'max'")
        if not thinking and reasoning_effort is not None:
            raise ValueError(
                "reasoning_effort requires thinking mode to be enabled"
            )

    @staticmethod
    def _first_choice(response):
        if not response.choices:
            raise ProviderResponseError()
        return response.choices[0]

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code in _RETRYABLE_STATUS_CODES or status_code >= 500

    @staticmethod
    def _suppress_sensitive_sdk_debug_logs() -> None:
        """抑制可能包含请求正文或认证信息的 SDK DEBUG 日志。"""
        for logger_name in _SENSITIVE_SDK_LOGGERS:
            logger = logging.getLogger(logger_name)
            if logger.isEnabledFor(logging.DEBUG):
                logger.setLevel(logging.WARNING)

    @staticmethod
    def _raise_terminal_status(status_code: int) -> None:
        if status_code in {401, 403}:
            raise ProviderAuthenticationError() from None
        if status_code == 402:
            raise ProviderBalanceError() from None
        raise ProviderRequestError() from None

    def _wait_before_retry(self, failed_attempt: int) -> None:
        exponential = min(
            2.0 ** (failed_attempt - 1),
            _MAX_BACKOFF_SECONDS,
        )
        jitter = max(0.0, min(float(self._random()), 1.0))
        self._sleep(exponential + jitter)

    def _log_json_failure(
        self,
        *,
        category: str,
        semantic_attempt: int,
        finish_reason: object,
        response,
    ) -> None:
        usage = getattr(response, "usage", None)
        logger.warning(
            "json_generation_failed category=%s semantic_attempt=%s/%s "
            "model=%s max_tokens=%s finish_reason=%s "
            "prompt_tokens=%s completion_tokens=%s",
            category,
            semantic_attempt,
            MAX_JSON_GENERATION_ATTEMPTS,
            self._model,
            self._max_tokens,
            _safe_finish_reason(finish_reason),
            self._usage_value_for_log(usage, "prompt_tokens"),
            self._usage_value_for_log(usage, "completion_tokens"),
        )

    def _build_result(
        self,
        response,
        value: T,
    ) -> JsonGenerationResult[T]:
        usage = response.usage
        return JsonGenerationResult(
            value=value,
            model=response.model or self._model,
            prompt_tokens=self._usage_value(usage, "prompt_tokens"),
            completion_tokens=self._usage_value(
                usage,
                "completion_tokens",
            ),
            cache_hit_tokens=self._usage_value(
                usage,
                "prompt_cache_hit_tokens",
            ),
            request_id=getattr(response, "_request_id", None),
        )

    @staticmethod
    def _usage_value(usage, name: str) -> int:
        if usage is None:
            return 0
        value = getattr(usage, name, None)
        if value is None:
            extras = getattr(usage, "model_extra", None) or {}
            value = extras.get(name, 0)
        return int(value or 0)

    @staticmethod
    def _usage_value_for_log(usage, name: str) -> int:
        if usage is None:
            return 0
        value = getattr(usage, name, None)
        if value is None:
            extras = getattr(usage, "model_extra", None) or {}
            value = extras.get(name)
        return value if isinstance(value, int) else 0


class AsyncDeepSeekProvider(DeepSeekProvider):
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random: Callable[[], float] = random_module.random,
    ) -> None:
        if not settings.deepseek_api_key:
            raise ProviderNotConfiguredError()

        self._model = settings.deepseek_model
        self._provider = settings.generation_provider
        self._max_attempts = settings.deepseek_max_attempts
        self._max_tokens = settings.deepseek_max_tokens
        self._sleep_async = sleep
        self._random = random
        self._closed = False
        self.summary_concurrency_mode = settings.summary_concurrency_mode
        self.summary_initial_concurrency = (
            settings.summary_initial_concurrency
        )
        self.summary_max_concurrency = (
            settings.summary_effective_concurrency_limit
        )

        timeout_seconds = float(settings.deepseek_timeout_seconds)
        timeout = httpx.Timeout(
            timeout_seconds,
            connect=10.0,
            read=timeout_seconds,
            write=60.0,
            pool=10.0,
        )
        if http_client is None:
            connection_limit = settings.summary_effective_concurrency_limit
            http_client = httpx.AsyncClient(
                timeout=timeout,
                limits=httpx.Limits(
                    max_connections=connection_limit,
                    max_keepalive_connections=min(
                        connection_limit,
                        256,
                    ),
                ),
            )
        self._async_client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            timeout=timeout,
            max_retries=0,
            http_client=http_client,
        )

    def generate_json(self, **_: object):
        raise RuntimeError(
            "AsyncDeepSeekProvider requires generate_json_async"
        )

    async def generate_json_async(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: Type[T],
        thinking: bool = False,
        reasoning_effort: Optional[str] = None,
        before_attempt: Optional[Callable[[], None]] = None,
    ) -> JsonGenerationResult[T]:
        self._validate_reasoning_options(thinking, reasoning_effort)
        messages = [
            {
                "role": "system",
                "content": f"{system_prompt.rstrip()}{_JSON_INSTRUCTION}",
            },
            {"role": "user", "content": user_prompt},
        ]

        for semantic_attempt in range(MAX_JSON_GENERATION_ATTEMPTS):
            response = await self._create_completion_async(
                messages=messages,
                thinking=thinking,
                reasoning_effort=reasoning_effort,
                before_attempt=before_attempt,
            )
            choice = self._first_choice(response)
            finish_reason = choice.finish_reason
            if finish_reason == "length":
                self._log_json_failure(
                    category="length",
                    semantic_attempt=semantic_attempt + 1,
                    finish_reason=finish_reason,
                    response=response,
                )
                raise ProviderResponseError(category="length")
            if finish_reason in _TERMINAL_FINISH_REASONS or finish_reason != "stop":
                self._log_json_failure(
                    category="finish",
                    semantic_attempt=semantic_attempt + 1,
                    finish_reason=finish_reason,
                    response=response,
                )
                raise ProviderResponseError(category="finish")

            value, feedback, failure_category = _validate_json_content(
                choice.message.content,
                response_model,
            )
            if value is not None:
                return self._build_result(response, value)

            assert failure_category is not None
            self._log_json_failure(
                category=failure_category,
                semantic_attempt=semantic_attempt + 1,
                finish_reason=finish_reason,
                response=response,
            )
            if semantic_attempt == MAX_JSON_GENERATION_ATTEMPTS - 1:
                raise ProviderResponseError(category=failure_category)
            _append_semantic_retry_feedback(messages, feedback)

        raise ProviderResponseError()

    async def _create_completion_async(
        self,
        *,
        messages: list,
        thinking: bool,
        reasoning_effort: Optional[str],
        before_attempt: Optional[Callable[[], None]],
    ):
        request_options: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "max_tokens": self._max_tokens,
            "response_format": {"type": "json_object"},
        }
        if self._provider == "deepseek":
            request_options["extra_body"] = {
                "thinking": {
                    "type": "enabled" if thinking else "disabled",
                }
            }
        if reasoning_effort is not None and self._provider == "deepseek":
            request_options["reasoning_effort"] = reasoning_effort

        for attempt in range(1, self._max_attempts + 1):
            if before_attempt is not None:
                before_attempt()
            try:
                self._suppress_sensitive_sdk_debug_logs()
                response = await self._async_client.chat.completions.create(
                    **request_options
                )
            except openai.APIConnectionError:
                if attempt < self._max_attempts:
                    await self._wait_before_retry_async(attempt)
                    continue
                raise ProviderUnavailableError() from None
            except openai.APIStatusError as error:
                status_code = error.status_code
                if self._is_retryable_status(status_code):
                    if attempt < self._max_attempts:
                        await self._wait_before_retry_async(attempt)
                        continue
                    if status_code == 429:
                        raise ProviderRateLimitError() from None
                    raise ProviderUnavailableError() from None
                self._raise_terminal_status(status_code)
            except openai.APIError:
                raise ProviderUnavailableError() from None

            choice = self._first_choice(response)
            if choice.finish_reason == "insufficient_system_resource":
                if attempt < self._max_attempts:
                    await self._wait_before_retry_async(attempt)
                    continue
                raise ProviderUnavailableError()
            return response

        raise ProviderUnavailableError()

    async def _wait_before_retry_async(self, failed_attempt: int) -> None:
        exponential = min(
            2.0 ** (failed_attempt - 1),
            _MAX_BACKOFF_SECONDS,
        )
        jitter = max(0.0, min(float(self._random()), 1.0))
        await self._sleep_async(exponential + jitter)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._async_client.close()

    def close(self) -> None:
        if self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclose())
        else:
            loop.create_task(self.aclose())
