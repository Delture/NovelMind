from fastapi import FastAPI

from backend.app.api.novels import router as novels_router


app = FastAPI(title="NovelMind")
app.include_router(novels_router, prefix="/api")


@app.get("/health")
def health_check() -> dict[str, str]:
    """Return a simple health check response."""
    return {"status": "ok", "project": "NovelMind"}
