"""SQLAlchemy models for generated analysis data and background jobs."""

from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import relationship

from backend.app.core.database import Base
from backend.app.core.time import utc_now


def _new_uuid() -> str:
    """Return a portable UUID string for an analysis job."""
    return str(uuid4())


class ChapterSummary(Base):
    """The current generated summary for one chapter."""

    __tablename__ = "chapter_summaries"
    __table_args__ = (
        UniqueConstraint(
            "chapter_id",
            name="uq_chapter_summaries_chapter_id",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    chapter_id = Column(
        Integer,
        ForeignKey("chapters.id", ondelete="CASCADE"),
        nullable=False,
    )
    summary = Column(Text, nullable=False)
    key_events = Column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
    )
    plot_progression = Column(Text, nullable=False)
    cliffhanger = Column(Text, nullable=True)
    model = Column(String(100), nullable=False)
    prompt_version = Column(String(50), nullable=False)
    source_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    chapter = relationship("Chapter", back_populates="summary")


class AnalysisJob(Base):
    """One persisted background analysis task for a novel."""

    __tablename__ = "analysis_jobs"
    __table_args__ = (
        CheckConstraint(
            "job_type IN ("
            "'chapter_summaries', 'characters', 'rag_index', "
            "'report', 'full_analysis'"
            ")",
            name="ck_analysis_jobs_job_type",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_analysis_jobs_status",
        ),
        CheckConstraint(
            "progress_current >= 0",
            name="ck_analysis_jobs_progress_current_nonnegative",
        ),
        CheckConstraint(
            "progress_total >= 0",
            name="ck_analysis_jobs_progress_total_nonnegative",
        ),
        CheckConstraint(
            "progress_current <= progress_total",
            name="ck_analysis_jobs_progress_within_total",
        ),
        CheckConstraint(
            "target_chapter_index IS NULL "
            "OR target_chapter_index >= 1",
            name="ck_analysis_jobs_target_chapter_index_positive",
        ),
        CheckConstraint(
            "force_refresh IN (0, 1)",
            name="ck_analysis_jobs_force_refresh_boolean",
        ),
        Index(
            "ix_analysis_jobs_novel_status",
            "novel_id",
            "status",
        ),
        Index(
            "ix_analysis_jobs_novel_type_created",
            "novel_id",
            "job_type",
            "created_at",
        ),
        Index(
            "ux_analysis_jobs_active_novel_type",
            "novel_id",
            "job_type",
            unique=True,
            sqlite_where=text(
                "status IN ('queued', 'running')"
            ),
        ),
    )

    id = Column(String(36), primary_key=True, default=_new_uuid)
    novel_id = Column(
        Integer,
        ForeignKey("novels.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_type = Column(String(40), nullable=False)
    target_chapter_index = Column(Integer, nullable=True)
    force_refresh = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("0"),
    )
    status = Column(String(20), nullable=False, default="queued")
    progress_current = Column(Integer, nullable=False, default=0)
    progress_total = Column(Integer, nullable=False, default=0)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    novel = relationship("Novel", back_populates="analysis_jobs")
