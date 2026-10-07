"""共享确定性测试数据与假提供商；不发送网络请求。"""
from datetime import datetime
from typing import Callable, Generator
from unittest.mock import Mock
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from backend.app.analyzer.characters import (
    CHARACTER_EXTRACTION_PROMPT_VERSION,
    CHARACTER_MERGE_PROMPT_VERSION,
    CHARACTER_RELATIONSHIP_PROMPT_VERSION,
    character_analysis_source_hash,
    character_chapter_source_hash,
)
from backend.app.api.characters import (
    StartCharacterAnalysisRequest,
    _schedule_character_job,
    get_character,
    get_character_estimate,
    list_character_appearances,
    list_character_relationships,
    list_characters,
    start_character_analysis,
)
from backend.app.core.config import load_settings
from backend.app.core.database import Base, create_db_engine
from backend.app.main import app
from backend.app.models.analysis import AnalysisJob
from backend.app.models.character import (
    ChapterCharacterExtraction,
    Character,
    CharacterAnalysis,
    CharacterAppearance,
    CharacterRelationship,
)
from backend.app.models.novel import Chapter, Novel

def _add_novel(
    session: Session,
    *,
    filename: str = "characters-api.txt",
) -> Novel:
    novel = Novel(
        filename=filename,
        encoding="utf-8",
        chapter_count=2,
        chapters=[
            Chapter(
                chapter_index=1,
                title="第一章",
                content="林舟收到来信。",
                character_count=7,
            ),
            Chapter(
                chapter_index=2,
                title="第二章",
                content="苏晚与林舟重逢。",
                character_count=8,
            ),
        ],
    )
    session.add(novel)
    session.commit()
    session.refresh(novel)
    return novel


def _extraction_data(
    chapter_index: int,
    name: str,
) -> dict[str, object]:
    return {
        "observations": [
            {
                "observation_id": (
                    f"chapter-{chapter_index}-part-1-observation-1"
                ),
                "name": name,
                "aliases": [],
                "short_description": f"{name}的章节简介",
                "mention_count": 1,
                "appearance_summary": f"{name}在本章出场。",
            }
        ]
    }


def _add_current_extractions(
    session: Session,
    novel: Novel,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for chapter, name in zip(
        novel.chapters,
        ("林舟", "苏晚"),
        strict=True,
    ):
        data = _extraction_data(chapter.chapter_index, name)
        source_hash = character_chapter_source_hash(
            chapter.title,
            chapter.content,
        )
        session.add(
            ChapterCharacterExtraction(
                chapter_id=chapter.id,
                extraction_data=data,
                source_hash=source_hash,
                model="deepseek-v4-pro",
                prompt_version=CHARACTER_EXTRACTION_PROMPT_VERSION,
            )
        )
        records.append(
            {
                "chapter_index": chapter.chapter_index,
                "source_hash": source_hash,
                "extraction_data": data,
            }
        )
    session.flush()
    return records


def _add_snapshot(
    session: Session,
    novel: Novel,
    *,
    current: bool = True,
) -> tuple[CharacterAnalysis, Character, Character]:
    records = _add_current_extractions(session, novel)
    analysis = CharacterAnalysis(
        novel_id=novel.id,
        source_hash=(
            character_analysis_source_hash(records)
            if current
            else "f" * 64
        ),
        model="deepseek-v4-pro",
        extraction_prompt_version=CHARACTER_EXTRACTION_PROMPT_VERSION,
        merge_prompt_version=CHARACTER_MERGE_PROMPT_VERSION,
        relationship_prompt_version=(
            CHARACTER_RELATIONSHIP_PROMPT_VERSION
        ),
        chapter_count=2,
    )
    session.add(analysis)
    session.flush()
    lin = Character(
        analysis_id=analysis.id,
        canonical_name="林舟",
        aliases=["阿舟"],
        short_description="收到神秘来信的调查者。",
        first_chapter_index=1,
        last_chapter_index=2,
        chapter_count=2,
        mention_count=5,
    )
    su = Character(
        analysis_id=analysis.id,
        canonical_name="苏晚",
        aliases=[],
        short_description="林舟的旧友。",
        first_chapter_index=2,
        last_chapter_index=2,
        chapter_count=1,
        mention_count=2,
    )
    session.add_all([lin, su])
    session.flush()
    session.add_all(
        [
            CharacterAppearance(
                analysis_id=lin.analysis_id,
                novel_id=novel.id,
                character_id=lin.id,
                chapter_id=novel.chapters[0].id,
                mention_count=2,
                summary="林舟收到一封来信。",
            ),
            CharacterAppearance(
                analysis_id=lin.analysis_id,
                novel_id=novel.id,
                character_id=lin.id,
                chapter_id=novel.chapters[1].id,
                mention_count=3,
                summary="林舟与苏晚重逢。",
            ),
            CharacterAppearance(
                analysis_id=su.analysis_id,
                novel_id=novel.id,
                character_id=su.id,
                chapter_id=novel.chapters[1].id,
                mention_count=2,
                summary="苏晚与林舟重逢。",
            ),
            CharacterRelationship(
                analysis_id=analysis.id,
                source_character_id=lin.id,
                target_character_id=su.id,
                directed=False,
                relation_type="旧友",
                description="两人在第二章重逢。",
                evidence_chapter_indices=[2],
            ),
        ]
    )
    session.commit()
    session.refresh(analysis)
    return analysis, lin, su
