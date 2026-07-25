"""SQLAlchemy models for novels and chapters."""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from backend.app.core.database import Base


class Novel(Base):
    """A TXT novel uploaded to NovelMind."""

    __tablename__ = "novels"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String(255), nullable=False)
    encoding = Column(String(20), nullable=False)
    chapter_count = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    chapters = relationship(
        "Chapter",
        back_populates="novel",
        cascade="all, delete-orphan",
        order_by="Chapter.chapter_index",
    )


class Chapter(Base):
    """One parsed chapter that belongs to a novel."""

    __tablename__ = "chapters"

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
