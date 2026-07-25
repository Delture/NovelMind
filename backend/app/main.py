from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from backend.app.api.novels import router as novels_router
from backend.app.core.database import init_db


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Initialize application resources when the server starts."""
    init_db()
    yield


app = FastAPI(title="NovelMind", lifespan=lifespan)
app.include_router(novels_router, prefix="/api")


@app.get("/health")
def health_check() -> dict[str, str]:
    """Return a simple health check response."""
    return {"status": "ok", "project": "NovelMind"}
