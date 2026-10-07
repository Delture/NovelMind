"""本机网页的来源校验；防止其他网站借浏览器调用写接口。"""

from urllib.parse import urlsplit
from starlette.responses import JSONResponse
from starlette.requests import Request


class LocalOriginMiddleware:
    """直接使用 ASGI 中间件，避免请求上下文取消后中断异步后台任务。"""

    def __init__(self, app):
        """保留下游 ASGI 应用，不引入额外任务组或响应流包装。"""
        self.app = app

    async def __call__(self, scope, receive, send):
        """校验 HTTP 来源，合法请求直接委托原应用，生命周期消息直接透传。"""
        if scope["type"] == "http":
            error = origin_error(Request(scope))
            if error:
                await JSONResponse({"detail": error}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def origin_error(request):
    """仅允许回环 Host；有 Origin 的写请求须来自同一本机网页端口。

    命令行和内部 ASGI 请求没有 Origin，仍可使用现有 API；浏览器跨站写请求和
    DNS 重绑定请求会被拒绝。本地部署保持回环监听，不增加公开服务入口。
    """
    if request.url.hostname not in {"localhost", "127.0.0.1", "::1", "testserver"}:
        return "仅允许本机访问。"
    origin = request.headers.get("origin")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
        try:
            parsed = urlsplit(origin)
            own = urlsplit(str(request.base_url))
            allowed = (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                       and parsed.port in {8000, 8501, own.port})
        except ValueError:
            allowed = False
        if not allowed:
            return "拒绝跨站写入请求。"
    return None
