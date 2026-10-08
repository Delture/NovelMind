"""Issue #3 的行为回归：活动任务冲突、状态落盘、跨标签页与来源门禁。"""

import asyncio
import threading
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from tests.web.test_web_workspace import web as web
from backend.app.core.database import create_db_engine
from backend.app.core.time import utc_now
from backend.app.models.analysis import AnalysisJob
from backend.app.models.workspace import Workflow
from backend.app.services.workflows import run_workflow


def test_workflow_rejects_active_summary_without_creating_failed_chain(web):
    client, factory, novel_id, _ = web
    with factory() as db:
        db.add(
            AnalysisJob(
                id=str(uuid4()),
                novel_id=novel_id,
                job_type="chapter_summaries",
                status="running",
                progress_current=0,
                progress_total=2,
            )
        )
        db.commit()
    response = client.post(
        f"/api/novels/{novel_id}/workflows",
        json=dict(
            kind="report", confirm_external_processing=True, confirm_content_rights=True
        ),
    )
    assert response.status_code == 409
    with factory() as db:
        assert db.query(Workflow).count() == 0


def test_workflow_claim_error_is_terminalized_after_lock_retries(web):
    _, factory, novel_id, _ = web
    key = str(uuid4())
    with factory() as db:
        db.add(Workflow(id=key, novel_id=novel_id, kind="report", created_at=utc_now()))
        db.commit()
    attempts = 0

    def initially_locked():
        nonlocal attempts
        attempts += 1
        if attempts <= 4:
            raise OperationalError("claim", {}, Exception("database is locked"))
        return factory()

    asyncio.run(run_workflow(key, initially_locked, None, True, True))
    with factory() as db:
        assert db.get(Workflow, key).status == "failed"
    assert attempts == 5


def test_stale_position_update_preserves_other_tabs_bookmarks(web):
    client, _, novel_id, _ = web
    path = f"/api/novels/{novel_id}/reading"
    stale = client.get(path).json()
    first = dict(id="a", chapter=1, paragraph=0, offset=0, note="A 标签页")
    second = dict(id="b", chapter=2, paragraph=0, offset=0, note="B 标签页")
    assert client.put(path + "/bookmarks/a", json=first).status_code == 200
    assert client.put(path + "/bookmarks/b", json=second).status_code == 200
    stale["position"]["chapter"] = 2
    assert (
        client.put(
            path + "/position", json={k: stale[k] for k in ("prefs", "position")}
        ).status_code
        == 200
    )
    first["note"] = "更新 A"
    assert client.put(path + "/bookmarks/a", json=first).status_code == 200
    saved = client.get(path).json()
    assert {b["id"]: b["note"] for b in saved["bookmarks"]} == {
        "a": "更新 A",
        "b": "B 标签页",
    }
    assert client.delete(path + "/bookmarks/a").status_code == 204
    assert [b["id"] for b in client.get(path).json()["bookmarks"]] == ["b"]


def test_same_port_origin_required_and_spoofed_host_does_not_bypass_ip_gate(web):
    client, _, _, _ = web
    payload = dict(decided=True, external=True, rights=True)
    response = client.put(
        "/api/workspace/privacy",
        json=payload,
        headers={"host": "127.0.0.1:8501", "origin": "http://127.0.0.1:8000"},
    )
    assert response.status_code == 403
    assert (
        client.put(
            "/api/workspace/privacy",
            json=payload,
            headers={"host": "127.0.0.1:8501", "origin": "http://127.0.0.1:8501"},
        ).status_code
        == 200
    )
    from backend.app.main import app

    with TestClient(
        app, base_url="http://127.0.0.1", client=("192.0.2.10", 1234)
    ) as remote:
        assert remote.get("/api/workspace/tasks").status_code == 403


def test_file_database_uses_wal_timeout_and_foreign_keys(tmp_path):
    engine = create_db_engine("sqlite:///" + str(tmp_path / "pragma.db"))
    try:
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
            assert connection.execute(text("PRAGMA busy_timeout")).scalar() == 5000
            assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
    finally:
        engine.dispose()


def test_upload_processing_runs_off_the_event_loop(web, monkeypatch):
    client, _, _, _ = web
    from backend.app.api import novels

    original = novels.decode_txt
    threads = []

    def decode(content):
        threads.append(threading.current_thread().name)
        return original(content)

    monkeypatch.setattr(novels, "decode_txt", decode)
    response = client.post(
        "/api/novels/upload?filename=thread.txt",
        content="第一章 原创\n原创正文。".encode(),
        headers={"content-type": "application/octet-stream"},
    )
    assert response.status_code == 200
    assert threads == ["AnyIO worker thread"]


def test_cancelling_queued_job_preserves_completed_results(web):
    client, factory, novel_id, _ = web
    key = str(uuid4())
    with factory() as db:
        db.add(
            AnalysisJob(
                id=key, novel_id=novel_id, job_type="chapter_summaries", status="queued"
            )
        )
        db.commit()
    assert client.post(f"/api/workspace/tasks/{key}/cancel").status_code == 204
    with factory() as db:
        job = db.get(AnalysisJob, key)
        assert job.status == "failed" and job.completed_at is not None
        assert "取消" in job.error_message


def test_cancel_between_dispatch_and_execution_does_not_send_model_request(web, monkeypatch):
    """取消与派发交错时，刚创建的子任务也必须结束而不执行。"""
    client, factory, novel_id, providers = web
    from backend.app.services import summaries
    original = summaries.start_chapter_summaries
    def cancel_after_dispatch(**kwargs):
        child = original(**kwargs)
        with factory() as db:
            db.query(Workflow).filter(Workflow.novel_id == novel_id, Workflow.status == "running").update(
                {"status": "failed", "error_message": "用户已取消任务。"})
            db.commit()
        return child
    monkeypatch.setattr(summaries, "start_chapter_summaries", cancel_after_dispatch)
    response = client.post(f"/api/novels/{novel_id}/workflows", json=dict(
        kind="report", confirm_external_processing=True, confirm_content_rights=True))
    assert response.status_code == 202
    assert providers and not any(provider.calls for provider in providers)
    with factory() as db:
        assert db.get(Workflow, response.json()["id"]).error_message == "用户已取消任务。"
        child = db.query(AnalysisJob).filter_by(novel_id=novel_id).one()
        assert child.status == "failed"
