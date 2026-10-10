import logging
from collections import defaultdict
from typing import Callable, Optional
from sqlalchemy.orm import Session
from backend.app.analyzer.llm import LLMProvider
from backend.app.core.time import utc_now
from backend.app.models.character import (
    Character,
    CharacterAnalysis,
    CharacterAppearance,
    CharacterRelationship,
)
from backend.app.models.novel import Novel
from .payloads import (
    CharacterRelationPayload,
    StoredCharacterObservation,
    _PreparedChapter,
    _ResolvedProfile,
    CHARACTER_EXTRACTION_PROMPT_VERSION,
    CHARACTER_MERGE_PROMPT_VERSION,
    CHARACTER_RELATIONSHIP_PROMPT_VERSION,
)
from .extraction import (
    _analysis_cache_is_current,
    _analysis_hash_input,
    _ensure_can_continue,
    _persist_chapter_extraction,
    _prepare_chapters,
    character_analysis_source_hash,
)
from .merge import _infer_relationships, _merge_profiles

logger = logging.getLogger(__name__)

def _persist_snapshot(
# 新人物、出场和关系在一个事务中替换，读者不会观察到新旧快照混杂。
    db: Session,
    novel: Novel,
    provider: LLMProvider,
    prepared: list[_PreparedChapter],
    source_hash: str,
    profiles: list[_ResolvedProfile],
    relationships: list[CharacterRelationPayload],
) -> CharacterAnalysis:
    """保存前复核逐章来源；任一失败 rollback 并保留旧快照。"""
    for item in prepared:
        if not item.changed:
            continue
        _persist_chapter_extraction(
            db,
            item,
            provider_model=provider.model,
        )

    now = utc_now()

    existing_analysis = (
        db.query(CharacterAnalysis)
        .filter(CharacterAnalysis.novel_id == novel.id)
        .one_or_none()
    )
    if existing_analysis is not None:
        db.delete(existing_analysis)
        db.flush()

    analysis = CharacterAnalysis(
        novel_id=novel.id,
        source_hash=source_hash,
        model=provider.model,
        extraction_prompt_version=CHARACTER_EXTRACTION_PROMPT_VERSION,
        merge_prompt_version=CHARACTER_MERGE_PROMPT_VERSION,
        relationship_prompt_version=(
            CHARACTER_RELATIONSHIP_PROMPT_VERSION
        ),
        chapter_count=len(prepared),
        created_at=now,
        updated_at=now,
    )
    db.add(analysis)
    db.flush()

    observations_by_id: dict[
        str,
        tuple[_PreparedChapter, StoredCharacterObservation],
    ] = {}
    for item in prepared:
        for observation in item.extraction_data.observations:
            observations_by_id[observation.observation_id] = (
                item,
                observation,
            )

    characters_by_profile: dict[str, Character] = {}
    appearance_rows: list[
        tuple[Character, _PreparedChapter, int, str]
    ] = []
    for profile in profiles:
        grouped: dict[int, list[StoredCharacterObservation]] = (
            defaultdict(list)
        )
        chapters_by_id: dict[int, _PreparedChapter] = {}
        for observation_id in profile.payload.observation_ids:
            prepared_chapter, observation = observations_by_id[
                observation_id
            ]
            grouped[prepared_chapter.chapter.id].append(observation)
            chapters_by_id[prepared_chapter.chapter.id] = (
                prepared_chapter
            )
        chapter_indices = [
            chapters_by_id[chapter_id].chapter.chapter_index
            for chapter_id in grouped
        ]
        character = Character(
            analysis_id=analysis.id,
            canonical_name=profile.payload.canonical_name,
            aliases=list(profile.payload.aliases),
            short_description=profile.payload.short_description,
            first_chapter_index=min(chapter_indices),
            last_chapter_index=max(chapter_indices),
            chapter_count=len(grouped),
            mention_count=sum(
                observation.mention_count
                for observations in grouped.values()
                for observation in observations
            ),
        )
        db.add(character)
        characters_by_profile[profile.profile_id] = character
        for chapter_id, observations in grouped.items():
            summaries = list(
                dict.fromkeys(
                    observation.appearance_summary
                    for observation in observations
                )
            )
            appearance_rows.append(
                (
                    character,
                    chapters_by_id[chapter_id],
                    sum(
                        observation.mention_count
                        for observation in observations
                    ),
                    "\n".join(summaries),
                )
            )
    db.flush()

    for character, prepared_chapter, mention_count, summary in (
        appearance_rows
    ):
        db.add(
            CharacterAppearance(
                analysis_id=analysis.id,
                novel_id=novel.id,
                character_id=character.id,
                chapter_id=prepared_chapter.chapter.id,
                mention_count=mention_count,
                summary=summary,
            )
        )
    for relation in relationships:
        source = characters_by_profile[relation.source_profile_id]
        target = characters_by_profile[relation.target_profile_id]
        db.add(
            CharacterRelationship(
                analysis_id=analysis.id,
                source_character_id=source.id,
                target_character_id=target.id,
                directed=bool(relation.directed),
                relation_type=relation.relation_type,
                description=relation.description,
                evidence_chapter_indices=list(
                    relation.evidence_chapter_indices
                ),
            )
        )
    db.flush()
    return analysis


def analyze_novel_characters(
    db: Session,
    novel: Novel,
    provider: LLMProvider,
    force: bool = False,
    persist_extractions_incrementally: bool = False,
    should_continue: Optional[Callable[[], bool]] = None,
    on_generation_step: Optional[Callable[[], None]] = None,
) -> tuple[CharacterAnalysis, bool]:
    """Build or reuse the current character snapshot without committing."""
    prepared = _prepare_chapters(
        db,
        novel,
        provider,
        force=force,
        persist_extractions_incrementally=(
            persist_extractions_incrementally
        ),
        should_continue=should_continue,
        on_generation_step=on_generation_step,
    )
    hash_input = _analysis_hash_input(prepared)
    source_hash = character_analysis_source_hash(hash_input)
    existing_analysis = (
        db.query(CharacterAnalysis)
        .filter(CharacterAnalysis.novel_id == novel.id)
        .one_or_none()
    )
    extraction_changed = any(item.changed for item in prepared)
    if (
        not force
        and not extraction_changed
        and _analysis_cache_is_current(
            existing_analysis,
            source_hash=source_hash,
            provider_model=provider.model,
            chapter_count=len(prepared),
        )
    ):
        assert existing_analysis is not None
        return existing_analysis, False

    profiles, batches = _merge_profiles(
        prepared,
        provider,
        should_continue=should_continue,
        on_generation_step=on_generation_step,
    )
    relationships = _infer_relationships(
        profiles,
        batches,
        provider,
        should_continue=should_continue,
        on_generation_step=on_generation_step,
    )
    _ensure_can_continue(should_continue)
    analysis = _persist_snapshot(
        db,
        novel,
        provider,
        prepared,
        source_hash,
        profiles,
        relationships,
    )
    return analysis, True
