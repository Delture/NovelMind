"""用户主动请求的模型目录探测；仅 GET /models，不保存配置或发送小说。"""

from backend.app.services.errors import ServiceError
import asyncio
import json
import time
from urllib.parse import urlsplit

import httpx


from backend.app.core.config import (
    ConfigurationError, normalize_provider_url, service_destination,
)

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_MODELS = 200
PROBE_TIMEOUT_SECONDS = 6

PRESETS = {
    "generation": [
        {"id": "deepseek", "label": "DeepSeek", "provider": "deepseek", "base_url": "https://api.deepseek.com"},
        {"id": "siliconflow", "label": "硅基流动", "provider": "openai_compatible", "base_url": "https://api.siliconflow.cn/v1"},
        {"id": "openai", "label": "OpenAI", "provider": "openai_compatible", "base_url": "https://api.openai.com/v1"},
        {"id": "openrouter", "label": "OpenRouter", "provider": "openai_compatible", "base_url": "https://openrouter.ai/api/v1"},
        {"id": "custom", "label": "自定义兼容服务", "provider": "openai_compatible", "base_url": ""},
    ],
    "embedding": [
        {"id": "siliconflow", "label": "硅基流动", "provider": "siliconflow", "base_url": "https://api.siliconflow.cn/v1"},
        {"id": "openai", "label": "OpenAI", "provider": "openai_compatible", "base_url": "https://api.openai.com/v1"},
        {"id": "custom", "label": "自定义兼容服务", "provider": "openai_compatible", "base_url": ""},
    ],
}
MESSAGES = {
    "success": "模型目录可访问。此结果仅验证 GET /models，不代表生成、向量接口或所选模型兼容性。",
    "authentication_failed": "服务拒绝了认证，请核对该地址使用的密钥和权限。",
    "unsupported": "该服务未提供可用的模型目录，请手动填写模型名称。",
    "rate_limited": "模型目录暂时限流，请稍后重试或手动填写。",
    "redirect_blocked": "服务返回重定向；为保护密钥未跟随，请核对直接服务地址。",
    "timeout": "模型目录连接超时，请稍后重试。",
    "unreachable": "无法连接该服务，请核对地址和网络。",
    "invalid_response": "服务未返回可识别的模型目录，请手动填写模型。",
    "response_too_large": "模型目录超过安全大小上限，请手动填写模型。",
    "remote_error": "模型目录暂时不可用，请稍后重试或手动填写。",
}


def select_probe_credentials(payload, settings):
    """留空仅可复用同角色、同协议、同规范地址凭据；构造客户端前完成校验。"""
    if not payload.confirm_connection:
        raise ServiceError(400, "请明确确认连接所填外部服务读取模型目录。")
    try:
        base_url = normalize_provider_url(payload.role, payload.provider, payload.base_url)
    except (ConfigurationError, ValueError):
        raise ServiceError(400, "服务地址或供应商无效，请检查设置。") from None
    if payload.role == "generation":
        old_provider, old_url, old_key = settings.generation_provider, settings.deepseek_base_url, settings.deepseek_api_key
    else:
        old_provider, old_url, old_key = settings.embedding_provider, settings.embedding_base_url, settings.embedding_api_key
    key = (payload.api_key or "").strip()
    if not key and payload.use_saved_key:
        if service_destination(payload.provider, base_url) != service_destination(old_provider, old_url):
            raise ServiceError(400, "新的服务地址需要输入对应的新密钥；不会发送原地址密钥。")
        key = old_key or ""
    if not key:
        raise ServiceError(400, "请输入密钥，或使用同地址已保存的密钥。")
    if any(ord(char) < 32 or ord(char) > 126 for char in key):
        raise ServiceError(400, "密钥格式无效。")
    return base_url, key


async def probe_models(payload, settings, transport=None):
    """有界流式读取目录；不跟随跳转，不返回远端错误正文或认证头。

    transport 可注入 httpx.MockTransport。总时限独立于连接/读取时限；拒绝
    压缩响应避免解压膨胀。所有失败映射为本机固定文案，不记录远端异常原文。
    """
    base_url, key = select_probe_credentials(payload, settings)
    started = time.monotonic()
    http_status = None

    def result(status, models=None, truncated=False):
        """仅输出公开目录和有限状态。"""
        return dict(status=status, message=MESSAGES[status],
                    latency_ms=round((time.monotonic() - started) * 1000),
                    models=models or [], truncated=truncated, http_status=http_status)

    params = {}
    parsed = urlsplit(base_url)
    if parsed.hostname == "api.siliconflow.cn" and parsed.path == "/v1":
        params["sub_type"] = "chat" if payload.role == "generation" else "embedding"
    if parsed.hostname == "openrouter.ai" and parsed.path == "/api/v1":
        params["limit"] = str(MAX_MODELS)
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(
                transport=transport, trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(5.0), max_redirects=0,
            ) as client:
                async with client.stream(
                    "GET", base_url.rstrip("/") + "/models", params=params,
                    headers={"Authorization": "Bearer " + key,
                             "Accept": "application/json", "Accept-Encoding": "identity"},
                ) as response:
                    http_status = response.status_code
                    if http_status in {401, 403}:
                        return result("authentication_failed")
                    if http_status in {404, 405, 501}:
                        return result("unsupported")
                    if http_status == 429:
                        return result("rate_limited")
                    if 300 <= http_status < 400:
                        return result("redirect_blocked")
                    if http_status != 200:
                        return result("remote_error")
                    if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                        return result("invalid_response")
                    length = response.headers.get("content-length", "")
                    if length.isdecimal() and int(length) > MAX_RESPONSE_BYTES:
                        return result("response_too_large")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            return result("response_too_large")
                        body.extend(chunk)
        document = json.loads(body)
        if not isinstance(document, dict) or not isinstance(document.get("data"), list):
            return result("invalid_response")
        models = []
        seen = set()
        for item in document["data"]:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            owner = item.get("owned_by")
            if not isinstance(model_id, str) or not 1 <= len(model_id) <= 100 or key in model_id:
                continue
            if any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in model_id) or model_id in seen:
                continue
            owner = owner if isinstance(owner, str) and len(owner) <= 100 and key not in owner and not any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in owner) else ""
            seen.add(model_id)
            if len(models) < MAX_MODELS:
                models.append(dict(id=model_id, owned_by=owner))
        return result("success", models, len(seen) > MAX_MODELS or len(document["data"]) >= MAX_MODELS)
    except (TimeoutError, httpx.TimeoutException):
        return result("timeout")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
        return result("invalid_response")
    except (httpx.HTTPError, OSError):
        return result("unreachable")
