"""章节摘要结果与通用后台任务的持久化模型。

外部模型调用可能持续数分钟，HTTP 请求不能一直阻塞等待，所以接口先创建
``AnalysisJob``，提交后再交给后台执行器。前端轮询同一行即可恢复进度。
生成结果与任务状态分表：任务失败不会覆盖上一份仍然可用的摘要。
"""

from __future__ import annotations
from datetime import datetime
from typing import TYPE_CHECKING
from sqlalchemy.orm import Mapped, mapped_column
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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
    """生成与数据库无关的字符串 UUID，避免依赖某种数据库 UUID 类型。"""
    return str(uuid4())

if TYPE_CHECKING:
    from backend.app.models.novel import Chapter
    from backend.app.models.novel import Novel



class ChapterSummary(Base):
    """某章当前有效的结构化摘要；一章最多保留一份当前快照。

    ``source_hash`` 绑定原文和提示版本。当原文改变时，即使数据库里已有结果，
    业务层也能判断它已过期；这是派生数据缓存不能只看“有没有记录”的原因。
    """

    __tablename__ = "chapter_summaries"
    __table_args__ = (
        UniqueConstraint(
            "chapter_id",
            name="uq_chapter_summaries_chapter_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    chapter_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("chapters.id", ondelete="CASCADE"),
        nullable=False,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    key_events: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
    )
    plot_progression: Mapped[str] = mapped_column(Text, nullable=False)
    cliffhanger: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    chapter: Mapped["Chapter"] = relationship("Chapter", back_populates="summary")


class AnalysisJob(Base):
    """一项可由界面查询和恢复的后台分析任务。

    状态机只允许 ``queued -> running -> succeeded/failed``。数据库检查约束保护
    进度范围；局部唯一索引保证同一小说同类任务最多一个处于活动状态。仅靠
    Python 中“先查询再插入”会有并发竞态，因此唯一性必须落到数据库层。
    """

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
        # partial unique index 只约束活动状态，历史成功/失败任务仍可保留多条。
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

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    novel_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("novels.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_chapter_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    force_refresh: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("0"),
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    progress_current: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    novel: Mapped["Novel"] = relationship("Novel", back_populates="analysis_jobs")
