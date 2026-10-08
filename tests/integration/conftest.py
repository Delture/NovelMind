"""Review-only isolation; production imports happen after temporary settings exist."""

from types import SimpleNamespace

import pytest


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Use fake credentials, a disposable working directory, and no HTTP egress."""
    import dotenv
    import httpx

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    values = {
        "DATABASE_URL": "sqlite:///" + (tmp_path / "review.db").as_posix(),
        "VECTOR_STORE_PATH": str(tmp_path / "vectors"),
        "MODEL_SETTINGS_PATH": str(tmp_path / "models.json"),
        "GENERATION_PROVIDER": "openai_compatible",
        "DEEPSEEK_API_KEY": "review-generation-A-not-real",
        "DEEPSEEK_BASE_URL": "https://generation-a.example/v1",
        "DEEPSEEK_MODEL": "review-generation-A",
        "EMBEDDING_PROVIDER": "openai_compatible",
        "EMBEDDING_API_KEY": "review-embedding-A-not-real",
        "EMBEDDING_BASE_URL": "https://embedding-a.example/v1",
        "EMBEDDING_MODEL": "review-embedding-A",
        "EMBEDDING_DIMENSIONS": "4",
        "SUMMARY_CONCURRENCY_MODE": "manual",
        "SUMMARY_MAX_CONCURRENCY": "1",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    def forbidden_http(*args, **kwargs):
        raise AssertionError("Review tests must never send an external HTTP request")

    async def forbidden_async_http(*args, **kwargs):
        forbidden_http()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden_http)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_async_http)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.core import config
    from backend.app.api import system

    config.save_runtime_model_settings(config.load_settings(values))
    from backend.app.core.local_web import LocalOriginMiddleware
    app = FastAPI()
    app.add_middleware(LocalOriginMiddleware)
    app.include_router(system.router, prefix="/api")
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    payload = {
        "generation": {
            "provider": "openai_compatible", "model": values["DEEPSEEK_MODEL"],
            "base_url": values["DEEPSEEK_BASE_URL"],
            "summary_concurrency_mode": "manual", "summary_max_concurrency": 1,
        },
        "embedding": {
            "provider": "openai_compatible", "model": values["EMBEDDING_MODEL"],
            "base_url": values["EMBEDDING_BASE_URL"], "dimensions": 4,
        },
    }
    try:
        yield SimpleNamespace(config=config, app=app, client=client, payload=payload,
                              path=tmp_path / "models.json", root=tmp_path)
    finally:
        client.close()
