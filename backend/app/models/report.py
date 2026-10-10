"""报告成功后一次性替换本行，刷新失败时旧报告仍可读；来源哈希、模型和提示版本共同决定缓存是否有效。"""

from __future__ import annotations
from datetime import datetime
from typing import Any, TYPE_CHECKING
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import relationship

from backend.app.core.database import Base
from backend.app.core.time import utc_now

if TYPE_CHECKING:
    from backend.app.models.novel import Novel



class NovelReport(Base):
    __tablename__ = "novel_reports"
    __table_args__ = (
        UniqueConstraint(
            "novel_id",
            name="uq_novel_reports_novel_id",
        ),
        CheckConstraint(
            "length(source_hash) = 64 "
            "AND source_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_novel_reports_source_hash",
        ),
        CheckConstraint(
            "json_valid(report_data) "
            "AND json_type(report_data) = 'object'",
            name="ck_novel_reports_data_object",
        ),
        CheckConstraint(
            "length(trim(model)) BETWEEN 1 AND 100",
            name="ck_novel_reports_model",
        ),
        CheckConstraint(
            "length(trim(prompt_version)) BETWEEN 1 AND 50",
            name="ck_novel_reports_prompt_version",
        ),
        CheckConstraint(
            "summary_count > 0",
            name="ck_novel_reports_summary_count_positive",
        ),
        CheckConstraint(
            "character_count >= 0",
            name="ck_novel_reports_character_count_nonnegative",
        ),
        CheckConstraint(
            "generation_steps > 0",
            name="ck_novel_reports_generation_steps_positive",
        ),
        CheckConstraint(
            "includes_character_analysis IN (0, 1)",
            name="ck_novel_reports_character_analysis_boolean",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    novel_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("novels.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    report_data: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON),
        default=dict,
        nullable=False,
    )
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    summary_count: Mapped[int] = mapped_column(Integer, nullable=False)
    character_count: Mapped[int] = mapped_column(Integer, nullable=False)
    generation_steps: Mapped[int] = mapped_column(Integer, nullable=False)
    includes_character_analysis: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("0"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    novel: Mapped["Novel"] = relationship("Novel", back_populates="report")
