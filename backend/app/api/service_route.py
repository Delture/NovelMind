
from fastapi import HTTPException
from fastapi.routing import APIRoute
from backend.app.services.errors import ServiceError


class ServiceRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def dispatch(request):
            try:
                return await handler(request)
            except ServiceError as error:
                raise HTTPException(error.status_code, error.detail) from error

        return dispatch
