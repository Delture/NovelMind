"""Tests for chapter summaries and unified analysis jobs."""

from datetime import datetime
from typing import Generator
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base, create_db_engine
from backend.app.models.analysis import AnalysisJob, ChapterSummary
from backend.app.models.novel import Chapter, Novel


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Use a fresh SQLite schema with real foreign-key enforcement."""
    engine = create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def add_novel(db_session: Session) -> Novel:
    """Create one novel and chapter for analysis-model tests."""
    novel = Novel(
        filename="story.txt",
        encoding="utf-8",
        chapter_count=1,
        chapters=[
            Chapter(
                chapter_index=1,
                title="第一章",
                content="主角推门而入。",
                character_count=7,
            )
        ],
    )
    db_session.add(novel)
    db_session.commit()
    db_session.refresh(novel)
    return novel


def test_chapter_summary_can_be_inserted_and_updated(
    db_session: Session,
) -> None:
    novel = add_novel(db_session)
    summary = ChapterSummary(
        chapter_id=novel.chapters[0].id,
        summary="主角进入房间。",
        key_events=["推门", "进入房间"],
        plot_progression="主角开始探索。",
        cliffhanger="房间里传来异响。",
        model="deepseek-v4-pro",
        prompt_version="summary-v1",
        source_hash="a" * 64,
    )
    db_session.add(summary)
    db_session.commit()
    summary_id = summary.id
    original_updated_at = summary.updated_at

    summary.summary = "主角进入房间并听见异响。"
    summary.key_events.pop()
    summary.key_events.append("听见异响")
    db_session.commit()
    db_session.expire_all()

    saved = db_session.get(ChapterSummary, summary_id)
    assert saved is not None
    assert saved.chapter.id == novel.chapters[0].id
    assert saved.summary == "主角进入房间并听见异响。"
    assert saved.key_events == ["推门", "听见异响"]
    assert saved.updated_at >= original_updated_at


def test_chapter_summary_is_unique_per_chapter(
    db_session: Session,
) -> None:
    novel = add_novel(db_session)
    chapter_id = novel.chapters[0].id

    for summary_text in ("第一次摘要", "重复摘要"):
        db_session.add(
            ChapterSummary(
                chapter_id=chapter_id,
                summary=summary_text,
                key_events=[],
                plot_progression="推进",
                cliffhanger="无",
                model="deepseek-v4-pro",
                prompt_version="summary-v1",
                source_hash="b" * 64,
            )
        )
        if summary_text == "第一次摘要":
            db_session.commit()
        else:
            with pytest.raises(IntegrityError):
                db_session.commit()
            db_session.rollback()


def test_analysis_job_uses_uuid_string_and_relationship(
    db_session: Session,
) -> None:
    novel = add_novel(db_session)
    job = AnalysisJob(
        novel_id=novel.id,
        job_type="chapter_summaries",
        status="queued",
        progress_current=0,
        progress_total=1,
    )
    db_session.add(job)
    db_session.commit()

    assert isinstance(job.id, str)
    assert str(UUID(job.id)) == job.id
    assert job.novel.id == novel.id
    assert novel.analysis_jobs == [job]
    assert isinstance(job.created_at, datetime)
    assert isinstance(job.updated_at, datetime)
    assert job.started_at is None
    assert job.completed_at is None
    assert job.target_chapter_index is None
    assert job.force_refresh is False


def test_analysis_job_persists_force_refresh(
    db_session: Session,
) -> None:
    novel = add_novel(db_session)
    job = AnalysisJob(
        novel_id=novel.id,
        job_type="chapter_summaries",
        force_refresh=True,
        status="queued",
        progress_current=0,
        progress_total=1,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    assert job.force_refresh is True


def test_only_one_active_job_is_allowed_per_novel_and_type(
    db_session: Session,
) -> None:
    novel = add_novel(db_session)
    db_session.add(
        AnalysisJob(
            novel_id=novel.id,
            job_type="chapter_summaries",
            status="queued",
            progress_current=0,
            progress_total=1,
        )
    )
    db_session.commit()

    db_session.add(
        AnalysisJob(
            novel_id=novel.id,
            job_type="chapter_summaries",
            status="running",
            progress_current=0,
            progress_total=1,
            target_chapter_index=1,
        )
    )
    with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
        db_session.commit()
    db_session.rollback()


def test_terminal_or_different_type_jobs_do_not_conflict(
    db_session: Session,
) -> None:
    novel = add_novel(db_session)
    db_session.add_all(
        [
            AnalysisJob(
                novel_id=novel.id,
                job_type="chapter_summaries",
                status="succeeded",
                progress_current=1,
                progress_total=1,
            ),
            AnalysisJob(
                novel_id=novel.id,
                job_type="chapter_summaries",
                status="failed",
                progress_current=0,
                progress_total=1,
                target_chapter_index=1,
            ),
            AnalysisJob(
                novel_id=novel.id,
                job_type="chapter_summaries",
                status="queued",
                progress_current=0,
                progress_total=1,
            ),
            AnalysisJob(
                novel_id=novel.id,
                job_type="characters",
                status="running",
                progress_current=0,
                progress_total=1,
            ),
        ]
    )

    db_session.commit()

    assert db_session.query(AnalysisJob).count() == 4


@pytest.mark.parametrize(
    ("overrides", "constraint_name"),
    [
        ({"job_type": "unknown"}, "ck_analysis_jobs_job_type"),
        ({"status": "unknown"}, "ck_analysis_jobs_status"),
        (
            {"progress_current": -1},
            "ck_analysis_jobs_progress_current_nonnegative",
        ),
        (
            {"progress_total": -1},
            "ck_analysis_jobs_progress_total_nonnegative",
        ),
        (
            {"progress_current": 2, "progress_total": 1},
            "ck_analysis_jobs_progress_within_total",
        ),
        (
            {"target_chapter_index": 0},
            "ck_analysis_jobs_target_chapter_index_positive",
        ),
        (
            {"target_chapter_index": -1},
            "ck_analysis_jobs_target_chapter_index_positive",
        ),
    ],
)
def test_analysis_job_rejects_invalid_values(
    db_session: Session,
    overrides: dict[str, object],
    constraint_name: str,
) -> None:
    novel = add_novel(db_session)
    values = {
        "novel_id": novel.id,
        "job_type": "chapter_summaries",
        "status": "queued",
        "progress_current": 0,
        "progress_total": 1,
    }
    values.update(overrides)
    db_session.add(AnalysisJob(**values))

    with pytest.raises(IntegrityError, match=constraint_name):
        db_session.commit()
    db_session.rollback()


def test_raw_novel_delete_cascades_to_analysis_data(
    db_session: Session,
) -> None:
    novel = add_novel(db_session)
    db_session.add_all(
        [
            ChapterSummary(
                chapter_id=novel.chapters[0].id,
                summary="摘要",
                key_events=[],
                plot_progression="推进",
                cliffhanger="无",
                model="deepseek-v4-pro",
                prompt_version="summary-v1",
                source_hash="c" * 64,
            ),
            AnalysisJob(
                novel_id=novel.id,
                job_type="chapter_summaries",
                status="succeeded",
                progress_current=1,
                progress_total=1,
            ),
        ]
    )
    db_session.commit()
    novel_id = novel.id

    db_session.execute(
        text("DELETE FROM novels WHERE id = :novel_id"),
        {"novel_id": novel_id},
    )
    db_session.commit()

    assert db_session.query(ChapterSummary).count() == 0
    assert db_session.query(AnalysisJob).count() == 0
