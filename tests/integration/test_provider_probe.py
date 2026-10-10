
import asyncio
from dataclasses import replace

import httpx
import pytest


@pytest.fixture
def probe_case(isolated_config):
    from fastapi.exceptions import RequestValidationError
    from backend.app.api import system
    from backend.app.main import validation_error

    case = isolated_config
    # Match the production validation boundary instead of FastAPI's raw-input echo.
    case.app.add_exception_handler(RequestValidationError, validation_error)
    case.calls = []

    def install(handler):
        async def capture(request):
            case.calls.append(request)
            response = handler(request)
            return await response if hasattr(response, "__await__") else response
        case.app.dependency_overrides[system.get_model_probe_transport] = lambda: httpx.MockTransport(capture)

    case.install = install
    install(lambda request: httpx.Response(200, json={"data": [{"id": "fake-model", "owned_by": "fake-owner"}]}))
    return case


def payload(case, role="generation", **overrides):
    body = dict(role=role, provider="openai_compatible", base_url=case.payload[role]["base_url"],
                api_key=None, use_saved_key=True, confirm_connection=True)
    body.update(overrides)
    return body


def post(case, body):
    return case.client.post("/api/system/model-probe", json=body)


def assert_public_safe(response, case, *extra_secrets):
    for secret in ("review-generation-A-not-real", "review-embedding-A-not-real", *extra_secrets):
        assert secret not in response.text


@pytest.mark.parametrize("role", ["generation", "embedding"])
@pytest.mark.parametrize("mode", ["saved", "equivalent-url", "draft-new-address"])
def test_probe_uses_only_role_bound_key_and_get_models_without_saving(probe_case, role, mode):
    case = probe_case
    body = payload(case, role)
    key = f"review-{role}-A-not-real"
    if mode == "equivalent-url":
        body.update(base_url=f"https://{role.upper()}-A.EXAMPLE:443/v1/", api_key="  ")
    elif mode == "draft-new-address":
        key = "new-draft-key-not-real"
        body.update(base_url="https://new-draft.example/api/v2", api_key="  " + key + "  ", use_saved_key=False)
    original = case.path.read_bytes()
    settings = case.config.get_settings()
    response = post(case, body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "success" and data["http_status"] == 200
    assert data["models"] == [{"id": "fake-model", "owned_by": "fake-owner"}]
    assert 0 <= data["latency_ms"] < 2000
    assert len(case.calls) == 1
    request = case.calls[0]
    assert request.method == "GET" and request.url.path.endswith("/models")
    assert request.content == b""
    assert request.headers["Authorization"] == "Bearer " + key
    assert request.headers["Accept-Encoding"] == "identity"
    assert all(0 < value <= 5 for value in request.extensions["timeout"].values())
    assert case.path.read_bytes() == original and case.config.get_settings() == settings
    assert_public_safe(response, case, key)


@pytest.mark.parametrize("role", ["generation", "embedding"])
@pytest.mark.parametrize("change", ["address", "no_saved_key", "no_consent", "invalid_key"])
def test_invalid_or_unapproved_probe_has_no_remote_request(probe_case, role, change):
    case = probe_case
    body = payload(case, role)
    if change == "address":
        body["base_url"] = "https://other-destination.example/v1"
    elif change == "no_saved_key":
        body.update(use_saved_key=False, api_key=" ")
    elif change == "no_consent":
        body["confirm_connection"] = False
    else:
        body["api_key"] = "secret-with-newline\n"
        body["api_key"] += "inside"
    original = case.path.read_bytes()
    response = post(case, body)
    assert response.status_code == 400, response.text
    assert case.calls == [] and case.path.read_bytes() == original
    assert_public_safe(response, case, "secret-with-newline")


@pytest.mark.parametrize("status,expected", [(401, "authentication_failed"), (403, "authentication_failed"),
    (429, "rate_limited"), (404, "unsupported"), (405, "unsupported"), (501, "unsupported"),
    (500, "remote_error"), (301, "redirect_blocked"), (302, "redirect_blocked"),
    (303, "redirect_blocked"), (307, "redirect_blocked"), (308, "redirect_blocked")])
def test_http_failures_are_sanitized_and_authenticated_redirects_not_followed(probe_case, caplog, status, expected):
    case = probe_case
    remote = "REMOTE_PRIVATE_ERROR_review-generation-A-not-real"
    case.install(lambda request: httpx.Response(status, text=remote,
        headers={"Location": "https://redirect.example/" + remote}))
    original = case.path.read_bytes()
    response = post(case, payload(case))
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == expected and data["http_status"] == status
    assert data["models"] == []
    assert len(case.calls) == 1, "Redirect must not forward authentication to a second request"
    assert_public_safe(response, case, remote)
    assert remote not in caplog.text
    assert case.path.read_bytes() == original


def test_same_destination_different_provider_cannot_reuse_saved_key(probe_case):
    case = probe_case
    case.config.save_runtime_model_settings(
        replace(case.config.get_settings(), deepseek_base_url="https://api.deepseek.com")
    )
    response = post(case, payload(case, provider="deepseek", base_url="https://api.deepseek.com"))
    assert response.status_code == 400 and case.calls == []


def test_total_deadline_cancels_slow_transport(probe_case, monkeypatch):
    from backend.app.services import model_probe
    case = probe_case
    cancelled = []
    async def delayed(request):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
    case.install(delayed)
    monkeypatch.setattr(model_probe, "PROBE_TIMEOUT_SECONDS", 0.025)
    response = post(case, payload(case))
    assert response.status_code == 200 and response.json()["status"] == "timeout"
    assert cancelled == [True]


class CountingStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.reads, self.closed = chunks, 0, False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("case_name", ["advertised_oversize", "chunked_oversize", "compressed"])
def test_large_or_compressed_response_stops_reading_and_closes_stream(probe_case, case_name):
    from backend.app.services.model_probe import MAX_RESPONSE_BYTES
    case = probe_case
    chunk = b"x" * (MAX_RESPONSE_BYTES // 2)
    stream = CountingStream([chunk] * 10)
    headers = {}
    if case_name == "advertised_oversize":
        headers["Content-Length"] = str(MAX_RESPONSE_BYTES + 1)
    elif case_name == "compressed":
        headers["Content-Encoding"] = "gzip"
    case.install(lambda request: httpx.Response(200, headers=headers, stream=stream))
    response = post(case, payload(case))
    assert response.status_code == 200
    assert response.json()["status"] == ("invalid_response" if case_name == "compressed" else "response_too_large")
    assert stream.closed and stream.reads == (3 if case_name == "chunked_oversize" else 0)


def test_presets_are_role_specific_public_and_do_not_connect(probe_case):
    response = probe_case.client.get("/api/system/provider-presets")
    assert response.status_code == 200
    options = response.json()
    assert {item["id"] for item in options["generation"]} == {"deepseek", "siliconflow", "openai", "openrouter", "custom"}
    assert {item["id"] for item in options["embedding"]} == {"siliconflow", "openai", "custom"}
    assert probe_case.calls == []
    assert_public_safe(response, probe_case)


def test_concurrent_probe_responses_keep_their_own_draft_credentials(probe_case):
    """Server isolation; frontend latest-response ownership is tested after UI freeze."""
    from backend.app.api.system import ModelProbeRequest
    from backend.app.services.model_probe import probe_models
    case = probe_case
    original = case.path.read_bytes()
    captured = {}

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def handler(request):
            name = "A" if request.url.host == "probe-a.example" else "B"
            captured[name] = request.headers["Authorization"]
            if name == "A":
                entered.set()
                await release.wait()
            return httpx.Response(200, json={"data": [{"id": "model-" + name}]})
        settings = case.config.get_settings()
        make = lambda name: ModelProbeRequest(**payload(case, base_url=f"https://probe-{name.lower()}.example/v1",
                                                        api_key=f"draft-{name}-not-real", use_saved_key=False))
        first = asyncio.create_task(probe_models(make("A"), settings, httpx.MockTransport(handler)))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            second = await probe_models(make("B"), settings, httpx.MockTransport(handler))
            assert second["models"] == [{"id": "model-B", "owned_by": ""}]
        finally:
            release.set()
        first_result = await asyncio.wait_for(first, 1)
        assert first_result["models"] == [{"id": "model-A", "owned_by": ""}]
    asyncio.run(run())
    assert captured == {"A": "Bearer draft-A-not-real", "B": "Bearer draft-B-not-real"}
    assert case.path.read_bytes() == original


