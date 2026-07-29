"""DeepSeek V4 Pro implementation of structured JSON generation."""

import json
import logging
import random as random_module
import time
from typing import Callable, Dict, Optional, Type

import httpx
import openai
from openai import OpenAI
from pydantic import BaseModel, ValidationError

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
    "\n\n请仅输出有效的 JSON 对象，不要包含 Markdown 代码块或其他文本。"
    'JSON 字面对象示例：{"field":"value"}；实际字段必须遵循当前任务要求。'
)
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 429})
_TERMINAL_FINISH_REASONS = frozenset(
    {"length", "content_filter", "tool_calls"}
)
MAX_JSON_GENERATION_ATTEMPTS = 3
_MAX_BACKOFF_SECONDS = 30.0
_SENSITIVE_SDK_LOGGERS = (
    "openai",
    "openai._base_client",
    "httpx",
    "httpcore",
)


class DeepSeekProvider:
    """Generate validated JSON through DeepSeek's OpenAI-compatible API."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: Optional[httpx.Client] = None,
        sleep: Callable[[float], None] = time.sleep,
        random: Callable[[], float] = random_module.random,
    ) -> None:
        if not settings.deepseek_api_key:
            raise ProviderNotConfiguredError()

        self._model = settings.deepseek_model
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
        client_options: Dict[str, object] = {
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
        """Return the configured DeepSeek model identifier."""
        return self._model

    def close(self) -> None:
        """Close the provider-owned OpenAI/HTTPX client exactly once."""
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
        """Generate JSON, allowing cancellation before every API attempt."""
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
            if finish_reason in _TERMINAL_FINISH_REASONS:
                raise ProviderResponseError()
            if finish_reason != "stop":
                raise ProviderResponseError()

            content = choice.message.content
            if content and content.strip():
                try:
                    value = response_model.model_validate(json.loads(content))
                except (json.JSONDecodeError, TypeError, ValidationError):
                    pass
                else:
                    return self._build_result(response, value)

            if semantic_attempt == MAX_JSON_GENERATION_ATTEMPTS - 1:
                raise ProviderResponseError()

        raise ProviderResponseError()

    def _create_completion(
        self,
        *,
        messages: list,
        thinking: bool,
        reasoning_effort: Optional[str],
        before_attempt: Optional[Callable[[], None]],
    ):
        request_options = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "max_tokens": self._max_tokens,
            "response_format": {"type": "json_object"},
            "extra_body": {
                "thinking": {
                    "type": "enabled" if thinking else "disabled",
                }
            },
        }
        if reasoning_effort is not None:
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
