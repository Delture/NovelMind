"""``nullable=False`` 与 UniqueConstraint 是最终数据防线；
``cascade`` 与 ``ondelete=CASCADE`` 分别照顾 ORM 层与数据库层的级联删除。
"""

from __future__ import annotations
from datetime import datetime
from typing import TYPE_CHECKING
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from backend.app.core.database import Base
from backend.app.core.time import utc_now

if TYPE_CHECKING:
    from backend.app.models.analysis import AnalysisJob
    from backend.app.models.character import ChapterCharacterExtraction
    from backend.app.models.analysis import ChapterSummary
    from backend.app.models.character import CharacterAnalysis
    from backend.app.models.character import CharacterAppearance
    from backend.app.models.report import NovelReport
    from backend.app.models.rag import RagChunkRecord
    from backend.app.models.rag import RagIndex
    from backend.app.models.rag import RagQuestion



class Novel(Base):
    __tablename__ = "novels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    encoding: Mapped[str] = mapped_column(String(20), nullable=False)
    chapter_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    # 一对多：删除小说时，章节及派生结果一同清除，避免孤儿记录。
    chapters: Mapped[list["Chapter"]] = relationship(
        "Chapter",
        back_populates="novel",
        cascade="all, delete-orphan",
        order_by="Chapter.chapter_index",
        passive_deletes=True,
    )
    analysis_jobs: Mapped[list["AnalysisJob"]] = relationship(
        "AnalysisJob",
        back_populates="novel",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    character_analysis: Mapped["CharacterAnalysis"] = relationship(
        "CharacterAnalysis",
        back_populates="novel",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    rag_index: Mapped["RagIndex"] = relationship(
        "RagIndex",
        back_populates="novel",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    rag_questions: Mapped[list["RagQuestion"]] = relationship(
        "RagQuestion",
        back_populates="novel",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    report: Mapped["NovelReport"] = relationship(
        "NovelReport",
        back_populates="novel",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )


class Chapter(Base):
    """小说的一章原文，是摘要、人物分析和 RAG 的共同事实来源。"""

    __tablename__ = "chapters"
    # 业务序号在同一小说内唯一；复合唯一键还供派生表证明章节确属该小说。
    __table_args__ = (
        UniqueConstraint(
            "novel_id",
            "chapter_index",
            name="ux_chapters_novel_index",
        ),
        UniqueConstraint(
            "novel_id",
            "id",
            name="uq_chapters_novel_id_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    novel_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("novels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chapter_index: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    character_count: Mapped[int] = mapped_column(Integer, nullable=False)

    novel: Mapped["Novel"] = relationship("Novel", back_populates="chapters")
    summary: Mapped["ChapterSummary"] = relationship(
        "ChapterSummary",
        back_populates="chapter",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    character_extraction: Mapped["ChapterCharacterExtraction"] = relationship(
        "ChapterCharacterExtraction",
        back_populates="chapter",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    character_appearances: Mapped[list["CharacterAppearance"]] = relationship(
        "CharacterAppearance",
        back_populates="chapter",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    rag_chunks: Mapped[list["RagChunkRecord"]] = relationship(
        "RagChunkRecord",
        back_populates="chapter",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# 以下导入用于注册字符串形式的关系目标；放在末尾可规避模型间循环导入。
from backend.app.models import analysis as _analysis  # noqa: E402,F401
from backend.app.models import character as _character  # noqa: E402,F401
from backend.app.models import rag as _rag  # noqa: E402,F401
from backend.app.models import report as _report  # noqa: E402,F401
