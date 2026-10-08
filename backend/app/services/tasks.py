"""共享供应商依赖、公开任务响应及派发失败补偿。业务冲突规则保留在各服务。"""
import logging
from datetime import datetime
from typing import Callable, Optional
from pydantic import BaseModel
from backend.app.analyzer.llm import LLMProvider
from backend.app.analyzer.jobs import mark_analysis_job_failed_with_sqlite_lock_retry
from backend.app.models.analysis import AnalysisJob
logger = logging.getLogger(__name__)
ProviderFactory = Callable[[], LLMProvider]
DEEPSEEK_TERMS_URL = "https://cdn.deepseek.com/policies/zh-CN/deepseek-terms-of-use.html"
DEEPSEEK_PRIVACY_URL = "https://cdn.deepseek.com/policies/zh-CN/deepseek-privacy-policy.html"
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


def _close_provider(provider: LLMProvider) -> None:
    """Release an unused provider without masking the API response."""
    close = getattr(provider, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        logger.exception("Could not close an unused LLM provider.")


def fail_dispatch(*, db, session_factory, job_id, job_type, message):
    """结束已提交但未交给 worker 的任务；补偿使用独立会话和锁重试。"""
    try:
        db.rollback()
    except Exception:
        logger.error("Could not reset request session for %s", job_id)
    return mark_analysis_job_failed_with_sqlite_lock_retry(
        session_factory, job_id, job_type, message, expected_status="queued",
        failure_context="background dispatch failure")

from backend.app.services.dependencies import get_session_factory as get_session_factory, get_llm_provider as get_llm_provider, get_summary_llm_provider as get_summary_llm_provider, get_llm_provider_factory as get_llm_provider_factory, get_sync_llm_provider_factory as get_sync_llm_provider_factory
