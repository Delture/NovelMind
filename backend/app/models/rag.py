"""RAG 当前索引指针、分块定位信息和问答历史模型。

向量本体存放在独立本地向量库，主 SQLite 只保存“哪一个集合是当前集合”和每个
向量对应的精确原文范围。建索引时先写新集合，验证数量后才切换 ``RagIndex``；
因此失败不会破坏旧索引。问答再从主库回读原文并核验哈希，绝不盲信向量元数据。
"""

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
    UniqueConstraint,
    text,
)
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import relationship

from backend.app.core.database import Base
from backend.app.core.time import utc_now


def _new_uuid() -> str:
    """为 RAG 问答生成数据库无关的 UUID 字符串主键。"""
    return str(uuid4())


class RagIndex(Base):
    """一部小说唯一且已验证的当前向量快照指针。"""

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

    id = Column(Integer, primary_key=True)
    novel_id = Column(
        Integer,
        ForeignKey("novels.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_hash = Column(String(64), nullable=False)
    collection_name = Column(String(128), nullable=False)
    embedding_provider = Column(String(50), nullable=False)
    embedding_model = Column(String(100), nullable=False)
    embedding_dimensions = Column(Integer, nullable=False)
    chunk_version = Column(String(50), nullable=False)
    chunk_count = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    novel = relationship("Novel", back_populates="rag_index")
    chunks = relationship(
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

    id = Column(Integer, primary_key=True)
    rag_index_id = Column(
        Integer,
        ForeignKey("rag_indices.id", ondelete="CASCADE"),
        nullable=False,
    )
    chapter_id = Column(
        Integer,
        ForeignKey("chapters.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_id = Column(String(70), nullable=False)
    chapter_index = Column(Integer, nullable=False)
    chunk_index = Column(Integer, nullable=False)
    start_char = Column(Integer, nullable=False)
    end_char = Column(Integer, nullable=False)
    content_hash = Column(String(64), nullable=False)

    rag_index = relationship("RagIndex", back_populates="chunks")
    chapter = relationship("Chapter", back_populates="rag_chunks")


class RagQuestion(Base):
    """一条持久化问答任务及其证据、答案和引用白名单结果。"""

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

    id = Column(String(36), primary_key=True, default=_new_uuid)
    novel_id = Column(
        Integer,
        ForeignKey("novels.id", ondelete="CASCADE"),
        nullable=False,
    )
    index_source_hash = Column(String(64), nullable=False)
    question = Column(String(2_000), nullable=False)
    top_k = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default="queued")
    answer = Column(String(12_000), nullable=True)
    insufficient_evidence = Column(Boolean, nullable=True)
    citation_ids = Column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
    )
    evidence = Column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
    )
    generation_model = Column(String(100), nullable=False)
    prompt_version = Column(String(50), nullable=False)
    error_message = Column(String(1_000), nullable=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    novel = relationship("Novel", back_populates="rag_questions")
