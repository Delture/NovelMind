
import json
from collections import deque
from pathlib import Path
from typing import Callable, Optional, Type

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.analyzer import jobs as job_helpers
from backend.app.analyzer.character_pipeline import extraction as character_service
from backend.app.analyzer.characters import (
    CHARACTER_EXTRACTION_PROMPT_VERSION,
    CHARACTER_MERGE_PROMPT_VERSION,
    CHARACTER_RELATIONSHIP_PROMPT_VERSION,
    MAX_CHARACTER_STAGE_INPUT_CHARACTERS,
    CharacterMergePayload,
    CharacterReconciliationPayload,
    CharacterRelationPayload,
    CharacterRelationsPayload,
    ChapterCharacterExtractionPayload,
    CharacterObservationPayload,
    SynthesizedCharacterProfilePayload,
    analyze_novel_characters,
    run_character_job,
)
from backend.app.analyzer.llm import (
    JsonGenerationResult,
    ProviderResponseError,
    ProviderUnavailableError,
)
from backend.app.core.database import Base, create_db_engine
from backend.app.models.analysis import AnalysisJob
from backend.app.models.character import (
    ChapterCharacterExtraction,
    Character,
    CharacterAnalysis,
    CharacterAppearance,
    CharacterRelationship,
)
from backend.app.models.novel import Chapter, Novel


ResponseFactory = Callable[
    [Type[BaseModel], str],
    object,
]


class QueueProvider:
    model = "deepseek-v4-pro"

    def __init__(self, responses: list[object]) -> None:
        self.responses = deque(responses)
        self.calls: list[dict[str, object]] = []
        self.close_calls = 0

    def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: Type[BaseModel],
        thinking: bool = False,
        reasoning_effort: Optional[str] = None,
        before_attempt: Optional[Callable[[], None]] = None,
    ) -> JsonGenerationResult:
        if before_attempt is not None:
            before_attempt()
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "response_model": response_model,
                "thinking": thinking,
                "reasoning_effort": reasoning_effort,
            }
        )
        if not self.responses:
            raise AssertionError("Unexpected provider call.")
        queued = self.responses.popleft()
        if isinstance(queued, Exception):
            raise queued
        if callable(queued):
            queued = queued(response_model, user_prompt)
        value = (
            queued
            if isinstance(queued, response_model)
            else response_model.model_validate(queued)
        )
        return JsonGenerationResult(
            value=value,
            model=self.model,
            prompt_tokens=100,
            completion_tokens=50,
            cache_hit_tokens=0,
            request_id="req-character-test",
        )

    def close(self) -> None:
        self.close_calls += 1


@pytest.fixture()
def session_factory() -> Callable[[], Session]:
    engine = create_db_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
    )
    try:
        yield factory
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _add_novel(
    session: Session,
    *,
    contents: Optional[list[str]] = None,
) -> Novel:
    chapter_contents = contents or ["林默推门而入。", "阿默再次遇见苏晴。"]
    novel = Novel(
        filename="characters.txt",
        encoding="utf-8",
        chapter_count=len(chapter_contents),
        chapters=[
            Chapter(
                chapter_index=index,
                title=f"第{index}章",
                content=content,
                character_count=len(content),
            )
            for index, content in enumerate(chapter_contents, start=1)
        ],
    )
    session.add(novel)
    session.commit()
    session.refresh(novel)
    return novel


def _observation(
    name: str,
    *,
    aliases: Optional[list[str]] = None,
    mentions: int = 1,
    summary: Optional[str] = None,
) -> dict[str, object]:
    return {
        "name": name,
        "aliases": aliases or [],
        "short_description": f"{name}的人物简介",
        "mention_count": mentions,
        "appearance_summary": summary or f"{name}在本章出场。",
    }


def _extract(
    *observations: dict[str, object],
    has_more: bool = False,
) -> dict[str, object]:
    return {
        "observations": list(observations),
        "has_more": has_more,
    }


def _prompt_data(user_prompt: str) -> dict[str, object]:
    return json.loads(user_prompt.split("\n", 1)[1])


def _merge_by_name(
    _: Type[BaseModel],
    user_prompt: str,
) -> dict[str, object]:
    data = _prompt_data(user_prompt)
    grouped: dict[str, list[dict[str, object]]] = {}
    for chapter in data["chapters"]:
        for observation in chapter["observations"]:
            name = str(observation["name"])
            grouped.setdefault(name, []).append(observation)
    return {
        "profiles": [
            {
                "observation_ids": [
                    item["observation_id"] for item in observations
                ],
                "canonical_name": name,
                "aliases": [],
                "short_description": f"{name}的全书档案",
            }
            for name, observations in grouped.items()
        ]
    }


def _reconcile_separately(
    _: Type[BaseModel],
    user_prompt: str,
) -> dict[str, object]:
    data = _prompt_data(user_prompt)
    return {
        "groups": [
            {"profile_ids": [profile["profile_id"]]}
            for profile in data["profiles"]
        ]
    }


def _reconcile_all(
    _: Type[BaseModel],
    user_prompt: str,
) -> dict[str, object]:
    data = _prompt_data(user_prompt)
    return {
        "groups": [
            {
                "profile_ids": [
                    profile["profile_id"]
                    for profile in data["profiles"]
                ]
            }
        ]
    }


def _synthesize_profiles(
    _: Type[BaseModel],
    user_prompt: str,
) -> dict[str, object]:
    data = _prompt_data(user_prompt)
    profiles = data["profiles"]
    return {
        "profile_ids": [
            profile["profile_id"] for profile in profiles
        ],
        "canonical_name": profiles[0]["canonical_name"],
        "aliases": [],
        "short_description": "外部 API 合并后的全书档案",
    }


def _no_relationships(
    _: Type[BaseModel],
    __: str,
) -> dict[str, object]:
    return {"relationships": []}


def test_payloads_reject_untrusted_or_invalid_shapes() -> None:
    with pytest.raises(ValidationError):
        ChapterCharacterExtractionPayload.model_validate(
            {
                "observations": [
                    {
                        **_observation("林默"),
                        "unsupported": "do not accept",
                    }
                ],
                "has_more": False,
            }
        )
    with pytest.raises(ValidationError):
        ChapterCharacterExtractionPayload.model_validate(
            {"observations": [_observation(" ", mentions=0)]}
        )
    with pytest.raises(ValidationError):
        ChapterCharacterExtractionPayload.model_validate(
            {"observations": [], "has_more": True}
        )
    with pytest.raises(ValidationError):
        CharacterMergePayload.model_validate(
            {
                "profiles": [
                    {
                        "observation_ids": ["obs-1", "obs-1"],
                        "canonical_name": "林默",
                        "aliases": [],
                        "short_description": "简介",
                    }
                ]
            }
        )
    with pytest.raises(ValidationError):
        CharacterRelationPayload.model_validate(
            {
                "source_profile_id": "profile-0001",
                "target_profile_id": "profile-0001",
                "directed": False,
                "relation_type": "朋友",
                "description": "旧友",
                "evidence_chapter_indices": [1],
            }
        )
    with pytest.raises(ValidationError):
        CharacterRelationPayload.model_validate(
            {
                "source_profile_id": "profile-0001",
                "target_profile_id": "profile-0002",
                "directed": False,
                "relation_type": "朋友",
                "description": "旧友",
                "evidence_chapter_indices": [],
            }
        )


def test_observation_drops_primary_name_from_aliases() -> None:
    payload = CharacterObservationPayload.model_validate(
        {
            **_observation("林默", aliases=["林默", "默哥", "林默"]),
        }
    )
    assert payload.aliases == ["默哥"]
    again = CharacterObservationPayload.model_validate(
        payload.model_dump(mode="json")
    )
    assert again.aliases == ["默哥"]


def test_observation_truncates_aliases_preserving_order() -> None:
    payload = CharacterObservationPayload.model_validate(
        {
            **_observation(
                "林默",
                aliases=["默哥", "林哥哥", "默哥", "少侠", "小默"],
            ),
        }
    )
    assert payload.aliases == ["默哥", "林哥哥", "少侠"]


def test_extraction_truncates_observations_and_infers_has_more() -> None:
    first_eight = [
        _observation(f"人物{index}") for index in range(1, 9)
    ]
    overflow = [
        _observation("人物9"),
        _observation("人物1"),
        _observation("人物10"),
    ]
    truncated = ChapterCharacterExtractionPayload.model_validate(
        {
            "observations": first_eight + overflow,
            "has_more": False,
        }
    )
    assert [item.name for item in truncated.observations] == [
        f"人物{index}" for index in range(1, 9)
    ]
    assert truncated.has_more is True

    missing_full = ChapterCharacterExtractionPayload.model_validate(
        {"observations": first_eight}
    )
    assert missing_full.has_more is True

    missing_partial = ChapterCharacterExtractionPayload.model_validate(
        {"observations": first_eight[:3]}
    )
    assert missing_partial.has_more is False

    respected = ChapterCharacterExtractionPayload.model_validate(
        _extract(*first_eight[:2], has_more=True)
    )
    assert respected.has_more is True


def test_extraction_still_rejects_unknown_fields_and_bad_has_more() -> None:
    with pytest.raises(ValidationError):
        ChapterCharacterExtractionPayload.model_validate(
            {
                "observations": [_observation("林默")],
                "has_more": False,
                "extra_note": "nope",
            }
        )
    with pytest.raises(ValidationError):
        ChapterCharacterExtractionPayload.model_validate(
            {
                "observations": [_observation("林默")],
                "has_more": "true",
            }
        )


def test_extraction_prompt_states_json_integer_and_boolean_contract() -> None:
    prompt = character_service._extraction_system_prompt()
    assert CHARACTER_EXTRACTION_PROMPT_VERSION == "character-extraction-v6"
    assert "JSON 整数" in prompt
    assert "JSON boolean" in prompt
    assert '"has_more":false' in prompt


def test_extraction_split_bounds_pathological_json_escaping_and_keeps_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        character_service,
        "MAX_CHARACTER_EXTRACTION_CHARACTERS",
        100_000,
    )
    monkeypatch.setattr(
        character_service,
        "MAX_CHARACTER_EXTRACTION_TEXT_REQUEST_CHARACTERS",
        300,
    )
    title = '"\\控制'
    content = ('"\\\n\t\r\x01正文' * 200) + "结尾"

    chunks = character_service._split_character_text(
        content,
        title=title,
        chapter_index=1,
    )

    assert len(chunks) > 1
    assert "".join(chunks) == content
    for part_index, chunk in enumerate(chunks, start=1):
        request_data = character_service._character_extraction_request_data(
            title=title,
            chapter_index=1,
            part_index=part_index,
            part_count=len(chunks),
            page_index=1,
            previous_observations=[],
            text=chunk,
        )
        assert len(character_service._canonical_json(request_data)) <= 300


def test_every_later_stage_api_payload_respects_serialized_input_bound(
    session_factory: Callable[[], Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "backend.app.analyzer.character_pipeline.merge.MAX_CHARACTER_MERGE_BATCH_ITEMS",
        1,
    )
    provider = QueueProvider(
        [
            _extract(
                _observation("阿默", aliases=["林默"]),
                _observation("林先生"),
            ),
            _merge_by_name,
            _merge_by_name,
            _reconcile_all,
            _synthesize_profiles,
            _no_relationships,
        ]
    )

    with session_factory() as session:
        novel = _add_novel(
            session,
            contents=["阿默也被称作林先生。"],
        )
        analyze_novel_characters(session, novel, provider)

    later_stage_calls = provider.calls[1:]
    assert later_stage_calls
    for call in later_stage_calls:
        serialized_data = call["user_prompt"].split("\n", 1)[1]
        assert len(serialized_data) <= MAX_CHARACTER_STAGE_INPUT_CHARACTERS


def test_character_analysis_uses_api_and_builds_local_statistics(
    session_factory: Callable[[], Session],
) -> None:
    provider = QueueProvider(
        [
            _extract(_observation("林默", mentions=2)),
            _extract(
                _observation(
                    "林默",
                    aliases=["阿默"],
                    mentions=3,
                    summary="林默与苏晴重逢。",
                ),
                _observation("苏晴", mentions=1),
            ),
            _merge_by_name,
            _reconcile_separately,
            lambda _, prompt: {
                "relationships": [
                    {
                        "source_profile_id": (
                            _prompt_data(prompt)["profiles"][0][
                                "profile_id"
                            ]
                        ),
                        "target_profile_id": (
                            _prompt_data(prompt)["profiles"][1][
                                "profile_id"
                            ]
                        ),
                        "directed": False,
                        "relation_type": "旧友",
                        "description": "两人在第二章重逢。",
                        "evidence_chapter_indices": [2],
                    }
                ]
            },
        ]
    )
    with session_factory() as session:
        novel = _add_novel(session)
        analysis, created = analyze_novel_characters(
            session,
            novel,
            provider,
        )
        session.commit()
        analysis_id = analysis.id

    assert created is True
    assert len(provider.calls) == 5
    assert all(call["thinking"] is False for call in provider.calls)
    assert "指代同一性判准（Identity Resolution Test）" in str(provider.calls[0]["system_prompt"])

    with session_factory() as session:
        analysis = session.get(CharacterAnalysis, analysis_id)
        characters = (
            session.query(Character)
            .filter(Character.analysis_id == analysis_id)
            .order_by(Character.canonical_name)
            .all()
        )
        appearances = (
            session.query(CharacterAppearance)
            .join(Character)
            .filter(Character.analysis_id == analysis_id)
            .all()
        )
        relationships = (
            session.query(CharacterRelationship)
            .filter(CharacterRelationship.analysis_id == analysis_id)
            .all()
        )
        assert analysis is not None
        assert analysis.model == provider.model
        assert analysis.extraction_prompt_version == (
            CHARACTER_EXTRACTION_PROMPT_VERSION
        )
        assert analysis.merge_prompt_version == (
            CHARACTER_MERGE_PROMPT_VERSION
        )
        assert analysis.relationship_prompt_version == (
            CHARACTER_RELATIONSHIP_PROMPT_VERSION
        )
        assert len(characters) == 2
        lin = next(
            item for item in characters if item.canonical_name == "林默"
        )
        assert lin.first_chapter_index == 1
        assert lin.last_chapter_index == 2
        assert lin.chapter_count == 2
        assert lin.mention_count == 5
        assert len(
            [
                item
                for item in appearances
                if item.character_id == lin.id
            ]
        ) == 2
        assert len(relationships) == 1
        assert relationships[0].evidence_chapter_indices == [2]


def test_cross_batch_profiles_with_different_names_merge_only_after_api(
    session_factory: Callable[[], Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        'backend.app.analyzer.character_pipeline.merge.MAX_CHARACTER_MERGE_BATCH_ITEMS',
        1,
    )
    provider = QueueProvider(
        [
            _extract(
                _observation("阿默", aliases=["林默"], mentions=2),
                _observation("林先生", mentions=3),
            ),
            _merge_by_name,
            _merge_by_name,
            _reconcile_all,
            _synthesize_profiles,
            _no_relationships,
        ]
    )

    with session_factory() as session:
        novel = _add_novel(
            session,
            contents=["阿默以林先生的身份再次出现。"],
        )
        analysis, created = analyze_novel_characters(
            session,
            novel,
            provider,
        )
        session.commit()
        character = (
            session.query(Character)
            .filter(Character.analysis_id == analysis.id)
            .one()
        )

        assert created is True
        assert character.canonical_name == "阿默"
        assert character.mention_count == 5
        assert character.chapter_count == 1

    assert [call["response_model"] for call in provider.calls] == [
        ChapterCharacterExtractionPayload,
        CharacterMergePayload,
        CharacterMergePayload,
        CharacterReconciliationPayload,
        SynthesizedCharacterProfilePayload,
        CharacterRelationsPayload,
    ]


def test_relationship_inference_covers_profiles_from_different_batches(
    session_factory: Callable[[], Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        'backend.app.analyzer.character_pipeline.merge.MAX_CHARACTER_MERGE_BATCH_ITEMS',
        1,
    )

    def cross_batch_relationship(
        _: Type[BaseModel],
        user_prompt: str,
    ) -> dict[str, object]:
        data = _prompt_data(user_prompt)
        profile_ids = [
            str(profile["profile_id"])
            for profile in data["profiles"]
        ]
        return {
            "relationships": [
                {
                    "source_profile_id": profile_ids[0],
                    "target_profile_id": profile_ids[1],
                    "directed": False,
                    "relation_type": "同事",
                    "description": "两人在同一章共同办案。",
                    "evidence_chapter_indices": [1],
                }
            ]
        }

    provider = QueueProvider(
        [
            _extract(
                _observation("林默"),
                _observation("苏晴"),
            ),
            _merge_by_name,
            _merge_by_name,
            _reconcile_separately,
            cross_batch_relationship,
        ]
    )

    with session_factory() as session:
        novel = _add_novel(
            session,
            contents=["林默与苏晴共同办案。"],
        )
        analysis, _ = analyze_novel_characters(
            session,
            novel,
            provider,
        )
        session.commit()
        relationship = (
            session.query(CharacterRelationship)
            .filter(CharacterRelationship.analysis_id == analysis.id)
            .one()
        )

        assert relationship.relation_type == "同事"
        assert relationship.evidence_chapter_indices == [1]


@pytest.mark.parametrize(
    "bad_reconciliation",
    [
        {
            "groups": [
                {"profile_ids": ["batch-0001-profile-0001"]},
            ]
        },
        {
            "groups": [
                {
                    "profile_ids": [
                        "batch-0001-profile-0001",
                        "unknown-profile",
                    ]
                },
                {"profile_ids": ["batch-0002-profile-0001"]},
            ]
        },
        {
            "groups": [
                {"profile_ids": ["batch-0001-profile-0001"]},
                {
                    "profile_ids": [
                        "batch-0001-profile-0001",
                        "batch-0002-profile-0001",
                    ]
                },
            ]
        },
    ],
)
def test_reconciliation_must_partition_candidate_profiles_exactly_once(
    session_factory: Callable[[], Session],
    monkeypatch: pytest.MonkeyPatch,
    bad_reconciliation: dict[str, object],
) -> None:
    monkeypatch.setattr(
        'backend.app.analyzer.character_pipeline.merge.MAX_CHARACTER_MERGE_BATCH_ITEMS',
        1,
    )
    provider = QueueProvider(
        [
            _extract(
                _observation("林默"),
                _observation("苏晴"),
            ),
            _merge_by_name,
            _merge_by_name,
            bad_reconciliation,
        ]
    )

    with session_factory() as session:
        novel = _add_novel(
            session,
            contents=["林默遇见苏晴。"],
        )
        with pytest.raises(ProviderResponseError):
            analyze_novel_characters(session, novel, provider)
        session.rollback()
        assert session.query(CharacterAnalysis).count() == 0


def test_character_extraction_continues_api_pages_without_silent_loss(
    session_factory: Callable[[], Session],
) -> None:
    first_page = [
        _observation("群像人物", summary=f"第 {index} 条观察")
        for index in range(1, 9)
    ]
    second_page = [
        _observation("群像人物", summary=f"第 {index} 条观察")
        for index in range(9, 11)
    ]
    provider = QueueProvider(
        [
            _extract(*first_page, has_more=True),
            _extract(*second_page),
            _merge_by_name,
            _no_relationships,
        ]
    )

    with session_factory() as session:
        novel = _add_novel(
            session,
            contents=["十个人物依次在这一段中出现。"],
        )
        analysis, _ = analyze_novel_characters(
            session,
            novel,
            provider,
        )
        session.commit()
        extraction = session.query(ChapterCharacterExtraction).one()
        character = (
            session.query(Character)
            .filter(Character.analysis_id == analysis.id)
            .one()
        )

        assert len(extraction.extraction_data["observations"]) == 10
        assert character.mention_count == 10

    second_request = _prompt_data(
        str(provider.calls[1]["user_prompt"])
    )
    assert second_request["page_index"] == 2
    assert len(second_request["previous_observations"]) == 8
    assert len(provider.calls) == 4


def test_character_extraction_page_limit_fails_atomically(
    session_factory: Callable[[], Session],
) -> None:
    provider = QueueProvider(
        [
            _extract(
                _observation(
                    "群像人物",
                    summary=f"分页观察 {page_index}",
                ),
                has_more=True,
            )
            for page_index in range(1, 17)
        ]
    )

    with session_factory() as session:
        novel = _add_novel(session, contents=["超大群像章节。"])
        with pytest.raises(ProviderResponseError):
            analyze_novel_characters(session, novel, provider)
        session.rollback()
        assert session.query(CharacterAnalysis).count() == 0
        assert session.query(ChapterCharacterExtraction).count() == 0

    assert len(provider.calls) == 16


def test_unchanged_analysis_is_zero_call_cache_hit_and_force_rebuilds(
    session_factory: Callable[[], Session],
) -> None:
    initial_provider = QueueProvider(
        [
            _extract(_observation("林默")),
            _extract(_observation("林默")),
            _merge_by_name,
            _no_relationships,
        ]
    )
    with session_factory() as session:
        novel = _add_novel(session)
        initial, created = analyze_novel_characters(
            session,
            novel,
            initial_provider,
        )
        session.commit()
        novel_id = novel.id
        initial_id = initial.id
    assert created is True

    cached_provider = QueueProvider([])
    with session_factory() as session:
        novel = session.get(Novel, novel_id)
        cached, created = analyze_novel_characters(
            session,
            novel,
            cached_provider,
        )
        assert cached.id == initial_id
        assert created is False
    assert cached_provider.calls == []

    forced_provider = QueueProvider(
        [
            _extract(_observation("林默")),
            _extract(_observation("林默")),
            _merge_by_name,
            _no_relationships,
        ]
    )
    with session_factory() as session:
        novel = session.get(Novel, novel_id)
        rebuilt, created = analyze_novel_characters(
            session,
            novel,
            forced_provider,
            force=True,
        )
        session.commit()
        assert rebuilt.id is not None
        assert created is True
    assert len(forced_provider.calls) == 4


def test_one_changed_chapter_only_reextracts_it_then_rebuilds_global(
    session_factory: Callable[[], Session],
) -> None:
    initial_provider = QueueProvider(
        [
            _extract(_observation("林默")),
            _extract(_observation("苏晴")),
            _merge_by_name,
            _reconcile_separately,
            _no_relationships,
        ]
    )
    with session_factory() as session:
        novel = _add_novel(session)
        analyze_novel_characters(session, novel, initial_provider)
        session.commit()
        novel_id = novel.id
        unchanged_chapter_id = novel.chapters[0].id
        unchanged_extraction_id = (
            session.query(ChapterCharacterExtraction.id)
            .filter(
                ChapterCharacterExtraction.chapter_id
                == unchanged_chapter_id
            )
            .scalar()
        )

    changed_provider = QueueProvider(
        [
            _extract(_observation("苏晴", mentions=2)),
            _merge_by_name,
            _reconcile_separately,
            _no_relationships,
        ]
    )
    with session_factory() as session:
        novel = session.get(Novel, novel_id)
        novel.chapters[1].content += "她决定同行。"
        session.commit()
        analyze_novel_characters(session, novel, changed_provider)
        session.commit()
        assert (
            session.query(ChapterCharacterExtraction.id)
            .filter(
                ChapterCharacterExtraction.chapter_id
                == unchanged_chapter_id
            )
            .scalar()
            == unchanged_extraction_id
        )

    assert len(changed_provider.calls) == 4
    assert changed_provider.calls[0]["response_model"] is (
        ChapterCharacterExtractionPayload
    )
    assert changed_provider.calls[1]["response_model"] is (
        CharacterMergePayload
    )
    assert changed_provider.calls[2]["response_model"] is (
        CharacterReconciliationPayload
    )
    assert changed_provider.calls[3]["response_model"] is (
        CharacterRelationsPayload
    )


@pytest.mark.parametrize(
    "bad_merge",
    [
        {
            "profiles": [],
        },
        {
            "profiles": [
                {
                    "observation_ids": ["unknown-observation"],
                    "canonical_name": "林默",
                    "aliases": [],
                    "short_description": "档案",
                }
            ]
        },
    ],
)
def test_merge_must_partition_every_observation_exactly_once(
    session_factory: Callable[[], Session],
    bad_merge: dict[str, object],
) -> None:
    provider = QueueProvider(
        [
            _extract(_observation("林默")),
            bad_merge,
        ]
    )
    with session_factory() as session:
        novel = _add_novel(session, contents=["林默出现。"])
        with pytest.raises(ProviderResponseError):
            analyze_novel_characters(session, novel, provider)
        session.rollback()
        assert session.query(CharacterAnalysis).count() == 0
        assert session.query(ChapterCharacterExtraction).count() == 0


@pytest.mark.parametrize(
    "relation_factory",
    [
        lambda profile_ids: {
            "source_profile_id": "profile-9999",
            "target_profile_id": profile_ids[1],
            "directed": False,
            "relation_type": "旧友",
            "description": "无效端点",
            "evidence_chapter_indices": [1],
        },
        lambda profile_ids: {
            "source_profile_id": profile_ids[0],
            "target_profile_id": profile_ids[1],
            "directed": False,
            "relation_type": "旧友",
            "description": "无效章节",
            "evidence_chapter_indices": [999],
        },
    ],
)
def test_relationships_reject_unknown_profiles_or_chapters(
    session_factory: Callable[[], Session],
    relation_factory: Callable[[list[str]], dict[str, object]],
) -> None:
    def invalid_relationships(
        _: Type[BaseModel],
        prompt: str,
    ) -> dict[str, object]:
        profile_ids = [
            str(item["profile_id"])
            for item in _prompt_data(prompt)["profiles"]
        ]
        return {
            "relationships": [relation_factory(profile_ids)],
        }

    provider = QueueProvider(
        [
            _extract(
                _observation("林默"),
                _observation("苏晴"),
            ),
            _merge_by_name,
            _reconcile_separately,
            invalid_relationships,
        ]
    )
    with session_factory() as session:
        novel = _add_novel(session, contents=["林默遇见苏晴。"])
        with pytest.raises(ProviderResponseError):
            analyze_novel_characters(session, novel, provider)
        session.rollback()
        assert session.query(CharacterAnalysis).count() == 0


def test_failed_rebuild_preserves_previous_atomic_snapshot(
    session_factory: Callable[[], Session],
) -> None:
    initial_provider = QueueProvider(
        [
            _extract(_observation("林默")),
            _merge_by_name,
            _no_relationships,
        ]
    )
    with session_factory() as session:
        novel = _add_novel(session, contents=["林默出现。"])
        analysis, _ = analyze_novel_characters(
            session,
            novel,
            initial_provider,
        )
        session.commit()
        novel_id = novel.id
        analysis_id = analysis.id
        old_extraction_hash = (
            session.query(ChapterCharacterExtraction.source_hash).scalar()
        )

    failed_provider = QueueProvider(
        [
            _extract(_observation("苏晴")),
            ProviderUnavailableError(),
        ]
    )
    with session_factory() as session:
        novel = session.get(Novel, novel_id)
        novel.chapters[0].content = "苏晴出现。"
        session.commit()
        with pytest.raises(ProviderUnavailableError):
            analyze_novel_characters(
                session,
                novel,
                failed_provider,
            )
        session.rollback()

    with session_factory() as session:
        analysis = session.get(CharacterAnalysis, analysis_id)
        assert analysis is not None
        assert [item.canonical_name for item in analysis.characters] == [
            "林默"
        ]
        assert (
            session.query(ChapterCharacterExtraction.source_hash).scalar()
            == old_extraction_hash
        )


@pytest.mark.parametrize(
    "failed_stage",
    ["reconciliation", "synthesis", "relationship"],
)
def test_failed_later_api_stage_preserves_previous_atomic_snapshot(
    session_factory: Callable[[], Session],
    failed_stage: str,
) -> None:
    initial_provider = QueueProvider(
        [
            _extract(_observation("林默")),
            _merge_by_name,
            _no_relationships,
        ]
    )
    with session_factory() as session:
        novel = _add_novel(session, contents=["林默出现。"])
        analysis, _ = analyze_novel_characters(
            session,
            novel,
            initial_provider,
        )
        session.commit()
        novel_id = novel.id
        analysis_id = analysis.id
        old_extraction_hash = (
            session.query(ChapterCharacterExtraction.source_hash).scalar()
        )

    def invalid_relationships(
        _: Type[BaseModel],
        user_prompt: str,
    ) -> dict[str, object]:
        profile_ids = [
            str(item["profile_id"])
            for item in _prompt_data(user_prompt)["profiles"]
        ]
        return {
            "relationships": [
                {
                    "source_profile_id": profile_ids[0],
                    "target_profile_id": profile_ids[1],
                    "directed": False,
                    "relation_type": "旧友",
                    "description": "引用了请求外章节。",
                    "evidence_chapter_indices": [999],
                }
            ]
        }

    responses: list[object] = [
        _extract(
            _observation("苏晴"),
            _observation("顾远"),
        ),
        _merge_by_name,
    ]
    if failed_stage == "reconciliation":
        responses.append(
            {"groups": [{"profile_ids": ["unknown-profile"]}]}
        )
        expected_error = ProviderResponseError
    elif failed_stage == "synthesis":
        responses.extend(
            [_reconcile_all, ProviderUnavailableError()]
        )
        expected_error = ProviderUnavailableError
    else:
        responses.extend(
            [_reconcile_separately, invalid_relationships]
        )
        expected_error = ProviderResponseError

    with session_factory() as session:
        novel = session.get(Novel, novel_id)
        assert novel is not None
        novel.chapters[0].content = "苏晴与顾远出现。"
        session.commit()
        with pytest.raises(expected_error):
            analyze_novel_characters(
                session,
                novel,
                QueueProvider(responses),
            )
        session.rollback()

    with session_factory() as session:
        analysis = session.get(CharacterAnalysis, analysis_id)
        assert analysis is not None
        assert [item.canonical_name for item in analysis.characters] == [
            "林默"
        ]
        assert (
            session.query(ChapterCharacterExtraction.source_hash).scalar()
            == old_extraction_hash
        )


def test_run_character_job_completes_and_closes_provider(
    session_factory: Callable[[], Session],
) -> None:
    provider = QueueProvider(
        [
            _extract(_observation("林默")),
            _merge_by_name,
            _no_relationships,
        ]
    )
    with session_factory() as session:
        novel = _add_novel(session, contents=["林默出现。"])
        session.add(
            AnalysisJob(
                id="characters-success",
                novel_id=novel.id,
                job_type="characters",
                status="queued",
                progress_current=0,
                progress_total=3,
            )
        )
        session.commit()

    run_character_job(
        "characters-success",
        provider,
        session_factory,
    )

    with session_factory() as session:
        job = session.get(AnalysisJob, "characters-success")
        assert job is not None
        assert job.status == "succeeded"
        assert job.progress_current == 3
        assert job.progress_total == 3
        assert job.completed_at is not None
        assert session.query(CharacterAnalysis).count() == 1
    assert provider.close_calls == 1


def test_character_job_expands_progress_for_runtime_reconciliation_calls(
    session_factory: Callable[[], Session],
) -> None:
    provider = QueueProvider(
        [
            _extract(
                _observation("林默"),
                _observation("苏晴"),
            ),
            _merge_by_name,
            _reconcile_separately,
            _no_relationships,
        ]
    )
    with session_factory() as session:
        novel = _add_novel(
            session,
            contents=["林默遇见苏晴。"],
        )
        session.add(
            AnalysisJob(
                id="characters-dynamic-progress",
                novel_id=novel.id,
                job_type="characters",
                status="queued",
                progress_current=0,
                progress_total=3,
            )
        )
        session.commit()

    run_character_job(
        "characters-dynamic-progress",
        provider,
        session_factory,
    )

    with session_factory() as session:
        job = session.get(AnalysisJob, "characters-dynamic-progress")
        assert job is not None
        assert job.status == "succeeded"
        assert job.progress_current == 4
        assert job.progress_total == 4
    assert provider.close_calls == 1


def test_failed_character_job_persists_completed_chapters_for_resume(
    session_factory: Callable[[], Session],
) -> None:
    with session_factory() as session:
        novel = _add_novel(session)
        novel_id = novel.id
        session.add(
            AnalysisJob(
                id="characters-resume-first",
                novel_id=novel_id,
                job_type="characters",
                status="queued",
                progress_current=0,
                progress_total=4,
            )
        )
        session.commit()

    failed_provider = QueueProvider(
        [
            _extract(_observation("林默")),
            ProviderResponseError(),
        ]
    )
    run_character_job(
        "characters-resume-first",
        failed_provider,
        session_factory,
    )

    with session_factory() as session:
        failed_job = session.get(
            AnalysisJob,
            "characters-resume-first",
        )
        assert failed_job is not None
        assert failed_job.status == "failed"
        cached = session.query(ChapterCharacterExtraction).one()
        assert cached.chapter.chapter_index == 1
        session.add(
            AnalysisJob(
                id="characters-resume-second",
                novel_id=novel_id,
                job_type="characters",
                status="queued",
                progress_current=0,
                progress_total=4,
            )
        )
        session.commit()

    resumed_provider = QueueProvider(
        [
            _extract(_observation("苏晴")),
            _merge_by_name,
            _reconcile_separately,
            _no_relationships,
        ]
    )
    run_character_job(
        "characters-resume-second",
        resumed_provider,
        session_factory,
    )

    with session_factory() as session:
        resumed_job = session.get(
            AnalysisJob,
            "characters-resume-second",
        )
        assert resumed_job is not None
        assert resumed_job.status == "succeeded"
        assert session.query(ChapterCharacterExtraction).count() == 2
    first_request = _prompt_data(
        str(resumed_provider.calls[0]["user_prompt"])
    )
    assert first_request["chapter_index"] == 2
    assert len(resumed_provider.calls) == 4


def test_run_character_job_stops_when_novel_is_deleted_during_api_call(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "characters-delete.sqlite3"
    engine = create_db_engine(
        f"sqlite:///{database_path.as_posix()}",
        connect_args={"timeout": 1.0},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
    )
    novel_id = 0

    class DeleteAfterGenerationProvider(QueueProvider):
        def generate_json(self, **kwargs: object) -> JsonGenerationResult:
            result = super().generate_json(**kwargs)
            with factory() as delete_session:
                stored_novel = delete_session.get(Novel, novel_id)
                assert stored_novel is not None
                delete_session.delete(stored_novel)
                delete_session.commit()
            return result

    provider = DeleteAfterGenerationProvider(
        [_extract(_observation("林默"))]
    )
    try:
        with factory() as session:
            novel = _add_novel(session, contents=["林默出现。"])
            novel_id = novel.id
            session.add(
                AnalysisJob(
                    id="characters-delete",
                    novel_id=novel_id,
                    job_type="characters",
                    status="queued",
                    progress_current=0,
                    progress_total=3,
                )
            )
            session.commit()

        run_character_job(
            "characters-delete",
            provider,
            factory,
        )

        with factory() as session:
            assert session.query(Novel).count() == 0
            assert session.query(AnalysisJob).count() == 0
            assert session.query(CharacterAnalysis).count() == 0
            assert session.query(ChapterCharacterExtraction).count() == 0
        assert len(provider.calls) == 1
        assert provider.close_calls == 1
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_run_character_job_terminalizes_sqlite_lock_and_allows_requeue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "characters-lock.sqlite3"
    engine = create_db_engine(
        f"sqlite:///{database_path.as_posix()}",
        connect_args={"timeout": 0.02},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
    )

    class LockAfterGenerationProvider(QueueProvider):
        def __init__(self, novel_id: int) -> None:
            super().__init__([_extract(_observation("林默"))])
            self.novel_id = novel_id
            self.lock_session: Optional[Session] = None

        def generate_json(self, **kwargs: object) -> JsonGenerationResult:
            result = super().generate_json(**kwargs)
            self.lock_session = factory()
            locked_novel = self.lock_session.get(Novel, self.novel_id)
            assert locked_novel is not None
            locked_novel.filename = "hold-character-lock.txt"
            self.lock_session.flush()
            return result

        def release_lock(self) -> None:
            if self.lock_session is None:
                return
            self.lock_session.rollback()
            self.lock_session.close()
            self.lock_session = None

        def close(self) -> None:
            self.release_lock()
            super().close()

    provider: Optional[LockAfterGenerationProvider] = None
    retry_attempts: list[int] = []

    def release_after_retries(failed_attempt: int) -> None:
        retry_attempts.append(failed_attempt)
        if failed_attempt == 2:
            assert provider is not None
            provider.release_lock()

    monkeypatch.setattr(
        job_helpers,
        "_sleep_before_sqlite_retry",
        release_after_retries,
    )
    try:
        with factory() as session:
            novel = _add_novel(session, contents=["林默出现。"])
            novel_id = novel.id
            session.add(
                AnalysisJob(
                    id="characters-lock",
                    novel_id=novel_id,
                    job_type="characters",
                    status="queued",
                    progress_current=0,
                    progress_total=3,
                )
            )
            session.commit()

        provider = LockAfterGenerationProvider(novel_id)
        run_character_job("characters-lock", provider, factory)

        with factory() as session:
            failed = session.get(AnalysisJob, "characters-lock")
            assert failed is not None
            assert failed.status == "failed"
            assert failed.completed_at is not None
            assert failed.error_message == (
                "人物分析任务无法获取本地数据库写锁，请重试。"
            )
            session.add(
                AnalysisJob(
                    id="characters-requeued",
                    novel_id=novel_id,
                    job_type="characters",
                    status="queued",
                    progress_current=0,
                    progress_total=3,
                )
            )
            session.commit()
        assert retry_attempts == [1, 2]
        assert provider.close_calls == 1
    finally:
        if provider is not None:
            provider.release_lock()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
