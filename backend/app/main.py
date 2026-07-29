from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from backend.app.api.characters import router as characters_router
from backend.app.api.novels import router as novels_router
from backend.app.api.rag import router as rag_router
from backend.app.api.reports import router as reports_router
from backend.app.api.summaries import router as summaries_router
from backend.app.api.system import router as system_router
from backend.app.analyzer.summaries import fail_interrupted_jobs
from backend.app.rag.answering import fail_interrupted_rag_questions
from backend.app.rag.maintenance import cleanup_orphan_rag_collections
from backend.app.core.config import settings
from backend.app.core.database import SessionLocal, init_db


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Initialize application resources when the server starts."""
    init_db()
    fail_interrupted_jobs(SessionLocal)
    fail_interrupted_rag_questions(SessionLocal)
    cleanup_orphan_rag_collections(
        SessionLocal,
        settings.vector_store_path,
    )
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(characters_router, prefix="/api")
app.include_router(novels_router, prefix="/api")
app.include_router(rag_router, prefix="/api")
app.include_router(reports_router, prefix="/api")
app.include_router(summaries_router, prefix="/api")
app.include_router(system_router, prefix="/api")


@app.get("/health")
def health_check() -> dict[str, str]:
    """Return a simple health check response."""
    return {"status": "ok", "project": "NovelMind"}
