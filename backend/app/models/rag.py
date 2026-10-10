"""建索引时先写新集合，验证数量后才切换 RagIndex，失败不破坏旧索引；
问答再从主库回读原文并核验哈希，不盲信向量元数据。
"""

from __future__ import annotations
from datetime import datetime
from typing import Any, TYPE_CHECKING
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
    UniqueConstraint,
    text,
)
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import relationship

from backend.app.core.database import Base
from backend.app.core.time import utc_now


def _new_uuid() -> str:
    return str(uuid4())

if TYPE_CHECKING:
    from backend.app.models.novel import Chapter
    from backend.app.models.novel import Novel



class RagIndex(Base):
    __tablename__ = "rag_indices"
    __table_args__ = (
        UniqueConstraint(
            "novel_id",
            name="uq_rag_indices_novel_id",
        ),
        UniqueConstraint(
            "collection_name",
            name="uq_rag_indices_collection_name",
        ),
        CheckConstraint(
            "length(source_hash) = 64",
            name="ck_rag_indices_source_hash",
        ),
        CheckConstraint(
            "length(trim(collection_name)) > 0",
            name="ck_rag_indices_collection_name",
        ),
        CheckConstraint(
            "length(trim(embedding_provider)) BETWEEN 1 AND 50",
            name="ck_rag_indices_embedding_provider",
        ),
        CheckConstraint(
            "length(trim(embedding_model)) BETWEEN 1 AND 100",
            name="ck_rag_indices_embedding_model",
        ),
        CheckConstraint(
            "embedding_dimensions BETWEEN 1 AND 8192",
            name="ck_rag_indices_embedding_dimensions",
        ),
        CheckConstraint(
            "length(trim(chunk_version)) > 0",
            name="ck_rag_indices_chunk_version",
        ),
        CheckConstraint(
            "chunk_count > 0",
            name="ck_rag_indices_chunk_count_positive",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    novel_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("novels.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    collection_name: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding_provider: Mapped[str] = mapped_column(String(50), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(100), nullable=False)
    embedding_dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_version: Mapped[str] = mapped_column(String(50), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    novel: Mapped["Novel"] = relationship("Novel", back_populates="rag_index")
    chunks: Mapped[list["RagChunkRecord"]] = relationship(
        "RagChunkRecord",
        back_populates="rag_index",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="RagChunkRecord.chapter_index, "
        "RagChunkRecord.chunk_index",
    )


class RagChunkRecord(Base):
    """一个向量对应的章节、字符范围和哈希；本表不复制原文。"""

    __tablename__ = "rag_chunks"
    __table_args__ = (
        UniqueConstraint(
            "rag_index_id",
            "chunk_id",
            name="uq_rag_chunks_index_chunk_id",
        ),
        UniqueConstraint(
            "rag_index_id",
            "chapter_id",
            "chunk_index",
            name="uq_rag_chunks_index_chapter_chunk",
        ),
        CheckConstraint(
            "length(chunk_id) = 70",
            name="ck_rag_chunks_chunk_id",
        ),
        CheckConstraint(
            "chapter_index >= 1 AND chunk_index >= 1",
            name="ck_rag_chunks_indexes_positive",
        ),
        CheckConstraint(
            "start_char >= 0 AND end_char > start_char",
            name="ck_rag_chunks_character_range",
        ),
        CheckConstraint(
            "length(content_hash) = 64",
            name="ck_rag_chunks_content_hash",
        ),
        Index(
            "ix_rag_chunks_index_chapter",
            "rag_index_id",
            "chapter_index",
            "chunk_index",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rag_index_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("rag_indices.id", ondelete="CASCADE"),
        nullable=False,
    )
    chapter_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("chapters.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_id: Mapped[str] = mapped_column(String(70), nullable=False)
    chapter_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_char: Mapped[int] = mapped_column(Integer, nullable=False)
    end_char: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    rag_index: Mapped["RagIndex"] = relationship("RagIndex", back_populates="chunks")
    chapter: Mapped["Chapter"] = relationship("Chapter", back_populates="rag_chunks")


class RagQuestion(Base):
    __tablename__ = "rag_questions"
    __table_args__ = (
        CheckConstraint(
            "length(trim(question)) BETWEEN 1 AND 2000",
            name="ck_rag_questions_question_length",
        ),
        CheckConstraint(
            "length(index_source_hash) = 64",
            name="ck_rag_questions_source_hash",
        ),
        CheckConstraint(
            "top_k BETWEEN 3 AND 10",
            name="ck_rag_questions_top_k",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_rag_questions_status",
        ),
        CheckConstraint(
            "insufficient_evidence IS NULL "
            "OR insufficient_evidence IN (0, 1)",
            name="ck_rag_questions_insufficient_boolean",
        ),
        CheckConstraint(
            "json_valid(citation_ids) "
            "AND json_type(citation_ids) = 'array'",
            name="ck_rag_questions_citation_ids_array",
        ),
        CheckConstraint(
            "json_valid(evidence) AND json_type(evidence) = 'array'",
            name="ck_rag_questions_evidence_array",
        ),
        CheckConstraint(
            "length(trim(generation_model)) > 0",
            name="ck_rag_questions_generation_model",
        ),
        CheckConstraint(
            "length(trim(prompt_version)) > 0",
            name="ck_rag_questions_prompt_version",
        ),
        Index(
            "ix_rag_questions_novel_created",
            "novel_id",
            "created_at",
        ),
        Index(
            "ux_rag_questions_active_novel",
            "novel_id",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    novel_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("novels.id", ondelete="CASCADE"),
        nullable=False,
    )
    index_source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    question: Mapped[str] = mapped_column(String(2_000), nullable=False)
    top_k: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    answer: Mapped[str | None] = mapped_column(String(12_000), nullable=True)
    insufficient_evidence: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    citation_ids: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
    )
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
    )
    generation_model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(1_000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    novel: Mapped["Novel"] = relationship("Novel", back_populates="rag_questions")
