"""人物逐章缓存、当前快照、出场与关系的 ORM 模型。

人物分析采用“两层持久化”：逐章抽取可增量复用，整书归并结果则作为一个当前
快照原子替换。这样失败重试不必重复付费处理未变化章节，也不会让用户看到只
更新了一半的人物网络。大量复合外键用于保证人物、章节、关系都属于同一小说
和同一快照，而不是只验证某个整数 ID 恰好存在。
"""

from __future__ import annotations
from datetime import datetime
from typing import Any, TYPE_CHECKING
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import relationship

from backend.app.core.database import Base
from backend.app.core.time import utc_now

if TYPE_CHECKING:
    from backend.app.models.novel import Chapter
    from backend.app.models.novel import Novel



class ChapterCharacterExtraction(Base):
    """单章当前人物观察缓存，来源哈希一致时才允许复用。"""

    __tablename__ = "chapter_character_extractions"
    __table_args__ = (
        UniqueConstraint(
            "chapter_id",
            name="uq_chapter_character_extractions_chapter_id",
        ),
        CheckConstraint(
            "json_valid(extraction_data) "
            "AND json_type(extraction_data) = 'object'",
            name="ck_chapter_character_extractions_data_object",
        ),
        CheckConstraint(
            "length(source_hash) = 64 "
            "AND source_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_chapter_character_extractions_source_hash",
        ),
        CheckConstraint(
            "length(trim(model)) BETWEEN 1 AND 100",
            name="ck_chapter_character_extractions_model",
        ),
        CheckConstraint(
            "length(trim(prompt_version)) BETWEEN 1 AND 50",
            name="ck_chapter_character_extractions_prompt_version",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chapter_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("chapters.id", ondelete="CASCADE"),
        nullable=False,
    )
    extraction_data: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON),
        default=dict,
        nullable=False,
    )
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    chapter: Mapped["Chapter"] = relationship(
        "Chapter",
        back_populates="character_extraction",
    )


class CharacterAnalysis(Base):
    """一部小说当前完整人物分析的快照根记录。"""

    __tablename__ = "character_analyses"
    __table_args__ = (
        UniqueConstraint(
            "novel_id",
            name="uq_character_analyses_novel_id",
        ),
        UniqueConstraint(
            "id",
            "novel_id",
            name="uq_character_analyses_id_novel_id",
        ),
        CheckConstraint(
            "length(source_hash) = 64 "
            "AND source_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_character_analyses_source_hash",
        ),
        CheckConstraint(
            "length(trim(model)) BETWEEN 1 AND 100",
            name="ck_character_analyses_model",
        ),
        CheckConstraint(
            "length(trim(extraction_prompt_version)) BETWEEN 1 AND 50",
            name="ck_character_analyses_extraction_prompt_version",
        ),
        CheckConstraint(
            "length(trim(merge_prompt_version)) BETWEEN 1 AND 50",
            name="ck_character_analyses_merge_prompt_version",
        ),
        CheckConstraint(
            "length(trim(relationship_prompt_version)) BETWEEN 1 AND 50",
            name="ck_character_analyses_relationship_prompt_version",
        ),
        CheckConstraint(
            "chapter_count >= 0",
            name="ck_character_analyses_chapter_count_nonnegative",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    novel_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("novels.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    extraction_prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    merge_prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    relationship_prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    chapter_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    novel: Mapped["Novel"] = relationship(
        "Novel",
        back_populates="character_analysis",
    )
    characters: Mapped[list["Character"]] = relationship(
        "Character",
        back_populates="analysis",
        cascade="all, delete-orphan",
        order_by="Character.id",
        passive_deletes=True,
    )
    relationships: Mapped[list["CharacterRelationship"]] = relationship(
        "CharacterRelationship",
        back_populates="analysis",
        cascade="all, delete-orphan",
        foreign_keys="CharacterRelationship.analysis_id",
        passive_deletes=True,
        overlaps=(
            "incoming_relationships,outgoing_relationships,"
            "source_character,target_character"
        ),
    )


class Character(Base):
    """快照中经过跨章消歧后的规范人物，而不是原文里的一次名字出现。"""

    __tablename__ = "characters"
    __table_args__ = (
        UniqueConstraint(
            "analysis_id",
            "id",
            name="uq_characters_analysis_id_id",
        ),
        CheckConstraint(
            "length(trim(canonical_name)) BETWEEN 1 AND 255",
            name="ck_characters_canonical_name",
        ),
        CheckConstraint(
            "json_valid(aliases) AND json_type(aliases) = 'array'",
            name="ck_characters_aliases_array",
        ),
        CheckConstraint(
            "length(trim(short_description)) > 0",
            name="ck_characters_short_description",
        ),
        CheckConstraint(
            "first_chapter_index >= 1 "
            "AND last_chapter_index >= first_chapter_index",
            name="ck_characters_chapter_range",
        ),
        CheckConstraint(
            "chapter_count > 0",
            name="ck_characters_chapter_count_positive",
        ),
        CheckConstraint(
            "chapter_count <= "
            "(last_chapter_index - first_chapter_index + 1)",
            name="ck_characters_chapter_count_within_range",
        ),
        CheckConstraint(
            "mention_count >= chapter_count",
            name="ck_characters_mention_count_valid",
        ),
        Index(
            "ix_characters_analysis_chapter_count",
            "analysis_id",
            "chapter_count",
        ),
        Index(
            "ix_characters_analysis_canonical_name",
            "analysis_id",
            "canonical_name",
        ),
        Index(
            "ix_characters_analysis_mention_count",
            "analysis_id",
            "mention_count",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analysis_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("character_analyses.id", ondelete="CASCADE"),
        nullable=False,
    )
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
    )
    short_description: Mapped[str] = mapped_column(Text, nullable=False)
    first_chapter_index: Mapped[int] = mapped_column(Integer, nullable=False)
    last_chapter_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chapter_count: Mapped[int] = mapped_column(Integer, nullable=False)
    mention_count: Mapped[int] = mapped_column(Integer, nullable=False)

    analysis: Mapped["CharacterAnalysis"] = relationship(
        "CharacterAnalysis",
        back_populates="characters",
    )
    appearances: Mapped[list["CharacterAppearance"]] = relationship(
        "CharacterAppearance",
        back_populates="character",
        cascade="all, delete-orphan",
        order_by="CharacterAppearance.id",
        passive_deletes=True,
    )
    outgoing_relationships: Mapped[list["CharacterRelationship"]] = relationship(
        "CharacterRelationship",
        primaryjoin=(
            "and_("
            "Character.analysis_id == CharacterRelationship.analysis_id, "
            "Character.id == "
            "CharacterRelationship.source_character_id"
            ")"
        ),
        foreign_keys=(
            "[CharacterRelationship.analysis_id, "
            "CharacterRelationship.source_character_id]"
        ),
        back_populates="source_character",
        passive_deletes=True,
        overlaps=(
            "analysis,incoming_relationships,relationships,"
            "target_character"
        ),
    )
    incoming_relationships: Mapped[list["CharacterRelationship"]] = relationship(
        "CharacterRelationship",
        primaryjoin=(
            "and_("
            "Character.analysis_id == CharacterRelationship.analysis_id, "
            "Character.id == "
            "CharacterRelationship.target_character_id"
            ")"
        ),
        foreign_keys=(
            "[CharacterRelationship.analysis_id, "
            "CharacterRelationship.target_character_id]"
        ),
        back_populates="target_character",
        passive_deletes=True,
        overlaps=(
            "analysis,outgoing_relationships,relationships,"
            "source_character"
        ),
    )


class CharacterAppearance(Base):
    """One character's aggregate appearance in one chapter."""

    __tablename__ = "character_appearances"
    __table_args__ = (
        ForeignKeyConstraint(
            ["analysis_id", "novel_id"],
            ["character_analyses.id", "character_analyses.novel_id"],
            name="fk_character_appearances_analysis_novel",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["analysis_id", "character_id"],
            ["characters.analysis_id", "characters.id"],
            name="fk_character_appearances_character",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["novel_id", "chapter_id"],
            ["chapters.novel_id", "chapters.id"],
            name="fk_character_appearances_chapter",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "character_id",
            "chapter_id",
            name="uq_character_appearances_character_chapter",
        ),
        CheckConstraint(
            "mention_count > 0",
            name="ck_character_appearances_mention_count_positive",
        ),
        CheckConstraint(
            "length(trim(summary)) > 0",
            name="ck_character_appearances_summary",
        ),
        Index(
            "ix_character_appearances_chapter_id",
            "chapter_id",
        ),
        Index(
            "ix_character_appearances_analysis_id",
            "analysis_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analysis_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    novel_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    character_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chapter_id: Mapped[int] = mapped_column(Integer, nullable=False)
    mention_count: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)

    character: Mapped["Character"] = relationship(
        "Character",
        back_populates="appearances",
    )
    chapter: Mapped["Chapter"] = relationship(
        "Chapter",
        back_populates="character_appearances",
    )


class CharacterRelationship(Base):
    """One typed relationship between two characters in a snapshot."""

    __tablename__ = "character_relationships"
    __table_args__ = (
        ForeignKeyConstraint(
            ["analysis_id"],
            ["character_analyses.id"],
            name="fk_character_relationships_analysis",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["analysis_id", "source_character_id"],
            ["characters.analysis_id", "characters.id"],
            name="fk_character_relationships_source",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["analysis_id", "target_character_id"],
            ["characters.analysis_id", "characters.id"],
            name="fk_character_relationships_target",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "analysis_id",
            "source_character_id",
            "target_character_id",
            "directed",
            "relation_type",
            name="uq_character_relationships_snapshot_edge",
        ),
        CheckConstraint(
            "directed IN (0, 1)",
            name="ck_character_relationships_directed_boolean",
        ),
        CheckConstraint(
            "source_character_id <> target_character_id",
            name="ck_character_relationships_no_self",
        ),
        CheckConstraint(
            "directed = 1 "
            "OR source_character_id < target_character_id",
            name="ck_character_relationships_undirected_canonical",
        ),
        CheckConstraint(
            "length(trim(relation_type)) BETWEEN 1 AND 100",
            name="ck_character_relationships_relation_type",
        ),
        CheckConstraint(
            "length(trim(description)) > 0",
            name="ck_character_relationships_description",
        ),
        CheckConstraint(
            "json_valid(evidence_chapter_indices) "
            "AND json_type(evidence_chapter_indices) = 'array' "
            "AND json_array_length(evidence_chapter_indices) > 0",
            name="ck_character_relationships_evidence_nonempty",
        ),
        Index(
            "ix_character_relationships_analysis_target",
            "analysis_id",
            "target_character_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analysis_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_character_id: Mapped[int] = mapped_column(Integer, nullable=False)
    target_character_id: Mapped[int] = mapped_column(Integer, nullable=False)
    directed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    relation_type: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_chapter_indices: Mapped[list[int]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
    )

    analysis: Mapped["CharacterAnalysis"] = relationship(
        "CharacterAnalysis",
        back_populates="relationships",
        foreign_keys=[analysis_id],
        overlaps=(
            "incoming_relationships,outgoing_relationships,"
            "source_character,target_character"
        ),
    )
    source_character: Mapped["Character"] = relationship(
        "Character",
        primaryjoin=(
            "and_("
            "CharacterRelationship.analysis_id == Character.analysis_id, "
            "CharacterRelationship.source_character_id == Character.id"
            ")"
        ),
        foreign_keys=[analysis_id, source_character_id],
        back_populates="outgoing_relationships",
        overlaps=(
            "analysis,incoming_relationships,relationships,"
            "target_character"
        ),
    )
    target_character: Mapped["Character"] = relationship(
        "Character",
        primaryjoin=(
            "and_("
            "CharacterRelationship.analysis_id == Character.analysis_id, "
            "CharacterRelationship.target_character_id == Character.id"
            ")"
        ),
        foreign_keys=[analysis_id, target_character_id],
        back_populates="incoming_relationships",
        overlaps=(
            "analysis,outgoing_relationships,relationships,"
            "source_character"
        ),
    )
