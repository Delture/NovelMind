"""SQLAlchemy models for novels and chapters."""

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
    """A TXT novel uploaded to NovelMind."""

    __tablename__ = "novels"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String(255), nullable=False)
    encoding = Column(String(20), nullable=False)
    chapter_count = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)

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


class Chapter(Base):
    """One parsed chapter that belongs to a novel."""

    __tablename__ = "chapters"
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


# Register string-based relationships when this module is imported alone.
from backend.app.models import analysis as _analysis  # noqa: E402,F401
from backend.app.models import character as _character  # noqa: E402,F401
from backend.app.models import rag as _rag  # noqa: E402,F401
