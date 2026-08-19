"""全书报告的输入估算、后台启动和当前快照读取接口。

报告不重发正文，而是校验章节摘要来源并可选使用当前人物快照。准备输入、界面
披露和后台执行共享同一规则。成功才原子替换旧报告，刷新失败不清空已有结果。
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
    status,
)
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.analyzer.deepseek import MAX_JSON_GENERATION_ATTEMPTS
from backend.app.analyzer.jobs import (
    mark_analysis_job_failed_with_sqlite_lock_retry,
)
from backend.app.analyzer.llm import LLMProvider
from backend.app.analyzer.reports import (
    NovelReportPayload,
    ReportSourceError,
    estimate_report_generation_steps,
    prepare_report_source,
    report_is_current,
    run_report_job,
)
from backend.app.api.characters import (
    DEEPSEEK_PRIVACY_URL,
    DEEPSEEK_TERMS_URL,
)
from backend.app.api.summaries import (
    AnalysisJobResponse,
    ProviderFactory,
    _job_response,
    get_sync_llm_provider_factory,
    get_session_factory,
)
from backend.app.core.config import (
    GENERATION_PROVIDERS,
    Settings,
    get_settings,
)
from backend.app.core.database import get_db
from backend.app.models.analysis import AnalysisJob
from backend.app.models.novel import Novel
from backend.app.models.report import NovelReport


router = APIRouter(tags=["reports"])
logger = logging.getLogger(__name__)


# ``force`` 仅忽略可复用报告，不会绕过授权或来源完整性检查。
class StartReportRequest(BaseModel):
    """Explicit external-processing and paid-refresh consent."""

    model_config = ConfigDict(extra="forbid")

    confirm_external_processing: StrictBool
    confirm_content_rights: StrictBool
    force: StrictBool = False
    confirm_force_refresh: StrictBool = False


class ReportEstimateResponse(BaseModel):
    """Bounded external work and the exact derived-data disclosure."""

    novel_id: int
    chapter_count: int
    summary_count: int
    character_count: int
    includes_character_analysis: bool
    estimated_generation_steps: int
    maximum_request_attempts: int
    estimated_force_generation_steps: int
    maximum_force_request_attempts: int
    provider: str
    model: str
    sends_original_text: bool
    sends_chapter_titles: bool
    sends_derived_summaries: bool
    sends_derived_character_data: bool
    data_handling_notice: str
    terms_url: str
    privacy_url: str
    up_to_date: bool


class ReportResponse(BaseModel):
    """One saved structured report and safe provenance metadata."""

    novel_id: int
    report: NovelReportPayload
    model: str
    prompt_version: str
    summary_count: int
    character_count: int
    generation_steps: int
    includes_character_analysis: bool
    up_to_date: bool
    created_at: datetime
    updated_at: datetime


def _source_or_http_error(
    *,
    db: Session,
    novel_id: int,
    model: str,
) -> object:
    """准备已验证报告输入，把 ``ReportSourceError`` 翻译成 HTTP 409。

    ``db`` 和 ``novel_id`` 定位派生数据，``model`` 参与缓存来源判断。返回实际为
    PreparedReportSource；类型写 object 是为保持依赖边界，调用者立即读取其字段。
    """
    try:
        return prepare_report_source(
            db,
            novel_id=novel_id,
            expected_model=model,
        )
    except ReportSourceError as error:
        message = str(error)
        code = (
            status.HTTP_404_NOT_FOUND
            if message in {"未找到指定小说。"}
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(status_code=code, detail=message) from None


def _active_report_job(
    db: Session,
    novel_id: int,
) -> Optional[AnalysisJob]:
    """查询该小说活动报告任务；没有时返回 None。"""
    return (
        db.query(AnalysisJob)
        .filter(
            AnalysisJob.novel_id == novel_id,
            AnalysisJob.job_type == "report",
            AnalysisJob.status.in_(("queued", "running")),
        )
        .order_by(AnalysisJob.created_at.desc())
        .first()
    )


def _close_provider(provider: LLMProvider) -> None:
    """尽力调用报告 provider 的可选 close，关闭异常仅写日志。"""
    close = getattr(provider, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        logger.exception("Could not close an unused report provider.")


def _mark_undispatched_report_failed(
    *,
    db: Session,
    session_factory: Callable[[], Session],
    job_id: str,
) -> bool:
    """派发失败后终结已提交的 queued 报告任务，返回补偿是否达成。"""
    try:
        db.rollback()
    except Exception:
        logger.exception(
            "Could not reset request session for report job %s.",
            job_id,
        )
    return mark_analysis_job_failed_with_sqlite_lock_retry(
        session_factory,
        job_id,
        "report",
        "报告任务未能加入后台队列，请重试。",
        expected_status="queued",
        failure_context="background dispatch failure",
    )


# 报告任务先入库后派发；补偿逻辑消除“显示排队但没有工作线程”的状态。
def _schedule_report_job(
    *,
    novel_id: int,
    progress_total: int,
    request: StartReportRequest,
    background_tasks: BackgroundTasks,
    db: Session,
    provider: LLMProvider,
    session_factory: Callable[[], Session],
) -> AnalysisJobResponse:
    """持久化报告任务并安全移交生成客户端。

    ``novel_id/progress_total/request`` 定义范围、进度和 force 授权；FastAPI
    ``background_tasks`` 接收 worker；``db/session_factory`` 分别服务当前事务和
    补偿事务。成功返回任务响应，派发前后失败都会关闭 provider 并终结任务。
    """
    provider_transferred = False
    try:
        active = _active_report_job(db, novel_id)
        if active is not None:
            if bool(active.force_refresh) != bool(request.force):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "该小说已有不同刷新模式的报告任务正在运行，"
                        "请等待其完成后重试。"
                    ),
                )
            return _job_response(active)

        job = AnalysisJob(
            id=str(uuid4()),
            novel_id=novel_id,
            job_type="report",
            target_chapter_index=None,
            force_refresh=bool(request.force),
            status="queued",
            progress_current=0,
            progress_total=progress_total,
        )
        db.add(job)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            active = _active_report_job(db, novel_id)
            if active is None:
                raise
            if bool(active.force_refresh) != bool(request.force):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "该小说已有不同刷新模式的报告任务正在运行，"
                        "请等待其完成后重试。"
                    ),
                )
            return _job_response(active)
        try:
            db.refresh(job)
            response = _job_response(job)
        except Exception:
            if not _mark_undispatched_report_failed(
                db=db,
                session_factory=session_factory,
                job_id=job.id,
            ):
                logger.error(
                    "Committed report job %s remained queued after "
                    "response preparation failed.",
                    job.id,
                )
            raise
        try:
            background_tasks.add_task(
                run_report_job,
                job.id,
                provider,
                session_factory,
            )
        except Exception:
            if not _mark_undispatched_report_failed(
                db=db,
                session_factory=session_factory,
                job_id=job.id,
            ):
                logger.error(
                    "Report job %s stayed queued after dispatch failure.",
                    job.id,
                )
            raise
        provider_transferred = True
        return response
    finally:
        if not provider_transferred:
            _close_provider(provider)


def _build_report_estimate(
    *,
    db: Session,
    novel_id: int,
    settings: Settings,
) -> ReportEstimateResponse:
    """生成报告授权页所需的纯本地估算。

    ``db/novel_id`` 定位并复核全部当前摘要与可选人物快照，``settings`` 提供当前
    生成模型。返回摘要/人物数量、批次、预计步骤、最大尝试和外发字符；不构造
    provider，不产生模型费用。
    """
    source = _source_or_http_error(
        db=db,
        novel_id=novel_id,
        model=settings.deepseek_model,
    )
    saved = (
        db.query(NovelReport)
        .filter(NovelReport.novel_id == novel_id)
        .one_or_none()
    )
    up_to_date = report_is_current(
        saved,
        source=source,
        model=settings.deepseek_model,
    )
    force_steps = estimate_report_generation_steps(source)
    estimated_steps = 0 if up_to_date else force_steps
    attempt_multiplier = (
        settings.deepseek_max_attempts
        * MAX_JSON_GENERATION_ATTEMPTS
    )
    return ReportEstimateResponse(
        novel_id=novel_id,
        chapter_count=source.chapter_count,
        summary_count=source.chapter_count,
        character_count=source.total_character_count,
        includes_character_analysis=(
            source.includes_character_analysis
        ),
        estimated_generation_steps=estimated_steps,
        maximum_request_attempts=(
            estimated_steps * attempt_multiplier
        ),
        estimated_force_generation_steps=force_steps,
        maximum_force_request_attempts=(
            force_steps * attempt_multiplier
        ),
        provider=settings.generation_provider,
        model=settings.deepseek_model,
        sends_original_text=False,
        sends_chapter_titles=True,
        sends_derived_summaries=True,
        sends_derived_character_data=(
            source.includes_character_analysis
        ),
        data_handling_notice=(
            (
                "章节标题、已生成的章节摘要，以及可选的当前人物档案和"
                "关系数据将发送至 DeepSeek，并可能在中国处理；小说正文"
                "不会在报告阶段再次发送。根据 DeepSeek 当前条款，输入"
                "可能被缓存、留存并用于服务改进或模型优化；请仅提交你"
                "有权处理的内容。"
            )
            if settings.generation_provider == "deepseek"
            else (
                "章节标题、摘要及可选人物数据将发送至 "
                f"{GENERATION_PROVIDERS[settings.generation_provider]}；"
                "报告阶段不会再次发送小说正文。"
            )
        ),
        terms_url=(
            DEEPSEEK_TERMS_URL
            if settings.generation_provider == "deepseek"
            else settings.deepseek_base_url
        ),
        privacy_url=(
            DEEPSEEK_PRIVACY_URL
            if settings.generation_provider == "deepseek"
            else settings.deepseek_base_url
        ),
        up_to_date=up_to_date,
    )


@router.get(
    "/novels/{novel_id}/report/estimate",
    response_model=ReportEstimateResponse,
)
def get_report_estimate(
    novel_id: int = PathParameter(..., ge=1),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> ReportEstimateResponse:
    """Describe report prerequisites, data transfer, and API bounds."""
    return _build_report_estimate(
        db=db,
        novel_id=novel_id,
        settings=settings,
    )


@router.post(
    "/novels/{novel_id}/report",
    response_model=AnalysisJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
# 路由只做授权、来源准备和任务创建；昂贵生成在后台执行。
def start_report(
    request: StartReportRequest,
    background_tasks: BackgroundTasks,
    novel_id: int = PathParameter(..., ge=1),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
    provider_factory: ProviderFactory = Depends(
        get_sync_llm_provider_factory
    ),
    session_factory: Callable[[], Session] = Depends(get_session_factory),
) -> AnalysisJobResponse:
    """Start one external report job after all explicit consent gates."""
    if db.get(Novel, novel_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到指定小说。",
        )
    if not request.confirm_external_processing:
        provider_label = GENERATION_PROVIDERS[settings.generation_provider]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "报告会把章节标题、派生摘要及可选人物分析数据发送至 "
                f"{provider_label} API，请先明确确认。"
            ),
        )
    if not request.confirm_content_rights:
        provider_label = GENERATION_PROVIDERS[settings.generation_provider]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "请确认你有权把这些小说派生数据提交给 "
                f"{provider_label} API，"
                "并已了解其数据处理条款。"
            ),
        )
    if request.force and not request.confirm_force_refresh:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="强制刷新会重复产生 API 调用，请再次明确确认。",
        )
    active = _active_report_job(db, novel_id)
    if active is not None:
        if bool(active.force_refresh) != bool(request.force):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "该小说已有不同刷新模式的报告任务正在运行，"
                    "请等待其完成后重试。"
                ),
            )
        return _job_response(active)

    changing_sources = (
        db.query(AnalysisJob.id)
        .filter(
            AnalysisJob.novel_id == novel_id,
            AnalysisJob.job_type.in_(("chapter_summaries", "characters")),
            AnalysisJob.status.in_(("queued", "running")),
        )
        .first()
    )
    if changing_sources is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="摘要或人物分析正在更新，请等待其完成后再生成报告。",
        )

    estimate = _build_report_estimate(
        db=db,
        novel_id=novel_id,
        settings=settings,
    )
    provider = provider_factory()
    return _schedule_report_job(
        novel_id=novel_id,
        progress_total=(
            estimate.estimated_force_generation_steps
            if request.force
            else estimate.estimated_generation_steps
        ),
        request=request,
        background_tasks=background_tasks,
        db=db,
        provider=provider,
        session_factory=session_factory,
    )


@router.get(
    "/novels/{novel_id}/report",
    response_model=ReportResponse,
)
def get_report(
    novel_id: int = PathParameter(..., ge=1),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> ReportResponse:
    """Return the saved report even when its inputs later become stale."""
    if db.get(Novel, novel_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到指定小说。",
        )
    saved = (
        db.query(NovelReport)
        .filter(NovelReport.novel_id == novel_id)
        .one_or_none()
    )
    if saved is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="该小说尚未生成分析报告。",
        )
    try:
        payload = NovelReportPayload.model_validate(saved.report_data)
    except ValidationError:
        logger.error(
            "Stored report %s failed schema validation.",
            saved.id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="已保存的报告格式无效，请重新生成。",
        ) from None
    try:
        source = prepare_report_source(
            db,
            novel_id=novel_id,
            expected_model=settings.deepseek_model,
        )
    except ReportSourceError:
        up_to_date = False
    else:
        up_to_date = report_is_current(
            saved,
            source=source,
            model=settings.deepseek_model,
        )
    return ReportResponse(
        novel_id=novel_id,
        report=payload,
        model=saved.model,
        prompt_version=saved.prompt_version,
        summary_count=saved.summary_count,
        character_count=saved.character_count,
        generation_steps=saved.generation_steps,
        includes_character_analysis=bool(
            saved.includes_character_analysis
        ),
        up_to_date=up_to_date,
        created_at=saved.created_at,
        updated_at=saved.updated_at,
    )
