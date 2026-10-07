"""Browser-only model catalog transport; all outcomes are generated in memory."""

import asyncio
import httpx


class BrowserProbeFixture:
    """Capture public wire metadata, never credentials or request bodies."""

    MODES = {"success", "401", "403", "429", "404", "500", "redirect", "invalid", "oversize", "timeout"}

    def __init__(self):
        self.mode = "success"
        self.delay_ms = 0
        self.tag = ""
        self.requests = []

    def configure(self, mode="success", delay_ms=0, tag=""):
        if mode not in self.MODES or not isinstance(delay_ms, int) or not 0 <= delay_ms <= 5000:
            raise ValueError("Invalid isolated probe scenario")
        if not isinstance(tag, str) or len(tag) > 40:
            raise ValueError("Invalid isolated probe tag")
        self.mode, self.delay_ms, self.tag = mode, delay_ms, tag

    async def handle(self, request):
        assert request.method == "GET" and request.url.path.endswith("/models")
        assert request.content == b"", "A model catalog probe must not contain novel text"
        mode, delay_ms, tag = self.mode, self.delay_ms, self.tag
        record = dict(method=request.method, host=request.url.host, path=request.url.path,
                      query=dict(request.url.params), auth_present="authorization" in request.headers,
                      mode=mode, tag=tag, finished=False)
        self.requests.append(record)
        try:
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000)
            if mode == "timeout":
                raise httpx.ReadTimeout("isolated fixture timeout", request=request)
            if mode in {"401", "403", "429", "404", "500"}:
                return httpx.Response(int(mode), text="UNTRUSTED_REMOTE_FIXTURE " + request.headers.get("Authorization", ""))
            if mode == "redirect":
                return httpx.Response(302, headers={"Location": "https://never-contact.invalid/models"})
            if mode == "invalid":
                return httpx.Response(200, content=b"UNTRUSTED_REMOTE_FIXTURE not json")
            if mode == "oversize":
                return httpx.Response(200, headers={"Content-Length": "2097152"}, stream=EmptyCatalogStream())
            if tag:
                models = [f"fixture-model-{tag}"]
            elif request.url.host == "api.deepseek.com":
                models = ["deepseek-v4-pro", "deepseek-v4-flash"]
            elif request.url.host == "api.siliconflow.cn" and request.url.params.get("sub_type") == "embedding":
                # Deliberately include another embedding model: listing it must not
                # silently bypass the preset's fixed-model/dimension save contract.
                models = ["Qwen/Qwen3-Embedding-4B", "fixture-other-embedding"]
            else:
                models = ["fixture-chat", "fixture-embedding", "fixture-long-model-" + "x" * 65]
            return httpx.Response(200, json={"data": [{"id": model, "owned_by": "isolated-fixture"} for model in models]})
        finally:
            record["finished"] = True

    def transport(self):
        return httpx.MockTransport(self.handle)


class EmptyCatalogStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b""


def forbid_real_http(*args, **kwargs):
    """Fail closed before DNS/socket activity if a browser fixture loses an override."""
    raise AssertionError("Isolated browser QA cannot use a real HTTP transport")


async def forbid_real_async_http(*args, **kwargs):
    forbid_real_http()
