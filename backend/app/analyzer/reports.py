"""报告每条引用都必须属于当前小说章节白名单；执行前后再次检查来源是否变化，
成功后才原子替换旧报告，避免“新报告引用旧材料”。
"""

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Callable, Optional, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.app.analyzer.characters import (
    CHARACTER_EXTRACTION_PROMPT_VERSION,
    CHARACTER_MERGE_PROMPT_VERSION,
    CHARACTER_RELATIONSHIP_PROMPT_VERSION,
    StoredChapterExtractionData,
    character_analysis_source_hash,
    character_chapter_source_hash,
)
from backend.app.analyzer.jobs import (
    claim_analysis_job,
    advance_analysis_job,
    is_sqlite_lock_error,
    mark_analysis_job_failed_with_sqlite_lock_retry,
)
from backend.app.analyzer.llm import (
    LLMProvider,
    LLMProviderError,
    ProviderResponseError,
)
from backend.app.analyzer.summaries import (
    SUMMARY_PROMPT_VERSION,
    chapter_summary_source_hash,
)
from backend.app.analyzer.prompting import novelmind_system_prompt
from backend.app.core.sqlite_retry import retry_sqlite
from backend.app.core.time import utc_now
from backend.app.models.analysis import AnalysisJob, ChapterSummary
from backend.app.models.character import (
    ChapterCharacterExtraction,
    Character,
    CharacterAnalysis,
    CharacterRelationship,
)
from backend.app.models.novel import Chapter, Novel
from backend.app.models.report import NovelReport


logger = logging.getLogger(__name__)

REPORT_PROMPT_VERSION = "novel-report-v2"
MAX_REPORT_BATCH_CHARACTERS = 46_000
MAX_REPORT_FINAL_INPUT_CHARACTERS = 60_000
REPORT_REDUCTION_GROUP_SIZE = 3
MAX_REPORT_CHARACTER_PROFILES = 24
MAX_REPORT_RELATIONSHIPS = 40
MAX_REPORT_DESCRIPTION_CHARACTERS = 320
MAX_REPORT_ALIASES = 8
MAX_REPORT_ALIAS_CHARACTERS = 64
REPORT_JOB_SQLITE_MAX_ATTEMPTS = 4
REPORT_JOB_SQLITE_RETRY_BASE_SECONDS = 0.05


class ReportSourceError(RuntimeError):
    """表示本地派生输入缺失、过期或互相不一致，不应发起外部调用。"""


class _ReportJobCancelled(RuntimeError):
    """Stop after a report job or its source novel disappears."""


def _required_text(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("text cannot be blank")
    return normalized


def _validated_chapter_indices(values: list[int]) -> list[int]:
    if len(values) != len(set(values)):
        raise ValueError("chapter evidence cannot contain duplicates")
    if values != sorted(values):
        raise ValueError("chapter evidence must be sorted")
    return values


class ReportEvidenceNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: StrictStr = Field(min_length=1, max_length=100)
    analysis: StrictStr = Field(min_length=1, max_length=800)
    evidence_chapter_indices: list[StrictInt] = Field(
        min_length=1,
        max_length=32,
    )

    @field_validator("topic", "analysis")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("evidence_chapter_indices")
    @classmethod
    def validate_evidence(cls, values: list[int]) -> list[int]:
        return _validated_chapter_indices(values)


class ReportEvidenceNotesPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notes: list[ReportEvidenceNote] = Field(
        min_length=1,
        max_length=16,
    )


class ReportSectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: StrictStr = Field(min_length=1, max_length=100)
    analysis: StrictStr = Field(min_length=1, max_length=4_000)
    evidence_chapter_indices: list[StrictInt] = Field(
        min_length=1,
        max_length=100,
    )

    @field_validator("title", "analysis")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("evidence_chapter_indices")
    @classmethod
    def validate_evidence(cls, values: list[int]) -> list[int]:
        return _validated_chapter_indices(values)


class NovelReportPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: StrictStr = Field(min_length=1, max_length=200)
    executive_summary: ReportSectionPayload
    plot_structure: ReportSectionPayload
    character_dynamics: ReportSectionPayload
    pacing: ReportSectionPayload
    themes: list[ReportSectionPayload] = Field(
        default_factory=list,
        max_length=8,
    )
    strengths: list[ReportSectionPayload] = Field(
        default_factory=list,
        max_length=8,
    )
    risks: list[ReportSectionPayload] = Field(
        default_factory=list,
        max_length=8,
    )
    recommendations: list[ReportSectionPayload] = Field(
        default_factory=list,
        max_length=8,
    )

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        return _required_text(value)

    def sections(self) -> list[ReportSectionPayload]:
        return [
            self.executive_summary,
            self.plot_structure,
            self.character_dynamics,
            self.pacing,
            *self.themes,
            *self.strengths,
            *self.risks,
            *self.recommendations,
        ]


@dataclass(frozen=True)
class PreparedReportSource:
    novel_id: int
    filename: str
    chapter_count: int
    summary_data: tuple[dict[str, object], ...]
    character_data: tuple[dict[str, object], ...]
    relationship_data: tuple[dict[str, object], ...]
    total_character_count: int
    includes_character_analysis: bool
    source_hash: str


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _current_character_source(
    db: Session,
    *,
    novel: Novel,
    expected_model: str,
) -> Optional[
    tuple[
        CharacterAnalysis,
        tuple[dict[str, object], ...],
        tuple[dict[str, object], ...],
        int,
    ]
]:
    """不存在或任一完整性条件不符时返回 None，报告便只使用章节摘要。"""
    analysis = (
        db.query(CharacterAnalysis)
        .filter(CharacterAnalysis.novel_id == novel.id)
        .one_or_none()
    )
    if (
        analysis is None
        or analysis.model != expected_model
        or analysis.extraction_prompt_version
        != CHARACTER_EXTRACTION_PROMPT_VERSION
        or analysis.merge_prompt_version != CHARACTER_MERGE_PROMPT_VERSION
        or analysis.relationship_prompt_version
        != CHARACTER_RELATIONSHIP_PROMPT_VERSION
        or analysis.chapter_count != novel.chapter_count
    ):
        return None

    extraction_rows = (
        db.query(Chapter, ChapterCharacterExtraction)
        .outerjoin(
            ChapterCharacterExtraction,
            ChapterCharacterExtraction.chapter_id == Chapter.id,
        )
        .filter(Chapter.novel_id == novel.id)
        .order_by(Chapter.chapter_index)
        .all()
    )
    hash_input: list[dict[str, object]] = []
    for chapter, extraction in extraction_rows:
        if (
            extraction is None
            or extraction.source_hash
            != character_chapter_source_hash(
                chapter.title,
                chapter.content,
            )
            or extraction.model != expected_model
            or extraction.prompt_version
            != CHARACTER_EXTRACTION_PROMPT_VERSION
        ):
            return None
        try:
            extraction_data = StoredChapterExtractionData.model_validate(
                extraction.extraction_data
            )
        except ValidationError:
            return None
        hash_input.append(
            {
                "chapter_index": int(chapter.chapter_index),
                "source_hash": extraction.source_hash,
                "extraction_data": extraction_data.model_dump(mode="json"),
            }
        )
    if (
        len(hash_input) != novel.chapter_count
        or character_analysis_source_hash(hash_input)
        != analysis.source_hash
    ):
        return None

    characters = (
        db.query(Character)
        .filter(Character.analysis_id == analysis.id)
        .order_by(
            Character.mention_count.desc(),
            Character.chapter_count.desc(),
            Character.id,
        )
        .all()
    )
    selected = characters[:MAX_REPORT_CHARACTER_PROFILES]
    selected_ids = {int(character.id) for character in selected}
    character_data = tuple(
        {
            "character_id": int(character.id),
            "name": character.canonical_name,
            "aliases": [
                str(alias)[:MAX_REPORT_ALIAS_CHARACTERS]
                for alias in list(character.aliases)[:MAX_REPORT_ALIASES]
            ],
            "description": character.short_description[
                :MAX_REPORT_DESCRIPTION_CHARACTERS
            ],
            "first_chapter_index": int(character.first_chapter_index),
            "last_chapter_index": int(character.last_chapter_index),
            "chapter_count": int(character.chapter_count),
            "mention_count": int(character.mention_count),
        }
        for character in selected
    )
    names = {
        int(character.id): character.canonical_name
        for character in selected
    }
    relationships = (
        db.query(CharacterRelationship)
        .filter(
            CharacterRelationship.analysis_id == analysis.id,
            CharacterRelationship.source_character_id.in_(selected_ids),
            CharacterRelationship.target_character_id.in_(selected_ids),
        )
        .order_by(CharacterRelationship.id)
        .limit(MAX_REPORT_RELATIONSHIPS)
        .all()
        if selected_ids
        else []
    )
    relationship_data = tuple(
        {
            "source": names[int(relation.source_character_id)],
            "target": names[int(relation.target_character_id)],
            "directed": bool(relation.directed),
            "relation_type": relation.relation_type,
            "description": relation.description[
                :MAX_REPORT_DESCRIPTION_CHARACTERS
            ],
            "evidence_chapter_indices": list(
                relation.evidence_chapter_indices
            ),
        }
        for relation in relationships
    )
    return (
        analysis,
        character_data,
        relationship_data,
        len(characters),
    )


def prepare_report_source(
# 估算接口与执行器都调用同一准备函数，保证界面披露和真实外发范围一致。
    db: Session,
    *,
    novel_id: int,
    expected_model: str,
) -> PreparedReportSource:
    """Load only current derived analysis and hash exactly what is sent."""
    novel = db.get(Novel, novel_id)
    if novel is None:
        raise ReportSourceError("未找到指定小说。")
    rows = (
        db.query(Chapter, ChapterSummary)
        .outerjoin(
            ChapterSummary,
            ChapterSummary.chapter_id == Chapter.id,
        )
        .filter(Chapter.novel_id == novel_id)
        .order_by(Chapter.chapter_index)
        .all()
    )
    if not rows:
        raise ReportSourceError("该小说没有可用于报告的章节。")

    summaries: list[dict[str, object]] = []
    for chapter, summary in rows:
        if (
            summary is None
            or summary.source_hash
            != chapter_summary_source_hash(
                chapter.title,
                chapter.content,
            )
            or summary.model != expected_model
            or summary.prompt_version != SUMMARY_PROMPT_VERSION
        ):
            raise ReportSourceError(
                "报告要求每一章都有当前版本的章节摘要；"
                "请先完成或刷新全部章节摘要。"
            )
        summaries.append(
            {
                "chapter_index": int(chapter.chapter_index),
                "title": chapter.title,
                "summary": summary.summary,
                "key_events": list(summary.key_events),
                "plot_progression": summary.plot_progression,
                "cliffhanger": summary.cliffhanger,
            }
        )

    character_source = _current_character_source(
        db,
        novel=novel,
        expected_model=expected_model,
    )
    if character_source is None:
        character_data: tuple[dict[str, object], ...] = ()
        relationship_data: tuple[dict[str, object], ...] = ()
        total_character_count = 0
        includes_character_analysis = False
    else:
        (
            _,
            character_data,
            relationship_data,
            total_character_count,
        ) = character_source
        includes_character_analysis = True

    hash_material = {
        "prompt_version": REPORT_PROMPT_VERSION,
        "model": expected_model,
        "filename": novel.filename,
        "summaries": summaries,
        "characters": list(character_data),
        "relationships": list(relationship_data),
    }
    source_hash = hashlib.sha256(
        _canonical_json(hash_material).encode("utf-8")
    ).hexdigest()
    return PreparedReportSource(
        novel_id=int(novel.id),
        filename=novel.filename,
        chapter_count=len(summaries),
        summary_data=tuple(summaries),
        character_data=character_data,
        relationship_data=relationship_data,
        total_character_count=total_character_count,
        includes_character_analysis=includes_character_analysis,
        source_hash=source_hash,
    )


def _summary_batches(
    summaries: tuple[dict[str, object], ...],
) -> list[list[dict[str, object]]]:
    batches: list[list[dict[str, object]]] = []
    current: list[dict[str, object]] = []
    for summary in summaries:
        candidate = [*current, summary]
        if (
            current
            and len(_canonical_json(candidate))
            > MAX_REPORT_BATCH_CHARACTERS
        ):
            batches.append(current)
            current = [summary]
        else:
            current = candidate
        if len(_canonical_json(current)) > MAX_REPORT_BATCH_CHARACTERS:
            raise ReportSourceError(
                "单章摘要数据过长，无法安全生成报告；"
                "请重新生成更精简的章节摘要。"
            )
    if current:
        batches.append(current)
    return batches


def estimate_report_generation_steps(
    source: PreparedReportSource,
) -> int:
    batch_count = len(_summary_batches(source.summary_data))
    steps = batch_count
    while batch_count > 1:
        batch_count = math.ceil(
            batch_count / REPORT_REDUCTION_GROUP_SIZE
        )
        steps += batch_count
    return steps + 1


def report_is_current(
    report: Optional[NovelReport],
    *,
    source: PreparedReportSource,
    model: str,
) -> bool:
    if report is None:
        return False
    if (
        report.source_hash != source.source_hash
        or report.model != model
        or report.prompt_version != REPORT_PROMPT_VERSION
        or report.summary_count != source.chapter_count
        or report.character_count != source.total_character_count
        or bool(report.includes_character_analysis)
        != source.includes_character_analysis
    ):
        return False
    try:
        NovelReportPayload.model_validate(report.report_data)
    except ValidationError:
        return False
    return True


def _system_prompt() -> str:
    return novelmind_system_prompt(
        role="资深中文小说分析编辑与叙事分析师",
        material_scope=(
            "chapter_summaries 与 evidence_notes 是派生摘要，不是小说原文；"
            "character_profiles 与 character_relationships 也是派生分析。"
            "因此可以判断事件结构、因果、角色行动、关系变化、线索、承诺和"
            "章节/阶段层面的推进节奏；不能可靠评价原文句式、修辞、具体用词、"
            "真实字数、段落节奏或作者实际语言风格，也不能把规划或摘要效果"
            "表述成原文已经实现的阅读效果。"
        ),
        task_rules=(
            "关键结论必须引用实际支持它的 chapter_index，引用只取最关键的"
            "章节，不堆砌无关证据。优先识别作品持续吸引读者的运行机制、"
            "结构根因与问题依赖关系；不要把同一问题换词重复到多个栏目。"
            "优势必须说明其具体机制和作用；风险必须说明位置、根因与影响；"
            "建议采用“对象或阶段 → 具体操作 → 预期效果 → 代价或风险 →"
            "完成判据”的闭环，并保护已经成立的优势。只展开材料足以支持、"
            "会影响作者决策的内容；可选数组没有可靠内容时返回空数组，不为"
            "填满模板制造主题、优点、风险或建议。不得给出无依据的评分、"
            "读者比例、市场概率或平台趋势。"
        ),
    )


def _ensure_can_continue(
    should_continue: Optional[Callable[[], bool]],
) -> None:
    """任务/来源失效时停止下一次模型调用。"""
    if should_continue is not None and not should_continue():
        raise _ReportJobCancelled()


def _validate_note_citations(
    payload: ReportEvidenceNotesPayload,
    allowed_indices: set[int],
) -> None:
    for note in payload.notes:
        if not set(note.evidence_chapter_indices) <= allowed_indices:
            raise ProviderResponseError()


def _validate_report_citations(
    payload: NovelReportPayload,
    allowed_indices: set[int],
) -> None:
    for section in payload.sections():
        if not set(section.evidence_chapter_indices) <= allowed_indices:
            raise ProviderResponseError()


def generate_report_payload(
# 分批/分层过程控制单次提示体积；最终结果还要统一检查章节引用白名单。
    source: PreparedReportSource,
    provider: LLMProvider,
    *,
    should_continue: Optional[Callable[[], bool]] = None,
    on_generation_step: Optional[Callable[[], None]] = None,
) -> tuple[NovelReportPayload, int]:
    note_payloads: list[ReportEvidenceNotesPayload] = []
    generation_steps = 0
    for summaries in _summary_batches(source.summary_data):
        allowed = {
            int(cast(int, summary["chapter_index"])) for summary in summaries
        }
        _ensure_can_continue(should_continue)
        result = provider.generate_json(
            system_prompt=_system_prompt(),
            user_prompt=(
                "执行全书报告的证据提炼步骤：把下一行 JSON 中的连续章节"
                "摘要压缩为最小充分证据集。优先保留不可逆事件、关键选择与"
                "代价、明确因果、主线目标/阻力变化、关系质变、规则变化、"
                "伏笔/承诺的建立或回收，以及能支持结构、人物和阶段节奏"
                "判断的信号；删除重复复述和无后续影响细节。最多返回 16 条 "
                "notes；每条只含 topic、analysis、"
                "evidence_chapter_indices。analysis 说明“发生或变化了什么"
                " → 原因/选择 → 结果/影响”，对摘要无法裁决的冲突或不确定"
                "如实保留。引用只能来自本批章节：\n"
                f"{_canonical_json({'chapter_summaries': summaries})}"
            ),
            response_model=ReportEvidenceNotesPayload,
            thinking=False,
            before_attempt=partial(
                _ensure_can_continue,
                should_continue,
            ),
        )
        _validate_note_citations(result.value, allowed)
        note_payloads.append(result.value)
        generation_steps += 1
        if on_generation_step is not None:
            on_generation_step()

    while len(note_payloads) > 1:
        reduced: list[ReportEvidenceNotesPayload] = []
        for start in range(
            0,
            len(note_payloads),
            REPORT_REDUCTION_GROUP_SIZE,
        ):
            group = note_payloads[
                start : start + REPORT_REDUCTION_GROUP_SIZE
            ]
            allowed = {
                chapter_index
                for payload in group
                for note in payload.notes
                for chapter_index in note.evidence_chapter_indices
            }
            _ensure_can_continue(should_continue)
            result = provider.generate_json(
                system_prompt=_system_prompt(),
                user_prompt=(
                    "执行全书报告的证据归并步骤：合并下一行 JSON 中按剧情"
                    "顺序排列的连续证据笔记。按因果和状态变化去重，优先保留"
                    "不可逆事件、目标/阻力变化、关键选择与代价、关系或规则"
                    "质变、伏笔/承诺及其后果；不得把不同事件因措辞相近而"
                    "错误合并，不得强行裁决输入中的冲突。仍最多返回 16 条 "
                    "notes，每条只含 topic、analysis、"
                    "evidence_chapter_indices，不得增加输入中没有的事实或"
                    "章节引用：\n"
                    f"{_canonical_json({'note_groups': [item.model_dump(mode='json') for item in group]})}"
                ),
                response_model=ReportEvidenceNotesPayload,
                thinking=False,
                before_attempt=partial(
                    _ensure_can_continue,
                    should_continue,
                ),
            )
            _validate_note_citations(result.value, allowed)
            reduced.append(result.value)
            generation_steps += 1
            if on_generation_step is not None:
                on_generation_step()
        note_payloads = reduced

    allowed_indices = {
        int(cast(int, summary["chapter_index"])) for summary in source.summary_data
    }
    final_input = {
        "filename": source.filename,
        "chapter_count": source.chapter_count,
        "evidence_notes": note_payloads[0].model_dump(mode="json"),
        "character_analysis_included": (
            source.includes_character_analysis
        ),
        "character_profiles": list(source.character_data),
        "character_relationships": list(source.relationship_data),
    }
    if (
        len(_canonical_json(final_input))
        > MAX_REPORT_FINAL_INPUT_CHARACTERS
    ):
        raise ReportSourceError(
            "报告派生数据超过安全请求上限；"
            "请减少过长的人物别名或重新生成更精简的分析数据。"
        )
    _ensure_can_continue(should_continue)
    report_result = provider.generate_json(
        system_prompt=_system_prompt(),
        user_prompt=(
            "执行全书报告的最终编辑步骤。结论先行，并按影响作者决策的"
            "优先级组织，而不是机械复述所有笔记。字段严格为 title、"
            "executive_summary、plot_structure、character_dynamics、"
            "pacing、themes、strengths、risks、recommendations。"
            "executive_summary 用 1—3 个核心结论说明作品运行机制、最大"
            "优势、根因级风险与材料限制；plot_structure 检查目标—行动—"
            "阻力—选择—代价—新局面的因果连续性、阶段转折、主支线和"
            "悬念/承诺；character_dynamics 聚焦目标、选择、关系变化、"
            "叙事功能与一致性；pacing 只评价摘要可支持的章节/阶段推进、"
            "信息与情绪任务配置，不评价原文句式或段落速度。themes、"
            "strengths、risks、recommendations 都是对象数组，只列证据"
            "充分且互不重复的项目，没有可靠项目可为空数组。除 title 外，"
            "每个对象只含 title、analysis、evidence_chapter_indices。"
            "recommendations.analysis 必须写明对象/阶段、根因、具体操作、"
            "预期效果、代价或风险与完成判据；引用触发该建议的关键章节。"
            "character_analysis_included=false 时，character_dynamics "
            "只能依据 evidence_notes，并在 analysis 中明确这一限制：\n"
            f"{_canonical_json(final_input)}"
        ),
        response_model=NovelReportPayload,
        thinking=True,
        reasoning_effort="high",
        before_attempt=partial(
            _ensure_can_continue,
            should_continue,
        ),
    )
    _validate_report_citations(report_result.value, allowed_indices)
    generation_steps += 1
    if on_generation_step is not None:
        on_generation_step()
    return report_result.value, generation_steps


from backend.app.core.sqlite_retry import sleep_before_retry as _sleep_before_sqlite_retry


def _claim_report_job(
    db: Session,
    job_id: str,
    claimed_at: datetime,
) -> int:
    return claim_analysis_job(db, job_id, "report", claimed_at, sleep=_sleep_before_sqlite_retry)


def _report_job_has_live_source(
    session_factory: Callable[[], Session],
    *,
    job_id: str,
    novel_id: int,
) -> bool:
    with session_factory() as status_db:
        return (
            status_db.query(AnalysisJob.id)
            .join(Novel, Novel.id == AnalysisJob.novel_id)
            .filter(
                AnalysisJob.id == job_id,
                AnalysisJob.job_type == "report",
                AnalysisJob.status == "running",
                Novel.id == novel_id,
            )
            .first()
            is not None
        )


def _report_job_source_is_unchanged(
    session_factory: Callable[[], Session],
    *,
    job_id: str,
    novel_id: int,
    expected_model: str,
    expected_source_hash: str,
) -> bool:
    """前置条件失败也返回 False，调用者据此取消迟到结果。"""
    if not _report_job_has_live_source(
        session_factory,
        job_id=job_id,
        novel_id=novel_id,
    ):
        return False
    try:
        with session_factory() as source_db:
            current = prepare_report_source(
                source_db,
                novel_id=novel_id,
                expected_model=expected_model,
            )
        return current.source_hash == expected_source_hash
    except ReportSourceError:
        return False


def _mark_report_job_failed(
    session_factory: Callable[[], Session],
    job_id: str,
    error_message: str,
    *,
    expected_status: str,
    failure_context: str,
) -> bool:
    """只将仍处于 expected_status 的任务终结为 failed。"""
    return mark_analysis_job_failed_with_sqlite_lock_retry(
        session_factory,
        job_id,
        "report",
        error_message,
        expected_status=expected_status,
        failure_context=failure_context,
    )


def _close_provider(provider: LLMProvider) -> None:
    close = getattr(provider, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        logger.exception("Could not close the report provider.")


def run_report_job(
    job_id: str,
    provider: LLMProvider,
    session_factory: Callable[[], Session],
) -> None:
    """网络调用期间不持有 SQLite 写事务；失败保留旧 NovelReport。"""
    try:
        _run_report_job(job_id, provider, session_factory)
    finally:
        _close_provider(provider)


def _run_report_job(
# worker 在模型调用前后检查活动状态和来源哈希，迟到结果不会覆盖更新后的数据。
    job_id: str,
    provider: LLMProvider,
    session_factory: Callable[[], Session],
) -> None:
    with session_factory() as db:
        try:
            claimed = _claim_report_job(db, job_id, utc_now())
        except OperationalError as error:
            db.rollback()
            is_lock = is_sqlite_lock_error(error)
            if not is_lock:
                logger.exception("Could not claim report job %s.", job_id)
            message = (
                "报告任务无法获取本地数据库写锁，请重试。"
                if is_lock
                else "报告任务启动失败，请查看后端日志后重试。"
            )
            _mark_report_job_failed(
                session_factory,
                job_id,
                message,
                expected_status="queued",
                failure_context="claim failure",
            )
            return
        except Exception:
            db.rollback()
            logger.exception("Could not claim report job %s.", job_id)
            _mark_report_job_failed(
                session_factory,
                job_id,
                "报告任务启动失败，请查看后端日志后重试。",
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
                    AnalysisJob.force_refresh,
                )
                .filter(AnalysisJob.id == job_id)
                .one_or_none()
            )
            if scope is None:
                return
            novel_id, force_refresh = scope
            source = prepare_report_source(
                db,
                novel_id=int(novel_id),
                expected_model=provider.model,
            )
            continuation_check = partial(
                _report_job_source_is_unchanged,
                session_factory,
                job_id=job_id,
                novel_id=int(novel_id),
                expected_model=provider.model,
                expected_source_hash=source.source_hash,
            )
            existing = (
                db.query(NovelReport)
                .filter(NovelReport.novel_id == novel_id)
                .one_or_none()
            )
            if (
                not bool(force_refresh)
                and report_is_current(
                    existing,
                    source=source,
                    model=provider.model,
                )
            ):
                (
                    db.query(AnalysisJob)
                    .filter(
                        AnalysisJob.id == job_id,
                        AnalysisJob.status == "running",
                    )
                    .update(
                        {
                            AnalysisJob.progress_total: 0,
                            AnalysisJob.status: "succeeded",
                            AnalysisJob.completed_at: utc_now(),
                        },
                        synchronize_session=False,
                    )
                )
                db.commit()
                return

            expected_steps = estimate_report_generation_steps(source)
            initialized = (
                db.query(AnalysisJob)
                .filter(
                    AnalysisJob.id == job_id,
                    AnalysisJob.status == "running",
                )
                .update(
                    {
                        AnalysisJob.progress_total: expected_steps,
                        AnalysisJob.progress_current: 0,
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if initialized != 1:
                return

            def advance_progress() -> None:
                def progress_step():
                    with session_factory() as progress_db:
                        if not advance_analysis_job(progress_db, job_id, expand_total=False):
                            raise _ReportJobCancelled()
                retry_sqlite(progress_step, sleep=_sleep_before_sqlite_retry)

            payload, generation_steps = generate_report_payload(
                source,
                provider,
                should_continue=continuation_check,
                on_generation_step=advance_progress,
            )
            _ensure_can_continue(continuation_check)
            db.expire_all()
            current_source = prepare_report_source(
                db,
                novel_id=int(novel_id),
                expected_model=provider.model,
            )
            if current_source.source_hash != source.source_hash:
                raise ReportSourceError(
                    "生成期间章节摘要或人物分析已变化，旧报告已保留；"
                    "请重新生成。"
                )
            now = utc_now()
            existing = (
                db.query(NovelReport)
                .filter(NovelReport.novel_id == novel_id)
                .one_or_none()
            )
            if existing is None:
                existing = NovelReport(
                    novel_id=int(novel_id),
                    created_at=now,
                )
                db.add(existing)
            existing.source_hash = source.source_hash
            existing.report_data = payload.model_dump(mode="json")
            existing.model = provider.model
            existing.prompt_version = REPORT_PROMPT_VERSION
            existing.summary_count = source.chapter_count
            existing.character_count = source.total_character_count
            existing.generation_steps = generation_steps
            existing.includes_character_analysis = (
                source.includes_character_analysis
            )
            existing.updated_at = now
            completed = (
                db.query(AnalysisJob)
                .filter(
                    AnalysisJob.id == job_id,
                    AnalysisJob.status == "running",
                    AnalysisJob.progress_current
                    == AnalysisJob.progress_total,
                )
                .update(
                    {
                        AnalysisJob.status: "succeeded",
                        AnalysisJob.completed_at: now,
                    },
                    synchronize_session=False,
                )
            )
            if completed != 1:
                db.rollback()
                return
            db.commit()
        except _ReportJobCancelled:
            db.rollback()
            logger.info(
                "Report job %s stopped because its state or source "
                "was removed.",
                job_id,
            )
            _mark_report_job_failed(
                session_factory,
                job_id,
                "生成期间章节摘要或人物分析发生变化；旧报告已保留，请重新生成。",
                expected_status="running",
                failure_context="source changed during generation",
            )
        except Exception as error:
            db.rollback()
            if continuation_check is not None:
                try:
                    if not continuation_check():
                        return
                except Exception:
                    logger.exception(
                        "Could not verify report job %s cancellation.",
                        job_id,
                    )
            is_lock = (
                isinstance(error, OperationalError)
                and is_sqlite_lock_error(error)
            )
            if not isinstance(
                error,
                (LLMProviderError, ReportSourceError),
            ) and not is_lock:
                logger.exception("Unexpected report job failure.")
            if isinstance(error, (LLMProviderError, ReportSourceError)):
                message = str(error)
            elif is_lock:
                message = "报告任务无法获取本地数据库写锁，请重试。"
            else:
                message = "报告生成失败，请查看后端日志后重试。"
            _mark_report_job_failed(
                session_factory,
                job_id,
                message[:1_000],
                expected_status="running",
                failure_context="execution failure",
            )
