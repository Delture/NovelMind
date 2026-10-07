"""仅由隔离浏览器检查器启动：真实服务与数据库，确定性假模型提供商。"""

import os
import time
import tempfile
from pathlib import Path
from threading import Event
from dataclasses import replace

if os.environ.get("NOVELMIND_QA") != "isolated" or os.environ.get("PYTHON_DOTENV_DISABLED") != "1":
    raise RuntimeError("Browser QA requires explicit isolation and disabled .env loading")

import httpx
from sqlalchemy.engine import make_url
from backend.app.main import app
from backend.app.api import rag, summaries, system
from backend.app.core.config import get_settings, settings, save_runtime_model_settings
from backend.app.core.database import SessionLocal
from backend.app.models.rag import RagChunkRecord
from backend.app.rag.vector_store import LocalVectorStore
from backend.app.rag.answering import RagAnswerPayload
from tests.support.summary_fakes import FakeProvider as SummaryProvider
from tests.support.report_fakes import FakeReportProvider
from tests.support.embedding_fakes import FakeProvider as EmbeddingProvider
from tests.support.answer_fakes import FakeLLMProvider
from tests.support.character_fixtures import _add_novel, _add_snapshot
from tests.support.probe_fakes import BrowserProbeFixture, forbid_real_http, forbid_real_async_http

temporary_root = Path(make_url(settings.database_url).database).resolve().parent
settings_path = Path(os.environ.get("MODEL_SETTINGS_PATH", "")).resolve()
if (not temporary_root.is_relative_to(Path(tempfile.gettempdir()).resolve())
        or not temporary_root.name.startswith("novelmind-web-")
        or settings_path.parent != temporary_root
        or not Path(settings.vector_store_path).resolve().is_relative_to(temporary_root)):
    raise RuntimeError("This test server requires an isolated temporary database.")

test_settings = replace(settings, deepseek_api_key="test-key-never-sent", embedding_api_key="test-key-never-sent",
                        embedding_base_url="https://api.siliconflow.cn/v1")
# A real temporary override lets browser saves/restores observe the real settings
# lifecycle. A static get_settings override would hide failed preset persistence.
if not settings_path.exists():
    save_runtime_model_settings(test_settings)
probe_fixture = BrowserProbeFixture()
httpx.HTTPTransport.handle_request = forbid_real_http
httpx.AsyncHTTPTransport.handle_async_request = forbid_real_async_http

release_provider = Event()
release_provider.set()
provider_entered = Event()
pause_scope = ""


def wait_for_test_release(scope):
    """Hold only fake provider work so page closure happens before completion."""
    if pause_scope == scope:
        provider_entered.set()
        if not release_provider.wait(40):
            raise RuntimeError("Browser test did not release fake-provider barrier")


class SlowSummaryProvider(SummaryProvider):
    """让刷新发生在任务运行期间，稳定覆盖浏览器断开连接的场景。"""

    def generate_json(self, **kwargs):
        wait_for_test_release("summary")
        time.sleep(.4)
        return super().generate_json(**kwargs)


class PausableEmbeddingProvider(EmbeddingProvider):
    """Deterministic vectors with an explicit barrier for disconnect verification."""

    def embed(self, texts, *, before_attempt=None):
        wait_for_test_release("embedding")
        return super().embed(texts, before_attempt=before_attempt)


def embedding_factory():
    provider = PausableEmbeddingProvider()
    provider.provider_id = test_settings.embedding_provider
    return provider


def answer_factory():
    with SessionLocal() as db:
        chunk = db.query(RagChunkRecord).first()
        return FakeLLMProvider(RagAnswerPayload(answer="林舟收到了来信。<script>这里是纯文本</script>", citation_ids=[chunk.chunk_id], insufficient_evidence=False))


app.dependency_overrides.update({
    system.get_model_probe_transport: probe_fixture.transport,
    summaries.get_llm_provider_factory: lambda: SlowSummaryProvider,
    summaries.get_sync_llm_provider_factory: lambda: FakeReportProvider,
    rag.get_embedding_factory: lambda: embedding_factory,
    rag.get_generation_factory: lambda: answer_factory,
    rag.get_vector_store_factory: lambda: lambda: LocalVectorStore(test_settings.vector_store_path),
})


@app.post("/api/qa/seed")
def seed():
    with SessionLocal() as db:
        novel = _add_novel(db, filename="原创故事验收.txt")
        _add_snapshot(db, novel)
        db.commit()
        return {"id":novel.id}


@app.post("/api/qa/pause/{scope}")
def pause(scope: str):
    """Pause subsequent fake summary/index work; this route exists only in QA."""
    global pause_scope
    assert scope in {"summary", "embedding"}
    pause_scope = scope
    provider_entered.clear()
    release_provider.clear()
    return {"paused": True}


@app.post("/api/qa/barrier")
def barrier():
    """Report whether a worker really reached its fake provider."""
    return {"entered": provider_entered.is_set()}


@app.post("/api/qa/release")
def release():
    """Release the held worker only after the previous browser context is closed."""
    release_provider.set()
    return {"released": True}


@app.post("/api/qa/probe-scenario")
def set_probe_scenario(value: dict):
    """Test-only success/error/delay control; it cannot choose a real transport."""
    probe_fixture.configure(**value)
    return {"mode": probe_fixture.mode, "delay_ms": probe_fixture.delay_ms, "tag": probe_fixture.tag}


@app.post("/api/qa/probe-records")
def probe_records():
    """Return public fake-request metadata for debounce/race assertions, never keys."""
    return {"items": list(probe_fixture.requests)}
