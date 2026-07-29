"""Concurrency and scope tests for summary job scheduling."""

from pathlib import Path
from threading import Barrier, Lock, Thread
from typing import Callable
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.analyzer import summaries as summaries_service
from backend.app.analyzer import jobs as jobs_service
from backend.app.api import summaries as summaries_api
from backend.app.api.summaries import (
    StartSummariesRequest,
    _schedule_summary_job,
    start_chapter_summaries,
)
from backend.app.core.database import Base, create_db_engine
from backend.app.models.analysis import AnalysisJob
from backend.app.models.novel import Chapter, Novel


def _file_session_factory(
    tmp_path: Path,
    *,
    timeout: float = 5.0,
) -> tuple[Callable[[], Session], object]:
    engine = create_db_engine(
        f"sqlite:///{(tmp_path / 'summary-jobs.db').as_posix()}",
        connect_args={"timeout": timeout},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
    )
    return factory, engine


def _add_novel(factory: Callable[[], Session]) -> int:
    with factory() as session:
        novel = Novel(
            filename="concurrency.txt",
            encoding="utf-8",
            chapter_count=2,
            chapters=[
                Chapter(
                    chapter_index=index,
                    title=f"第{index}章",
                    content=f"第{index}章正文",
                    character_count=5,
                )
                for index in (1, 2)
            ],
        )
        session.add(novel)
        session.commit()
        return int(novel.id)


def test_same_scope_concurrent_requests_create_only_one_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, engine = _file_session_factory(tmp_path)
    novel_id = _add_novel(factory)
    rendezvous = Barrier(2)
    counter_lock = Lock()
    initial_checks = 0
    original_lookup = summaries_api._active_summary_job

    def synchronized_lookup(
        db: Session,
        selected_novel_id: int,
    ) -> AnalysisJob:
        nonlocal initial_checks
        result = original_lookup(db, selected_novel_id)
        with counter_lock:
            initial_checks += 1
            should_wait = initial_checks <= 2
        if should_wait:
            rendezvous.wait(timeout=5)
        return result

    monkeypatch.setattr(
        summaries_api,
        "_active_summary_job",
        synchronized_lookup,
    )
    task_collectors = [Mock(), Mock()]
    results = []
    errors = []

    def start(collector: Mock) -> None:
        try:
            with factory() as session:
                results.append(
                    _schedule_summary_job(
                        novel_id=novel_id,
                        progress_total=2,
                        request=StartSummariesRequest(
                            confirm_external_processing=True,
                            confirm_content_rights=True,
                        ),
                        background_tasks=collector,
                        db=session,
                        provider=Mock(),
                        session_factory=factory,
                    )
                )
        except Exception as error:  # pragma: no cover - asserted below
            errors.append(error)

    threads = [
        Thread(target=start, args=(collector,))
        for collector in task_collectors
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    try:
        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        assert len(results) == 2
        assert results[0].id == results[1].id
        with factory() as session:
            assert session.query(AnalysisJob).count() == 1
        assert sum(
            collector.add_task.call_count
            for collector in task_collectors
        ) == 1
    finally:
        engine.dispose()


def test_different_scope_is_not_reported_as_same_active_job(
    tmp_path: Path,
) -> None:
    factory, engine = _file_session_factory(tmp_path)
    novel_id = _add_novel(factory)
    try:
        with factory() as session:
            _schedule_summary_job(
                novel_id=novel_id,
                progress_total=2,
                request=StartSummariesRequest(
                    confirm_external_processing=True,
                    confirm_content_rights=True,
                ),
                background_tasks=Mock(),
                db=session,
                provider=Mock(),
                session_factory=factory,
            )

        with factory() as session:
            with pytest.raises(HTTPException) as error:
                _schedule_summary_job(
                    novel_id=novel_id,
                    progress_total=1,
                    request=StartSummariesRequest(
                        confirm_external_processing=True,
                        confirm_content_rights=True,
                    ),
                    background_tasks=Mock(),
                    db=session,
                    provider=Mock(),
                    session_factory=factory,
                    target_chapter_index=2,
                )

        assert error.value.status_code == 409
        with factory() as session:
            assert session.query(AnalysisJob).count() == 1
    finally:
        engine.dispose()


def test_same_single_chapter_scope_reuses_active_job(
    tmp_path: Path,
) -> None:
    factory, engine = _file_session_factory(tmp_path)
    novel_id = _add_novel(factory)
    tasks = Mock()
    try:
        with factory() as session:
            first = _schedule_summary_job(
                novel_id=novel_id,
                progress_total=1,
                request=StartSummariesRequest(
                    confirm_external_processing=True,
                    confirm_content_rights=True,
                ),
                background_tasks=tasks,
                db=session,
                provider=Mock(),
                session_factory=factory,
                target_chapter_index=2,
            )
            second = _schedule_summary_job(
                novel_id=novel_id,
                progress_total=1,
                request=StartSummariesRequest(
                    confirm_external_processing=True,
                    confirm_content_rights=True,
                ),
                background_tasks=tasks,
                db=session,
                provider=Mock(),
                session_factory=factory,
                target_chapter_index=2,
            )

        assert first.id == second.id
        assert first.target_chapter_index == 2
        assert second.target_chapter_index == 2
        tasks.add_task.assert_called_once()
    finally:
        engine.dispose()


def test_different_force_mode_is_not_silently_reused(
    tmp_path: Path,
) -> None:
    factory, engine = _file_session_factory(tmp_path)
    novel_id = _add_novel(factory)
    try:
        with factory() as session:
            first = _schedule_summary_job(
                novel_id=novel_id,
                progress_total=2,
                request=StartSummariesRequest(
                    confirm_external_processing=True,
                    confirm_content_rights=True,
                    force=False,
                ),
                background_tasks=Mock(),
                db=session,
                provider=Mock(),
                session_factory=factory,
            )
            with pytest.raises(HTTPException) as error:
                _schedule_summary_job(
                    novel_id=novel_id,
                    progress_total=2,
                    request=StartSummariesRequest(
                        confirm_external_processing=True,
                        confirm_content_rights=True,
                        force=True,
                    ),
                    background_tasks=Mock(),
                    db=session,
                    provider=Mock(),
                    session_factory=factory,
                )

        assert first.force is False
        assert error.value.status_code == 409
        assert "刷新模式" in str(error.value.detail)
    finally:
        engine.dispose()


def test_missing_novel_foreign_key_race_returns_not_found(
    tmp_path: Path,
) -> None:
    factory, engine = _file_session_factory(tmp_path)
    try:
        with factory() as session:
            with pytest.raises(HTTPException) as error:
                _schedule_summary_job(
                    novel_id=999,
                    progress_total=1,
                    request=StartSummariesRequest(
                        confirm_external_processing=True,
                        confirm_content_rights=True,
                    ),
                    background_tasks=Mock(),
                    db=session,
                    provider=Mock(),
                    session_factory=factory,
                )

        assert error.value.status_code == 404
    finally:
        engine.dispose()


def test_unrelated_integrity_error_is_not_misreported_as_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, engine = _file_session_factory(tmp_path)
    novel_id = _add_novel(factory)
    with factory() as session:
        session.add(
            AnalysisJob(
                id="duplicate-id",
                novel_id=novel_id,
                job_type="chapter_summaries",
                status="succeeded",
                progress_current=2,
                progress_total=2,
            )
        )
        session.commit()
    monkeypatch.setattr(
        summaries_api,
        "uuid4",
        lambda: "duplicate-id",
    )

    try:
        with factory() as session:
            with pytest.raises(IntegrityError):
                _schedule_summary_job(
                    novel_id=novel_id,
                    progress_total=2,
                    request=StartSummariesRequest(
                        confirm_external_processing=True,
                        confirm_content_rights=True,
                    ),
                    background_tasks=Mock(),
                    db=session,
                    provider=Mock(),
                    session_factory=factory,
                )
    finally:
        engine.dispose()


def test_provider_factory_is_deferred_until_all_consents_pass(
    tmp_path: Path,
) -> None:
    factory, engine = _file_session_factory(tmp_path)
    novel_id = _add_novel(factory)
    provider_factory = Mock()
    try:
        with factory() as session:
            with pytest.raises(HTTPException) as error:
                start_chapter_summaries(
                    novel_id=novel_id,
                    request=StartSummariesRequest(
                        confirm_external_processing=True,
                        confirm_content_rights=False,
                    ),
                    background_tasks=Mock(),
                    db=session,
                    provider_factory=provider_factory,
                    session_factory=factory,
                )

        assert error.value.status_code == 400
        assert "有权" in str(error.value.detail)
        provider_factory.assert_not_called()
    finally:
        engine.dispose()


def test_reused_active_job_closes_new_unused_provider(
    tmp_path: Path,
) -> None:
    factory, engine = _file_session_factory(tmp_path)
    novel_id = _add_novel(factory)
    active_provider = Mock()
    unused_provider = Mock()
    request = StartSummariesRequest(
        confirm_external_processing=True,
        confirm_content_rights=True,
    )
    try:
        with factory() as session:
            first = _schedule_summary_job(
                novel_id=novel_id,
                progress_total=2,
                request=request,
                background_tasks=Mock(),
                db=session,
                provider=active_provider,
                session_factory=factory,
            )
            second = _schedule_summary_job(
                novel_id=novel_id,
                progress_total=2,
                request=request,
                background_tasks=Mock(),
                db=session,
                provider=unused_provider,
                session_factory=factory,
            )

        assert first.id == second.id
        unused_provider.close.assert_called_once_with()
        active_provider.close.assert_not_called()
    finally:
        engine.dispose()


def test_refresh_failure_marks_committed_job_failed_and_closes_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, engine = _file_session_factory(tmp_path)
    novel_id = _add_novel(factory)
    provider = Mock()
    tasks = Mock()
    try:
        with factory() as session:
            monkeypatch.setattr(
                session,
                "refresh",
                Mock(
                    side_effect=RuntimeError(
                        "refresh failed with private details"
                    )
                ),
            )
            with pytest.raises(RuntimeError, match="refresh failed"):
                _schedule_summary_job(
                    novel_id=novel_id,
                    progress_total=2,
                    request=StartSummariesRequest(
                        confirm_external_processing=True,
                        confirm_content_rights=True,
                    ),
                    background_tasks=tasks,
                    db=session,
                    provider=provider,
                    session_factory=factory,
                )

        with factory() as session:
            job = session.query(AnalysisJob).one()
            assert job.status == "failed"
            assert job.completed_at is not None
            assert job.error_message == (
                "摘要任务未能加入后台队列，请重试。"
            )
            assert "private" not in job.error_message
        tasks.add_task.assert_not_called()
        provider.close.assert_called_once_with()
    finally:
        engine.dispose()


def test_dispatch_failure_marks_committed_job_failed_and_closes_provider(
    tmp_path: Path,
) -> None:
    factory, engine = _file_session_factory(tmp_path)
    novel_id = _add_novel(factory)
    provider = Mock()
    tasks = Mock()
    tasks.add_task.side_effect = RuntimeError(
        "dispatch failed with private details"
    )
    try:
        with factory() as session:
            with pytest.raises(RuntimeError, match="dispatch failed"):
                _schedule_summary_job(
                    novel_id=novel_id,
                    progress_total=2,
                    request=StartSummariesRequest(
                        confirm_external_processing=True,
                        confirm_content_rights=True,
                    ),
                    background_tasks=tasks,
                    db=session,
                    provider=provider,
                    session_factory=factory,
                )

        with factory() as session:
            job = session.query(AnalysisJob).one()
            assert job.status == "failed"
            assert job.completed_at is not None
            assert job.error_message == (
                "摘要任务未能加入后台队列，请重试。"
            )
            assert "private" not in job.error_message
        tasks.add_task.assert_called_once()
        provider.close.assert_called_once_with()
    finally:
        engine.dispose()


def test_dispatch_failure_retries_locked_terminalization_and_allows_requeue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, engine = _file_session_factory(tmp_path, timeout=0.02)
    novel_id = _add_novel(factory)
    provider = Mock()
    retry_attempts: list[int] = []

    class LockingFailingBackgroundTasks:
        def __init__(self) -> None:
            self.lock_session: Session | None = None
            self.add_task_calls = 0

        def add_task(self, *args: object, **kwargs: object) -> None:
            self.add_task_calls += 1
            self.lock_session = factory()
            locked_novel = self.lock_session.get(Novel, novel_id)
            assert locked_novel is not None
            locked_novel.filename = "hold-dispatch-compensation-lock.txt"
            self.lock_session.flush()
            raise RuntimeError(
                "dispatch failed with private novel details"
            )

        def release_lock(self) -> None:
            if self.lock_session is None:
                return
            self.lock_session.rollback()
            self.lock_session.close()
            self.lock_session = None

    tasks = LockingFailingBackgroundTasks()

    def release_lock_after_retries(failed_attempt: int) -> None:
        retry_attempts.append(failed_attempt)
        if failed_attempt == 2:
            tasks.release_lock()

    monkeypatch.setattr(
        jobs_service,
        "_sleep_before_sqlite_retry",
        release_lock_after_retries,
    )

    try:
        with factory() as session:
            with pytest.raises(RuntimeError, match="dispatch failed"):
                _schedule_summary_job(
                    novel_id=novel_id,
                    progress_total=2,
                    request=StartSummariesRequest(
                        confirm_external_processing=True,
                        confirm_content_rights=True,
                    ),
                    background_tasks=tasks,
                    db=session,
                    provider=provider,
                    session_factory=factory,
                )

        with factory() as session:
            failed_job = session.query(AnalysisJob).one()
            failed_job_id = failed_job.id
            assert failed_job.status == "failed"
            assert failed_job.completed_at is not None
            assert failed_job.error_message == (
                "摘要任务未能加入后台队列，请重试。"
            )
            assert "private" not in failed_job.error_message

        requeued_tasks = Mock()
        requeued_provider = Mock()
        with factory() as session:
            requeued = _schedule_summary_job(
                novel_id=novel_id,
                progress_total=2,
                request=StartSummariesRequest(
                    confirm_external_processing=True,
                    confirm_content_rights=True,
                ),
                background_tasks=requeued_tasks,
                db=session,
                provider=requeued_provider,
                session_factory=factory,
            )

        with factory() as session:
            failed_job = session.get(AnalysisJob, failed_job_id)
            requeued_job = session.get(AnalysisJob, requeued.id)
            assert failed_job is not None
            assert failed_job.status == "failed"
            assert requeued_job is not None
            assert requeued_job.status == "queued"

        assert retry_attempts == [1, 2]
        assert tasks.add_task_calls == 1
        provider.close.assert_called_once_with()
        requeued_tasks.add_task.assert_called_once()
        requeued_provider.close.assert_not_called()
    finally:
        tasks.release_lock()
        engine.dispose()
