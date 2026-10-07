"""人物分析 worker 阶段；保留有界抽取、确定性统计与失败原子性。"""
import logging
import time
from datetime import datetime
from functools import partial
from typing import Callable, Optional
from sqlalchemy import case
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from backend.app.analyzer.jobs import (
    is_sqlite_lock_error,
    mark_analysis_job_failed_with_sqlite_lock_retry,
)
from backend.app.analyzer.llm import LLMProvider, LLMProviderError
from backend.app.core.time import utc_now
from backend.app.models.analysis import AnalysisJob
from backend.app.models.novel import Novel
from .payloads import (
    _CharacterJobCancelled,
    CHARACTER_JOB_SQLITE_MAX_ATTEMPTS,
    CHARACTER_JOB_SQLITE_RETRY_BASE_SECONDS,
    logger,
)
from .extraction import _planned_character_generation_steps
from .persistence import analyze_novel_characters

logger = logging.getLogger(__name__)

def _sleep_before_claim_retry(failed_attempt: int) -> None:
    """领取人物任务遇 SQLite 写锁后按次数执行短退避。"""
    time.sleep(
        CHARACTER_JOB_SQLITE_RETRY_BASE_SECONDS
        * (2 ** (failed_attempt - 1))
    )


def _claim_character_job(
    db: Session,
    job_id: str,
    claimed_at: datetime,
) -> int:
    """有界 SQLite 锁重试地领取人物任务。

    db 是 worker 会话，job_id 定位任务，claimed_at 写入开始时间。条件 UPDATE 只
    接受 queued，因此重复派发仅一个 worker 得到返回 1；已被领取/结束返回 0，
    非锁数据库错误继续抛出。
    """
    for attempt in range(1, CHARACTER_JOB_SQLITE_MAX_ATTEMPTS + 1):
        try:
            claimed = (
                db.query(AnalysisJob)
                .filter(
                    AnalysisJob.id == job_id,
                    AnalysisJob.job_type == "characters",
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
            return int(claimed)
        except OperationalError as error:
            db.rollback()
            if (
                not is_sqlite_lock_error(error)
                or attempt == CHARACTER_JOB_SQLITE_MAX_ATTEMPTS
            ):
                raise
            logger.warning(
                "Character job %s claim hit a SQLite write lock "
                "(attempt %s/%s).",
                job_id,
                attempt,
                CHARACTER_JOB_SQLITE_MAX_ATTEMPTS,
            )
            _sleep_before_claim_retry(attempt)
    return 0


def _character_job_has_live_source(
    session_factory: Callable[[], Session],
    *,
    job_id: str,
    novel_id: int,
) -> bool:
    """确认人物任务仍运行且小说仍存在，供每次模型调用前取消检测。"""
    with session_factory() as status_db:
        return (
            status_db.query(AnalysisJob.id)
            .join(Novel, Novel.id == AnalysisJob.novel_id)
            .filter(
                AnalysisJob.id == job_id,
                AnalysisJob.job_type == "characters",
                AnalysisJob.status == "running",
                Novel.id == novel_id,
            )
            .first()
            is not None
        )


def _close_provider(provider: LLMProvider) -> None:
    """调用人物 provider 的可选 close；异常只记录日志。"""
    close = getattr(provider, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        logger.exception("Could not close the character provider.")


def run_character_job(
    job_id: str,
    provider: LLMProvider,
    session_factory: Callable[[], Session],
) -> None:
    """Execute one full-book character job and release its provider."""
    try:
        _run_character_job(job_id, provider, session_factory)
    finally:
        _close_provider(provider)


def _run_character_job(
# worker 必须重新从数据库读取任务范围，不能信任进程内回调参数作为事实来源。
    job_id: str,
    provider: LLMProvider,
    session_factory: Callable[[], Session],
) -> None:
    """领取任务、准备章节、归并/推理并原子保存人物快照。

    网络调用不持有写事务；异常由外层写 failed，旧快照保持可用。
    """
    with session_factory() as db:
        try:
            claimed = _claim_character_job(db, job_id, utc_now())
        except Exception as error:
            db.rollback()
            locked = (
                isinstance(error, OperationalError)
                and is_sqlite_lock_error(error)
            )
            if locked:
                logger.error(
                    "Could not claim character job %s because the "
                    "SQLite database remained locked.",
                    job_id,
                )
            else:
                logger.exception(
                    "Could not claim character job %s.",
                    job_id,
                )
            message = (
                "人物分析任务无法获取本地数据库写锁，请重试。"
                if locked
                else "人物分析任务启动失败，请查看后端日志后重试。"
            )
            mark_analysis_job_failed_with_sqlite_lock_retry(
                session_factory,
                job_id,
                "characters",
                message,
                expected_status="queued",
                failure_context="claim failure",
            )
            return
        if claimed != 1:
            return

        continuation_check: Optional[Callable[[], bool]] = None
        try:
            scope = (
                db.query(
                    AnalysisJob.novel_id,
                    AnalysisJob.target_chapter_index,
                    AnalysisJob.force_refresh,
                )
                .filter(AnalysisJob.id == job_id)
                .one_or_none()
            )
            if scope is None:
                return
            novel_id, target_chapter_index, force_refresh = scope
            if target_chapter_index is not None:
                raise RuntimeError(
                    "人物分析只支持完整小说任务。"
                )
            novel = db.get(Novel, novel_id)
            if novel is None:
                return
            continuation_check = partial(
                _character_job_has_live_source,
                session_factory,
                job_id=job_id,
                novel_id=novel_id,
            )
            planned_steps = _planned_character_generation_steps(
                db,
                novel,
                provider_model=provider.model,
                force=bool(force_refresh),
            )
            initialized = (
                db.query(AnalysisJob)
                .filter(
                    AnalysisJob.id == job_id,
                    AnalysisJob.status == "running",
                )
                .update(
                    {
                        AnalysisJob.progress_current: 0,
                        AnalysisJob.progress_total: planned_steps,
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if initialized != 1:
                return

            def record_generation_step() -> None:
                """模型每完成一步便以独立短事务推进任务进度。

                若人物消歧产生估算外步骤，先增加 progress_total 再增加 current，
                始终满足数据库 ``current <= total``。SQLite 锁按有界策略重试；任务
                已取消或删除时抛内部取消异常。
                """
                for attempt in range(
                    1, CHARACTER_JOB_SQLITE_MAX_ATTEMPTS + 1
                ):
                    try:
                        with session_factory() as progress_db:
                            next_progress = (
                                AnalysisJob.progress_current + 1
                            )
                            progressed = (
                                progress_db.query(AnalysisJob)
                                .filter(
                                    AnalysisJob.id == job_id,
                                    AnalysisJob.status == "running",
                                )
                                .update(
                                    {
                                        AnalysisJob.progress_current: (
                                            next_progress
                                        ),
                                        AnalysisJob.progress_total: case(
                                            (
                                                AnalysisJob.progress_total
                                                < next_progress,
                                                next_progress,
                                            ),
                                            else_=(
                                                AnalysisJob.progress_total
                                            ),
                                        ),
                                    },
                                    synchronize_session=False,
                                )
                            )
                            progress_db.commit()
                        if progressed != 1:
                            raise _CharacterJobCancelled()
                        return
                    except OperationalError as error:
                        if (
                            not is_sqlite_lock_error(error)
                            or attempt
                            == CHARACTER_JOB_SQLITE_MAX_ATTEMPTS
                        ):
                            raise
                        _sleep_before_claim_retry(attempt)

            analyze_novel_characters(
                db,
                novel,
                provider,
                force=bool(force_refresh),
                persist_extractions_incrementally=True,
                should_continue=continuation_check,
                on_generation_step=record_generation_step,
            )
            completed = (
                db.query(AnalysisJob)
                .filter(
                    AnalysisJob.id == job_id,
                    AnalysisJob.status == "running",
                )
                .update(
                    {
                        AnalysisJob.status: "succeeded",
                        AnalysisJob.progress_total: (
                            AnalysisJob.progress_current
                        ),
                        AnalysisJob.completed_at: utc_now(),
                    },
                    synchronize_session=False,
                )
            )
            if completed != 1:
                db.rollback()
                return
            db.commit()
        except _CharacterJobCancelled:
            db.rollback()
            logger.info(
                "Character job %s stopped because its running state "
                "or source was removed.",
                job_id,
            )
        except Exception as error:
            db.rollback()
            if continuation_check is not None:
                try:
                    if not continuation_check():
                        logger.info(
                            "Character job %s stopped because its "
                            "running state or source was removed.",
                            job_id,
                        )
                        return
                except Exception:
                    logger.exception(
                        "Could not verify character job %s "
                        "cancellation.",
                        job_id,
                    )
            locked = (
                isinstance(error, OperationalError)
                and is_sqlite_lock_error(error)
            )
            if locked:
                logger.error(
                    "Character job %s could not persist local progress "
                    "because the SQLite database remained locked.",
                    job_id,
                )
            elif not isinstance(error, LLMProviderError):
                logger.exception(
                    "Unexpected character analysis job failure."
                )
            message = (
                str(error)
                if isinstance(error, LLMProviderError)
                else (
                    "人物分析任务无法获取本地数据库写锁，请重试。"
                    if locked
                    else (
                        "人物分析任务执行失败，请查看后端日志后重试。"
                    )
                )
            )[:1_000]
            if not mark_analysis_job_failed_with_sqlite_lock_retry(
                session_factory,
                job_id,
                "characters",
                message,
                expected_status="running",
                failure_context="execution failure",
            ):
                logger.error(
                    "Character job %s could not be transitioned out "
                    "of running state.",
                    job_id,
                )
