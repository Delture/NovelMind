"""终态必须可靠落盘，否则界面会一直转圈；SQLite 锁冲突用新的短会话有界重试。"""

import logging
from typing import Callable, Any

from backend.app.core.sqlite_retry import retry_sqlite
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.app.core.time import utc_now
from backend.app.models.analysis import AnalysisJob


logger = logging.getLogger(__name__)

ANALYSIS_JOB_SQLITE_MAX_ATTEMPTS = 4
ANALYSIS_JOB_SQLITE_RETRY_BASE_SECONDS = 0.05


from backend.app.core.sqlite_retry import is_sqlite_lock_error as is_sqlite_lock_error


from backend.app.core.sqlite_retry import (
    sleep_before_retry as _sleep_before_sqlite_retry,
)


def mark_analysis_job_failed_with_sqlite_lock_retry(
    # 调用者传 expected_status，UPDATE 同时检查旧状态，形成轻量比较并交换：
    # 已被其他线程完成的任务不会被迟到的异常覆盖成 failed。
    session_factory: Callable[[], Session],
    job_id: str,
    job_type: str,
    error_message: str,
    *,
    expected_status: str,
    failure_context: str,
) -> bool:
    if expected_status not in {"queued", "running"}:
        raise ValueError("expected_status must be queued or running")

    def terminalize():
        with session_factory() as status_db:
            updated = (
                status_db.query(AnalysisJob)
                .filter(
                    AnalysisJob.id == job_id,
                    AnalysisJob.job_type == job_type,
                    AnalysisJob.status == expected_status,
                )
                .update(
                    {
                        AnalysisJob.status: "failed",
                        AnalysisJob.error_message: error_message,
                        AnalysisJob.completed_at: utc_now(),
                    },
                    synchronize_session=False,
                )
            )
            status_db.commit()
            if updated == 1:
                return True
            job = status_db.get(AnalysisJob, job_id)
            return job is None or job.status != expected_status

    try:
        return retry_sqlite(terminalize, sleep=_sleep_before_sqlite_retry)
    except Exception:
        logger.error(
            "Could not mark %s analysis job %s failed after %s.",
            job_type,
            job_id,
            failure_context,
        )
        return False


def claim_analysis_job(
    db, job_id, job_type, claimed_at, *, sleep=_sleep_before_sqlite_retry
):
    """统一 queued→running 的条件事务；失败先回滚再按写锁策略重试。"""
    from backend.app.core.sqlite_retry import retry_sqlite

    def claim():
        try:
            count = (
                db.query(AnalysisJob)
                .filter(
                    AnalysisJob.id == job_id,
                    AnalysisJob.job_type == job_type,
                    AnalysisJob.status == "queued",
                )
                .update(
                    {
                        AnalysisJob.status: "running",
                        AnalysisJob.progress_current: 0,
                        AnalysisJob.error_message: None,
                        AnalysisJob.started_at: claimed_at,
                        AnalysisJob.completed_at: None,
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            return int(count)
        except OperationalError:
            db.rollback()
            raise

    return retry_sqlite(claim, sleep=sleep)


def advance_analysis_job(
    db: Session, job_id: str, *, expand_total: bool = False
) -> bool:
    """统一进度事务；人物动态步骤可扩大总数，其他任务不越过估算上界。"""
    from sqlalchemy import case

    query = db.query(AnalysisJob).filter(
        AnalysisJob.id == job_id, AnalysisJob.status == "running"
    )
    if not expand_total:
        query = query.filter(AnalysisJob.progress_current < AnalysisJob.progress_total)
    next_progress = AnalysisJob.progress_current + 1
    values: dict[Any, Any] = {AnalysisJob.progress_current: next_progress}
    if expand_total:
        values[AnalysisJob.progress_total] = case(
            (AnalysisJob.progress_total < next_progress, next_progress),
            else_=AnalysisJob.progress_total,
        )
    updated = query.update({**values}, synchronize_session=False)
    if updated != 1:
        db.rollback()
        return False
    db.commit()
    return True
