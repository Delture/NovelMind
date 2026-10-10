"""Optional AI chapter-boundary review: estimate, schedule, apply merge plan."""

from __future__ import annotations

import json
import logging
from typing import Callable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm import Session
from starlette import status

from backend.app.analyzer.chapter_review import (
    CHAPTER_REVIEW_PROMPT_VERSION,
    ChapterReviewPayload,
    ChapterSnapshot,
    MAX_REVIEW_CHAPTERS,
    apply_review_plan,
    catalog_for_prompt,
    map_chapter_index,
    plan_is_identity,
    review_system_prompt,
    validate_review_plan,
)
from backend.app.analyzer.deepseek import MAX_JSON_GENERATION_ATTEMPTS
from backend.app.analyzer.jobs import claim_analysis_job
from backend.app.analyzer.llm import LLMProvider
from backend.app.core.config import GENERATION_PROVIDERS, Settings
from backend.app.core.sqlite_retry import is_sqlite_lock_error, retry_sqlite
from backend.app.core.time import utc_now
from backend.app.models.analysis import AnalysisJob, ChapterSummary
from backend.app.models.character import CharacterAnalysis
from backend.app.models.novel import Chapter, Novel
from backend.app.models.rag import RagIndex
from backend.app.models.report import NovelReport
from backend.app.models.workspace import ReadingState, Workflow
from backend.app.services.errors import ServiceError
from backend.app.services.execution import TaskDispatcher
from backend.app.services.tasks import (
    AnalysisJobResponse,
    DEEPSEEK_PRIVACY_URL,
    DEEPSEEK_TERMS_URL,
    ProviderFactory,
    _close_provider,
    _job_response,
    fail_dispatch,
)

logger = logging.getLogger(__name__)

JOB_TYPE = "chapter_review"
RELATED_JOB_TYPES = (
    "chapter_summaries",
    "characters",
    "rag_index",
    "report",
    JOB_TYPE,
)


class StartChapterReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_external_processing: StrictBool
    confirm_content_rights: StrictBool


class ChapterReviewEstimateResponse(BaseModel):
    novel_id: int
    chapter_count: int
    character_count: int
    reviewable: bool
    estimated_generation_steps: int
    maximum_request_attempts: int
    provider: str
    provider_label: str
    model: str
    configured: bool
    data_handling_notice: str
    terms_url: str
    privacy_url: str
    prompt_version: str = CHAPTER_REVIEW_PROMPT_VERSION


def ensure_no_active_chapter_review(db: Session, novel_id: int) -> None:
    active = (
        db.query(AnalysisJob.id)
        .filter(
            AnalysisJob.novel_id == novel_id,
            AnalysisJob.job_type == JOB_TYPE,
            AnalysisJob.status.in_(("queued", "running")),
        )
        .first()
    )
    if active is not None:
        raise ServiceError(
            status_code=status.HTTP_409_CONFLICT,
            detail="章节目录复核进行中，请等待完成后再启动其他分析任务。",
        )


def _ensure_review_allowed(db: Session, novel_id: int) -> None:
    active = (
        db.query(AnalysisJob)
        .filter(
            AnalysisJob.novel_id == novel_id,
            AnalysisJob.job_type.in_(RELATED_JOB_TYPES),
            AnalysisJob.status.in_(("queued", "running")),
        )
        .first()
    )
    if active is not None:
        raise ServiceError(
            status_code=status.HTTP_409_CONFLICT,
            detail="该小说已有分析或目录复核任务进行中，请等待完成后再试。",
        )
    if (
        db.query(Workflow.id)
        .filter(
            Workflow.novel_id == novel_id,
            Workflow.status.in_(("queued", "running")),
        )
        .first()
        is not None
    ):
        raise ServiceError(
            status_code=status.HTTP_409_CONFLICT,
            detail="该小说已有任务链进行中，请等待完成后再复核目录。",
        )
    if (
        db.query(ChapterSummary.id)
        .join(Chapter)
        .filter(Chapter.novel_id == novel_id)
        .first()
        is not None
    ):
        raise ServiceError(
            status_code=status.HTTP_409_CONFLICT,
            detail="本书已有章节摘要，本版不支持在已有分析结果上复核目录。",
        )
    if (
        db.query(CharacterAnalysis.id)
        .filter(CharacterAnalysis.novel_id == novel_id)
        .first()
        is not None
    ):
        raise ServiceError(
            status_code=status.HTTP_409_CONFLICT,
            detail="本书已有人物分析，本版不支持在已有分析结果上复核目录。",
        )
    if db.query(RagIndex.id).filter(RagIndex.novel_id == novel_id).first() is not None:
        raise ServiceError(
            status_code=status.HTTP_409_CONFLICT,
            detail="本书已有原文索引，本版不支持在已有分析结果上复核目录。",
        )
    if db.query(NovelReport.id).filter(NovelReport.novel_id == novel_id).first() is not None:
        raise ServiceError(
            status_code=status.HTTP_409_CONFLICT,
            detail="本书已有全书报告，本版不支持在已有分析结果上复核目录。",
        )


def get_chapter_review_estimate(
    novel_id: int, settings: Settings, db: Session
) -> ChapterReviewEstimateResponse:
    novel = db.get(Novel, novel_id)
    if novel is None:
        raise ServiceError(status_code=status.HTTP_404_NOT_FOUND, detail="未找到指定小说。")
    chapters = (
        db.query(Chapter)
        .filter(Chapter.novel_id == novel_id)
        .order_by(Chapter.chapter_index)
        .all()
    )
    chapter_count = len(chapters)
    character_count = sum(int(chapter.character_count) for chapter in chapters)
    reviewable = bool(
        settings.deepseek_configured and 1 <= chapter_count <= MAX_REVIEW_CHAPTERS
    )
    steps = 1 if reviewable else 0
    attempts = steps * settings.deepseek_max_attempts * MAX_JSON_GENERATION_ATTEMPTS
    return ChapterReviewEstimateResponse(
        novel_id=novel_id,
        chapter_count=chapter_count,
        character_count=character_count,
        reviewable=reviewable,
        estimated_generation_steps=steps,
        maximum_request_attempts=attempts,
        provider=settings.generation_provider,
        provider_label=GENERATION_PROVIDERS[settings.generation_provider],
        model=settings.deepseek_model,
        configured=settings.deepseek_configured,
        data_handling_notice=(
            "复核会把各章标题、字数和每章开头约 120 字正文片段发送至外部生成模型 API；"
            "不会发送全书全文。预计 1 个生成步骤，失败时可能按配置重试。"
            "失败则保留当前规则分章。"
        ),
        terms_url=DEEPSEEK_TERMS_URL,
        privacy_url=DEEPSEEK_PRIVACY_URL,
    )


def start_chapter_review(
    request: StartChapterReviewRequest,
    background_tasks: TaskDispatcher,
    novel_id: int,
    db: Session,
    provider_factory: ProviderFactory,
    session_factory: Callable[[], Session],
    settings: Settings,
) -> AnalysisJobResponse:
    novel = db.get(Novel, novel_id)
    if novel is None:
        raise ServiceError(status_code=status.HTTP_404_NOT_FOUND, detail="未找到指定小说。")
    if not request.confirm_external_processing:
        raise ServiceError(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="复核目录会把章节标题与正文片段发送至外部生成模型 API，请先明确确认。",
        )
    if not request.confirm_content_rights:
        raise ServiceError(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请确认你有权把小说标题和正文片段提交给外部生成模型 API，并已了解其数据处理条款。",
        )
    if not settings.deepseek_configured:
        raise ServiceError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="尚未配置外部生成模型，无法复核目录。",
        )
    chapter_count = db.query(Chapter.id).filter(Chapter.novel_id == novel_id).count()
    if chapter_count < 1:
        raise ServiceError(status_code=status.HTTP_400_BAD_REQUEST, detail="小说没有可复核的章节。")
    if chapter_count > MAX_REVIEW_CHAPTERS:
        raise ServiceError(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"章节超过 {MAX_REVIEW_CHAPTERS} 章，本版请直接使用规则分章结果。",
        )
    _ensure_review_allowed(db, novel_id)

    provider = provider_factory()
    active = (
        db.query(AnalysisJob)
        .filter(
            AnalysisJob.novel_id == novel_id,
            AnalysisJob.job_type == JOB_TYPE,
            AnalysisJob.status.in_(("queued", "running")),
        )
        .first()
    )
    if active is not None:
        _close_provider(provider)
        return _job_response(active)

    job = AnalysisJob(
        id=str(uuid4()),
        novel_id=novel_id,
        job_type=JOB_TYPE,
        status="queued",
        progress_current=0,
        progress_total=1,
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        _close_provider(provider)
        if db.get(Novel, novel_id) is None:
            raise ServiceError(
                status_code=status.HTTP_404_NOT_FOUND, detail="未找到指定小说。"
            ) from error
        active = (
            db.query(AnalysisJob)
            .filter(
                AnalysisJob.novel_id == novel_id,
                AnalysisJob.job_type == JOB_TYPE,
                AnalysisJob.status.in_(("queued", "running")),
            )
            .first()
        )
        if active is not None:
            return _job_response(active)
        raise

    job_id = job.id
    try:
        background_tasks.add_task(
            run_chapter_review_job, job_id, novel_id, provider, session_factory
        )
    except Exception:
        fail_dispatch(
            db=db,
            session_factory=session_factory,
            job_id=job_id,
            job_type=JOB_TYPE,
            message="目录复核任务未能加入后台队列，请重试。",
        )
        _close_provider(provider)
        raise
    return _job_response(job)


def _mark_failed(session_factory, job_id: str, expected: str, message: str) -> None:
    def terminalize():
        with session_factory() as db:
            updated = (
                db.query(AnalysisJob)
                .filter(
                    AnalysisJob.id == job_id,
                    AnalysisJob.job_type == JOB_TYPE,
                    AnalysisJob.status == expected,
                )
                .update(
                    {
                        AnalysisJob.status: "failed",
                        AnalysisJob.error_message: message,
                        AnalysisJob.completed_at: utc_now(),
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            return updated

    try:
        retry_sqlite(terminalize)
    except Exception:
        logger.exception("Could not mark chapter review job %s failed", job_id)


def run_chapter_review_job(
    job_id: str,
    novel_id: int,
    provider: LLMProvider,
    session_factory: Callable[[], Session],
) -> None:
    claimed_at = utc_now()
    try:
        with session_factory() as db:
            try:
                claimed = claim_analysis_job(db, job_id, JOB_TYPE, claimed_at)
            except OperationalError as error:
                db.rollback()
                message = (
                    "目录复核无法获取本地数据库写锁，请重试。"
                    if is_sqlite_lock_error(error)
                    else "目录复核启动失败，请稍后重试。"
                )
                _mark_failed(session_factory, job_id, "queued", message)
                return
            except Exception:
                db.rollback()
                logger.exception("Could not claim chapter review job %s", job_id)
                _mark_failed(
                    session_factory, job_id, "queued", "目录复核启动失败，请稍后重试。"
                )
                return
            if claimed != 1:
                return

            rows = (
                db.query(Chapter)
                .filter(Chapter.novel_id == novel_id)
                .order_by(Chapter.chapter_index)
                .all()
            )
            snapshots = [
                ChapterSnapshot(
                    index=row.chapter_index,
                    title=row.title,
                    content=row.content,
                    character_count=int(row.character_count),
                )
                for row in rows
            ]
            fingerprint = [(s.index, s.title, s.character_count) for s in snapshots]

        if not snapshots:
            _mark_failed(session_factory, job_id, "running", "小说没有可复核的章节。")
            return

        def still_running() -> None:
            with session_factory() as db:
                job = db.get(AnalysisJob, job_id)
                if job is None or job.status != "running":
                    raise RuntimeError("chapter review cancelled")

        try:
            still_running()
            result = provider.generate_json(
                system_prompt=review_system_prompt(),
                user_prompt=(
                    "校对下一行 JSON 中的章节目录。N="
                    f"{len(snapshots)}。证据不足则原样返回：\n"
                    f"{json.dumps({'chapters': catalog_for_prompt(snapshots)}, ensure_ascii=False)}"
                ),
                response_model=ChapterReviewPayload,
                thinking=False,
                before_attempt=still_running,
            )
            plan = validate_review_plan(result.value, len(snapshots))
        except Exception as error:
            logger.info("chapter review model/validation failed: %s", error)
            _mark_failed(
                session_factory,
                job_id,
                "running",
                "目录复核失败，已保留规则分章。",
            )
            return

        rebuilt = apply_review_plan(snapshots, plan)

        def commit_result():
            with session_factory() as db:
                # 条件 UPDATE 同时拿写锁并确认仍为 running；rowcount=0 表示已取消。
                locked = (
                    db.query(AnalysisJob)
                    .filter(
                        AnalysisJob.id == job_id,
                        AnalysisJob.job_type == JOB_TYPE,
                        AnalysisJob.status == "running",
                    )
                    .update(
                        {AnalysisJob.updated_at: utc_now()},
                        synchronize_session=False,
                    )
                )
                if locked != 1:
                    return "cancelled"
                job = db.get(AnalysisJob, job_id)
                if job is None or job.status != "running":
                    return "cancelled"
                current = (
                    db.query(Chapter)
                    .filter(Chapter.novel_id == novel_id)
                    .order_by(Chapter.chapter_index)
                    .all()
                )
                current_fp = [
                    (c.chapter_index, c.title, int(c.character_count)) for c in current
                ]
                if current_fp != fingerprint:
                    return "changed"
                if (
                    db.query(ChapterSummary.id)
                    .join(Chapter)
                    .filter(Chapter.novel_id == novel_id)
                    .first()
                    is not None
                    or db.query(CharacterAnalysis.id)
                    .filter(CharacterAnalysis.novel_id == novel_id)
                    .first()
                    is not None
                    or db.query(RagIndex.id).filter(RagIndex.novel_id == novel_id).first()
                    is not None
                    or db.query(NovelReport.id)
                    .filter(NovelReport.novel_id == novel_id)
                    .first()
                    is not None
                ):
                    return "derived"

                if not plan_is_identity(plan, snapshots):
                    db.query(Chapter).filter(Chapter.novel_id == novel_id).delete(
                        synchronize_session=False
                    )
                    db.flush()
                    db.expire_all()
                    reading = db.get(ReadingState, novel_id)
                    if reading is not None and isinstance(reading.value, dict):
                        reading.value = _remap_reading(reading.value, plan, snapshots)
                        flag_modified(reading, "value")
                    for item in rebuilt:
                        db.add(
                            Chapter(
                                novel_id=novel_id,
                                chapter_index=item.index,
                                title=item.title,
                                content=item.content,
                                character_count=item.character_count,
                            )
                        )
                    db.query(Novel).filter(Novel.id == novel_id).update(
                        {Novel.chapter_count: len(rebuilt)},
                        synchronize_session=False,
                    )

                job.status = "succeeded"
                job.progress_current = 1
                job.progress_total = 1
                job.error_message = None
                job.completed_at = utc_now()
                db.commit()
                return "ok"

        try:
            outcome = retry_sqlite(commit_result)
        except Exception:
            logger.exception("chapter review save failed for job %s", job_id)
            _mark_failed(
                session_factory,
                job_id,
                "running",
                "目录复核未能保存，已保留规则分章。",
            )
            return
        if outcome == "ok":
            return
        if outcome == "cancelled":
            return
        message = {
            "changed": "复核期间章节已变化，已取消写入并保留当前目录。",
            "derived": "复核完成前已出现分析结果，已取消写入并保留当前目录。",
        }.get(str(outcome), "目录复核未能保存，已保留规则分章。")
        _mark_failed(session_factory, job_id, "running", message)
    finally:
        _close_provider(provider)


def _paragraph_count(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip())


def _preceding_paragraphs(old_chapter: int, item, snapshots_by_index: dict) -> int:
    """Non-empty lines before old_chapter's section in a merged chapter body."""
    offset = 0
    for src in range(item.from_index, old_chapter):
        snap = snapshots_by_index[src]
        if src > item.from_index and snap.title.strip():
            offset += 1
        offset += _paragraph_count(snap.content)
    return offset


def _remap_anchor(old_chapter: int, paragraph, plan, snapshots_by_index: dict) -> tuple[int, int]:
    new_chapter = map_chapter_index(old_chapter, plan)
    for item in plan:
        if item.from_index <= old_chapter <= item.to_index:
            if item.from_index == item.to_index:
                return new_chapter, paragraph if isinstance(paragraph, int) else -1
            preceding = _preceding_paragraphs(old_chapter, item, snapshots_by_index)
            if not isinstance(paragraph, int) or paragraph < 0:
                # 被合入章的章首对准内部标题（或无标题时的首段），不是整章开头。
                return new_chapter, -1 if old_chapter == item.from_index else preceding
            title_pad = 0
            if old_chapter > item.from_index and snapshots_by_index[old_chapter].title.strip():
                title_pad = 1
            return new_chapter, preceding + title_pad + paragraph
    return new_chapter, paragraph if isinstance(paragraph, int) else -1


def _remap_reading(value: dict, plan, snapshots: list) -> dict:
    by_index = {snap.index: snap for snap in snapshots}
    updated = dict(value)
    position = updated.get("position")
    if isinstance(position, dict) and isinstance(position.get("chapter"), int):
        position = dict(position)
        chapter, paragraph = _remap_anchor(
            position["chapter"], position.get("paragraph", -1), plan, by_index
        )
        position["chapter"] = chapter
        position["paragraph"] = paragraph
        updated["position"] = position
    bookmarks = updated.get("bookmarks")
    if isinstance(bookmarks, list):
        remapped = []
        for bookmark in bookmarks:
            if not isinstance(bookmark, dict):
                remapped.append(bookmark)
                continue
            item = dict(bookmark)
            if isinstance(item.get("chapter"), int):
                old_chapter = item["chapter"]
                old_paragraph = item.get("paragraph", -1)
                chapter, paragraph = _remap_anchor(
                    old_chapter, old_paragraph, plan, by_index
                )
                item["chapter"] = chapter
                item["paragraph"] = paragraph
                # Keep id aligned with chapter:paragraph keys the reader generates.
                if item.get("id") == f"{old_chapter}:{old_paragraph}":
                    item["id"] = f"{chapter}:{paragraph}"
            remapped.append(item)
        updated["bookmarks"] = remapped
    return updated
