"""全书分析报告的当前原子快照。

报告只使用已验证的章节摘要和可选人物快照，不再次发送整部正文。``report_data``
保存经过 Pydantic 校验的完整结构；业务层生成成功后一次性替换本行，使刷新失败
时旧报告继续可读。来源哈希、模型和提示版本共同决定缓存是否仍然有效。
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
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


class NovelReport(Base):
    """某部小说当前完整、可引用且来源可追踪的报告快照。"""

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

    id = Column(Integer, primary_key=True)
    novel_id = Column(
        Integer,
        ForeignKey("novels.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_hash = Column(String(64), nullable=False)
    report_data = Column(
        MutableDict.as_mutable(JSON),
        default=dict,
        nullable=False,
    )
    model = Column(String(100), nullable=False)
    prompt_version = Column(String(50), nullable=False)
    summary_count = Column(Integer, nullable=False)
    character_count = Column(Integer, nullable=False)
    generation_steps = Column(Integer, nullable=False)
    includes_character_analysis = Column(
        Boolean,
        default=False,
        server_default=text("0"),
        nullable=False,
    )
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    novel = relationship("Novel", back_populates="report")
