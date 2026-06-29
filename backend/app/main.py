from fastapi import FastAPI


app = FastAPI(title="NovelMind")


@app.get("/health")
def health_check() -> dict[str, str]:
    """Return a simple health check response."""
    return {"status": "ok", "project": "NovelMind"}
