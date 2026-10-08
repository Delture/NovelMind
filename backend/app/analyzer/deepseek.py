"""基于 OpenAI 兼容协议的同步/异步结构化生成客户端。

本模块负责网络边界：建立 SDK 客户端、限制超时和重试、去除 Markdown JSON
围栏、解析 JSON、用 Pydantic 校验字段，并把供应商异常映射为项目统一错误。
语法正确不等于业务正确，因此最多进行有限次“语义纠错”尝试；网络层重试与
JSON 纠错分别计数，避免无上限付费。日志绝不记录提示全文、密钥或模型推理。
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


def _strip_json_fence(content: str) -> str:
    """兼容模型偶尔返回的 ```json 围栏，只剥离完整外层，不猜测内容。"""
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
    """把校验错误压缩成安全反馈，不把原始敏感输入再次拼入提示。"""
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
) -> tuple[Optional[T], str]:
    """Validate one response and return safe feedback for a retry."""
    if not isinstance(content, str) or not content.strip():
        return None, "上一次输出为空。"
    try:
        decoded = json.loads(_strip_json_fence(content))
    except (json.JSONDecodeError, TypeError):
        return None, "上一次输出不是有效的 JSON 对象。"
    try:
        return response_model.model_validate(decoded), ""
    except ValidationError as error:
        return (
            None,
            "上一次输出未通过字段校验（"
            f"{_validation_feedback(error)}）。",
        )


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
    """同步结构化生成实现，供人物、报告和 RAG 回答流水线使用。"""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: Optional[httpx.Client] = None,
        sleep: Callable[[float], None] = time.sleep,
        random: Callable[[], float] = random_module.random,
    ) -> None:
        """根据 Settings 创建同步生成客户端，并保存重试/令牌配置快照。

        可选 ``http_client``、``sleep``、``random`` 用于测试；密钥缺失立即失败。
        SDK 自带重试被关闭，由本类统一分类、计数和退避。
        """
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

            value, feedback = _validate_json_content(
                choice.message.content,
                response_model,
            )
            if value is not None:
                return self._build_result(response, value)

            if semantic_attempt == MAX_JSON_GENERATION_ATTEMPTS - 1:
                raise ProviderResponseError()
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
        """执行一次语义尝试内的有界网络重试并返回 SDK completion。

        ``messages`` 是当前对话（可能含模式纠错反馈）；thinking/reasoning 控制供应商
        选项；``before_attempt`` 在每次真正发网前检查任务是否仍有效。
        """
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
        """验证 reasoning_effort 只为 high/max，且必须同时开启 thinking。"""
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
        """返回 SDK 响应第一候选；无 choices 视为无法安全使用的响应。"""
        if not response.choices:
            raise ProviderResponseError()
        return response.choices[0]

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        """返回 HTTP 状态是否适合有限网络重试。"""
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
        """把 401/403、402 和其他不可重试状态分别映射为项目错误。"""
        if status_code in {401, 403}:
            raise ProviderAuthenticationError() from None
        if status_code == 402:
            raise ProviderBalanceError() from None
        raise ProviderRequestError() from None

    def _wait_before_retry(self, failed_attempt: int) -> None:
        """执行封顶指数退避加随机抖动；failed_attempt 从 1 开始。"""
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
        """组合已验证 Pydantic 值与模型、token、请求 ID 等非敏感元数据。"""
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
        """兼容 SDK 标准属性与 model_extra，缺失 token 计数统一返回 0。"""
        if usage is None:
            return 0
        value = getattr(usage, name, None)
        if value is None:
            extras = getattr(usage, "model_extra", None) or {}
            value = extras.get(name, 0)
        return int(value or 0)


class AsyncDeepSeekProvider(DeepSeekProvider):
    """异步实现，允许互不依赖的章节摘要在同一事件循环中并发等待网络。"""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random: Callable[[], float] = random_module.random,
    ) -> None:
        """创建异步 SDK 客户端；注入项与同步版相同，但 sleep/http client 可 await。"""
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
        """Reject accidental use through the synchronous provider path."""
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
        """Generate validated JSON without occupying one thread per call."""
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
            if finish_reason in _TERMINAL_FINISH_REASONS:
                raise ProviderResponseError()
            if finish_reason != "stop":
                raise ProviderResponseError()

            value, feedback = _validate_json_content(
                choice.message.content,
                response_model,
            )
            if value is not None:
                return self._build_result(response, value)

            if semantic_attempt == MAX_JSON_GENERATION_ATTEMPTS - 1:
                raise ProviderResponseError()
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
        """异步执行一次语义尝试内的有界网络重试。

        参数与同步版本相同；每次 await 发网前执行取消回调。返回 SDK completion，
        认证/余额/限流/服务故障转换为项目统一异常。
        """
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
        """异步等待封顶指数退避加随机抖动，不阻塞事件循环线程。"""
        exponential = min(
            2.0 ** (failed_attempt - 1),
            _MAX_BACKOFF_SECONDS,
        )
        jitter = max(0.0, min(float(self._random()), 1.0))
        await self._sleep_async(exponential + jitter)

    async def aclose(self) -> None:
        """Close the async HTTP pool exactly once."""
        if self._closed:
            return
        self._closed = True
        await self._async_client.close()

    def close(self) -> None:
        """Allow unused instances to be released from synchronous routes."""
        if self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclose())
        else:
            loop.create_task(self.aclose())
