"""Tests for report provenance, generation, jobs, and API gates."""

from dataclasses import replace
from typing import Callable, Generator, Optional, Type
from unittest.mock import Mock

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.analyzer.llm import (
    JsonGenerationResult,
    LLMProviderError,
)
from backend.app.analyzer.reports import (
    REPORT_PROMPT_VERSION,
    NovelReportPayload,
    ReportEvidenceNote,
    ReportEvidenceNotesPayload,
    ReportSectionPayload,
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
from backend.app.api.reports import (
    StartReportRequest,
    get_report,
    get_report_estimate,
    start_report,
)
from backend.app.core.config import Settings, load_settings
from backend.app.core.database import Base, create_db_engine
from backend.app.main import app
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


def section(
    title: str,
    *,
    evidence: Optional[list[int]] = None,
) -> ReportSectionPayload:
    return ReportSectionPayload(
        title=title,
        analysis=f"{title}分析",
        evidence_chapter_indices=evidence or [1],
    )


def report_payload(
    *,
    evidence: Optional[list[int]] = None,
) -> NovelReportPayload:
    citations = evidence or [1]
    return NovelReportPayload(
        title="全书分析报告",
        executive_summary=section("执行摘要", evidence=citations),
        plot_structure=section("情节结构", evidence=citations),
        character_dynamics=section("人物动态", evidence=citations),
        pacing=section("节奏", evidence=citations),
        themes=[section("主题", evidence=citations)],
        strengths=[section("优点", evidence=citations)],
        risks=[section("风险", evidence=citations)],
        recommendations=[section("建议", evidence=citations)],
    )


def test_report_payload_allows_empty_unsupported_optional_sections() -> None:
    payload = NovelReportPayload(
        title="材料边界报告",
        executive_summary=section("执行摘要"),
        plot_structure=section("情节结构"),
        character_dynamics=section("人物动态"),
        pacing=section("节奏"),
    )

    assert payload.themes == []
    assert payload.strengths == []
    assert payload.risks == []
    assert payload.recommendations == []


class FakeReportProvider:
    model = "deepseek-v4-pro"

    def __init__(
        self,
        *,
        invalid_final_citation: bool = False,
        failure: Optional[Exception] = None,
    ) -> None:
        self.invalid_final_citation = invalid_final_citation
        self.failure = failure
        self.calls: list[dict[str, object]] = []
        self.closed = False

    def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: Type,
        thinking: bool = False,
        reasoning_effort: Optional[str] = None,
        before_attempt=None,
    ):
        if before_attempt is not None:
            before_attempt()
        if self.failure is not None:
            raise self.failure
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "response_model": response_model,
                "thinking": thinking,
                "reasoning_effort": reasoning_effort,
            }
        )
        if response_model is ReportEvidenceNotesPayload:
            value = ReportEvidenceNotesPayload(
                notes=[
                    ReportEvidenceNote(
                        topic="情节",
                        analysis="证据分析",
                        evidence_chapter_indices=[1],
                    )
                ]
            )
        elif response_model is NovelReportPayload:
            value = report_payload(
                evidence=(
                    [999] if self.invalid_final_citation else [1]
                )
            )
        else:
            raise AssertionError("unexpected response model")
        return JsonGenerationResult(
            value=value,
            model=self.model,
            prompt_tokens=1,
            completion_tokens=1,
            cache_hit_tokens=0,
            request_id="request-test",
        )

    def close(self) -> None:
        self.closed = True


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


def test_report_routes_are_registered() -> None:
    paths = app.openapi()["paths"]
    assert "get" in paths["/api/novels/{novel_id}/report/estimate"]
    assert {"get", "post"} <= set(
        paths["/api/novels/{novel_id}/report"]
    )


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
    assert "最小充分证据集" in all_prompts
    assert "对象/阶段、根因、具体操作" in all_prompts
    assert "派生摘要，不是小说原文" in all_system_prompts
    assert "不能可靠评价原文句式" in all_system_prompts
    assert "可选数组没有可靠内容时返回空数组" in all_system_prompts
    assert provider.calls[-1]["thinking"] is True
    assert provider.calls[-1]["reasoning_effort"] == "high"


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
        assert estimate.maximum_request_attempts == 18
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


@pytest.mark.parametrize(
    "start_request",
    [
        StartReportRequest(
            confirm_external_processing=False,
            confirm_content_rights=True,
        ),
        StartReportRequest(
            confirm_external_processing=True,
            confirm_content_rights=False,
        ),
        StartReportRequest(
            confirm_external_processing=True,
            confirm_content_rights=True,
            force=True,
            confirm_force_refresh=False,
        ),
    ],
)
def test_report_consent_failures_construct_no_provider(
    session_factory,
    start_request: StartReportRequest,
) -> None:
    calls = 0

    def provider_factory():
        nonlocal calls
        calls += 1
        return FakeReportProvider()

    with session_factory() as db:
        novel = add_novel_with_summaries(db)
        with pytest.raises(HTTPException) as error:
            start_report(
                request=start_request,
                background_tasks=BackgroundTasks(),
                novel_id=novel.id,
                settings=configured_settings(),
                db=db,
                provider_factory=provider_factory,
                session_factory=session_factory,
            )
    assert error.value.status_code == 400
    assert calls == 0


def test_report_start_schedules_one_background_job(
    session_factory,
) -> None:
    provider = FakeReportProvider()
    tasks = BackgroundTasks()
    with session_factory() as db:
        novel = add_novel_with_summaries(db)
        response = start_report(
            request=StartReportRequest(
                confirm_external_processing=True,
                confirm_content_rights=True,
            ),
            background_tasks=tasks,
            novel_id=novel.id,
            settings=configured_settings(),
            db=db,
            provider_factory=lambda: provider,
            session_factory=session_factory,
        )
        assert response.job_type == "report"
        assert response.status == "queued"
        assert response.progress_total == 2
        assert db.query(AnalysisJob).count() == 1
    assert len(tasks.tasks) == 1
    assert provider.closed is False


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
