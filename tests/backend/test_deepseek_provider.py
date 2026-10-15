
import asyncio
import json
import logging
from dataclasses import replace
from typing import Callable, Dict, List, Optional

import httpx
import pytest
from pydantic import BaseModel

from backend.app.analyzer.deepseek import (
    MAX_JSON_GENERATION_ATTEMPTS,
    AsyncDeepSeekProvider,
    DeepSeekProvider,
)
from backend.app.analyzer.llm import (
    LLMProvider,
    ProviderResponseError,
    ProviderAuthenticationError,
    ProviderBalanceError,
    ProviderNotConfiguredError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderUnavailableError,
)
from backend.app.core.config import Settings, load_settings


class AnalysisPayload(BaseModel):
    summary: str
    characters: List[str]


def make_settings(
    *,
    api_key: Optional[str] = "deepseek-test-key",
    timeout_seconds: int = 123,
    max_attempts: int = 3,
    max_tokens: int = 8_192,
) -> Settings:
    return replace(
        load_settings({}),
        deepseek_api_key=api_key,
        deepseek_timeout_seconds=timeout_seconds,
        deepseek_max_attempts=max_attempts,
        deepseek_max_tokens=max_tokens,
    )


def completion_response(
    content: Optional[str] = '{"summary":"ok","characters":["Lin"]}',
    *,
    finish_reason: str = "stop",
    status_code: int = 200,
    error_message: str = "provider failure",
) -> httpx.Response:
    if status_code != 200:
        return httpx.Response(
            status_code,
            json={
                "error": {
                    "message": error_message,
                    "type": "provider_error",
                }
            },
        )

    return httpx.Response(
        200,
        headers={"x-request-id": "request-test-123"},
        json={
            "id": "completion-test-123",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "reasoning_content": (
                            "private chain of thought must never escape"
                        ),
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 17,
                "completion_tokens": 9,
                "total_tokens": 26,
                "prompt_cache_hit_tokens": 11,
                "prompt_cache_miss_tokens": 6,
                "completion_tokens_details": {"reasoning_tokens": 4},
            },
        },
    )


def make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    settings: Optional[Settings] = None,
    sleeps: Optional[List[float]] = None,
    jitter: float = 0.25,
) -> DeepSeekProvider:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return DeepSeekProvider(
        settings or make_settings(),
        http_client=http_client,
        sleep=(sleeps if sleeps is not None else []).append,
        random=lambda: jitter,
    )


def test_provider_requires_a_configured_api_key() -> None:
    with pytest.raises(
        ProviderNotConfiguredError,
        match="DEEPSEEK_API_KEY",
    ):
        DeepSeekProvider(make_settings(api_key=None))


def test_non_thinking_json_request_wire_contract_and_usage() -> None:
    captured: Dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        captured["timeout"] = request.extensions["timeout"]
        return completion_response()

    provider = make_provider(handler)

    result = provider.generate_json(
        system_prompt="You analyze novels.",
        user_prompt="Summarize chapter one.",
        response_model=AnalysisPayload,
    )

    assert isinstance(provider, LLMProvider)
    assert provider.model == "deepseek-v4-pro"
    assert result.value == AnalysisPayload(summary="ok", characters=["Lin"])
    assert result.model == "deepseek-v4-pro"
    assert result.prompt_tokens == 17
    assert result.completion_tokens == 9
    assert result.cache_hit_tokens == 11
    assert result.request_id == "request-test-123"

    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["authorization"] == "Bearer deepseek-test-key"
    assert captured["timeout"] == {
        "connect": 10.0,
        "read": 123.0,
        "write": 60.0,
        "pool": 10.0,
    }
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "deepseek-v4-pro"
    assert payload["stream"] is False
    assert payload["max_tokens"] == 8_192
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][0]["content"].startswith(
        "You analyze novels."
    )
    assert "JSON" in payload["messages"][0]["content"]
    assert '{"field":"value"}' in payload["messages"][0]["content"]
    assert "不得新增或遗漏字段" in payload["messages"][0]["content"]
    assert "推理过程" in payload["messages"][0]["content"]
    assert payload["messages"][1] == {
        "role": "user",
        "content": "Summarize chapter one.",
    }
    assert "reasoning_content" not in result.__dict__


def test_empty_and_invalid_json_are_retried_twice_then_succeed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    responses = [
        completion_response("  "),
        completion_response("{not-json"),
        completion_response('{"summary":"valid","characters":[]}'),
    ]
    calls = 0
    sleeps: List[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return response

    provider = make_provider(handler, sleeps=sleeps)
    with caplog.at_level(logging.WARNING, logger="backend.app.analyzer.deepseek"):
        result = provider.generate_json(
            system_prompt="Return JSON.",
            user_prompt="Analyze.",
            response_model=AnalysisPayload,
        )

    assert MAX_JSON_GENERATION_ATTEMPTS == 5
    assert calls == 3
    assert sleeps == []
    assert result.value.summary == "valid"
    categories = [
        record.getMessage()
        for record in caplog.records
        if "json_generation_failed" in record.getMessage()
    ]
    assert len(categories) == 2
    assert "category=empty" in categories[0]
    assert "semantic_attempt=1/5" in categories[0]
    assert "category=json" in categories[1]
    assert "semantic_attempt=2/5" in categories[1]


def test_before_attempt_can_stop_semantic_retries() -> None:
    calls = 0
    checks = 0

    class Cancelled(RuntimeError):
        pass

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return completion_response("{not-json")

    def before_attempt() -> None:
        nonlocal checks
        checks += 1
        if checks > 1:
            raise Cancelled()

    provider = make_provider(handler)
    with pytest.raises(Cancelled):
        provider.generate_json(
            system_prompt="Return JSON.",
            user_prompt="Analyze.",
            response_model=AnalysisPayload,
            before_attempt=before_attempt,
        )

    assert calls == 1
    assert checks == 2


@pytest.mark.parametrize("status_code", [408, 409, 429, 500, 503])
def test_retryable_http_statuses_use_exponential_backoff_with_jitter(
    status_code: int,
) -> None:
    calls = 0
    sleeps: List[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return completion_response(
                status_code=status_code,
                error_message="secret request details",
            )
        return completion_response()

    provider = make_provider(handler, sleeps=sleeps)
    result = provider.generate_json(
        system_prompt="Return JSON.",
        user_prompt="Analyze.",
        response_model=AnalysisPayload,
    )

    assert result.value.summary == "ok"
    assert calls == 3
    assert sleeps == [1.25, 2.25]


def test_connection_errors_are_retried_without_leaking_details() -> None:
    calls = 0
    sleeps: List[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError(
                "sensitive chapter and deepseek-test-key",
                request=request,
            )
        return completion_response()

    provider = make_provider(handler, sleeps=sleeps)
    result = provider.generate_json(
        system_prompt="Return JSON.",
        user_prompt="Analyze.",
        response_model=AnalysisPayload,
    )

    assert result.value.summary == "ok"
    assert calls == 2
    assert sleeps == [1.25]


def test_rate_limit_exhaustion_raises_safe_specific_error() -> None:
    calls = 0
    sleeps: List[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return completion_response(
            status_code=429,
            error_message="sensitive rate-limit diagnostic",
        )

    provider = make_provider(handler, sleeps=sleeps)

    with pytest.raises(ProviderRateLimitError) as captured_error:
        provider.generate_json(
            system_prompt="sensitive novel prompt",
            user_prompt="sensitive chapter text",
            response_model=AnalysisPayload,
        )

    assert calls == 3
    assert sleeps == [1.25, 2.25]
    assert captured_error.value.code == "provider_rate_limited"
    assert captured_error.value.retryable is True
    assert "sensitive" not in str(captured_error.value)


def test_debug_logging_does_not_expose_request_prompts(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_LOG", "debug")
    for logger_name in (
        "openai",
        "openai._base_client",
        "httpx",
        "httpcore",
    ):
        caplog.set_level(logging.DEBUG, logger=logger_name)
    provider = make_provider(lambda _: completion_response())

    provider.generate_json(
        system_prompt="SYSTEM_PROMPT_SENTINEL",
        user_prompt="NOVEL_BODY_SENTINEL",
        response_model=AnalysisPayload,
    )

    assert "SYSTEM_PROMPT_SENTINEL" not in caplog.text
    assert "NOVEL_BODY_SENTINEL" not in caplog.text


def test_provider_close_is_idempotent_and_closes_http_client() -> None:
    http_client = httpx.Client(
        transport=httpx.MockTransport(lambda _: completion_response())
    )
    provider = DeepSeekProvider(
        make_settings(),
        http_client=http_client,
    )

    assert http_client.is_closed is False
    provider.close()
    provider.close()

    assert http_client.is_closed is True


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (401, ProviderAuthenticationError),
        (402, ProviderBalanceError),
        (403, ProviderAuthenticationError),
        (422, ProviderRequestError),
    ],
)
def test_terminal_http_statuses_are_not_retried(
    status_code: int,
    error_type: type,
) -> None:
    calls = 0
    sleeps: List[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return completion_response(
            status_code=status_code,
            error_message="sensitive provider diagnostic",
        )

    provider = make_provider(handler, sleeps=sleeps)

    with pytest.raises(error_type) as captured_error:
        provider.generate_json(
            system_prompt="sensitive novel prompt",
            user_prompt="sensitive chapter text",
            response_model=AnalysisPayload,
        )

    assert calls == 1
    assert sleeps == []
    assert "sensitive" not in str(captured_error.value)


def test_sdk_retries_are_disabled() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return completion_response(status_code=500)

    provider = make_provider(
        handler,
        settings=make_settings(max_attempts=1),
    )

    with pytest.raises(ProviderUnavailableError):
        provider.generate_json(
            system_prompt="Return JSON.",
            user_prompt="Analyze.",
            response_model=AnalysisPayload,
        )

    assert calls == 1

@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "tool_calls"])
def test_terminal_finish_reasons_are_not_semantically_retried(
    finish_reason: str,
) -> None:
    calls = 0
    sleeps: List[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return completion_response(None, finish_reason=finish_reason)

    provider = make_provider(handler, sleeps=sleeps)
    with pytest.raises(ProviderResponseError) as captured:
        provider.generate_json(
            system_prompt="Return JSON.",
            user_prompt="Analyze.",
            response_model=AnalysisPayload,
        )
    assert calls == 1
    assert sleeps == []
    if finish_reason == "length":
        assert captured.value.category == "length"
        assert "输出上限" in str(captured.value)
    else:
        assert captured.value.category == "finish"
        assert "无法解析" in str(captured.value)


@pytest.mark.parametrize(
    ("invalid_content", "expected_category"),
    [
        (None, "empty"),
        ("", "empty"),
        ("   ", "empty"),
        ("{not-json", "json"),
        ("[]", "pydantic"),
        ('{"summary":1,"characters":[]}', "pydantic"),
    ],
)
def test_semantic_failures_stop_at_retry_limit(
    invalid_content: Optional[str],
    expected_category: str,
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return completion_response(invalid_content)

    provider = make_provider(handler)
    with pytest.raises(ProviderResponseError, match="无法解析") as captured:
        provider.generate_json(
            system_prompt="sensitive novel prompt",
            user_prompt="sensitive chapter text",
            response_model=AnalysisPayload,
        )
    assert calls == MAX_JSON_GENERATION_ATTEMPTS
    assert captured.value.category == expected_category
    message = str(captured.value)
    assert "sensitive novel prompt" not in message
    assert "sensitive chapter text" not in message
    assert "deepseek-test-key" not in message


def test_before_attempt_can_stop_network_retries() -> None:
    calls = 0
    checks = 0

    class Cancelled(RuntimeError):
        pass

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return completion_response(status_code=503)

    def before_attempt() -> None:
        nonlocal checks
        checks += 1
        if checks > 1:
            raise Cancelled()

    provider = make_provider(handler)
    with pytest.raises(Cancelled):
        provider.generate_json(
            system_prompt="Return JSON.",
            user_prompt="Analyze.",
            response_model=AnalysisPayload,
            before_attempt=before_attempt,
        )
    assert calls == 1
    assert checks == 2



def test_provider_response_error_preserves_default_contract() -> None:
    default = ProviderResponseError()
    assert default.category == "invalid"
    assert default.code == "provider_response_invalid"
    assert default.retryable is False
    assert "无法解析" in str(default)

    length = ProviderResponseError(category="length")
    assert length.category == "length"
    assert length.code == "provider_response_invalid"
    assert length.retryable is False
    assert "输出上限" in str(length)


def test_pydantic_failure_is_retried_then_succeeds() -> None:
    responses = [
        completion_response('{"summary":1,"characters":[]}'),
        completion_response('{"summary":"ok","characters":["Lin"]}'),
    ]
    calls = 0
    payloads: List[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        payloads.append(json.loads(request.content.decode("utf-8")))
        response = responses[calls]
        calls += 1
        return response

    provider = make_provider(handler)
    result = provider.generate_json(
        system_prompt="Return JSON.",
        user_prompt="Analyze.",
        response_model=AnalysisPayload,
    )
    assert calls == 2
    assert result.value.summary == "ok"
    assert len(payloads[1]["messages"]) == 3
    assert "未通过字段校验" in payloads[1]["messages"][2]["content"]


def test_length_response_is_rejected_even_with_valid_json() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return completion_response(
            '{"summary":"ok","characters":[]}',
            finish_reason="length",
        )

    provider = make_provider(handler)
    with pytest.raises(ProviderResponseError) as captured:
        provider.generate_json(
            system_prompt="Return JSON.",
            user_prompt="Analyze.",
            response_model=AnalysisPayload,
        )
    assert calls == 1
    assert captured.value.category == "length"
    assert "输出上限" in str(captured.value)


@pytest.mark.parametrize(
    ("content", "finish_reason", "expected_category"),
    [
        (None, "length", "length"),
        ("  ", "stop", "empty"),
        ("{not-json", "stop", "json"),
        ("[]", "stop", "pydantic"),
        (None, "content_filter", "finish"),
    ],
)
def test_json_failure_logs_and_errors_do_not_expose_content(
    content: Optional[str],
    finish_reason: str,
    expected_category: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return completion_response(content, finish_reason=finish_reason)

    provider = make_provider(handler)
    with caplog.at_level(logging.WARNING, logger="backend.app.analyzer.deepseek"):
        with pytest.raises(ProviderResponseError) as captured:
            provider.generate_json(
                system_prompt="SECRET_SYSTEM_PROMPT",
                user_prompt="SECRET_USER_PROMPT deepseek-test-key",
                response_model=AnalysisPayload,
            )

    assert captured.value.category == expected_category
    combined = caplog.text + str(captured.value)
    assert "SECRET_SYSTEM_PROMPT" not in combined
    assert "SECRET_USER_PROMPT" not in combined
    assert "deepseek-test-key" not in combined
    assert "private chain of thought" not in combined
    assert f"category={expected_category}" in caplog.text
    assert "prompt_tokens=" in caplog.text
    assert "completion_tokens=" in caplog.text
    assert "max_tokens=" in caplog.text


def _make_async_provider(
    handler: Callable[[httpx.Request], httpx.Response],
) -> AsyncDeepSeekProvider:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AsyncDeepSeekProvider(
        make_settings(),
        http_client=http_client,
        sleep=lambda _: asyncio.sleep(0),
        random=lambda: 0.25,
    )


@pytest.mark.parametrize(
    ("content", "finish_reason", "expected_category", "expected_calls"),
    [
        (None, "length", "length", 1),
        (None, "content_filter", "finish", 1),
        ("  ", "stop", "empty", MAX_JSON_GENERATION_ATTEMPTS),
        ("{not-json", "stop", "json", MAX_JSON_GENERATION_ATTEMPTS),
        ("[]", "stop", "pydantic", MAX_JSON_GENERATION_ATTEMPTS),
    ],
)
def test_async_json_failures_preserve_retry_semantics(
    content: Optional[str],
    finish_reason: str,
    expected_category: str,
    expected_calls: int,
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return completion_response(content, finish_reason=finish_reason)

    async def run() -> None:
        provider = _make_async_provider(handler)
        try:
            with pytest.raises(ProviderResponseError) as captured:
                await provider.generate_json_async(
                    system_prompt="Return JSON.",
                    user_prompt="Analyze.",
                    response_model=AnalysisPayload,
                )
            assert captured.value.category == expected_category
            if expected_category == "length":
                assert "输出上限" in str(captured.value)
            else:
                assert "无法解析" in str(captured.value)
        finally:
            await provider.aclose()

    asyncio.run(run())
    assert calls == expected_calls


def test_async_json_validation_failure_then_success() -> None:
    responses = [
        completion_response("  "),
        completion_response("{not-json"),
        completion_response('{"summary":1,"characters":[]}'),
        completion_response('{"summary":"ok","characters":[]}'),
    ]
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return response

    async def run() -> AnalysisPayload:
        provider = _make_async_provider(handler)
        try:
            result = await provider.generate_json_async(
                system_prompt="Return JSON.",
                user_prompt="Analyze.",
                response_model=AnalysisPayload,
            )
            return result.value
        finally:
            await provider.aclose()

    value = asyncio.run(run())
    assert calls == 4
    assert value.summary == "ok"


def _completion_with_usage(
    content: Optional[str] = None,
    *,
    finish_reason: str = "stop",
    prompt_tokens: object = 17,
    completion_tokens: object = 9,
) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"x-request-id": "request-test-123"},
        json={
            "id": "completion-test-123",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "reasoning_content": (
                            "private chain of thought must never escape"
                        ),
                    },
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": 26,
            },
        },
    )


def test_malformed_usage_does_not_break_failure_classification(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _completion_with_usage(
            "{not-json",
            prompt_tokens="INVALID_USAGE_SENTINEL",
            completion_tokens="ALSO_BAD",
        )

    provider = make_provider(handler)
    with caplog.at_level(logging.WARNING, logger="backend.app.analyzer.deepseek"):
        with pytest.raises(ProviderResponseError) as captured:
            provider.generate_json(
                system_prompt="Return JSON.",
                user_prompt="Analyze.",
                response_model=AnalysisPayload,
            )
    assert calls == MAX_JSON_GENERATION_ATTEMPTS
    assert captured.value.category == "json"
    assert "INVALID_USAGE_SENTINEL" not in caplog.text
    assert "ALSO_BAD" not in str(captured.value)
    assert "prompt_tokens=0" in caplog.text
    assert "completion_tokens=0" in caplog.text


def test_async_malformed_usage_preserves_length_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _completion_with_usage(
            '{"summary":"ok","characters":[]}',
            finish_reason="length",
            prompt_tokens="INVALID_USAGE_SENTINEL",
        )

    async def run() -> None:
        provider = _make_async_provider(handler)
        try:
            with pytest.raises(ProviderResponseError) as captured:
                await provider.generate_json_async(
                    system_prompt="Return JSON.",
                    user_prompt="Analyze.",
                    response_model=AnalysisPayload,
                )
            assert captured.value.category == "length"
            assert "输出上限" in str(captured.value)
            assert "INVALID_USAGE_SENTINEL" not in str(captured.value)
        finally:
            await provider.aclose()

    with caplog.at_level(logging.WARNING, logger="backend.app.analyzer.deepseek"):
        asyncio.run(run())
    assert calls == 1
    assert "INVALID_USAGE_SENTINEL" not in caplog.text
    assert "finish_reason=length" in caplog.text


def test_unknown_finish_reason_logs_as_other(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return completion_response(None, finish_reason="weird_provider_reason")

    provider = make_provider(handler)
    with caplog.at_level(logging.WARNING, logger="backend.app.analyzer.deepseek"):
        with pytest.raises(ProviderResponseError) as captured:
            provider.generate_json(
                system_prompt="Return JSON.",
                user_prompt="Analyze.",
                response_model=AnalysisPayload,
            )
    assert captured.value.category == "finish"
    assert "finish_reason=other" in caplog.text
    assert "weird_provider_reason" not in caplog.text
