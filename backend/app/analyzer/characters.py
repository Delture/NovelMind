"""人物逐章抽取、跨章消歧、关系推理和原子快照持久化。

这是项目最复杂的流水线，可按五步阅读：准备章节与复用缓存；分页抽取人物观察；
分批归并候选档案；必要时跨批次身份调和；推理关系并一次性保存当前快照。
模型负责语义判断，本地负责确定性编排、ID 白名单、分区完整性、长度/数量上限、
来源哈希和数据库事务。任何阶段失败都保留旧快照，不向用户展示半成品。
"""

import hashlib
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Callable, Optional, Type, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import case
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.app.analyzer.jobs import (
    is_sqlite_lock_error,
    mark_analysis_job_failed_with_sqlite_lock_retry,
)
from backend.app.analyzer.llm import (
    LLMProvider,
    LLMProviderError,
    ProviderResponseError,
)
from backend.app.analyzer.prompting import novelmind_system_prompt
from backend.app.core.time import utc_now
from backend.app.models.analysis import AnalysisJob
from backend.app.models.character import (
    ChapterCharacterExtraction,
    Character,
    CharacterAnalysis,
    CharacterAppearance,
    CharacterRelationship,
)
from backend.app.models.novel import Chapter, Novel


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


def _canonical_json(value: object) -> str:
    """稳定序列化 JSON，确保同一人物来源数据产生同一哈希。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def character_chapter_source_hash(title: str, content: str) -> str:
    """为单章抽取缓存计算稳定来源标识。"""
    material = _canonical_json(
        {
            "title": title,
            "content": content,
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def character_analysis_source_hash(
    extractions: list[dict[str, object]],
) -> str:
    """Hash a strict chapter-ordered sequence of extraction results."""
    try:
        validated = [
            _AnalysisSourceItem.model_validate(item)
            for item in extractions
        ]
    except ValidationError as error:
        raise ValueError("invalid character extraction hash input") from error
    indices = [item.chapter_index for item in validated]
    if indices != sorted(indices) or len(indices) != len(set(indices)):
        raise ValueError(
            "character extraction hash input must be strictly "
            "chapter ordered"
        )
    material = [
        item.model_dump(mode="json")
        for item in validated
    ]
    return hashlib.sha256(
        _canonical_json(material).encode("utf-8")
    ).hexdigest()


def _character_extraction_request_data(
    *,
    title: str,
    chapter_index: int,
    part_index: int,
    part_count: int,
    page_index: int,
    previous_observations: list[dict[str, object]],
    text: str,
) -> dict[str, object]:
    """组装一次逐章抽取请求的 JSON 材料。

    chapter_index/title 标识章节，fragment 是当前片段，known_observation_ids 防止
    后续分页重复人物，part/page_number 标识位置，continuation 说明是否承接上一页。
    返回字典只用于序列化提示，不修改任何输入。
    """
    return {
        "title": title,
        "chapter_index": chapter_index,
        "part_index": part_index,
        "part_count": part_count,
        "page_index": page_index,
        "previous_observations": previous_observations,
        "text": text,
    }


def _extraction_request_fits(
    text: str,
    *,
    title: str,
    chapter_index: int,
    part_marker: int,
) -> bool:
    """序列化一次抽取请求，返回它是否落在单次提示字符预算内。"""
    request_data = _character_extraction_request_data(
        title=title,
        chapter_index=chapter_index,
        part_index=part_marker,
        part_count=part_marker,
        page_index=MAX_CHARACTER_EXTRACTION_PAGES,
        previous_observations=[],
        text=text,
    )
    return (
        len(text) <= MAX_CHARACTER_EXTRACTION_CHARACTERS
        and len(_canonical_json(request_data))
        <= MAX_CHARACTER_EXTRACTION_TEXT_REQUEST_CHARACTERS
    )


def _largest_fitting_prefix(
    text: str,
    *,
    title: str,
    chapter_index: int,
    part_marker: int,
) -> int:
    """用二分搜索找出从 start 开始可装入请求预算的最长文本前缀长度。"""
    lower = 1
    upper = len(text)
    best = 0
    while lower <= upper:
        middle = (lower + upper) // 2
        if _extraction_request_fits(
            text[:middle],
            title=title,
            chapter_index=chapter_index,
            part_marker=part_marker,
        ):
            best = middle
            lower = middle + 1
        else:
            upper = middle - 1
    return best


def _split_character_text(
    content: str,
    *,
    title: str = "",
    chapter_index: int = 1,
) -> list[str]:
    """Split in order using the serialized extraction JSON budget."""
    marker_digits = len(str(max(1, len(content))))
    part_marker = (10 ** marker_digits) - 1
    if not _extraction_request_fits(
        "",
        title=title,
        chapter_index=chapter_index,
        part_marker=part_marker,
    ):
        raise ProviderResponseError()
    if not content:
        return [""]

    chunks: list[str] = []
    current = ""
    for paragraph in content.splitlines(keepends=True):
        remaining = paragraph
        while remaining:
            candidate = current + remaining
            if _extraction_request_fits(
                candidate,
                title=title,
                chapter_index=chapter_index,
                part_marker=part_marker,
            ):
                current = candidate
                remaining = ""
                continue
            if current:
                chunks.append(current)
                current = ""
                continue
            fitting_length = _largest_fitting_prefix(
                remaining,
                title=title,
                chapter_index=chapter_index,
                part_marker=part_marker,
            )
            if fitting_length < 1:
                raise ProviderResponseError()
            chunks.append(remaining[:fitting_length])
            remaining = remaining[fitting_length:]
    if current:
        chunks.append(current)

    part_count = len(chunks)
    for part_index, chunk in enumerate(chunks, start=1):
        request_data = _character_extraction_request_data(
            title=title,
            chapter_index=chapter_index,
            part_index=part_index,
            part_count=part_count,
            page_index=MAX_CHARACTER_EXTRACTION_PAGES,
            previous_observations=[],
            text=chunk,
        )
        if (
            len(_canonical_json(request_data))
            > MAX_CHARACTER_EXTRACTION_TEXT_REQUEST_CHARACTERS
        ):
            raise ProviderResponseError()
    return chunks


def estimate_character_extraction_steps(content: str) -> int:
    """Conservatively count calls including worst-case title escaping."""
    return len(
        _split_character_text(
            content,
            title="\\" * 255,
            chapter_index=2_147_483_647,
        )
    )


def maximum_character_generation_steps(
    extraction_steps: int,
    *,
    cached_observation_count: int = 0,
    rebuild_required: bool = True,
) -> int:
    """Bound calls even when pending extraction output is not known yet."""
    if extraction_steps < 0 or cached_observation_count < 0:
        raise ValueError("character generation counts cannot be negative")
    if not rebuild_required:
        return 0
    maximum_extraction_calls = (
        extraction_steps * MAX_CHARACTER_EXTRACTION_PAGES
    )
    maximum_observations = (
        cached_observation_count
        + maximum_extraction_calls
        * MAX_CHARACTER_EXTRACTION_OBSERVATIONS
    )
    if maximum_observations == 0:
        return extraction_steps
    maximum_profile_groups = (
        maximum_observations
        + MAX_CHARACTER_PROFILE_GROUP_ITEMS
        - 1
    ) // MAX_CHARACTER_PROFILE_GROUP_ITEMS
    maximum_group_pairs = (
        maximum_profile_groups * (maximum_profile_groups + 1) // 2
    )
    maximum_merge_calls = (
        maximum_observations
        + MAX_CHARACTER_MERGE_BATCH_ITEMS
        - 1
    ) // MAX_CHARACTER_MERGE_BATCH_ITEMS
    maximum_evidence_batches = (
        maximum_observations
        + MAX_CHARACTER_RELATIONSHIP_EVIDENCE_ITEMS
        - 1
    ) // MAX_CHARACTER_RELATIONSHIP_EVIDENCE_ITEMS
    return (
        maximum_extraction_calls
        + maximum_merge_calls
        + maximum_group_pairs  # reconciliation comparisons
        + max(0, maximum_observations - 1)  # synthesis reductions
        + maximum_group_pairs * maximum_evidence_batches
    )


def _extraction_system_prompt() -> str:
    """构造逐章人物观察提示，要求分页信号和局部事实，不提前跨章合并。"""
    return novelmind_system_prompt(
        role="中文小说人物观察提取助手",
        material_scope=(
            "title 与 text 是当前章节的连续原文片段。previous_observations "
            "只是同一片段此前分页返回的派生观察，用于防止重复；它不能证明"
            "原文中没有其他人物，也不能覆盖当前原文。"
        ),
        task_rules=(
            "按人物在当前片段中的首次出现顺序，继续提取尚未返回的人物。"
            "只把能够可靠归属于同一指代对象的称呼合为一条观察；代词、同名"
            "角色或身份不明的称谓存在歧义时，不猜测、不跨人物归并。name "
            "使用片段中最明确的称呼；aliases 只收录片段明确支持的同一人物"
            "称呼；mention_count 只统计当前 text 内能可靠归属于该人物的"
            "显式称呼或指代；short_description 用事实概括身份/阵营、当前"
            "目标或叙事功能；appearance_summary 概括本片段中的关键行动、"
            "选择、信息、结果以及关系或状态变化，不能只堆性格标签。若同一"
            "人物没有新观察，不重复 previous_observations。严格返回 "
            "observations 与 has_more；每条 observation 只含 name、"
            "aliases、short_description、mention_count、"
            "appearance_summary。若仍有未返回人物，has_more=true；否则"
            "为 false；true 时 observations 不得为空。单页最多 8 条；"
            "name 最多 64 字符；aliases 最多 3 项且每项最多 48 字符；"
            "short_description 最多 192 字符；appearance_summary 最多 "
            "256 字符。"
        ),
    )


def _merge_system_prompt() -> str:
    """构造观察归并提示，要求每个 observation_id 恰好归入一个人物档案。"""
    return novelmind_system_prompt(
        role="中文小说人物消歧与档案归并助手",
        material_scope=(
            "chapters 中的人物观察是从原文提取的派生数据，不是原文；其"
            "描述可能不完整，不能因为措辞相似就判定为同一人物。"
        ),
        task_rules=(
            "对全部 observation_id 做完整且互斥的分区。只有明确别名、身份"
            "连续性、稳定关系/阵营、事件连续性等证据共同支持时才归并；同名"
            "本身不是充分证据，不同姓名也不能仅凭类型惯例判为同一人。身份、"
            "时空、关系或行动逻辑冲突且无法裁决时保持分开。canonical_name "
            "选用材料中最稳定明确的称呼；aliases 只合并已获支持的称呼；"
            "short_description 紧凑保留身份、目标、主线作用、关键选择和"
            "状态变化，冲突或不确定不得强行裁决。严格返回 profiles；每项"
            "只含 observation_ids、canonical_name、aliases、"
            "short_description。每个输入 observation_id 必须出现且只能"
            "出现一次，不得创造 ID 或增加小说事实。"
        ),
    )


def _reconciliation_system_prompt() -> str:
    """构造跨批候选身份调和提示，只允许引用给定本地 profile_id。"""
    return novelmind_system_prompt(
        role="中文小说跨批人物消歧助手",
        material_scope=(
            "profiles 是前序归并产生的不可信派生档案，不是原文。档案较短"
            "或名字相同都不自动提高其可信度。"
        ),
        task_rules=(
            "把确有证据属于同一人物的 profile_id 放入同一组；优先核对明确"
            "别名、身份与阵营、事件连续性、稳定关系和行动逻辑。仅同名、同类"
            "角色功能或相似描述不足以合并；出现身份、时空或关系冲突且无法"
            "裁决时必须保持分开。严格返回 groups，每项只含 profile_ids；"
            "对全部输入 profile_id 做完整互斥分区，每个 ID 恰好出现一次，"
            "不得创造 ID。"
        ),
    )


def _synthesis_system_prompt() -> str:
    """构造同一身份候选的名称、别名和简介综合提示。"""
    return novelmind_system_prompt(
        role="中文小说人物档案合成助手",
        material_scope=(
            "profiles 是前序外部消歧已确认属于同一人物的不可信派生档案；"
            "本步骤只合成档案，不重新拆组。"
        ),
        task_rules=(
            "去除重复信息但保留会约束后续分析的身份/阵营、目标、主线作用、"
            "关键选择、代价、关系与状态变化。canonical_name 使用最稳定明确"
            "的称呼；aliases 只保留输入支持的称呼。输入若存在未裁决冲突，"
            "在 short_description 中简短保留，不自行补全。严格只返回 "
            "profile_ids、canonical_name、aliases、short_description；"
            "profile_ids 必须完整照抄且不重复，不增加事实。"
        ),
    )


def _relationship_system_prompt() -> str:
    """构造人物关系推理提示，要求关系端点和证据章节来自白名单。"""
    return novelmind_system_prompt(
        role="中文小说人物关系分析助手",
        material_scope=(
            "profiles 与 chapters 中的观察均为不可信派生数据，不是原文。"
            "本次只能判断给定 profiles 之间、由给定 chapters 支持的关系。"
        ),
        task_rules=(
            "只提取有具体观察支持，并实际影响利益、信任、依赖、权力、情感"
            "或行动选择的关系；同场出现、同阵营标签或类型惯例不足以证明亲属、"
            "恋爱、敌对等关系。directed 只在影响或认知明确单向时为 true；"
            "无向关系使用稳定端点顺序。relation_type 使用中性准确的短标签；"
            "description 说明关系依据、当前状态和材料明确呈现的变化，不把"
            "推测写成事实。每条只引用最能直接支持它的章节。端点只能使用 "
            "profiles 的 profile_id，不得自连；证据只能使用 chapters 的 "
            "chapter_index。严格返回 relationships；每项只含 "
            "source_profile_id、target_profile_id、directed、"
            "relation_type、description、evidence_chapter_indices，证据"
            "数组必须非空。"
        ),
    )


def _ensure_can_continue(
    should_continue: Optional[Callable[[], bool]],
) -> None:
    """调用可选任务存活回调；失效时抛取消异常，避免继续付费调用。"""
    if should_continue is not None and not should_continue():
        raise _CharacterJobCancelled()


def _generate_payload(
    *,
    provider: LLMProvider,
    system_prompt: str,
    user_prompt: str,
    response_model: Type[T],
    should_continue: Optional[Callable[[], bool]],
    on_generation_step: Optional[Callable[[], None]],
) -> T:
    """统一调用 provider.generate_json 并返回已验证 Pydantic 值。

    ``response_model`` 指定本步骤合同，thinking/reasoning 传给供应商；生成前调用
    should_continue。泛型 T 让调用者得到具体 payload 类型而非 BaseModel。
    """
    _ensure_can_continue(should_continue)
    result = provider.generate_json(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_model=response_model,
        thinking=False,
        before_attempt=(
            lambda: _ensure_can_continue(should_continue)
        ),
    )
    _ensure_can_continue(should_continue)
    try:
        value = response_model.model_validate(result.value)
    except ValidationError as error:
        raise ProviderResponseError() from error
    if on_generation_step is not None:
        on_generation_step()
    return value


def _extract_chapter(
# 一章可能被切成多段且每段可分页；所有页都通过契约后才构成完整单章缓存。
    chapter: Chapter,
    provider: LLMProvider,
    *,
    should_continue: Optional[Callable[[], bool]],
    on_generation_step: Optional[Callable[[], None]],
) -> StoredChapterExtractionData:
    """切分并分页抽取一章，返回可持久化的完整观察缓存。

    ``chapter`` 提供序号/标题/正文，provider 调用外部模型，should_continue 在每次
    付费请求前检测取消。每个片段可能多页；函数拒绝重复页面与超出安全上限，并
    为观察分配包含章、片段、序号的稳定本地 ID。
    """
    chunks = _split_character_text(
        chapter.content,
        title=chapter.title,
        chapter_index=chapter.chapter_index,
    )
    observations: list[StoredCharacterObservation] = []
    chapter_fingerprints: set[str] = set()
    for part_index, chunk in enumerate(chunks, start=1):
        part_observations: list[StoredCharacterObservation] = []
        part_fingerprints: set[str] = set()
        for page_index in range(
            1,
            MAX_CHARACTER_EXTRACTION_PAGES + 1,
        ):
            history = [
                {
                    "observation_id": observation.observation_id,
                    "name": _bounded_text(observation.name, 64),
                    "aliases": [
                        _bounded_text(alias, 48)
                        for alias in observation.aliases[:3]
                    ],
                    "short_description": _bounded_text(
                        observation.short_description,
                        96,
                    ),
                }
                for observation in part_observations
            ]
            request_data = _character_extraction_request_data(
                title=chapter.title,
                chapter_index=chapter.chapter_index,
                part_index=part_index,
                part_count=len(chunks),
                page_index=page_index,
                previous_observations=history,
                text=chunk,
            )
            request_json = _canonical_json(request_data)
            if (
                len(request_json)
                > MAX_CHARACTER_EXTRACTION_REQUEST_CHARACTERS
            ):
                raise ProviderResponseError()
            payload = _generate_payload(
                provider=provider,
                system_prompt=_extraction_system_prompt(),
                user_prompt=(
                    "请从下一行 JSON 的当前章节连续片段提取人物观察：\n"
                    f"{request_json}"
                ),
                response_model=ChapterCharacterExtractionPayload,
                should_continue=should_continue,
                on_generation_step=on_generation_step,
            )
            for observation in payload.observations:
                fingerprint = _canonical_json(
                    observation.model_dump(mode="json")
                )
                if fingerprint in part_fingerprints:
                    raise ProviderResponseError()
                part_fingerprints.add(fingerprint)
                if fingerprint in chapter_fingerprints:
                    continue
                chapter_fingerprints.add(fingerprint)
                stored = StoredCharacterObservation(
                    observation_id=(
                        f"chapter-{chapter.id}-part-{part_index}-"
                        f"observation-{len(part_observations) + 1}"
                    ),
                    **observation.model_dump(),
                )
                part_observations.append(stored)
                observations.append(stored)
            if not payload.has_more:
                break
        else:
            raise ProviderResponseError()
    return StoredChapterExtractionData(observations=observations)


def _load_cached_extraction(
    extraction: Optional[ChapterCharacterExtraction],
    *,
    source_hash: str,
    provider_model: str,
) -> Optional[StoredChapterExtractionData]:
    """仅在 source_hash、模型和提示版本完全匹配时解析并返回单章缓存。"""
    if (
        extraction is None
        or extraction.source_hash != source_hash
        or extraction.model != provider_model
        or extraction.prompt_version
        != CHARACTER_EXTRACTION_PROMPT_VERSION
    ):
        return None
    try:
        return StoredChapterExtractionData.model_validate(
            extraction.extraction_data
        )
    except ValidationError:
        logger.warning(
            "Ignoring invalid cached character extraction for chapter %s.",
            extraction.chapter_id,
        )
        return None


def _persist_chapter_extraction(
    db: Session,
    prepared: _PreparedChapter,
    *,
    provider_model: str,
) -> ChapterCharacterExtraction:
    """Upsert one completed chapter extraction without touching snapshots."""
    extraction = (
        db.query(ChapterCharacterExtraction)
        .filter(
            ChapterCharacterExtraction.chapter_id
            == prepared.chapter.id
        )
        .one_or_none()
    )
    now = utc_now()
    if extraction is None:
        extraction = ChapterCharacterExtraction(
            chapter_id=prepared.chapter.id,
            created_at=now,
        )
        db.add(extraction)
    extraction.extraction_data = prepared.extraction_data.model_dump(
        mode="json"
    )
    extraction.source_hash = prepared.source_hash
    extraction.model = provider_model
    extraction.prompt_version = CHARACTER_EXTRACTION_PROMPT_VERSION
    extraction.updated_at = now
    return extraction


def _prepare_chapters(
    db: Session,
    novel: Novel,
    provider: LLMProvider,
    *,
    force: bool,
    persist_extractions_incrementally: bool,
    should_continue: Optional[Callable[[], bool]],
    on_generation_step: Optional[Callable[[], None]],
) -> list[_PreparedChapter]:
    """读取全部章节、计算来源哈希、复用有效缓存并抽取变化章节。

    返回按章节顺序的 PreparedChapter 列表；新抽取会增量落盘，使失败后可恢复。
    """
    chapters = (
        db.query(Chapter)
        .filter(Chapter.novel_id == novel.id)
        .order_by(Chapter.chapter_index)
        .all()
    )
    chapter_ids = [chapter.id for chapter in chapters]
    extraction_query = db.query(ChapterCharacterExtraction)
    if chapter_ids:
        extraction_query = extraction_query.filter(
            ChapterCharacterExtraction.chapter_id.in_(chapter_ids)
        )
        existing_by_chapter = {
            item.chapter_id: item
            for item in extraction_query.all()
        }
    else:
        existing_by_chapter = {}

    prepared: list[_PreparedChapter] = []
    for chapter in chapters:
        source_hash = character_chapter_source_hash(
            chapter.title,
            chapter.content,
        )
        existing = existing_by_chapter.get(chapter.id)
        cached = None
        if not force:
            cached = _load_cached_extraction(
                existing,
                source_hash=source_hash,
                provider_model=provider.model,
            )
        changed = cached is None
        extraction_data = (
            _extract_chapter(
                chapter,
                provider,
                should_continue=should_continue,
                on_generation_step=on_generation_step,
            )
            if changed
            else cached
        )
        prepared_chapter = _PreparedChapter(
            chapter=chapter,
            source_hash=source_hash,
            extraction_data=extraction_data,
            existing=existing,
            changed=changed,
        )
        prepared.append(prepared_chapter)
        if changed and persist_extractions_incrementally:
            _ensure_can_continue(should_continue)
            _persist_chapter_extraction(
                db,
                prepared_chapter,
                provider_model=provider.model,
            )
            db.commit()
            _ensure_can_continue(should_continue)
    return prepared


def _analysis_hash_input(
    prepared: list[_PreparedChapter],
) -> list[dict[str, object]]:
    """把准备后章节压缩为稳定字典列表，作为整书人物来源哈希输入。"""
    return [
        {
            "chapter_index": item.chapter.chapter_index,
            "source_hash": item.source_hash,
            "extraction_data": item.extraction_data.model_dump(
                mode="json"
            ),
        }
        for item in prepared
    ]


def _analysis_cache_is_current(
    analysis: Optional[CharacterAnalysis],
    *,
    source_hash: str,
    provider_model: str,
    chapter_count: int,
) -> bool:
    """比较现有人物快照的来源哈希、模型和三段提示版本，判断是否可复用。"""
    return bool(
        analysis is not None
        and analysis.source_hash == source_hash
        and analysis.model == provider_model
        and analysis.extraction_prompt_version
        == CHARACTER_EXTRACTION_PROMPT_VERSION
        and analysis.merge_prompt_version
        == CHARACTER_MERGE_PROMPT_VERSION
        and analysis.relationship_prompt_version
        == CHARACTER_RELATIONSHIP_PROMPT_VERSION
        and analysis.chapter_count == chapter_count
    )


def _planned_character_generation_steps(
    db: Session,
    novel: Novel,
    *,
    provider_model: str,
    force: bool,
) -> int:
    """估算一次人物任务在正常路径需要的模型生成步骤。

    读取章节并判断哪些单章缓存仍有效；变化章节按与执行器相同的切分算法计数，
    再加入观察归并、身份调和、综合与关系批次。返回值用于进度初始 total，不把
    极端安全上限伪装成预计进度。
    """
    chapters = (
        db.query(Chapter)
        .filter(Chapter.novel_id == novel.id)
        .order_by(Chapter.chapter_index)
        .all()
    )
    chapter_ids = [chapter.id for chapter in chapters]
    existing_by_chapter = {
        extraction.chapter_id: extraction
        for extraction in (
            db.query(ChapterCharacterExtraction)
            .filter(
                ChapterCharacterExtraction.chapter_id.in_(chapter_ids)
            )
            .all()
            if chapter_ids
            else []
        )
    }
    extraction_steps = 0
    current_inputs: list[dict[str, object]] = []
    all_current = not force
    for chapter in chapters:
        source_hash = character_chapter_source_hash(
            chapter.title,
            chapter.content,
        )
        cached = None
        if not force:
            cached = _load_cached_extraction(
                existing_by_chapter.get(chapter.id),
                source_hash=source_hash,
                provider_model=provider_model,
            )
        if cached is None:
            all_current = False
            extraction_steps += estimate_character_extraction_steps(
                chapter.content
            )
            continue
        current_inputs.append(
            {
                "chapter_index": chapter.chapter_index,
                "source_hash": source_hash,
                "extraction_data": cached.model_dump(mode="json"),
            }
        )

    if not all_current:
        # This is a user-facing progress estimate, not the combinatorial
        # safety ceiling used for request-bound disclosure. Pagination and
        # reconciliation can add calls at runtime; record_generation_step
        # expands the total if that happens.
        return extraction_steps + 2

    source_hash = character_analysis_source_hash(current_inputs)
    analysis = (
        db.query(CharacterAnalysis)
        .filter(CharacterAnalysis.novel_id == novel.id)
        .one_or_none()
    )
    if _analysis_cache_is_current(
        analysis,
        source_hash=source_hash,
        provider_model=provider_model,
        chapter_count=len(chapters),
    ):
        return 0
    return 2


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


def _compact_observation(
    observation: StoredCharacterObservation,
) -> dict[str, object]:
    """Keep later-stage prompts bounded without changing stored data."""
    return {
        "observation_id": observation.observation_id,
        "name": _bounded_text(observation.name, 80),
        "aliases": [
            _bounded_text(alias, 80)
            for alias in observation.aliases[:3]
        ],
        "short_description": _bounded_text(
            observation.short_description,
            160,
        ),
        "mention_count": observation.mention_count,
        "appearance_summary": _bounded_text(
            observation.appearance_summary,
            240,
        ),
    }


def _compact_profile(
    profile: _ResolvedProfile,
) -> dict[str, object]:
    """Return only bounded fields needed for relationship inference."""
    return {
        "profile_id": profile.profile_id,
        "canonical_name": _bounded_text(
            profile.payload.canonical_name,
            80,
        ),
        "aliases": [
            _bounded_text(alias, 80)
            for alias in profile.payload.aliases[:3]
        ],
        "short_description": _bounded_text(
            profile.payload.short_description,
            300,
        ),
    }


def _chapter_data_for_observations(
    observations: list[
        tuple[_PreparedChapter, StoredCharacterObservation]
    ],
    *,
    include_titles: bool,
) -> list[dict[str, object]]:
    """按 observation ID 聚合所涉章节/片段信息，供人物归并提示提供证据上下文。"""
    chapters_by_id: dict[int, dict[str, object]] = {}
    for item, observation in observations:
        chapter_data = chapters_by_id.setdefault(
            item.chapter.id,
            {
                "chapter_index": item.chapter.chapter_index,
                **(
                    {
                        "chapter_title": _bounded_text(
                            item.chapter.title,
                            255,
                        )
                    }
                    if include_titles
                    else {}
                ),
                "observations": [],
            },
        )
        chapter_observations = chapter_data["observations"]
        if not isinstance(chapter_observations, list):
            raise RuntimeError("invalid local character batch")
        chapter_observations.append(
            _compact_observation(observation)
        )
    return list(chapters_by_id.values())


def _merge_request_data(
    observations: list[
        tuple[_PreparedChapter, StoredCharacterObservation]
    ],
    *,
    batch_index: int,
    batch_count: int,
) -> dict[str, object]:
    """把一个观察批次和对应章节上下文组装为人物归并请求 JSON。"""
    return {
        "batch_index": batch_index,
        "batch_count": batch_count,
        "chapters": _chapter_data_for_observations(
            observations,
            include_titles=True,
        ),
    }


def _relationship_request_data(
    observations: list[
        tuple[_PreparedChapter, StoredCharacterObservation]
    ],
    profiles: list[_ResolvedProfile],
    *,
    batch_index: int,
    batch_count: int,
) -> dict[str, object]:
    """把候选人物与有限章节证据组装为关系推理请求 JSON。"""
    return {
        "batch_index": batch_index,
        "batch_count": batch_count,
        "profiles": [
            _compact_profile(profile) for profile in profiles
        ],
        "chapters": _chapter_data_for_observations(
            observations,
            include_titles=False,
        ),
    }


def _observation_batches(
    prepared: list[_PreparedChapter],
) -> list[_ObservationBatch]:
    """Partition observations under explicit count and size bounds."""
    batches: list[_ObservationBatch] = []
    current: list[
        tuple[_PreparedChapter, StoredCharacterObservation]
    ] = []
    for item in prepared:
        for observation in item.extraction_data.observations:
            candidate = [*current, (item, observation)]
            candidate_size = len(
                _canonical_json(
                    _merge_request_data(
                        candidate,
                        batch_index=999_999,
                        batch_count=999_999,
                    )
                )
            )
            if (
                current
                and len(candidate) > MAX_CHARACTER_MERGE_BATCH_ITEMS
            ):
                batches.append(
                    _ObservationBatch(observations=tuple(current))
                )
                current = [(item, observation)]
            else:
                if (
                    candidate_size
                    > MAX_CHARACTER_STAGE_INPUT_CHARACTERS
                ):
                    raise ProviderResponseError()
                current = candidate
            if len(
                _canonical_json(
                    _merge_request_data(
                        current,
                        batch_index=999_999,
                        batch_count=999_999,
                    )
                )
            ) > MAX_CHARACTER_STAGE_INPUT_CHARACTERS:
                raise ProviderResponseError()
    if current:
        batches.append(_ObservationBatch(observations=tuple(current)))
    return batches


def _validate_partition(
    payload: CharacterMergePayload,
    expected_observation_ids: set[str],
    *,
    profile_id_prefix: str = "profile",
) -> list[_ResolvedProfile]:
    """验证归并结果对 expected_ids 不重不漏，并分配本地 profile_id。"""
    seen: set[str] = set()
    resolved: list[_ResolvedProfile] = []
    for index, profile in enumerate(payload.profiles, start=1):
        for observation_id in profile.observation_ids:
            if (
                observation_id not in expected_observation_ids
                or observation_id in seen
            ):
                raise ProviderResponseError()
            seen.add(observation_id)
        resolved.append(
            _ResolvedProfile(
                profile_id=f"{profile_id_prefix}-{index:04d}",
                payload=profile,
            )
        )
    if seen != expected_observation_ids:
        raise ProviderResponseError()
    return resolved


def _profile_groups(
    profiles: list[_ResolvedProfile],
) -> list[list[_ResolvedProfile]]:
    """按固定上限把候选档案顺序分组，供跨批身份调和。"""
    return [
        profiles[start : start + MAX_CHARACTER_PROFILE_GROUP_ITEMS]
        for start in range(
            0,
            len(profiles),
            MAX_CHARACTER_PROFILE_GROUP_ITEMS,
        )
    ]


def _validate_profile_partition(
    payload: CharacterReconciliationPayload,
    expected_ids: set[str],
) -> list[set[str]]:
    """验证身份组对 expected_ids 严格分区，返回每组 ID 集合。"""
    seen: set[str] = set()
    groups: list[set[str]] = []
    for group in payload.groups:
        identifiers = set(group.profile_ids)
        if (
            len(identifiers) != len(group.profile_ids)
            or not identifiers <= expected_ids
            or identifiers & seen
        ):
            raise ProviderResponseError()
        seen.update(identifiers)
        groups.append(identifiers)
    if seen != expected_ids:
        raise ProviderResponseError()
    return groups


def _reconciliation_requests(
    profiles: list[_ResolvedProfile],
) -> list[list[_ResolvedProfile]]:
    """构造相邻及边界候选比较组，控制跨批身份调和提示规模。"""
    groups = _profile_groups(profiles)
    return [
        (
            list(groups[left])
            if left == right
            else [*groups[left], *groups[right]]
        )
        for left in range(len(groups))
        for right in range(left, len(groups))
        if left != right or len(groups[left]) > 1
    ]


def _same_identity_decisions(
    profiles: list[_ResolvedProfile],
    provider: LLMProvider,
    *,
    should_continue: Optional[Callable[[], bool]],
    on_generation_step: Optional[Callable[[], None]],
) -> dict[frozenset[str], bool]:
    """调用模型判断候选档案是否属于同一人物。

    profiles 是本地候选，provider 执行严格分区响应，should_continue 负责取消，
    record_generation_step 更新进度。返回以 frozenset 两 ID 为键的对称布尔映射；
    不在本地凭名字相似度猜身份。
    """
    decisions: dict[frozenset[str], bool] = {}
    requests = _reconciliation_requests(profiles)
    for request_index, candidates in enumerate(requests, start=1):
        request_data = {
            "comparison_index": request_index,
            "comparison_count": len(requests),
            "profiles": [
                _compact_profile(profile) for profile in candidates
            ],
        }
        if (
            len(_canonical_json(request_data))
            > MAX_CHARACTER_STAGE_INPUT_CHARACTERS
        ):
            raise ProviderResponseError()
        payload = _generate_payload(
            provider=provider,
            system_prompt=_reconciliation_system_prompt(),
            user_prompt=(
                "请完整分区下一行 JSON 中的候选人物档案：\n"
                f"{_canonical_json(request_data)}"
            ),
            response_model=CharacterReconciliationPayload,
            should_continue=should_continue,
            on_generation_step=on_generation_step,
        )
        candidate_ids = {
            profile.profile_id for profile in candidates
        }
        partition = _validate_profile_partition(
            payload,
            candidate_ids,
        )
        group_by_id = {
            profile_id: group_index
            for group_index, group in enumerate(partition)
            for profile_id in group
        }
        for left in range(len(candidates)):
            for right in range(left + 1, len(candidates)):
                pair = frozenset(
                    (
                        candidates[left].profile_id,
                        candidates[right].profile_id,
                    )
                )
                same = (
                    group_by_id[candidates[left].profile_id]
                    == group_by_id[candidates[right].profile_id]
                )
                previous = decisions.setdefault(pair, same)
                if previous != same:
                    raise ProviderResponseError()
    return decisions


def _complete_link_identity_clusters(
    profiles: list[_ResolvedProfile],
    decisions: dict[frozenset[str], bool],
) -> list[list[_ResolvedProfile]]:
    """用 complete-link 聚类：候选须与簇内每个成员都判为同一身份。"""
    clusters: list[list[_ResolvedProfile]] = []
    for profile in profiles:
        for cluster in clusters:
            if all(
                decisions.get(
                    frozenset((profile.profile_id, member.profile_id))
                )
                is True
                for member in cluster
            ):
                cluster.append(profile)
                break
        else:
            clusters.append([profile])
    return clusters


def _synthesize_identity_cluster(
    cluster: list[_ResolvedProfile],
    provider: LLMProvider,
    *,
    cluster_index: int,
    should_continue: Optional[Callable[[], bool]],
    on_generation_step: Optional[Callable[[], None]],
) -> _ResolvedProfile:
    """把一个确认同一身份的候选簇综合为单一规范档案。

    cluster 可能仍超过一次提示预算，因此函数分层合并；每轮要求模型返回精确的
    profile_id 分区并保留所有 observation_ids。返回新的 ResolvedProfile，ID 由
    本地确定，文本由模型生成并经 Pydantic 校验。
    """
    current = list(cluster)
    round_index = 0
    while len(current) > 1:
        round_index += 1
        reduced: list[_ResolvedProfile] = []
        for batch_index, start in enumerate(
            range(0, len(current), MAX_CHARACTER_PROFILE_GROUP_ITEMS),
            start=1,
        ):
            batch = current[
                start : start + MAX_CHARACTER_PROFILE_GROUP_ITEMS
            ]
            if len(batch) == 1:
                reduced.append(batch[0])
                continue
            request_data = {
                "profiles": [
                    _compact_profile(profile) for profile in batch
                ]
            }
            if (
                len(_canonical_json(request_data))
                > MAX_CHARACTER_STAGE_INPUT_CHARACTERS
            ):
                raise ProviderResponseError()
            payload = _generate_payload(
                provider=provider,
                system_prompt=_synthesis_system_prompt(),
                user_prompt=(
                    "请整合下一行 JSON 中已确认属于同一人物的档案：\n"
                    f"{_canonical_json(request_data)}"
                ),
                response_model=SynthesizedCharacterProfilePayload,
                should_continue=should_continue,
                on_generation_step=on_generation_step,
            )
            expected_ids = {profile.profile_id for profile in batch}
            if set(payload.profile_ids) != expected_ids:
                raise ProviderResponseError()
            observation_ids = [
                observation_id
                for profile in batch
                for observation_id in profile.payload.observation_ids
            ]
            reduced.append(
                _ResolvedProfile(
                    profile_id=(
                        f"synthesis-{cluster_index:04d}-"
                        f"{round_index:04d}-{batch_index:04d}"
                    ),
                    payload=MergedCharacterProfilePayload(
                        observation_ids=observation_ids,
                        canonical_name=payload.canonical_name,
                        aliases=list(payload.aliases),
                        short_description=payload.short_description,
                    ),
                )
            )
        current = reduced
    return current[0]


def _reconcile_profiles_with_api(
    profiles: list[_ResolvedProfile],
    provider: LLMProvider,
    *,
    should_continue: Optional[Callable[[], bool]],
    on_generation_step: Optional[Callable[[], None]],
) -> list[_ResolvedProfile]:
    """跨归并批次调和同一人物，并综合多候选身份簇。"""
    if not profiles:
        return []
    decisions = _same_identity_decisions(
        profiles,
        provider,
        should_continue=should_continue,
        on_generation_step=on_generation_step,
    )
    clusters = _complete_link_identity_clusters(
        profiles,
        decisions,
    )
    reconciled = [
        _synthesize_identity_cluster(
            cluster,
            provider,
            cluster_index=index,
            should_continue=should_continue,
            on_generation_step=on_generation_step,
        )
        for index, cluster in enumerate(clusters, start=1)
    ]
    return [
        _ResolvedProfile(
            profile_id=f"profile-{index:04d}",
            payload=profile.payload,
        )
        for index, profile in enumerate(reconciled, start=1)
    ]


def _merge_profiles(
    prepared: list[_PreparedChapter],
    provider: LLMProvider,
    *,
    should_continue: Optional[Callable[[], bool]],
    on_generation_step: Optional[Callable[[], None]],
) -> tuple[list[_ResolvedProfile], list[_ObservationBatch]]:
    """把全部逐章观察归并为全书规范人物。

    prepared 是有序章节缓存；函数先按提示预算生成 ObservationBatch，各批严格
    验证 observation_id 不重不漏，再跨批身份调和。返回 ``(最终档案, 原批次)``，
    后者继续为关系证据范围和本地统计提供来源。
    """
    batches = _observation_batches(prepared)
    batched_profiles: list[_ResolvedProfile] = []
    for batch_index, batch in enumerate(batches, start=1):
        request_data = _merge_request_data(
            list(batch.observations),
            batch_index=batch_index,
            batch_count=len(batches),
        )
        payload = _generate_payload(
            provider=provider,
            system_prompt=_merge_system_prompt(),
            user_prompt=(
                "请归并下一行 JSON 中的这一批全书人物观察，并完整分区全部 "
                "observation_id：\n"
                f"{_canonical_json(request_data)}"
            ),
            response_model=CharacterMergePayload,
            should_continue=should_continue,
            on_generation_step=on_generation_step,
        )
        batched_profiles.extend(
            _validate_partition(
                payload,
                batch.observation_ids,
                profile_id_prefix=f"batch-{batch_index:04d}-profile",
            )
        )
    return (
        _reconcile_profiles_with_api(
            batched_profiles,
            provider,
            should_continue=should_continue,
            on_generation_step=on_generation_step,
        ),
        batches,
    )


def _relationship_evidence_batches(
    profiles: list[_ResolvedProfile],
    observations: list[
        tuple[_PreparedChapter, StoredCharacterObservation]
    ],
    profile_by_observation_id: dict[str, _ResolvedProfile],
) -> list[list[
    tuple[_PreparedChapter, StoredCharacterObservation]
]]:
    """按提示字符预算构造关系推理的章节证据批次。

    prepared 提供逐章观察，profiles 给出合法人物白名单，base_request_characters
    预留固定人物档案开销。返回批次保留章节序号和局部观察；未知 ID、空证据不会
    被发给关系模型。
    """
    known_ids = {profile.profile_id for profile in profiles}
    relevant = [
        pair
        for pair in observations
        if profile_by_observation_id[
            pair[1].observation_id
        ].profile_id
        in known_ids
    ]
    batches: list[list[
        tuple[_PreparedChapter, StoredCharacterObservation]
    ]] = []
    current: list[
        tuple[_PreparedChapter, StoredCharacterObservation]
    ] = []
    for pair in relevant:
        candidate = [*current, pair]
        request_size = len(
            _canonical_json(
                _relationship_request_data(
                    candidate,
                    profiles,
                    batch_index=999_999,
                    batch_count=999_999,
                )
            )
        )
        if (
            current
            and len(candidate)
            > MAX_CHARACTER_RELATIONSHIP_EVIDENCE_ITEMS
        ):
            batches.append(current)
            current = [pair]
        else:
            if request_size > MAX_CHARACTER_STAGE_INPUT_CHARACTERS:
                raise ProviderResponseError()
            current = candidate
        if len(
            _canonical_json(
                _relationship_request_data(
                    current,
                    profiles,
                    batch_index=999_999,
                    batch_count=999_999,
                )
            )
        ) > MAX_CHARACTER_STAGE_INPUT_CHARACTERS:
            raise ProviderResponseError()
    if current:
        batches.append(current)
    return batches or [[]]


def _relationship_call_specs(
    profiles: list[_ResolvedProfile],
    source_batches: list[_ObservationBatch],
) -> list[tuple[
    list[_ResolvedProfile],
    list[tuple[_PreparedChapter, StoredCharacterObservation]],
]]:
    """组合人物分组和证据批次，返回每次关系模型调用的参数规格。"""
    groups = _profile_groups(profiles)
    all_observations = [
        pair
        for batch in source_batches
        for pair in batch.observations
    ]
    profile_by_observation_id = {
        observation_id: profile
        for profile in profiles
        for observation_id in profile.payload.observation_ids
    }
    specs: list[tuple[
        list[_ResolvedProfile],
        list[tuple[_PreparedChapter, StoredCharacterObservation]],
    ]] = []
    for left in range(len(groups)):
        for right in range(left, len(groups)):
            request_profiles = (
                list(groups[left])
                if left == right
                else [*groups[left], *groups[right]]
            )
            for evidence in _relationship_evidence_batches(
                request_profiles,
                all_observations,
                profile_by_observation_id,
            ):
                specs.append((request_profiles, evidence))
    return specs


def _infer_relationships(
    profiles: list[_ResolvedProfile],
    batches: list[_ObservationBatch],
    provider: LLMProvider,
    *,
    should_continue: Optional[Callable[[], bool]],
    on_generation_step: Optional[Callable[[], None]],
) -> list[CharacterRelationPayload]:
    """推理并合并整书人物关系。

    profiles 是合法端点，prepared/observation_batches 提供证据，provider 执行
    结构化生成。每次结果验证端点、关系方向和证据章节白名单；同一边重复出现时
    本地合并证据而不重复保存。返回最终 CharacterRelationPayload 列表。
    """
    if not profiles:
        return []
    specs = _relationship_call_specs(profiles, batches)
    edge_indices: dict[tuple[str, str, bool, str], int] = {}
    validated: list[CharacterRelationPayload] = []
    for batch_index, (batch_profiles, evidence) in enumerate(
        specs,
        start=1,
    ):
        request_data = _relationship_request_data(
            evidence,
            batch_profiles,
            batch_index=batch_index,
            batch_count=len(specs),
        )
        payload = _generate_payload(
            provider=provider,
            system_prompt=_relationship_system_prompt(),
            user_prompt=(
                "请根据下一行 JSON 的本地人物 ID 和派生观察推断关系：\n"
                f"{_canonical_json(request_data)}"
            ),
            response_model=CharacterRelationsPayload,
            should_continue=should_continue,
            on_generation_step=on_generation_step,
        )
        known_profiles = {
            profile.profile_id for profile in batch_profiles
        }
        valid_chapters = {
            item.chapter.chapter_index
            for item, _ in evidence
        }
        seen_in_batch: set[tuple[str, str, bool, str]] = set()
        for relation in payload.relationships:
            if (
                relation.source_profile_id not in known_profiles
                or relation.target_profile_id not in known_profiles
                or not set(relation.evidence_chapter_indices)
                <= valid_chapters
            ):
                raise ProviderResponseError()
            source = relation.source_profile_id
            target = relation.target_profile_id
            if not relation.directed and source > target:
                source, target = target, source
                relation = relation.model_copy(
                    update={
                        "source_profile_id": source,
                        "target_profile_id": target,
                    }
                )
            edge = (
                source,
                target,
                bool(relation.directed),
                relation.relation_type,
            )
            if edge in seen_in_batch:
                raise ProviderResponseError()
            seen_in_batch.add(edge)
            existing_index = edge_indices.get(edge)
            if existing_index is None:
                edge_indices[edge] = len(validated)
                validated.append(relation)
                continue
            existing = validated[existing_index]
            evidence = list(
                dict.fromkeys(
                    [
                        *existing.evidence_chapter_indices,
                        *relation.evidence_chapter_indices,
                    ]
                )
            )
            validated[existing_index] = existing.model_copy(
                update={"evidence_chapter_indices": evidence}
            )
    return validated


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
    """在单事务中替换人物快照、人物、出场和关系。

    保存前复核逐章来源；任一失败 rollback 并保留旧快照。返回新快照根记录。
    """
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
# 这是纯业务编排入口；任务状态的 claim/失败落盘由下面的 job runner 负责。
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


def _sleep_before_claim_retry(failed_attempt: int) -> None:
    """领取人物任务遇 SQLite 写锁后按次数执行短退避。"""
    time.sleep(
        CHARACTER_JOB_SQLITE_RETRY_BASE_SECONDS
        * (2 ** (failed_attempt - 1))
    )


def _claim_character_job(
    db: Session,
    job_id: str,
    claimed_at: datetime,
) -> int:
    """有界 SQLite 锁重试地领取人物任务。

    db 是 worker 会话，job_id 定位任务，claimed_at 写入开始时间。条件 UPDATE 只
    接受 queued，因此重复派发仅一个 worker 得到返回 1；已被领取/结束返回 0，
    非锁数据库错误继续抛出。
    """
    for attempt in range(1, CHARACTER_JOB_SQLITE_MAX_ATTEMPTS + 1):
        try:
            claimed = (
                db.query(AnalysisJob)
                .filter(
                    AnalysisJob.id == job_id,
                    AnalysisJob.job_type == "characters",
                    AnalysisJob.status == "queued",
                )
                .update(
                    {
                        AnalysisJob.status: "running",
                        AnalysisJob.progress_current: 0,
                        AnalysisJob.error_message: None,
                        AnalysisJob.started_at: claimed_at,
                        AnalysisJob.completed_at: None,
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            return int(claimed)
        except OperationalError as error:
            db.rollback()
            if (
                not is_sqlite_lock_error(error)
                or attempt == CHARACTER_JOB_SQLITE_MAX_ATTEMPTS
            ):
                raise
            logger.warning(
                "Character job %s claim hit a SQLite write lock "
                "(attempt %s/%s).",
                job_id,
                attempt,
                CHARACTER_JOB_SQLITE_MAX_ATTEMPTS,
            )
            _sleep_before_claim_retry(attempt)
    return 0


def _character_job_has_live_source(
    session_factory: Callable[[], Session],
    *,
    job_id: str,
    novel_id: int,
) -> bool:
    """确认人物任务仍运行且小说仍存在，供每次模型调用前取消检测。"""
    with session_factory() as status_db:
        return (
            status_db.query(AnalysisJob.id)
            .join(Novel, Novel.id == AnalysisJob.novel_id)
            .filter(
                AnalysisJob.id == job_id,
                AnalysisJob.job_type == "characters",
                AnalysisJob.status == "running",
                Novel.id == novel_id,
            )
            .first()
            is not None
        )


def _close_provider(provider: LLMProvider) -> None:
    """调用人物 provider 的可选 close；异常只记录日志。"""
    close = getattr(provider, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        logger.exception("Could not close the character provider.")


def run_character_job(
    job_id: str,
    provider: LLMProvider,
    session_factory: Callable[[], Session],
) -> None:
    """Execute one full-book character job and release its provider."""
    try:
        _run_character_job(job_id, provider, session_factory)
    finally:
        _close_provider(provider)


def _run_character_job(
# worker 必须重新从数据库读取任务范围，不能信任进程内回调参数作为事实来源。
    job_id: str,
    provider: LLMProvider,
    session_factory: Callable[[], Session],
) -> None:
    """领取任务、准备章节、归并/推理并原子保存人物快照。

    网络调用不持有写事务；异常由外层写 failed，旧快照保持可用。
    """
    with session_factory() as db:
        try:
            claimed = _claim_character_job(db, job_id, utc_now())
        except Exception as error:
            db.rollback()
            locked = (
                isinstance(error, OperationalError)
                and is_sqlite_lock_error(error)
            )
            if locked:
                logger.error(
                    "Could not claim character job %s because the "
                    "SQLite database remained locked.",
                    job_id,
                )
            else:
                logger.exception(
                    "Could not claim character job %s.",
                    job_id,
                )
            message = (
                "人物分析任务无法获取本地数据库写锁，请重试。"
                if locked
                else "人物分析任务启动失败，请查看后端日志后重试。"
            )
            mark_analysis_job_failed_with_sqlite_lock_retry(
                session_factory,
                job_id,
                "characters",
                message,
                expected_status="queued",
                failure_context="claim failure",
            )
            return
        if claimed != 1:
            return

        continuation_check: Optional[Callable[[], bool]] = None
        try:
            scope = (
                db.query(
                    AnalysisJob.novel_id,
                    AnalysisJob.target_chapter_index,
                    AnalysisJob.force_refresh,
                )
                .filter(AnalysisJob.id == job_id)
                .one_or_none()
            )
            if scope is None:
                return
            novel_id, target_chapter_index, force_refresh = scope
            if target_chapter_index is not None:
                raise RuntimeError(
                    "人物分析只支持完整小说任务。"
                )
            novel = db.get(Novel, novel_id)
            if novel is None:
                return
            continuation_check = partial(
                _character_job_has_live_source,
                session_factory,
                job_id=job_id,
                novel_id=novel_id,
            )
            planned_steps = _planned_character_generation_steps(
                db,
                novel,
                provider_model=provider.model,
                force=bool(force_refresh),
            )
            initialized = (
                db.query(AnalysisJob)
                .filter(
                    AnalysisJob.id == job_id,
                    AnalysisJob.status == "running",
                )
                .update(
                    {
                        AnalysisJob.progress_current: 0,
                        AnalysisJob.progress_total: planned_steps,
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if initialized != 1:
                return

            def record_generation_step() -> None:
                """模型每完成一步便以独立短事务推进任务进度。

                若人物消歧产生估算外步骤，先增加 progress_total 再增加 current，
                始终满足数据库 ``current <= total``。SQLite 锁按有界策略重试；任务
                已取消或删除时抛内部取消异常。
                """
                for attempt in range(
                    1, CHARACTER_JOB_SQLITE_MAX_ATTEMPTS + 1
                ):
                    try:
                        with session_factory() as progress_db:
                            next_progress = (
                                AnalysisJob.progress_current + 1
                            )
                            progressed = (
                                progress_db.query(AnalysisJob)
                                .filter(
                                    AnalysisJob.id == job_id,
                                    AnalysisJob.status == "running",
                                )
                                .update(
                                    {
                                        AnalysisJob.progress_current: (
                                            next_progress
                                        ),
                                        AnalysisJob.progress_total: case(
                                            (
                                                AnalysisJob.progress_total
                                                < next_progress,
                                                next_progress,
                                            ),
                                            else_=(
                                                AnalysisJob.progress_total
                                            ),
                                        ),
                                    },
                                    synchronize_session=False,
                                )
                            )
                            progress_db.commit()
                        if progressed != 1:
                            raise _CharacterJobCancelled()
                        return
                    except OperationalError as error:
                        if (
                            not is_sqlite_lock_error(error)
                            or attempt
                            == CHARACTER_JOB_SQLITE_MAX_ATTEMPTS
                        ):
                            raise
                        _sleep_before_claim_retry(attempt)

            analyze_novel_characters(
                db,
                novel,
                provider,
                force=bool(force_refresh),
                persist_extractions_incrementally=True,
                should_continue=continuation_check,
                on_generation_step=record_generation_step,
            )
            completed = (
                db.query(AnalysisJob)
                .filter(
                    AnalysisJob.id == job_id,
                    AnalysisJob.status == "running",
                )
                .update(
                    {
                        AnalysisJob.status: "succeeded",
                        AnalysisJob.progress_total: (
                            AnalysisJob.progress_current
                        ),
                        AnalysisJob.completed_at: utc_now(),
                    },
                    synchronize_session=False,
                )
            )
            if completed != 1:
                db.rollback()
                return
            db.commit()
        except _CharacterJobCancelled:
            db.rollback()
            logger.info(
                "Character job %s stopped because its running state "
                "or source was removed.",
                job_id,
            )
        except Exception as error:
            db.rollback()
            if continuation_check is not None:
                try:
                    if not continuation_check():
                        logger.info(
                            "Character job %s stopped because its "
                            "running state or source was removed.",
                            job_id,
                        )
                        return
                except Exception:
                    logger.exception(
                        "Could not verify character job %s "
                        "cancellation.",
                        job_id,
                    )
            locked = (
                isinstance(error, OperationalError)
                and is_sqlite_lock_error(error)
            )
            if locked:
                logger.error(
                    "Character job %s could not persist local progress "
                    "because the SQLite database remained locked.",
                    job_id,
                )
            elif not isinstance(error, LLMProviderError):
                logger.exception(
                    "Unexpected character analysis job failure."
                )
            message = (
                str(error)
                if isinstance(error, LLMProviderError)
                else (
                    "人物分析任务无法获取本地数据库写锁，请重试。"
                    if locked
                    else (
                        "人物分析任务执行失败，请查看后端日志后重试。"
                    )
                )
            )[:1_000]
            if not mark_analysis_job_failed_with_sqlite_lock_retry(
                session_factory,
                job_id,
                "characters",
                message,
                expected_status="running",
                failure_context="execution failure",
            ):
                logger.error(
                    "Character job %s could not be transitioned out "
                    "of running state.",
                    job_id,
                )
