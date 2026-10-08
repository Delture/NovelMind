"""将业务错误在 HTTP 边界统一转换，适用于每个承载服务的应用。"""

from fastapi import HTTPException
from fastapi.routing import APIRoute
from backend.app.services.errors import ServiceError


class ServiceRoute(APIRoute):
    """每个路由在协议边界统一映射领域错误。"""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def dispatch(request):
            try:
                return await handler(request)
            except ServiceError as error:
                raise HTTPException(error.status_code, error.detail) from error

        return dispatch
