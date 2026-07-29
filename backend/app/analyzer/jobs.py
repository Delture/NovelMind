"""Shared persistence helpers for background analysis jobs."""

import logging
import time
from typing import Callable

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.app.core.time import utc_now
from backend.app.models.analysis import AnalysisJob


logger = logging.getLogger(__name__)

ANALYSIS_JOB_SQLITE_MAX_ATTEMPTS = 4
ANALYSIS_JOB_SQLITE_RETRY_BASE_SECONDS = 0.05


def is_sqlite_lock_error(error: OperationalError) -> bool:
    """Return whether an operational error is SQLite lock contention."""
    message = str(getattr(error, "orig", error)).lower()
    return (
        "database is locked" in message
        or "database table is locked" in message
    )


def _sleep_before_sqlite_retry(failed_attempt: int) -> None:
    time.sleep(
        ANALYSIS_JOB_SQLITE_RETRY_BASE_SECONDS
        * (2 ** (failed_attempt - 1))
    )


def mark_analysis_job_failed_with_sqlite_lock_retry(
    session_factory: Callable[[], Session],
    job_id: str,
    job_type: str,
    error_message: str,
    *,
    expected_status: str,
    failure_context: str,
) -> bool:
    """Conditionally terminalize one job using bounded fresh sessions."""
    if expected_status not in {"queued", "running"}:
        raise ValueError("expected_status must be queued or running")

    for attempt in range(1, ANALYSIS_JOB_SQLITE_MAX_ATTEMPTS + 1):
        try:
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
        except OperationalError as error:
            if (
                not is_sqlite_lock_error(error)
                or attempt == ANALYSIS_JOB_SQLITE_MAX_ATTEMPTS
            ):
                logger.error(
                    "Could not mark %s analysis job %s failed after %s.",
                    job_type,
                    job_id,
                    failure_context,
                )
                return False
            logger.warning(
                "%s analysis job %s terminalization after %s hit a "
                "SQLite write lock (attempt %s/%s).",
                job_type,
                job_id,
                failure_context,
                attempt,
                ANALYSIS_JOB_SQLITE_MAX_ATTEMPTS,
            )
        except Exception:
            logger.exception(
                "Could not mark %s analysis job %s failed after %s.",
                job_type,
                job_id,
                failure_context,
            )
            return False
        _sleep_before_sqlite_retry(attempt)
    return False
