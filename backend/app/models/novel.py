"""小说与章节的 SQLAlchemy ORM 模型。

ORM 模型描述数据库列、数据库约束和 Python 对象关系。``nullable=False`` 与
``UniqueConstraint`` 是最终数据防线；关系不是数据副本，而是沿外键查询。
``cascade`` 和 ``ondelete=CASCADE`` 分别照顾 ORM 层与数据库层的级联删除。
"""

from sqlalchemy import (
    Column,
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


class Novel(Base):
    """一份已上传小说的元数据根对象，不在本行重复保存整部全文。"""

    __tablename__ = "novels"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String(255), nullable=False)
    encoding = Column(String(20), nullable=False)
    chapter_count = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)

    # 一对多：删除小说时，章节及派生结果一同清除，避免孤儿记录。
    chapters = relationship(
        "Chapter",
        back_populates="novel",
        cascade="all, delete-orphan",
        order_by="Chapter.chapter_index",
        passive_deletes=True,
    )
    analysis_jobs = relationship(
        "AnalysisJob",
        back_populates="novel",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    # ``uselist=False`` 将默认列表语义收紧为“一部小说一个当前快照”。
    character_analysis = relationship(
        "CharacterAnalysis",
        back_populates="novel",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    rag_index = relationship(
        "RagIndex",
        back_populates="novel",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    rag_questions = relationship(
        "RagQuestion",
        back_populates="novel",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    report = relationship(
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

    id = Column(Integer, primary_key=True, index=True)
    novel_id = Column(
        Integer,
        ForeignKey("novels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chapter_index = Column(Integer, nullable=False)
    title = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)
    character_count = Column(Integer, nullable=False)

    novel = relationship("Novel", back_populates="chapters")
    summary = relationship(
        "ChapterSummary",
        back_populates="chapter",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    character_extraction = relationship(
        "ChapterCharacterExtraction",
        back_populates="chapter",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    character_appearances = relationship(
        "CharacterAppearance",
        back_populates="chapter",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    rag_chunks = relationship(
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
