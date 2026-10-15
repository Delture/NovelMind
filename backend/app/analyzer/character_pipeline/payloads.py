import logging
from dataclasses import dataclass
from typing import Optional, TypeVar
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)
from backend.app.models.character import ChapterCharacterExtraction
from backend.app.models.novel import Chapter

logger = logging.getLogger(__name__)
CHARACTER_EXTRACTION_PROMPT_VERSION = "character-extraction-v5"
CHARACTER_MERGE_PROMPT_VERSION = "character-merge-v4"
CHARACTER_RELATIONSHIP_PROMPT_VERSION = "character-relationship-v4"
MAX_CHARACTER_EXTRACTION_CHARACTERS = 60_000
MAX_CHARACTER_EXTRACTION_TEXT_REQUEST_CHARACTERS = 60_000
MAX_CHARACTER_EXTRACTION_REQUEST_CHARACTERS = 200_000
MAX_CHARACTER_EXTRACTION_PAGES = 16
MAX_CHARACTER_EXTRACTION_OBSERVATIONS = 8
MAX_CHARACTER_NAME_CHARACTERS = 64
MAX_CHARACTER_OBSERVATION_ALIASES = 3
MAX_CHARACTER_ALIAS_CHARACTERS = 48
MAX_CHARACTER_OBSERVATION_DESCRIPTION_CHARACTERS = 192
MAX_CHARACTER_APPEARANCE_SUMMARY_CHARACTERS = 256
MAX_CHARACTER_MERGE_BATCH_ITEMS = 20
MAX_CHARACTER_RELATIONSHIP_EVIDENCE_ITEMS = 20
MAX_CHARACTER_PROFILE_GROUP_ITEMS = 8
MAX_CHARACTER_STAGE_INPUT_CHARACTERS = 48_000
CHARACTER_JOB_SQLITE_MAX_ATTEMPTS = 4
CHARACTER_JOB_SQLITE_RETRY_BASE_SECONDS = 0.05
T = TypeVar("T", bound=BaseModel)

class _CharacterJobCancelled(RuntimeError):
    """Stop work after the persisted job or source novel disappears."""


def _required_text(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("text cannot be blank")
    return normalized


def _normalized_unique_texts(
    values: list[str],
    *,
    maximum_length: int,
) -> list[str]:
    normalized: list[str] = []
    for value in values:
        item = value.strip()
        if not item:
            raise ValueError("list text cannot be blank")
        if len(item) > maximum_length:
            raise ValueError("list text is too long")
        if item not in normalized:
            normalized.append(item)
    return normalized


class CharacterObservationPayload(BaseModel):
    """一次局部人物观察，不等于已经完成全书消歧的规范人物。"""

    model_config = ConfigDict(extra="forbid")

    name: StrictStr = Field(
        min_length=1,
        max_length=MAX_CHARACTER_NAME_CHARACTERS,
    )
    aliases: list[StrictStr] = Field(
        default_factory=list,
        max_length=MAX_CHARACTER_OBSERVATION_ALIASES,
    )
    short_description: StrictStr = Field(
        min_length=1,
        max_length=(
            MAX_CHARACTER_OBSERVATION_DESCRIPTION_CHARACTERS
        ),
    )
    mention_count: StrictInt = Field(ge=1, le=1_000_000)
    appearance_summary: StrictStr = Field(
        min_length=1,
        max_length=MAX_CHARACTER_APPEARANCE_SUMMARY_CHARACTERS,
    )

    @field_validator(
        "name",
        "short_description",
        "appearance_summary",
    )
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, values: list[str]) -> list[str]:
        return _normalized_unique_texts(
            values,
            maximum_length=MAX_CHARACTER_ALIAS_CHARACTERS,
        )

    @model_validator(mode="after")
    def reject_name_as_alias(self) -> "CharacterObservationPayload":
        if self.name in self.aliases:
            raise ValueError("the primary name cannot also be an alias")
        return self


class ChapterCharacterExtractionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observations: list[CharacterObservationPayload] = Field(
        default_factory=list,
        max_length=MAX_CHARACTER_EXTRACTION_OBSERVATIONS,
    )
    has_more: StrictBool

    @model_validator(mode="after")
    def require_progress_when_more_exists(
        self,
    ) -> "ChapterCharacterExtractionPayload":
        if self.has_more and not self.observations:
            raise ValueError(
                "a continued extraction page cannot be empty"
            )
        return self


class StoredCharacterObservation(CharacterObservationPayload):
    observation_id: StrictStr = Field(
        pattern=(
            r"^chapter-[1-9][0-9]*-part-[1-9][0-9]*-"
            r"observation-[1-9][0-9]*$"
        ),
        max_length=120,
    )


class StoredChapterExtractionData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observations: list[StoredCharacterObservation] = Field(
        default_factory=list,
        max_length=10_000,
    )

    @model_validator(mode="after")
    def require_unique_ids(self) -> "StoredChapterExtractionData":
        identifiers = [
            observation.observation_id
            for observation in self.observations
        ]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("observation identifiers must be unique")
        return self


class MergedCharacterProfilePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_ids: list[StrictStr] = Field(
        min_length=1,
        max_length=10_000,
    )
    canonical_name: StrictStr = Field(min_length=1, max_length=255)
    aliases: list[StrictStr] = Field(default_factory=list, max_length=100)
    short_description: StrictStr = Field(min_length=1, max_length=8_000)

    @field_validator("canonical_name", "short_description")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, values: list[str]) -> list[str]:
        return _normalized_unique_texts(values, maximum_length=255)

    @field_validator("observation_ids")
    @classmethod
    def require_unique_observation_ids(
        cls,
        values: list[str],
    ) -> list[str]:
        normalized = _normalized_unique_texts(
            values,
            maximum_length=120,
        )
        if len(normalized) != len(values):
            raise ValueError(
                "profile observation identifiers cannot be duplicated"
            )
        return normalized

    @model_validator(mode="after")
    def reject_name_as_alias(self) -> "MergedCharacterProfilePayload":
        if self.canonical_name in self.aliases:
            raise ValueError("the canonical name cannot also be an alias")
        return self


class CharacterMergePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profiles: list[MergedCharacterProfilePayload] = Field(
        default_factory=list,
        max_length=10_000,
    )


class ReconciledProfileGroupPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_ids: list[StrictStr] = Field(
        min_length=1,
        max_length=MAX_CHARACTER_PROFILE_GROUP_ITEMS * 2,
    )

    @field_validator("profile_ids")
    @classmethod
    def require_unique_profile_ids(
        cls,
        values: list[str],
    ) -> list[str]:
        normalized = _normalized_unique_texts(
            values,
            maximum_length=80,
        )
        if len(normalized) != len(values):
            raise ValueError("profile identifiers cannot be duplicated")
        return normalized


class CharacterReconciliationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    groups: list[ReconciledProfileGroupPayload] = Field(
        min_length=1,
        max_length=MAX_CHARACTER_PROFILE_GROUP_ITEMS * 2,
    )


class SynthesizedCharacterProfilePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_ids: list[StrictStr] = Field(
        min_length=2,
        max_length=MAX_CHARACTER_PROFILE_GROUP_ITEMS,
    )
    canonical_name: StrictStr = Field(min_length=1, max_length=255)
    aliases: list[StrictStr] = Field(default_factory=list, max_length=100)
    short_description: StrictStr = Field(min_length=1, max_length=8_000)

    @field_validator("canonical_name", "short_description")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, values: list[str]) -> list[str]:
        return _normalized_unique_texts(values, maximum_length=255)

    @field_validator("profile_ids")
    @classmethod
    def require_unique_profile_ids(
        cls,
        values: list[str],
    ) -> list[str]:
        normalized = _normalized_unique_texts(
            values,
            maximum_length=80,
        )
        if len(normalized) != len(values):
            raise ValueError("profile identifiers cannot be duplicated")
        return normalized

    @model_validator(mode="after")
    def reject_name_as_alias(
        self,
    ) -> "SynthesizedCharacterProfilePayload":
        if self.canonical_name in self.aliases:
            raise ValueError("the canonical name cannot also be an alias")
        return self


class CharacterRelationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_profile_id: StrictStr = Field(
        min_length=1,
        max_length=40,
    )
    target_profile_id: StrictStr = Field(
        min_length=1,
        max_length=40,
    )
    directed: StrictBool
    relation_type: StrictStr = Field(min_length=1, max_length=100)
    description: StrictStr = Field(min_length=1, max_length=8_000)
    evidence_chapter_indices: list[StrictInt] = Field(
        min_length=1,
        max_length=10_000,
    )

    @field_validator(
        "source_profile_id",
        "target_profile_id",
        "relation_type",
        "description",
    )
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("evidence_chapter_indices")
    @classmethod
    def validate_evidence_chapters(
        cls,
        values: list[int],
    ) -> list[int]:
        if any(value < 1 for value in values):
            raise ValueError("evidence chapter indices must be positive")
        normalized = list(dict.fromkeys(values))
        if len(normalized) != len(values):
            raise ValueError(
                "evidence chapter indices cannot be duplicated"
            )
        return normalized

    @model_validator(mode="after")
    def reject_self_relationship(self) -> "CharacterRelationPayload":
        if self.source_profile_id == self.target_profile_id:
            raise ValueError("character relationships cannot self-link")
        return self


class CharacterRelationsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relationships: list[CharacterRelationPayload] = Field(
        default_factory=list,
        max_length=30_000,
    )


class _AnalysisSourceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_index: StrictInt = Field(ge=1)
    source_hash: StrictStr = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    extraction_data: StoredChapterExtractionData


@dataclass(frozen=True)
class _PreparedChapter:
    chapter: Chapter
    source_hash: str
    extraction_data: StoredChapterExtractionData
    existing: Optional[ChapterCharacterExtraction]
    changed: bool


@dataclass(frozen=True)
class _ResolvedProfile:
    profile_id: str
    payload: MergedCharacterProfilePayload


@dataclass(frozen=True)
class _ObservationBatch:
    observations: tuple[
        tuple[_PreparedChapter, StoredCharacterObservation],
        ...,
    ]

    @property
    def observation_ids(self) -> set[str]:
        return {
            observation.observation_id
            for _, observation in self.observations
        }


def _bounded_text(value: str, maximum_length: int) -> str:
    safe = "".join(
        "\ufffd"
        if 0xD800 <= ord(character) <= 0xDFFF
        else (
            " "
            if ord(character) < 32
            and character not in {"\n", "\r", "\t"}
            else character
        )
        for character in value
    )
    return safe[:maximum_length]
