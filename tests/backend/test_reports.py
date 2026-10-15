from tests.support.report_fakes import FakeReportProvider, report_payload
"""Tests for report provenance, generation, jobs, and API gates."""

from dataclasses import replace
from typing import Callable, Generator

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.analyzer.llm import (
    LLMProviderError,
)
from backend.app.analyzer.reports import (
    REPORT_PROMPT_VERSION,
    ReportSourceError,
    estimate_report_generation_steps,
    generate_report_payload,
    prepare_report_source,
    run_report_job,
)
from backend.app.analyzer.summaries import (
    SUMMARY_PROMPT_VERSION,
    chapter_summary_source_hash,
)
from backend.app.services.reports import (
    get_report,
    get_report_estimate,
)
from backend.app.core.config import Settings, load_settings
from backend.app.core.database import Base, create_db_engine
from backend.app.models.analysis import AnalysisJob, ChapterSummary
from backend.app.models.novel import Chapter, Novel
from backend.app.models.report import NovelReport


@pytest.fixture()
def session_factory() -> Generator[Callable[[], Session], None, None]:
    engine = create_db_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    try:
        yield factory
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def configured_settings() -> Settings:
    return replace(
        load_settings({}),
        deepseek_api_key="report-test-key",
    )


def add_novel_with_summaries(
    db: Session,
    *,
    content_suffix: str = "",
) -> Novel:
    settings = configured_settings()
    novel = Novel(
        filename="report-source.txt",
        encoding="utf-8",
        chapter_count=2,
        chapters=[
            Chapter(
                chapter_index=1,
                title="第一章",
                content=f"不可直接发送的正文甲{content_suffix}",
                character_count=10,
            ),
            Chapter(
                chapter_index=2,
                title="第二章",
                content=f"不可直接发送的正文乙{content_suffix}",
                character_count=10,
            ),
        ],
    )
    db.add(novel)
    db.flush()
    for chapter in novel.chapters:
        db.add(
            ChapterSummary(
                chapter_id=chapter.id,
                summary=f"第{chapter.chapter_index}章派生摘要",
                key_events=[f"事件{chapter.chapter_index}"],
                plot_progression=f"推进{chapter.chapter_index}",
                cliffhanger=None,
                model=settings.deepseek_model,
                prompt_version=SUMMARY_PROMPT_VERSION,
                source_hash=chapter_summary_source_hash(
                    chapter.title,
                    chapter.content,
                ),
            )
        )
    db.commit()
    db.refresh(novel)
    return novel


class SafeProviderFailure(LLMProviderError):
    def __init__(self) -> None:
        super().__init__(
            "外部报告服务暂不可用。",
            code="report_test_failure",
        )


def add_saved_report(
    db: Session,
    *,
    novel: Novel,
    source_hash: str,
) -> NovelReport:
    saved = NovelReport(
        novel_id=novel.id,
        source_hash=source_hash,
        report_data=report_payload().model_dump(mode="json"),
        model=configured_settings().deepseek_model,
        prompt_version=REPORT_PROMPT_VERSION,
        summary_count=2,
        character_count=0,
        generation_steps=2,
        includes_character_analysis=False,
    )
    db.add(saved)
    db.commit()
    db.refresh(saved)
    return saved


def test_report_source_requires_current_complete_summaries(
    session_factory,
) -> None:
    settings = configured_settings()
    with session_factory() as db:
        novel = Novel(
            filename="missing.txt",
            encoding="utf-8",
            chapter_count=1,
            chapters=[
                Chapter(
                    chapter_index=1,
                    title="第一章",
                    content="正文",
                    character_count=2,
                )
            ],
        )
        db.add(novel)
        db.commit()
        with pytest.raises(ReportSourceError, match="章节摘要"):
            prepare_report_source(
                db,
                novel_id=novel.id,
                expected_model=settings.deepseek_model,
            )

        db.add(
            ChapterSummary(
                chapter_id=novel.chapters[0].id,
                summary="摘要",
                key_events=[],
                plot_progression="推进",
                model=settings.deepseek_model,
                prompt_version=SUMMARY_PROMPT_VERSION,
                source_hash="a" * 64,
            )
        )
        db.commit()
        with pytest.raises(ReportSourceError, match="章节摘要"):
            prepare_report_source(
                db,
                novel_id=novel.id,
                expected_model=settings.deepseek_model,
            )


def test_report_generation_uses_only_derived_text_and_external_calls(
    session_factory,
) -> None:
    with session_factory() as db:
        novel = add_novel_with_summaries(
            db,
            content_suffix="ORIGINAL_SECRET",
        )
        source = prepare_report_source(
            db,
            novel_id=novel.id,
            expected_model=configured_settings().deepseek_model,
        )
    provider = FakeReportProvider()
    result, steps = generate_report_payload(source, provider)
    assert result.title == "全书分析报告"
    assert steps == estimate_report_generation_steps(source) == 2
    assert len(provider.calls) == 2
    all_prompts = "\n".join(
        str(call["user_prompt"]) for call in provider.calls
    )
    all_system_prompts = "\n".join(
        str(call["system_prompt"]) for call in provider.calls
    )
    assert "第1章派生摘要" in all_prompts
    assert "ORIGINAL_SECRET" not in all_prompts
    assert "全书视野判准（Full-Arc Horizon Test）" in all_prompts
    assert "串联阅读的具体方法" in all_prompts
    assert "按章节提炼的叙事状态摘要" in all_system_prompts
    assert "不得跨越材料边界去臆测原文的具体修辞造句" in all_system_prompts
    assert "可选数组没有可靠内容时返回空数组" in all_system_prompts
    assert "输出精炼" in all_prompts
    assert "目标不超过 300 字" in all_prompts
    assert provider.calls[-1]["thinking"] is False
    assert provider.calls[-1]["reasoning_effort"] is None


def test_report_generation_rejects_invented_chapter_citations(
    session_factory,
) -> None:
    with session_factory() as db:
        novel = add_novel_with_summaries(db)
        source = prepare_report_source(
            db,
            novel_id=novel.id,
            expected_model=configured_settings().deepseek_model,
        )
    with pytest.raises(LLMProviderError):
        generate_report_payload(
            source,
            FakeReportProvider(invalid_final_citation=True),
        )


def test_report_job_succeeds_and_closes_provider(
    session_factory,
) -> None:
    provider = FakeReportProvider()
    with session_factory() as db:
        novel = add_novel_with_summaries(db)
        job = AnalysisJob(
            novel_id=novel.id,
            job_type="report",
            status="queued",
            progress_current=0,
            progress_total=2,
        )
        db.add(job)
        db.commit()
        job_id = job.id
        novel_id = novel.id

    run_report_job(job_id, provider, session_factory)

    with session_factory() as db:
        job = db.get(AnalysisJob, job_id)
        saved = (
            db.query(NovelReport)
            .filter(NovelReport.novel_id == novel_id)
            .one()
        )
        assert job.status == "succeeded"
        assert job.progress_current == job.progress_total == 2
        assert saved.report_data["title"] == "全书分析报告"
        assert saved.summary_count == 2
        assert saved.includes_character_analysis is False
    assert provider.closed


def test_failed_refresh_preserves_previous_report(
    session_factory,
) -> None:
    provider = FakeReportProvider(failure=SafeProviderFailure())
    with session_factory() as db:
        novel = add_novel_with_summaries(db)
        source = prepare_report_source(
            db,
            novel_id=novel.id,
            expected_model=provider.model,
        )
        saved = add_saved_report(
            db,
            novel=novel,
            source_hash=source.source_hash,
        )
        old_updated_at = saved.updated_at
        job = AnalysisJob(
            novel_id=novel.id,
            job_type="report",
            force_refresh=True,
            status="queued",
            progress_current=0,
            progress_total=2,
        )
        db.add(job)
        db.commit()
        job_id = job.id
        saved_id = saved.id

    run_report_job(job_id, provider, session_factory)

    with session_factory() as db:
        job = db.get(AnalysisJob, job_id)
        saved = db.get(NovelReport, saved_id)
        assert job.status == "failed"
        assert job.error_message == "外部报告服务暂不可用。"
        assert saved.updated_at == old_updated_at
        assert saved.report_data["title"] == "全书分析报告"
    assert provider.closed


def test_report_estimate_discloses_derived_data_and_cache(
    session_factory,
) -> None:
    settings = configured_settings()
    with session_factory() as db:
        novel = add_novel_with_summaries(db)
        estimate = get_report_estimate(
            novel_id=novel.id,
            settings=settings,
            db=db,
        )
        assert estimate.estimated_generation_steps == 2
        assert estimate.maximum_request_attempts == 30
        assert estimate.sends_original_text is False
        assert estimate.sends_derived_summaries is True
        assert estimate.sends_derived_character_data is False
        assert estimate.up_to_date is False

        source = prepare_report_source(
            db,
            novel_id=novel.id,
            expected_model=settings.deepseek_model,
        )
        add_saved_report(
            db,
            novel=novel,
            source_hash=source.source_hash,
        )
        cached = get_report_estimate(
            novel_id=novel.id,
            settings=settings,
            db=db,
        )
        assert cached.up_to_date
        assert cached.estimated_generation_steps == 0
        assert cached.estimated_force_generation_steps == 2


def test_get_report_returns_stale_snapshot_without_destroying_it(
    session_factory,
) -> None:
    settings = configured_settings()
    with session_factory() as db:
        novel = add_novel_with_summaries(db)
        source = prepare_report_source(
            db,
            novel_id=novel.id,
            expected_model=settings.deepseek_model,
        )
        add_saved_report(
            db,
            novel=novel,
            source_hash=source.source_hash,
        )
        current = get_report(
            novel_id=novel.id,
            settings=settings,
            db=db,
        )
        assert current.up_to_date

        novel.chapters[0].content += "已变化"
        db.commit()
        stale = get_report(
            novel_id=novel.id,
            settings=settings,
            db=db,
        )
        assert stale.up_to_date is False
        assert stale.report.title == "全书分析报告"


def test_novel_report_constraints_and_delete_cascade(
    session_factory,
) -> None:
    with session_factory() as db:
        novel = add_novel_with_summaries(db)
        saved = NovelReport(
            novel_id=novel.id,
            source_hash="a" * 64,
            report_data=report_payload().model_dump(mode="json"),
            model="deepseek-v4-pro",
            prompt_version=REPORT_PROMPT_VERSION,
            summary_count=0,
            character_count=0,
            generation_steps=2,
            includes_character_analysis=False,
        )
        db.add(saved)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        source = prepare_report_source(
            db,
            novel_id=novel.id,
            expected_model=configured_settings().deepseek_model,
        )
        add_saved_report(
            db,
            novel=novel,
            source_hash=source.source_hash,
        )
        db.delete(novel)
        db.commit()
        assert db.query(NovelReport).count() == 0

def test_old_report_prompt_version_is_stale_but_preserved(
    session_factory,
) -> None:
    settings = configured_settings()
    with session_factory() as db:
        novel = add_novel_with_summaries(db)
        source = prepare_report_source(
            db,
            novel_id=novel.id,
            expected_model=settings.deepseek_model,
        )
        saved = NovelReport(
            novel_id=novel.id,
            source_hash=source.source_hash,
            report_data=report_payload().model_dump(mode="json"),
            model=settings.deepseek_model,
            prompt_version="novel-report-v2",
            summary_count=2,
            character_count=0,
            generation_steps=2,
            includes_character_analysis=False,
        )
        db.add(saved)
        db.commit()
        stale = get_report(
            novel_id=novel.id,
            settings=settings,
            db=db,
        )
        assert stale.up_to_date is False
        assert stale.report.title == "全书分析报告"
        assert stale.prompt_version == "novel-report-v2"
        assert db.query(NovelReport).count() == 1
