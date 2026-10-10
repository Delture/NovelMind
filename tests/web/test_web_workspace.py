
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from backend.app.api import summaries, rag, workspace
from backend.app.services import dependencies
from backend.app.core.config import get_settings, load_settings
from backend.app.core.database import create_db_engine, init_db, get_db
from backend.app.core.time import utc_now
from backend.app.main import app
from backend.app.models.workspace import Workflow, ReadingState
from backend.app.models.rag import RagChunkRecord
from backend.app.rag.vector_store import LocalVectorStore
from backend.app.rag.answering import RagAnswerPayload
from tests.support.summary_fakes import FakeProvider as SummaryProvider
from tests.support.report_fakes import FakeReportProvider
from tests.support.embedding_fakes import FakeProvider as EmbeddingProvider
from tests.support.answer_fakes import FakeLLMProvider


@pytest.fixture
def web(tmp_path):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = replace(load_settings({}), deepseek_api_key="test-only", embedding_api_key="test-only",
                       embedding_base_url="https://api.siliconflow.cn/v1", vector_store_path=str(tmp_path / "vectors"))
    providers = []

    def db():
        with factory() as session:
            yield session

    def summary_provider():
        provider = SummaryProvider()
        providers.append(provider)
        return provider

    def answer_provider():
        with factory() as session:
            chunk = session.query(RagChunkRecord).first()
            return FakeLLMProvider(RagAnswerPayload(answer="主角收到了来信。", citation_ids=[chunk.chunk_id], insufficient_evidence=False))

    def embedding_provider():
        provider = EmbeddingProvider()
        provider.provider_id = settings.embedding_provider
        return provider

    overrides = {
        get_db: db, get_settings: lambda: settings,
        workspace.get_workflow_session_factory: lambda: factory,
        summaries.get_session_factory: lambda: factory,
        rag.get_session_factory: lambda: factory,
        summaries.get_llm_provider_factory: lambda: summary_provider,
        dependencies.get_sync_llm_provider_factory: lambda: FakeReportProvider,
        rag.get_embedding_factory: lambda: embedding_provider,
        rag.get_generation_factory: lambda: answer_provider,
        rag.get_vector_store_factory: lambda: lambda: LocalVectorStore(settings.vector_store_path),
    }
    app.dependency_overrides.update(overrides)
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    response = client.post('/api/novels/upload?filename=原创测试.txt', content="第一章 来信\n主角收到来信。\n第二章 启程\n主角决定出发。".encode(), headers={"content-type":"application/octet-stream"})
    assert response.status_code == 200
    novel_id = response.json()['novel_id']
    yield client, factory, novel_id, providers
    client.close()
    for dependency in overrides:
        app.dependency_overrides.pop(dependency, None)
    engine.dispose()


def test_reading_and_privacy_persist_and_cascade(web):
    client, factory, novel_id, _ = web
    assert client.get('/api/workspace/privacy').json()['external'] is False
    value = dict(decided=True, external=True, rights=True)
    assert client.put('/api/workspace/privacy', json=value).status_code == 200
    assert client.get('/api/workspace/privacy').json() == value
    path = f'/api/novels/{novel_id}/reading'
    saved = client.get(path).json()
    saved['position'] = dict(chapter=2, paragraph=3, offset=4)
    saved['bookmarks'] = [dict(id='bookmark', chapter=1, paragraph=0, offset=0, snippet='<script>纯文本</script>')]
    assert client.put(path, json=saved).status_code == 200
    # Old bookmark payloads remain accepted; new bounded fields default empty.
    expected = {**saved, 'bookmarks': [{**saved['bookmarks'][0], 'label': '', 'note': ''}]}
    assert client.get(path).json() == expected
    saved['position']['chapter'] = 3
    assert client.put(path, json=saved).status_code == 422
    assert client.delete(f'/api/novels/{novel_id}').status_code == 204
    with factory() as db:
        assert db.get(ReadingState, novel_id) is None


def test_web_origin_gates_and_api_consent(web):
    client, _, novel_id, providers = web
    for origin in ['https://untrusted.example', 'null', 'http://127.0.0.1:3333']:
        assert client.put('/api/workspace/privacy', json={}, headers={'origin':origin}).status_code == 403
    assert client.get('/api/workspace/privacy', headers={'host':'evil.example'}).status_code == 403
    request = dict(kind='report', confirm_external_processing=False, confirm_content_rights=True)
    assert client.post(f'/api/novels/{novel_id}/workflows',json=request).status_code == 400
    assert not providers


def test_report_workflow_waits_for_summaries_and_reuses_cache(web):
    client, factory, novel_id, providers = web
    payload = dict(kind='report', confirm_external_processing=True, confirm_content_rights=True)
    result = client.post(f'/api/novels/{novel_id}/workflows',json=payload)
    assert result.status_code == 202
    tasks = client.get('/api/workspace/tasks').json()
    workflow = next(t for t in tasks if t['id'] == result.json()['id'])
    assert workflow['status'] == 'succeeded', workflow
    assert client.get(f'/api/novels/{novel_id}/report').json()['up_to_date'] is True
    assert len(providers[0].calls) == 2
    assert client.post(f'/api/novels/{novel_id}/workflows',json=payload).status_code == 202
    assert len(providers[1].calls) == 0


def test_index_question_workflow_uses_real_vector_store_and_citations(web):
    client, _, novel_id, _ = web
    result = client.post(f'/api/novels/{novel_id}/workflows',json=dict(kind='ask',question='主角收到什么？',confirm_external_processing=True,confirm_content_rights=True))
    assert result.status_code == 202
    workflow = next(t for t in client.get('/api/workspace/tasks').json() if t['id'] == result.json()['id'])
    assert workflow['status'] == 'succeeded', workflow
    answer = client.get(f'/api/novels/{novel_id}/rag/questions').json()['items'][0]
    assert answer['status'] == 'succeeded'
    assert answer['citations'][0]['chapter_index'] == 1


def test_workflow_failure_stops_next_step_and_restart_is_explicit(web):
    client, factory, novel_id, _ = web
    app.dependency_overrides[summaries.get_llm_provider_factory] = lambda: lambda: (_ for _ in ()).throw(ValueError('test failure'))
    result = client.post(f'/api/novels/{novel_id}/workflows',json=dict(kind='report',confirm_external_processing=True,confirm_content_rights=True))
    assert result.status_code == 202
    tasks = client.get('/api/workspace/tasks').json()
    workflow = next(t for t in tasks if t['id'] == result.json()['id'])
    assert workflow['status'] == 'failed'
    assert client.get(f'/api/novels/{novel_id}/report').status_code == 404
    with factory() as db:
        row = Workflow(id=str(uuid4()),novel_id=novel_id,kind='report',status='running',created_at=utc_now())
        db.add(row);db.commit();pending_id=row.id
    workspace.fail_interrupted_workflows(factory)
    with factory() as db:
        row = db.get(Workflow,pending_id)
        assert row.status == 'failed' and '重启' in row.error_message


def test_static_entry_serves_only_production_assets():
    from frontend.web_server import app as frontend
    client = TestClient(frontend, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    response = client.get('/')
    assert response.status_code == 200 and 'app.js' in response.text
    assert client.get('/app.js').status_code == 200
    assert client.get('/../.env').status_code == 404
    assert client.get('/design/rebuild/library.html').status_code == 404
