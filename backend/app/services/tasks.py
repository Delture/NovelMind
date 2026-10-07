"""共享供应商依赖、公开任务响应及派发失败补偿。业务冲突规则保留在各服务。"""
import logging
from datetime import datetime
from typing import Callable, Optional
from fastapi import Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from backend.app.analyzer.deepseek import DeepSeekProvider, AsyncDeepSeekProvider
from backend.app.analyzer.llm import LLMProvider
from backend.app.analyzer.jobs import mark_analysis_job_failed_with_sqlite_lock_retry
from backend.app.core.config import Settings, get_settings
from backend.app.core.database import SessionLocal
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
