
from typing import Generator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base, create_db_engine
from backend.app.models.character import (
    ChapterCharacterExtraction,
    Character,
    CharacterAnalysis,
    CharacterAppearance,
    CharacterRelationship,
)
from backend.app.models.novel import Chapter, Novel


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def add_novel(
    db_session: Session,
    *,
    filename: str = "story.txt",
) -> Novel:
    novel = Novel(
        filename=filename,
        encoding="utf-8",
        chapter_count=2,
        chapters=[
            Chapter(
                chapter_index=1,
                title="第一章",
                content="林舟遇见苏晚。",
                character_count=7,
            ),
            Chapter(
                chapter_index=2,
                title="第二章",
                content="苏晚帮助林舟脱险。",
                character_count=9,
            ),
        ],
    )
    db_session.add(novel)
    db_session.commit()
    db_session.refresh(novel)
    return novel


def add_analysis(
    db_session: Session,
    novel: Novel,
) -> CharacterAnalysis:
    analysis = CharacterAnalysis(
        novel_id=novel.id,
        source_hash="a" * 64,
        model="deepseek-v4-pro",
        extraction_prompt_version="characters-extract-v1",
        merge_prompt_version="characters-merge-v1",
        relationship_prompt_version="characters-relations-v1",
        chapter_count=novel.chapter_count,
    )
    db_session.add(analysis)
    db_session.flush()
    return analysis


def add_character(
    db_session: Session,
    analysis: CharacterAnalysis,
    *,
    name: str,
    first_chapter_index: int = 1,
    last_chapter_index: int = 2,
    chapter_count: int = 2,
    mention_count: int = 3,
) -> Character:
    character = Character(
        analysis_id=analysis.id,
        canonical_name=name,
        aliases=[],
        short_description=f"{name}的人物简介。",
        first_chapter_index=first_chapter_index,
        last_chapter_index=last_chapter_index,
        chapter_count=chapter_count,
        mention_count=mention_count,
    )
    db_session.add(character)
    db_session.flush()
    return character


def test_character_snapshot_round_trip_and_mutable_json(
    db_session: Session,
) -> None:
    novel = add_novel(db_session)
    extraction = ChapterCharacterExtraction(
        chapter_id=novel.chapters[0].id,
        extraction_data={
            "candidates": [
                {
                    "candidate_id": "chapter-1-character-1",
                    "name": "林舟",
                }
            ]
        },
        source_hash="b" * 64,
        model="deepseek-v4-pro",
        prompt_version="characters-extract-v1",
    )
    analysis = add_analysis(db_session, novel)
    source = add_character(db_session, analysis, name="林舟")
    target = add_character(
        db_session,
        analysis,
        name="苏晚",
        mention_count=2,
    )
    source.aliases.append("小舟")
    appearance = CharacterAppearance(
        analysis_id=source.analysis_id,
        novel_id=novel.id,
        character_id=source.id,
        chapter_id=novel.chapters[0].id,
        mention_count=2,
        summary="林舟首次出场并遇见苏晚。",
    )
    relationship = CharacterRelationship(
        analysis_id=analysis.id,
        source_character_id=source.id,
        target_character_id=target.id,
        directed=False,
        relation_type="盟友",
        description="两人在危机中互相帮助。",
        evidence_chapter_indices=[1],
    )
    db_session.add_all([extraction, appearance, relationship])
    db_session.commit()

    extraction.extraction_data["candidate_count"] = 1
    relationship.evidence_chapter_indices.append(2)
    db_session.commit()
    db_session.expire_all()

    saved_analysis = db_session.get(CharacterAnalysis, analysis.id)
    saved_extraction = db_session.get(
        ChapterCharacterExtraction,
        extraction.id,
    )
    saved_source = db_session.get(Character, source.id)
    saved_relationship = db_session.get(
        CharacterRelationship,
        relationship.id,
    )

    assert saved_analysis is not None
    assert saved_analysis.novel.id == novel.id
    assert [item.canonical_name for item in saved_analysis.characters] == [
        "林舟",
        "苏晚",
    ]
    assert saved_extraction is not None
    assert saved_extraction.chapter.id == novel.chapters[0].id
    assert saved_extraction.extraction_data["candidate_count"] == 1
    assert saved_source is not None
    assert saved_source.aliases == ["小舟"]
    assert saved_source.appearances[0].chapter.chapter_index == 1
    assert saved_source.outgoing_relationships == [saved_relationship]
    assert saved_relationship is not None
    assert saved_relationship.analysis.id == saved_analysis.id
    assert saved_relationship.target_character.canonical_name == "苏晚"
    assert saved_relationship.evidence_chapter_indices == [1, 2]


def test_extraction_and_analysis_are_unique_per_source(
    db_session: Session,
) -> None:
    novel = add_novel(db_session)
    db_session.add(
        ChapterCharacterExtraction(
            chapter_id=novel.chapters[0].id,
            extraction_data={"candidates": []},
            source_hash="c" * 64,
            model="deepseek-v4-pro",
            prompt_version="characters-extract-v1",
        )
    )
    add_analysis(db_session, novel)
    db_session.commit()

    db_session.add(
        ChapterCharacterExtraction(
            chapter_id=novel.chapters[0].id,
            extraction_data={"candidates": []},
            source_hash="d" * 64,
            model="deepseek-v4-pro",
            prompt_version="characters-extract-v1",
        )
    )
    with pytest.raises(
        IntegrityError,
        match="chapter_character_extractions.chapter_id",
    ):
        db_session.commit()
    db_session.rollback()

    db_session.add(
        CharacterAnalysis(
            novel_id=novel.id,
            source_hash="e" * 64,
            model="deepseek-v4-pro",
            extraction_prompt_version="characters-extract-v1",
            merge_prompt_version="characters-merge-v1",
            relationship_prompt_version="characters-relations-v1",
            chapter_count=2,
        )
    )
    with pytest.raises(
        IntegrityError,
        match="character_analyses.novel_id",
    ):
        db_session.commit()
    db_session.rollback()


def test_appearance_relationship_uniqueness_and_self_link_are_enforced(
    db_session: Session,
) -> None:
    novel = add_novel(db_session)
    analysis = add_analysis(db_session, novel)
    source = add_character(db_session, analysis, name="林舟")
    target = add_character(db_session, analysis, name="苏晚")
    db_session.add(
        CharacterAppearance(
            analysis_id=source.analysis_id,
            novel_id=novel.id,
            character_id=source.id,
            chapter_id=novel.chapters[0].id,
            mention_count=1,
            summary="林舟首次出场。",
        )
    )
    db_session.add(
        CharacterRelationship(
            analysis_id=analysis.id,
            source_character_id=source.id,
            target_character_id=target.id,
            directed=True,
            relation_type="帮助",
            description="林舟帮助苏晚。",
            evidence_chapter_indices=[1],
        )
    )
    db_session.commit()

    db_session.add(
        CharacterAppearance(
            analysis_id=source.analysis_id,
            novel_id=novel.id,
            character_id=source.id,
            chapter_id=novel.chapters[0].id,
            mention_count=1,
            summary="重复出场。",
        )
    )
    with pytest.raises(
        IntegrityError,
        match="character_appearances.character_id",
    ):
        db_session.commit()
    db_session.rollback()

    db_session.add(
        CharacterRelationship(
            analysis_id=analysis.id,
            source_character_id=source.id,
            target_character_id=target.id,
            directed=True,
            relation_type="帮助",
            description="重复关系。",
            evidence_chapter_indices=[1],
        )
    )
    with pytest.raises(
        IntegrityError,
        match="character_relationships.analysis_id",
    ):
        db_session.commit()
    db_session.rollback()

    db_session.add(
        CharacterRelationship(
            analysis_id=analysis.id,
            source_character_id=source.id,
            target_character_id=source.id,
            directed=True,
            relation_type="自我",
            description="不允许自连。",
            evidence_chapter_indices=[1],
        )
    )
    with pytest.raises(
        IntegrityError,
        match="ck_character_relationships_no_self",
    ):
        db_session.commit()
    db_session.rollback()


def test_same_canonical_name_can_identify_distinct_characters(
    db_session: Session,
) -> None:
    novel = add_novel(db_session)
    analysis = add_analysis(db_session, novel)
    first = add_character(
        db_session,
        analysis,
        name="无名客",
        first_chapter_index=1,
        last_chapter_index=1,
        chapter_count=1,
        mention_count=1,
    )
    second = add_character(
        db_session,
        analysis,
        name="无名客",
        first_chapter_index=2,
        last_chapter_index=2,
        chapter_count=1,
        mention_count=1,
    )
    db_session.commit()

    assert first.id != second.id
    assert (
        db_session.query(Character)
        .filter(
            Character.analysis_id == analysis.id,
            Character.canonical_name == "无名客",
        )
        .count()
        == 2
    )


def test_relationship_characters_must_belong_to_its_analysis(
    db_session: Session,
) -> None:
    first_novel = add_novel(db_session, filename="first.txt")
    second_novel = add_novel(db_session, filename="second.txt")
    first_analysis = add_analysis(db_session, first_novel)
    second_analysis = add_analysis(db_session, second_novel)
    source = add_character(db_session, first_analysis, name="林舟")
    target = add_character(db_session, second_analysis, name="苏晚")
    db_session.commit()

    db_session.add(
        CharacterRelationship(
            analysis_id=first_analysis.id,
            source_character_id=source.id,
            target_character_id=target.id,
            directed=True,
            relation_type="错误跨书关系",
            description="不应允许跨分析快照。",
            evidence_chapter_indices=[1],
        )
    )
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        db_session.commit()
    db_session.rollback()


def test_undirected_relationship_requires_canonical_endpoint_order(
    db_session: Session,
) -> None:
    novel = add_novel(db_session)
    analysis = add_analysis(db_session, novel)
    first = add_character(db_session, analysis, name="林舟")
    second = add_character(db_session, analysis, name="苏晚")
    db_session.commit()
    assert first.id < second.id

    db_session.add(
        CharacterRelationship(
            analysis_id=analysis.id,
            source_character_id=first.id,
            target_character_id=second.id,
            directed=False,
            relation_type="盟友",
            description="规范顺序的无向边。",
            evidence_chapter_indices=[1],
        )
    )
    db_session.commit()

    db_session.add(
        CharacterRelationship(
            analysis_id=analysis.id,
            source_character_id=second.id,
            target_character_id=first.id,
            directed=False,
            relation_type="盟友",
            description="反向无向边不允许持久化。",
            evidence_chapter_indices=[2],
        )
    )
    with pytest.raises(
        IntegrityError,
        match="ck_character_relationships_undirected_canonical",
    ):
        db_session.commit()
    db_session.rollback()

    db_session.add(
        CharacterRelationship(
            analysis_id=analysis.id,
            source_character_id=second.id,
            target_character_id=first.id,
            directed=True,
            relation_type="追踪",
            description="有向边允许反向端点顺序。",
            evidence_chapter_indices=[2],
        )
    )
    db_session.commit()


def test_appearance_chapter_must_belong_to_character_novel(
    db_session: Session,
) -> None:
    first_novel = add_novel(db_session, filename="first.txt")
    second_novel = add_novel(db_session, filename="second.txt")
    first_analysis = add_analysis(db_session, first_novel)
    add_analysis(db_session, second_novel)
    character = add_character(
        db_session,
        first_analysis,
        name="林舟",
    )
    db_session.commit()

    db_session.execute(
        text(
            """
            INSERT INTO character_appearances (
                analysis_id, novel_id,
                character_id,
                chapter_id,
                mention_count,
                summary
            )
            VALUES (
                :analysis_id, :novel_id,
                :character_id,
                :chapter_id,
                1,
                '同书合法出场。'
            )
            """
        ),
        {
            "analysis_id": first_analysis.id,
                "novel_id": first_novel.id,
                "character_id": character.id,
            "chapter_id": first_novel.chapters[0].id,
        },
    )
    db_session.commit()

    ownership = db_session.execute(
        text(
            """
            SELECT analysis_id, novel_id
            FROM character_appearances
            WHERE character_id = :character_id
            """
        ),
        {"character_id": character.id},
    ).one()
    assert ownership.analysis_id == first_analysis.id
    assert ownership.novel_id == first_novel.id

    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        db_session.execute(
            text(
                """
                INSERT INTO character_appearances (
                analysis_id, novel_id,
                    character_id,
                    chapter_id,
                    mention_count,
                    summary
                )
                VALUES (
                :analysis_id, :novel_id,
                    :character_id,
                    :chapter_id,
                    1,
                    '跨书非法出场。'
                )
                """
            ),
            {
                "analysis_id": first_analysis.id,
                "novel_id": first_novel.id,
                "character_id": character.id,
                "chapter_id": second_novel.chapters[0].id,
            },
        )
        db_session.commit()
    db_session.rollback()


@pytest.mark.parametrize(
    ("model_factory", "constraint_name"),
    [
        (
            lambda novel, analysis, source, target: Character(
                analysis_id=analysis.id,
                canonical_name="区间错误",
                aliases=[],
                short_description="错误。",
                first_chapter_index=2,
                last_chapter_index=1,
                chapter_count=1,
                mention_count=1,
            ),
            "ck_characters_chapter_range",
        ),
        (
            lambda novel, analysis, source, target: CharacterAppearance(
                analysis_id=source.analysis_id,
                novel_id=novel.id,
                character_id=source.id,
                chapter_id=novel.chapters[0].id,
                mention_count=0,
                summary="无有效提及。",
            ),
            "ck_character_appearances_mention_count_positive",
        ),
        (
            lambda novel, analysis, source, target: CharacterRelationship(
                analysis_id=analysis.id,
                source_character_id=source.id,
                target_character_id=target.id,
                directed=False,
                relation_type="无证据",
                description="证据章节不能为空。",
                evidence_chapter_indices=[],
            ),
            "ck_character_relationships_evidence_nonempty",
        ),
        (
            lambda novel, analysis, source, target: Character(
                analysis_id=analysis.id,
                canonical_name="零出场人物",
                aliases=[],
                short_description="错误。",
                first_chapter_index=1,
                last_chapter_index=1,
                chapter_count=0,
                mention_count=1,
            ),
            "ck_characters_chapter_count_positive",
        ),
    ],
)
def test_character_tables_reject_invalid_values(
    db_session: Session,
    model_factory,
    constraint_name: str,
) -> None:
    novel = add_novel(db_session)
    analysis = add_analysis(db_session, novel)
    source = add_character(db_session, analysis, name="林舟")
    target = add_character(db_session, analysis, name="苏晚")
    db_session.commit()

    db_session.add(model_factory(novel, analysis, source, target))
    with pytest.raises(IntegrityError, match=constraint_name):
        db_session.commit()
    db_session.rollback()


def test_raw_novel_delete_cascades_through_character_snapshot(
    db_session: Session,
) -> None:
    novel = add_novel(db_session)
    extraction = ChapterCharacterExtraction(
        chapter_id=novel.chapters[0].id,
        extraction_data={"candidates": []},
        source_hash="f" * 64,
        model="deepseek-v4-pro",
        prompt_version="characters-extract-v1",
    )
    analysis = add_analysis(db_session, novel)
    source = add_character(db_session, analysis, name="林舟")
    target = add_character(db_session, analysis, name="苏晚")
    db_session.add_all(
        [
            extraction,
            CharacterAppearance(
                analysis_id=source.analysis_id,
                novel_id=novel.id,
                character_id=source.id,
                chapter_id=novel.chapters[0].id,
                mention_count=1,
                summary="林舟出场。",
            ),
            CharacterRelationship(
                analysis_id=analysis.id,
                source_character_id=source.id,
                target_character_id=target.id,
                directed=False,
                relation_type="盟友",
                description="共同脱险。",
                evidence_chapter_indices=[1],
            ),
        ]
    )
    db_session.commit()

    db_session.execute(
        text("DELETE FROM novels WHERE id = :novel_id"),
        {"novel_id": novel.id},
    )
    db_session.commit()

    assert db_session.query(ChapterCharacterExtraction).count() == 0
    assert db_session.query(CharacterAnalysis).count() == 0
    assert db_session.query(Character).count() == 0
    assert db_session.query(CharacterAppearance).count() == 0
    assert db_session.query(CharacterRelationship).count() == 0


def test_raw_parent_deletes_cascade_only_matching_appearances(
    db_session: Session,
) -> None:
    novel = add_novel(db_session)
    analysis = add_analysis(db_session, novel)
    first = add_character(db_session, analysis, name="林舟")
    second = add_character(db_session, analysis, name="苏晚")
    db_session.add_all(
        [
            CharacterAppearance(
                analysis_id=first.analysis_id,
                novel_id=novel.id,
                character_id=first.id,
                chapter_id=novel.chapters[0].id,
                mention_count=1,
                summary="林舟在第一章出场。",
            ),
            CharacterAppearance(
                analysis_id=second.analysis_id,
                novel_id=novel.id,
                character_id=second.id,
                chapter_id=novel.chapters[1].id,
                mention_count=1,
                summary="苏晚在第二章出场。",
            ),
        ]
    )
    db_session.commit()

    db_session.execute(
        text("DELETE FROM chapters WHERE id = :chapter_id"),
        {"chapter_id": novel.chapters[0].id},
    )
    db_session.commit()

    remaining = db_session.query(CharacterAppearance).one()
    assert remaining.character_id == second.id
    assert db_session.query(Character).count() == 2

    db_session.execute(
        text("DELETE FROM characters WHERE id = :character_id"),
        {"character_id": second.id},
    )
    db_session.commit()

    assert db_session.query(CharacterAppearance).count() == 0
