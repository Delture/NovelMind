"""Exercise real workflow services while another request changes model configuration."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from tests.integration.workflow_fakes import ProviderAudit


@pytest.fixture
def workflow_case(isolated_config, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    from backend.app.api import novels, workspace
    from backend.app.core.database import create_db_engine, get_db, init_db
    from backend.app.services import rag, tasks

    case = isolated_config
    engine = create_db_engine(case.config.get_settings().database_url)
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def database():
        with factory() as db:
            yield db

    case.app.include_router(novels.router, prefix="/api")
    case.app.include_router(workspace.router, prefix="/api")
    case.app.dependency_overrides[get_db] = database
    case.app.dependency_overrides[workspace.get_workflow_session_factory] = lambda: factory
    audit = ProviderAudit(factory)
    # Keep production dependency injection and its lazy factories intact.
    monkeypatch.setattr(tasks, "AsyncDeepSeekProvider", lambda settings: audit.generation("summary", settings))
    monkeypatch.setattr(tasks, "DeepSeekProvider", lambda settings: audit.generation("report", settings))
    monkeypatch.setattr(rag, "DeepSeekProvider", lambda settings: audit.generation("answer", settings))
    monkeypatch.setattr(rag, "OpenAIEmbeddingProvider", audit.embedding)
    response = case.client.post("/api/novels/upload?filename=review-original.txt",
                                content="第一章 来信\n主角收到来信，决定出发调查。".encode(),
                                headers={"content-type": "application/octet-stream"})
    assert response.status_code == 200, response.text
    case.novel_id, case.factory, case.audit = response.json()["novel_id"], factory, audit
    try:
        yield case
    finally:
        audit.release.set()
        engine.dispose()


@pytest.mark.parametrize("kind, expected_roles", [("report", {"summary", "report"}), ("ask", {"embedding", "answer"})])
def test_whole_workflow_uses_accepted_snapshot_after_settings_change(workflow_case, kind, expected_roles):
    from fastapi.testclient import TestClient
    from backend.app.models.workspace import Workflow
    from backend.app.models.report import NovelReport
    from backend.app.models.rag import RagIndex, RagQuestion

    case = workflow_case
    original = case.config.get_settings()
    request = dict(kind=kind, question="主角收到什么？", confirm_external_processing=True, confirm_content_rights=True)

    def start():
        with TestClient(case.app) as client:
            return client.post(f"/api/novels/{case.novel_id}/workflows", json=request)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(start)
        try:
            entered = case.audit.entered.wait(5)
            if not entered and future.done():
                response = future.result()
                pytest.fail(f"Workflow never entered first provider: {response.status_code} {response.text}")
            assert entered, "Workflow failed to reach the first provider barrier"
            payload = deepcopy(case.payload)
            for role in ("generation", "embedding"):
                payload[role].update(base_url=f"https://{role}-b.example/v2", model=f"review-{role}-B",
                                     api_key=f"review-{role}-B-not-real")
            payload["embedding"]["dimensions"] = 6
            response = case.client.put("/api/system/model-settings", json=payload)
            assert response.status_code == 200, response.text
            changed = case.config.get_settings()
            assert changed.deepseek_base_url != original.deepseek_base_url
            assert changed.embedding_base_url != original.embedding_base_url
            assert changed.embedding_dimensions == 6
            # Changing a saved future preference must not alter this accepted request.
            response = case.client.put("/api/workspace/privacy", json=dict(decided=True, external=False, rights=False))
            assert response.status_code == 200, response.text
        finally:
            case.audit.release.set()
        response = future.result(timeout=20)

    assert response.status_code == 202, response.text
    with case.factory() as db:
        workflow = db.get(Workflow, response.json()["id"])
        assert workflow.status == "succeeded", workflow.error_message
        if kind == "report":
            report = db.query(NovelReport).one()
            assert report.model == original.deepseek_model
        else:
            index = db.query(RagIndex).one()
            assert index.embedding_model == original.embedding_model
            assert index.embedding_dimensions == original.embedding_dimensions
            question = db.query(RagQuestion).one()
            assert question.status == "succeeded"
            assert question.generation_model == original.deepseek_model
            assert question.answer == "主角收到来信。"
    assert {role for role, _ in case.audit.calls} == expected_roles
    assert {role for role, _ in case.audit.constructed} == expected_roles
    for role, settings in case.audit.constructed + case.audit.calls:
        assert settings == original, f"{role} picked up configuration changed after acceptance"
    public_tasks = case.client.get("/api/workspace/tasks")
    assert public_tasks.status_code == 200
    assert "not-real" not in public_tasks.text


@pytest.mark.parametrize("kind", ["report", "ask"])
@pytest.mark.parametrize("external, rights", [(False, True), (True, False), (False, False)])
def test_rejected_workflow_constructs_no_provider(workflow_case, kind, external, rights):
    from backend.app.models.workspace import Workflow
    from backend.app.models.analysis import AnalysisJob
    from backend.app.models.rag import RagQuestion

    case = workflow_case
    response = case.client.post(f"/api/novels/{case.novel_id}/workflows", json=dict(
        kind=kind, question="主角收到什么？", confirm_external_processing=external,
        confirm_content_rights=rights))
    assert response.status_code == 400, response.text
    assert case.audit.constructed == []
    assert case.audit.calls == []
    with case.factory() as db:
        assert db.query(Workflow).count() == 0
        assert db.query(AnalysisJob).count() == 0
        assert db.query(RagQuestion).count() == 0


@pytest.mark.parametrize("kind", ["report", "ask"])
def test_running_workflow_rejects_concurrent_start_and_duplicate_dispatch(workflow_case, kind):
    import asyncio
    from fastapi.testclient import TestClient
    from backend.app.models.workspace import Workflow
    from backend.app.services import workflows, tasks, rag

    case=workflow_case
    payload=dict(kind=kind,question="主角收到什么？",confirm_external_processing=True,confirm_content_rights=True)
    def start():
        with TestClient(case.app) as client:
            return client.post(f"/api/novels/{case.novel_id}/workflows",json=payload)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(start)
        try:
            assert case.audit.entered.wait(5)
            with case.factory() as db:
                row=db.query(Workflow).one()
                workflow_id=row.id
                assert row.status=="running"
            for other_kind in ("report","ask"):
                response=case.client.post(f"/api/novels/{case.novel_id}/workflows",json={**payload,"kind":other_kind})
                assert response.status_code==409,response.text
            settings=case.config.get_settings()
            execution=workflows.get_workflow_execution(settings,tasks.get_llm_provider_factory(settings),
                tasks.get_sync_llm_provider_factory(settings),rag.get_embedding_factory(settings),
                rag.get_generation_factory(settings),rag.get_vector_store_factory(settings))
            calls=list(case.audit.calls)
            asyncio.run(workflows.run_workflow(workflow_id,case.factory,execution,True,True))
            assert case.audit.calls==calls
        finally:
            case.audit.release.set()
        assert future.result(timeout=20).status_code==202
    calls=list(case.audit.calls)
    asyncio.run(workflows.run_workflow(workflow_id,case.factory,execution,True,True))
    assert case.audit.calls==calls
    with case.factory() as db:
        assert db.query(Workflow).count()==1
        assert db.get(Workflow,workflow_id).status=="succeeded"
