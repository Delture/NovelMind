"""Tests for the external Bailian text-embedding-v4 client."""

import json
import logging
from dataclasses import replace
from types import SimpleNamespace
from typing import Callable, Optional

import httpx
import pytest

from backend.app.core.config import Settings, load_settings
from backend.app.rag.embedding import (
    MAX_EMBEDDING_BATCH_SIZE,
    BailianEmbeddingProvider,
    EmbeddingAuthenticationError,
    EmbeddingBalanceError,
    EmbeddingNotConfiguredError,
    EmbeddingProvider,
    EmbeddingRateLimitError,
    EmbeddingRequestError,
    EmbeddingResponseError,
    EmbeddingUnavailableError,
)


def make_settings(
    *,
    api_key: Optional[str] = "bailian-test-key",
    max_attempts: int = 3,
) -> Settings:
    """Return a valid Beijing workspace configuration."""
    return replace(
        load_settings({}),
        embedding_api_key=api_key,
        embedding_base_url=(
            "https://ws-test.cn-beijing.maas.aliyuncs.com/"
            "compatible-mode/v1"
            if api_key
            else None
        ),
        embedding_max_attempts=max_attempts,
    )


def embedding_response(
    *,
    count: int = 2,
    dimensions: int = 1_024,
    status_code: int = 200,
    model: str = "text-embedding-v4",
    indices: Optional[list[int]] = None,
) -> httpx.Response:
    """Build one OpenAI-compatible embedding response."""
    if status_code != 200:
        return httpx.Response(
            status_code,
            json={
                "error": {
                    "message": "sensitive provider diagnostic",
                    "type": "provider_error",
                }
            },
        )
    item_indices = indices or list(range(count))
    return httpx.Response(
        200,
        headers={"x-request-id": "embedding-request-123"},
        json={
            "object": "list",
            "model": model,
            "data": [
                {
                    "object": "embedding",
                    "index": index,
                    "embedding": [
                        float(index + 1)
                        for _ in range(dimensions)
                    ],
                }
                for index in item_indices
            ],
            "usage": {
                "prompt_tokens": 23,
                "total_tokens": 23,
            },
        },
    )


def make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    settings: Optional[Settings] = None,
    sleeps: Optional[list[float]] = None,
) -> BailianEmbeddingProvider:
    """Create a provider backed by an in-memory HTTP transport."""
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return BailianEmbeddingProvider(
        settings or make_settings(),
        http_client=http_client,
        sleep=(sleeps if sleeps is not None else []).append,
        random=lambda: 0.25,
    )


def test_provider_requires_complete_embedding_configuration() -> None:
    with pytest.raises(
        EmbeddingNotConfiguredError,
        match="EMBEDDING_API_KEY",
    ):
        BailianEmbeddingProvider(make_settings(api_key=None))


def test_embedding_wire_contract_and_response_order() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        captured["timeout"] = request.extensions["timeout"]
        return embedding_response(indices=[1, 0])

    provider = make_provider(handler)
    result = provider.embed(["第一段", "第二段"])

    assert isinstance(provider, EmbeddingProvider)
    assert provider.model == "text-embedding-v4"
    assert provider.dimensions == 1_024
    assert len(result.vectors) == 2
    assert len(result.vectors[0]) == 1_024
    assert result.vectors[0][0] == 1.0
    assert result.vectors[1][0] == 2.0
    assert result.prompt_tokens == 23
    assert result.request_id == "embedding-request-123"
    assert captured["url"] == (
        "https://ws-test.cn-beijing.maas.aliyuncs.com/"
        "compatible-mode/v1/embeddings"
    )
    assert captured["authorization"] == "Bearer bailian-test-key"
    assert captured["payload"] == {
        "input": ["第一段", "第二段"],
        "model": "text-embedding-v4",
        "dimensions": 1_024,
        "encoding_format": "float",
    }
    assert captured["timeout"] == {
        "connect": 10.0,
        "read": 120.0,
        "write": 60.0,
        "pool": 10.0,
    }


@pytest.mark.parametrize(
    "texts",
    [
        [],
        [""],
        ["   "],
        ["ok"] * (MAX_EMBEDDING_BATCH_SIZE + 1),
    ],
)
def test_embedding_rejects_invalid_batches_without_network(
    texts: list[str],
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return embedding_response(count=1)

    provider = make_provider(handler)
    with pytest.raises(ValueError, match="1 to 10"):
        provider.embed(texts)
    assert calls == 0


@pytest.mark.parametrize(
    "response",
    [
        embedding_response(count=1),
        embedding_response(count=2, dimensions=768),
        embedding_response(count=2, model="text-embedding-v3"),
        embedding_response(count=2, indices=[0, 0]),
        embedding_response(count=2, indices=[0, 2]),
    ],
)
def test_embedding_rejects_invalid_response_shape(
    response: httpx.Response,
) -> None:
    provider = make_provider(lambda _: response)
    with pytest.raises(EmbeddingResponseError):
        provider.embed(["a", "b"])


def test_embedding_rejects_nonfinite_and_zero_vectors() -> None:
    response = embedding_response(count=1)
    payload = json.loads(response.content)
    payload["data"][0]["embedding"] = [0.0] * 1_024
    provider = make_provider(
        lambda _: httpx.Response(200, json=payload)
    )
    with pytest.raises(EmbeddingResponseError):
        provider.embed(["a"])

    for invalid_value in [float("nan"), float("inf")]:
        invalid_response = SimpleNamespace(
            model="text-embedding-v4",
            data=[
                SimpleNamespace(
                    index=0,
                    embedding=[invalid_value] * 1_024,
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1),
        )
        with pytest.raises(EmbeddingResponseError):
            provider._validate_response(
                invalid_response,
                expected_count=1,
            )


@pytest.mark.parametrize("status_code", [408, 409, 429, 500, 503])
def test_retryable_failures_use_bounded_backoff(
    status_code: int,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return embedding_response(status_code=status_code)
        return embedding_response(count=1)

    provider = make_provider(handler, sleeps=sleeps)
    result = provider.embed(["a"])
    assert len(result.vectors) == 1
    assert calls == 3
    assert sleeps == [1.25, 2.25]


def test_cancellation_runs_before_every_real_attempt() -> None:
    calls = 0
    checks = 0

    class Cancelled(RuntimeError):
        pass

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return embedding_response(status_code=503)

    def before_attempt() -> None:
        nonlocal checks
        checks += 1
        if checks > 1:
            raise Cancelled()

    provider = make_provider(handler)
    with pytest.raises(Cancelled):
        provider.embed(["a"], before_attempt=before_attempt)
    assert calls == 1
    assert checks == 2


def test_rate_limit_exhaustion_is_specific_and_safe() -> None:
    provider = make_provider(
        lambda _: embedding_response(status_code=429),
        settings=make_settings(max_attempts=1),
    )
    with pytest.raises(EmbeddingRateLimitError) as captured:
        provider.embed(["sensitive novel body"])
    assert captured.value.retryable is True
    assert "sensitive" not in str(captured.value)
    assert "bailian-test-key" not in str(captured.value)


def test_connection_exhaustion_is_safe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "sensitive novel body and bailian-test-key",
            request=request,
        )

    provider = make_provider(
        handler,
        settings=make_settings(max_attempts=1),
    )
    with pytest.raises(EmbeddingUnavailableError) as captured:
        provider.embed(["sensitive novel body"])
    assert "sensitive" not in str(captured.value)
    assert "bailian-test-key" not in str(captured.value)


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (401, EmbeddingAuthenticationError),
        (402, EmbeddingBalanceError),
        (403, EmbeddingAuthenticationError),
        (422, EmbeddingRequestError),
    ],
)
def test_terminal_statuses_are_not_retried(
    status_code: int,
    error_type: type,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return embedding_response(status_code=status_code)

    provider = make_provider(handler, sleeps=sleeps)
    with pytest.raises(error_type):
        provider.embed(["a"])
    assert calls == 1
    assert sleeps == []


def test_debug_logging_does_not_expose_embedding_input(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_LOG", "debug")
    for logger_name in ("openai", "httpx", "httpcore"):
        caplog.set_level(logging.DEBUG, logger=logger_name)
    provider = make_provider(lambda _: embedding_response(count=1))
    provider.embed(["NOVEL_BODY_SENTINEL"])
    assert "NOVEL_BODY_SENTINEL" not in caplog.text


def test_close_is_idempotent_and_closes_http_client() -> None:
    http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: embedding_response(count=1)
        )
    )
    provider = BailianEmbeddingProvider(
        make_settings(),
        http_client=http_client,
    )
    provider.close()
    provider.close()
    assert http_client.is_closed is True
