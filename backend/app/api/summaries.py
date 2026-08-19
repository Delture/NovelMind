"""章节摘要的估算、启动、结果分页和任务状态接口。

路由层只处理 HTTP 协议和任务调度，不实现摘要算法。标准顺序是：验证小说与
双重授权、计算范围、创建 queued 任务、提交事务、再交给 BackgroundTasks。
先提交保证后台线程能查到任务；派发失败则用独立会话补偿成 failed。数据库
局部唯一索引处理并发竞态，冲突转换为 HTTP 409。
"""

import logging
from datetime import datetime
from typing import Callable, Optional
from uuid import uuid4

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Path as PathParameter,
    Query,
    status,
)
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.analyzer.deepseek import (
    MAX_JSON_GENERATION_ATTEMPTS,
    AsyncDeepSeekProvider,
    DeepSeekProvider,
)
from backend.app.analyzer.llm import LLMProvider
from backend.app.analyzer.jobs import (
    mark_analysis_job_failed_with_sqlite_lock_retry,
)
from backend.app.analyzer.summaries import (
    SUMMARY_PROMPT_VERSION,
    SUMMARY_AUTO_RETRY_LIMIT,
    chapter_summary_source_hash,
    estimate_summary_generation_steps,
    run_summary_job,
)
from backend.app.core.config import (
    GENERATION_PROVIDERS,
    Settings,
    get_settings,
)
from backend.app.core.database import SessionLocal, get_db
from backend.app.models.analysis import AnalysisJob, ChapterSummary
from backend.app.models.novel import Chapter, Novel


router = APIRouter(tags=["analysis"])
logger = logging.getLogger(__name__)
ProviderFactory = Callable[[], LLMProvider]


# 请求只承载用户选择；任务状态由服务端决定，不能相信浏览器提交内部状态。
class StartSummariesRequest(BaseModel):
    """Explicit consent and refresh options for summary generation."""

    model_config = ConfigDict(extra="forbid")

    confirm_external_processing: StrictBool
    confirm_content_rights: StrictBool
    force: StrictBool = False


class AnalysisJobResponse(BaseModel):
    """Persistent progress for one background analysis task."""

    id: str
    novel_id: int
    job_type: str
    target_chapter_index: Optional[int]
    force: bool
    status: str
    progress_current: int
    progress_total: int
    error_message: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]


class ChapterSummaryResponse(BaseModel):
    """One saved structured chapter summary."""

    chapter_id: int
    chapter_index: int
    chapter_title: str
    summary: str
    key_events: list[str]
    plot_progression: str
    cliffhanger: Optional[str]
    model: str
    updated_at: datetime


class ChapterSummaryListResponse(BaseModel):
    """All completed chapter summaries for one novel."""

    items: list[ChapterSummaryResponse]
    total: int
    completed: int
    offset: int
    limit: int


class SummaryEstimateResponse(BaseModel):
    """Estimated external work shown before a user confirms generation."""

    novel_id: int
    chapter_count: int
    character_count: int
    cached_chapter_count: int
    pending_chapter_count: int
    estimated_generation_steps: int
    estimated_force_generation_steps: int
    maximum_request_attempts: int
    maximum_force_request_attempts: int
    concurrency_mode: str
    initial_concurrency: int
    maximum_concurrency: int
    effective_concurrency: int
    provider: str
    model: str
    sends_original_text: bool
    sends_chapter_titles: bool
    data_handling_notice: str
    terms_url: str
    privacy_url: str


class AnalysisJobListResponse(BaseModel):
    """Recent analysis jobs for one novel."""

    items: list[AnalysisJobResponse]
    total: int


def get_session_factory() -> Callable[[], Session]:
    """以依赖形式提供会话工厂，便于测试替换为隔离数据库。"""
    return SessionLocal


def get_llm_provider(
    settings: Settings = Depends(get_settings),
) -> LLMProvider:
    """Build the synchronous provider used by non-summary analyzers."""
    if not settings.deepseek_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "尚未配置生成模型 API 密钥，请前往“模型与密钥”设置"
                "（也可设置 DEEPSEEK_API_KEY）。"
            ),
        )
    return DeepSeekProvider(settings)


def get_summary_llm_provider(
    settings: Settings = Depends(get_settings),
) -> LLMProvider:
    """Build the async-capable provider used by summary workers."""
    if not settings.deepseek_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "尚未配置生成模型 API 密钥，请前往“模型与密钥”设置"
                "（也可设置 DEEPSEEK_API_KEY）。"
            ),
        )
    return AsyncDeepSeekProvider(settings)


def get_llm_provider_factory(
    settings: Settings = Depends(get_settings),
) -> ProviderFactory:
    """Defer async summary-client construction until consent succeeds."""
    return lambda: get_summary_llm_provider(settings)


def get_sync_llm_provider_factory(
    settings: Settings = Depends(get_settings),
) -> ProviderFactory:
    """Defer synchronous analysis-client construction until consent."""
    return lambda: get_llm_provider(settings)


def _job_response(job: AnalysisJob) -> AnalysisJobResponse:
    """把内部 AnalysisJob 复制为公开响应模型，避免直接序列化 ORM。"""
    return AnalysisJobResponse(
        id=job.id,
        novel_id=job.novel_id,
        job_type=job.job_type,
        target_chapter_index=job.target_chapter_index,
        force=bool(job.force_refresh),
        status=job.status,
        progress_current=job.progress_current,
        progress_total=job.progress_total,
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


def _ensure_no_active_report(db: Session, novel_id: int) -> None:
    """报告读取摘要期间禁止启动摘要变更；冲突时抛 HTTP 409。"""
    active_report = (
        db.query(AnalysisJob.id)
        .filter(
            AnalysisJob.novel_id == novel_id,
            AnalysisJob.job_type == "report",
            AnalysisJob.status.in_(("queued", "running")),
        )
        .first()
    )
    if active_report is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="报告生成期间不能更新摘要，请等待报告任务完成。",
        )


def _active_summary_job(
    db: Session,
    novel_id: int,
) -> Optional[AnalysisJob]:
    """返回某小说当前活动摘要任务，供友好冲突提示；无任务返回 None。"""
    return (
        db.query(AnalysisJob)
        .filter(
            AnalysisJob.novel_id == novel_id,
            AnalysisJob.job_type == "chapter_summaries",
            AnalysisJob.status.in_(("queued", "running")),
        )
        .order_by(AnalysisJob.created_at.desc())
        .first()
    )


def _close_provider(provider: LLMProvider) -> None:
    """Release an unused provider without masking the API response."""
    close = getattr(provider, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        logger.exception("Could not close an unused LLM provider.")


def _mark_committed_summary_job_failed(
    *,
    job_id: str,
    db: Session,
    session_factory: Callable[[], Session],
) -> bool:
    """Keep a committed job from remaining queued without a worker."""
    try:
        db.rollback()
    except Exception:
        logger.exception(
            "Could not reset the request session before terminalizing "
            "undispatched summary job %s.",
            job_id,
        )
    return mark_analysis_job_failed_with_sqlite_lock_retry(
        session_factory,
        job_id,
        "chapter_summaries",
        "摘要任务未能加入后台队列，请重试。",
        expected_status="queued",
        failure_context="background dispatch failure",
    )


# 派发发生在事务提交之后；否则后台线程可能先启动，却查询不到尚未提交的任务。
# 若提交后到派发之间出错，函数会补偿性把 queued 任务改成 failed。
def _schedule_summary_job(
    *,
    novel_id: int,
    progress_total: int,
    request: StartSummariesRequest,
    background_tasks: BackgroundTasks,
    db: Session,
    provider: LLMProvider,
    session_factory: Callable[[], Session],
    target_chapter_index: Optional[int] = None,
) -> AnalysisJobResponse:
    """创建并派发全书或单章摘要任务。

    ``target_chapter_index=None`` 表示全书；其余参数分别提供请求范围、进度、授权、
    后台容器、数据库和 provider 所有权。返回的任务 ID 供前端轮询。
    """
    active_job = _active_summary_job(db, novel_id)
    if active_job is not None:
        _close_provider(provider)
        if (
            active_job.target_chapter_index != target_chapter_index
            or bool(active_job.force_refresh) != request.force
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "该小说已有不同范围或刷新模式的摘要任务正在运行，"
                    "请等待其完成后重试。"
                ),
            )
        return _job_response(active_job)

    job = AnalysisJob(
        id=str(uuid4()),
        novel_id=novel_id,
        job_type="chapter_summaries",
        target_chapter_index=target_chapter_index,
        force_refresh=request.force,
        status="queued",
        progress_current=0,
        progress_total=progress_total,
    )
    job_id = job.id
    db.add(job)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        _close_provider(provider)
        if db.get(Novel, novel_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="未找到指定小说。",
            ) from error
        if (
            "UNIQUE constraint failed: "
            "analysis_jobs.novel_id, analysis_jobs.job_type"
            not in str(error.orig)
        ):
            raise
        active_job = _active_summary_job(db, novel_id)
        if active_job is None:
            raise
        if (
            active_job.target_chapter_index != target_chapter_index
            or bool(active_job.force_refresh) != request.force
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "该小说已有不同范围或刷新模式的摘要任务正在运行，"
                    "请等待其完成后重试。"
                ),
            )
        return _job_response(active_job)
    except Exception:
        db.rollback()
        _close_provider(provider)
        raise
    try:
        db.refresh(job)
        response = _job_response(job)
    except Exception:
        if not _mark_committed_summary_job_failed(
            job_id=job_id,
            db=db,
            session_factory=session_factory,
        ):
            logger.error(
                "Committed summary job %s remained queued after "
                "response preparation failed.",
                job_id,
            )
        _close_provider(provider)
        raise

    try:
        background_tasks.add_task(
            run_summary_job,
            job_id,
            provider,
            session_factory,
        )
    except Exception:
        if not _mark_committed_summary_job_failed(
            job_id=job_id,
            db=db,
            session_factory=session_factory,
        ):
            logger.error(
                "Committed summary job %s remained queued after "
                "background dispatch failed.",
                job_id,
            )
        _close_provider(provider)
        raise
    return response


@router.post(
    "/novels/{novel_id}/summaries",
    response_model=AnalysisJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
# HTTP 接口只创建任务并立即返回。耗时模型调用留在后台，前端轮询任务状态。
def start_chapter_summaries(
    request: StartSummariesRequest,
    background_tasks: BackgroundTasks,
    novel_id: int = PathParameter(..., ge=1),
    db: Session = Depends(get_db),
    provider_factory: ProviderFactory = Depends(get_llm_provider_factory),
    session_factory: Callable[[], Session] = Depends(get_session_factory),
) -> AnalysisJobResponse:
    """Start an idempotent background job for all chapter summaries."""
    novel = db.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到指定小说。",
        )
    if not request.confirm_external_processing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "生成摘要会把小说正文发送至外部生成模型 API，"
                "请先明确确认。"
            ),
        )
    if not request.confirm_content_rights:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "请确认你有权把小说标题和正文提交给外部生成模型 API，"
                "并已了解其数据处理条款。"
            ),
        )

    _ensure_no_active_report(db, novel_id)
    chapter_count = (
        db.query(func.count(Chapter.id))
        .filter(Chapter.novel_id == novel_id)
        .scalar()
        or 0
    )
    provider = provider_factory()
    return _schedule_summary_job(
        novel_id=novel_id,
        progress_total=chapter_count,
        request=request,
        background_tasks=background_tasks,
        db=db,
        provider=provider,
        session_factory=session_factory,
    )


@router.post(
    "/novels/{novel_id}/chapters/{chapter_index}/summary",
    response_model=AnalysisJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_single_chapter_summary(
    request: StartSummariesRequest,
    background_tasks: BackgroundTasks,
    novel_id: int = PathParameter(..., ge=1),
    chapter_index: int = PathParameter(..., ge=1),
    db: Session = Depends(get_db),
    provider_factory: ProviderFactory = Depends(get_llm_provider_factory),
    session_factory: Callable[[], Session] = Depends(get_session_factory),
) -> AnalysisJobResponse:
    """Start summary generation for one selected chapter."""
    chapter_exists = (
        db.query(Chapter.id)
        .filter(
            Chapter.novel_id == novel_id,
            Chapter.chapter_index == chapter_index,
        )
        .scalar()
    )
    if chapter_exists is None:
        if db.get(Novel, novel_id) is None:
            detail = "未找到指定小说。"
        else:
            detail = "未找到指定章节。"
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=detail,
        )
    if not request.confirm_external_processing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "生成摘要会把小说正文发送至外部生成模型 API，"
                "请先明确确认。"
            ),
        )
    if not request.confirm_content_rights:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "请确认你有权把小说标题和正文提交给外部生成模型 API，"
                "并已了解其数据处理条款。"
            ),
        )
    _ensure_no_active_report(db, novel_id)
    provider = provider_factory()
    return _schedule_summary_job(
        novel_id=novel_id,
        progress_total=1,
        request=request,
        background_tasks=background_tasks,
        db=db,
        provider=provider,
        session_factory=session_factory,
        target_chapter_index=chapter_index,
    )


@router.get(
    "/novels/{novel_id}/summaries",
    response_model=ChapterSummaryListResponse,
)
def list_chapter_summaries(
    novel_id: int = PathParameter(..., ge=1),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> ChapterSummaryListResponse:
    """Return completed summaries without loading chapter bodies."""
    novel = db.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到指定小说。",
        )

    completed = (
        db.query(func.count(ChapterSummary.id))
        .join(Chapter, ChapterSummary.chapter_id == Chapter.id)
        .filter(Chapter.novel_id == novel_id)
        .scalar()
        or 0
    )
    rows = (
        db.query(
            Chapter.id,
            Chapter.chapter_index,
            Chapter.title,
            ChapterSummary.summary,
            ChapterSummary.key_events,
            ChapterSummary.plot_progression,
            ChapterSummary.cliffhanger,
            ChapterSummary.model,
            ChapterSummary.updated_at,
        )
        .join(ChapterSummary, ChapterSummary.chapter_id == Chapter.id)
        .filter(Chapter.novel_id == novel_id)
        .order_by(Chapter.chapter_index)
        .offset(offset)
        .limit(limit)
        .all()
    )
    items = [
        ChapterSummaryResponse(
            chapter_id=row.id,
            chapter_index=row.chapter_index,
            chapter_title=row.title,
            summary=row.summary,
            key_events=list(row.key_events),
            plot_progression=row.plot_progression,
            cliffhanger=row.cliffhanger,
            model=row.model,
            updated_at=row.updated_at,
        )
        for row in rows
    ]
    return ChapterSummaryListResponse(
        items=items,
        total=novel.chapter_count,
        completed=completed,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/novels/{novel_id}/chapters/{chapter_index}/summary",
    response_model=ChapterSummaryResponse,
)
def get_chapter_summary(
    novel_id: int = PathParameter(..., ge=1),
    chapter_index: int = PathParameter(..., ge=1),
    db: Session = Depends(get_db),
) -> ChapterSummaryResponse:
    """Return one chapter summary without loading chapter content."""
    row = (
        db.query(
            Chapter.id,
            Chapter.chapter_index,
            Chapter.title,
            ChapterSummary.summary,
            ChapterSummary.key_events,
            ChapterSummary.plot_progression,
            ChapterSummary.cliffhanger,
            ChapterSummary.model,
            ChapterSummary.updated_at,
        )
        .outerjoin(
            ChapterSummary,
            ChapterSummary.chapter_id == Chapter.id,
        )
        .filter(
            Chapter.novel_id == novel_id,
            Chapter.chapter_index == chapter_index,
        )
        .one_or_none()
    )
    if row is None:
        if db.get(Novel, novel_id) is None:
            detail = "未找到指定小说。"
        else:
            detail = "未找到指定章节。"
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=detail,
        )
    if row.summary is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="该章节尚未生成摘要。",
        )
    return ChapterSummaryResponse(
        chapter_id=row.id,
        chapter_index=row.chapter_index,
        chapter_title=row.title,
        summary=row.summary,
        key_events=list(row.key_events),
        plot_progression=row.plot_progression,
        cliffhanger=row.cliffhanger,
        model=row.model,
        updated_at=row.updated_at,
    )


@router.get(
    "/novels/{novel_id}/summaries/estimate",
    response_model=SummaryEstimateResponse,
)
def get_summary_estimate(
    novel_id: int = PathParameter(..., ge=1),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> SummaryEstimateResponse:
    """Estimate work and disclosure before starting external generation."""
    novel = db.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到指定小说。",
        )
    chapter_count = 0
    character_count = 0
    generation_steps = 0
    force_generation_steps = 0
    cached_chapter_count = 0
    rows = (
        db.query(Chapter, ChapterSummary)
        .outerjoin(ChapterSummary, ChapterSummary.chapter_id == Chapter.id)
        .filter(Chapter.novel_id == novel_id)
        .order_by(Chapter.chapter_index)
        .yield_per(10)
    )
    for chapter, saved_summary in rows:
        chapter_count += 1
        character_count += int(chapter.character_count)
        chapter_steps = estimate_summary_generation_steps(chapter.content)
        force_generation_steps += chapter_steps
        summary_current = bool(
            saved_summary is not None
            and saved_summary.source_hash == chapter_summary_source_hash(
                chapter.title,
                chapter.content,
            )
            and saved_summary.prompt_version == SUMMARY_PROMPT_VERSION
            and saved_summary.model == settings.deepseek_model
        )
        if summary_current:
            cached_chapter_count += 1
        else:
            generation_steps += chapter_steps

    attempt_multiplier = (
        settings.deepseek_max_attempts
        * MAX_JSON_GENERATION_ATTEMPTS
        * (
            1 + SUMMARY_AUTO_RETRY_LIMIT
            if settings.summary_concurrency_mode == "auto"
            else 1
        )
    )
    maximum_request_attempts = generation_steps * attempt_multiplier
    maximum_force_request_attempts = (
        force_generation_steps * attempt_multiplier
    )
    return SummaryEstimateResponse(
        novel_id=novel_id,
        chapter_count=chapter_count,
        character_count=character_count,
        cached_chapter_count=cached_chapter_count,
        pending_chapter_count=chapter_count - cached_chapter_count,
        estimated_generation_steps=generation_steps,
        estimated_force_generation_steps=force_generation_steps,
        maximum_request_attempts=maximum_request_attempts,
        maximum_force_request_attempts=maximum_force_request_attempts,
        concurrency_mode=settings.summary_concurrency_mode,
        initial_concurrency=settings.summary_initial_concurrency,
        maximum_concurrency=(
            settings.summary_effective_concurrency_limit
        ),
        effective_concurrency=min(
            chapter_count,
            settings.summary_initial_concurrency,
        ),
        provider=settings.generation_provider,
        model=settings.deepseek_model,
        sends_original_text=True,
        sends_chapter_titles=True,
        data_handling_notice=(
            (
                "章节标题和正文将由 DeepSeek 在中国处理。根据 DeepSeek "
                "当前条款，输入可能被缓存、留存并用于服务改进或模型优化；"
                "请仅提交你有权处理的内容。"
            )
            if settings.generation_provider == "deepseek"
            else (
                "章节标题和正文将发送至 "
                f"{GENERATION_PROVIDERS[settings.generation_provider]} 处理。"
                "请仅提交你有权处理的内容，并以所选供应商的条款为准。"
            )
        ),
        terms_url=(
            "https://cdn.deepseek.com/policies/en-US/"
            "deepseek-open-platform-terms-of-service.html"
            if settings.generation_provider == "deepseek"
            else settings.deepseek_base_url
        ),
        privacy_url=(
            "https://cdn.deepseek.com/policies/en-US/"
            "deepseek-privacy-policy.html"
            if settings.generation_provider == "deepseek"
            else settings.deepseek_base_url
        ),
    )


@router.get(
    "/novels/{novel_id}/analysis-jobs",
    response_model=AnalysisJobListResponse,
)
def list_analysis_jobs(
    novel_id: int = PathParameter(..., ge=1),
    job_type: Optional[str] = Query(
        None,
        min_length=1,
        max_length=40,
    ),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> AnalysisJobListResponse:
    """Return recent persisted jobs so a refreshed UI can recover."""
    if db.get(Novel, novel_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到指定小说。",
        )
    query = db.query(AnalysisJob).filter(
        AnalysisJob.novel_id == novel_id
    )
    if job_type is not None:
        query = query.filter(AnalysisJob.job_type == job_type)
    total = query.count()
    jobs = (
        query.order_by(
            AnalysisJob.created_at.desc(),
            AnalysisJob.id.desc(),
        )
        .limit(limit)
        .all()
    )
    return AnalysisJobListResponse(
        items=[_job_response(job) for job in jobs],
        total=total,
    )


@router.get(
    "/analysis-jobs/{job_id}",
    response_model=AnalysisJobResponse,
)
def get_analysis_job(
    job_id: str,
    db: Session = Depends(get_db),
) -> AnalysisJobResponse:
    """Return current progress for one analysis job."""
    job = db.get(AnalysisJob, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到指定分析任务。",
        )
    return _job_response(job)
