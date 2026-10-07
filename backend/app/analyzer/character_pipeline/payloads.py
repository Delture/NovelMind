"""人物分析 payloads 阶段；保留有界抽取、确定性统计与失败原子性。"""
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
CHARACTER_EXTRACTION_PROMPT_VERSION = "character-extraction-v4"
CHARACTER_MERGE_PROMPT_VERSION = "character-merge-v3"
CHARACTER_RELATIONSHIP_PROMPT_VERSION = "character-relationship-v3"
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
    """去除模型文本首尾空白并拒绝空值，返回规范化字符串。"""
    normalized = value.strip()
    if not normalized:
        raise ValueError("text cannot be blank")
    return normalized


def _normalized_unique_texts(
    values: list[str],
    *,
    maximum_length: int,
) -> list[str]:
    """清理字符串列表、限制单项长度并按首次出现顺序去重。

    ``values`` 是模型返回列表，``maximum_length`` 是每项字符上限。空项或超长项
    抛 ValueError；重复项被合并，返回新列表而不修改调用者对象。
    """
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
        """清理人物名、描述和本段出场摘要。"""
        return _required_text(value)

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, values: list[str]) -> list[str]:
        """清理别名、限制长度并保持首次出现顺序去重。"""
        return _normalized_unique_texts(
            values,
            maximum_length=MAX_CHARACTER_ALIAS_CHARACTERS,
        )

    @model_validator(mode="after")
    def reject_name_as_alias(self) -> "CharacterObservationPayload":
        """拒绝主名同时出现在 aliases，返回已验证对象。"""
        if self.name in self.aliases:
            raise ValueError("the primary name cannot also be an alias")
        return self


class ChapterCharacterExtractionPayload(BaseModel):
    """Strict response for one chapter fragment extraction call."""

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
        """has_more=True 时本页必须至少给出一条观察，避免无限空分页。"""
        if self.has_more and not self.observations:
            raise ValueError(
                "a continued extraction page cannot be empty"
            )
        return self


class StoredCharacterObservation(CharacterObservationPayload):
    """A model observation augmented with a locally assigned identifier."""

    observation_id: StrictStr = Field(
        pattern=(
            r"^chapter-[1-9][0-9]*-part-[1-9][0-9]*-"
            r"observation-[1-9][0-9]*$"
        ),
        max_length=120,
    )


class StoredChapterExtractionData(BaseModel):
    """Validated JSON object persisted for one chapter."""

    model_config = ConfigDict(extra="forbid")

    observations: list[StoredCharacterObservation] = Field(
        default_factory=list,
        max_length=10_000,
    )

    @model_validator(mode="after")
    def require_unique_ids(self) -> "StoredChapterExtractionData":
        """确保持久化单章缓存中所有本地 observation_id 唯一。"""
        identifiers = [
            observation.observation_id
            for observation in self.observations
        ]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("observation identifiers must be unique")
        return self


class MergedCharacterProfilePayload(BaseModel):
    """One canonical profile produced by full-book disambiguation."""

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
        """清理规范人物名称与简介。"""
        return _required_text(value)

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, values: list[str]) -> list[str]:
        """清理全书人物别名并限制每项长度。"""
        return _normalized_unique_texts(values, maximum_length=255)

    @field_validator("observation_ids")
    @classmethod
    def require_unique_observation_ids(
        cls,
        values: list[str],
    ) -> list[str]:
        """清理 observation ID 并拒绝任何重复，保证归并结果是严格分区。"""
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
        """拒绝规范名同时作为自身别名。"""
        if self.canonical_name in self.aliases:
            raise ValueError("the canonical name cannot also be an alias")
        return self


class CharacterMergePayload(BaseModel):
    """Strict full-book profile merge response."""

    model_config = ConfigDict(extra="forbid")

    profiles: list[MergedCharacterProfilePayload] = Field(
        default_factory=list,
        max_length=10_000,
    )


class ReconciledProfileGroupPayload(BaseModel):
    """One identity group referring only to local candidate IDs."""

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
        """清理候选 profile ID，并要求同一身份组不重复引用。"""
        normalized = _normalized_unique_texts(
            values,
            maximum_length=80,
        )
        if len(normalized) != len(values):
            raise ValueError("profile identifiers cannot be duplicated")
        return normalized


class CharacterReconciliationPayload(BaseModel):
    """Strict partition returned for one candidate-group comparison."""

    model_config = ConfigDict(extra="forbid")

    groups: list[ReconciledProfileGroupPayload] = Field(
        min_length=1,
        max_length=MAX_CHARACTER_PROFILE_GROUP_ITEMS * 2,
    )


class SynthesizedCharacterProfilePayload(BaseModel):
    """Canonical text generated externally for one known identity."""

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
        """清理身份综合后的规范名和人物简介。"""
        return _required_text(value)

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, values: list[str]) -> list[str]:
        """清理综合人物别名并去重。"""
        return _normalized_unique_texts(values, maximum_length=255)

    @field_validator("profile_ids")
    @classmethod
    def require_unique_profile_ids(
        cls,
        values: list[str],
    ) -> list[str]:
        """要求综合结果恰好引用一组互不重复的本地候选 ID。"""
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
        """拒绝综合后的规范名重复出现在别名列表。"""
        if self.canonical_name in self.aliases:
            raise ValueError("the canonical name cannot also be an alias")
        return self


class CharacterRelationPayload(BaseModel):
    """One model-inferred edge referencing only local profile IDs."""

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
        """清理关系端点 ID、关系类型和描述文本。"""
        return _required_text(value)

    @field_validator("evidence_chapter_indices")
    @classmethod
    def validate_evidence_chapters(
        cls,
        values: list[int],
    ) -> list[int]:
        """要求证据章节均为正数且无重复，保持模型给出的顺序。"""
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
        """拒绝起点和终点为同一候选人物的自环关系。"""
        if self.source_profile_id == self.target_profile_id:
            raise ValueError("character relationships cannot self-link")
        return self


class CharacterRelationsPayload(BaseModel):
    """Strict relationship inference response."""

    model_config = ConfigDict(extra="forbid")

    relationships: list[CharacterRelationPayload] = Field(
        default_factory=list,
        max_length=30_000,
    )


class _AnalysisSourceItem(BaseModel):
    """参与整书来源哈希的一章：序号、单章哈希和已验证观察。"""
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
    """分析准备阶段的一章及缓存状态；changed 表示本次需重抽取。"""
    chapter: Chapter
    source_hash: str
    extraction_data: StoredChapterExtractionData
    existing: Optional[ChapterCharacterExtraction]
    changed: bool


@dataclass(frozen=True)
class _ResolvedProfile:
    """给模型归并档案分配稳定本地 profile_id 后的值对象。"""
    profile_id: str
    payload: MergedCharacterProfilePayload


@dataclass(frozen=True)
class _ObservationBatch:
    """一个可装入归并提示预算的 ``(章节, 观察)`` 不可变批次。"""
    observations: tuple[
        tuple[_PreparedChapter, StoredCharacterObservation],
        ...,
    ]

    @property
    def observation_ids(self) -> set[str]:
        """返回批次所有观察 ID 的集合，供分区完整性验证。"""
        return {
            observation.observation_id
            for _, observation in self.observations
        }


def _bounded_text(value: str, maximum_length: int) -> str:
    """Return a deterministic bounded copy for a later API stage."""
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
