import hashlib
import json
import logging
from typing import Callable, Optional, Type
from pydantic import ValidationError
from sqlalchemy.orm import Session
from backend.app.analyzer.llm import LLMProvider, ProviderResponseError
from backend.app.analyzer.prompting import novelmind_system_prompt
from backend.app.core.time import utc_now
from backend.app.models.character import ChapterCharacterExtraction, CharacterAnalysis
from backend.app.models.novel import Chapter, Novel
from .payloads import (
    ChapterCharacterExtractionPayload,
    StoredChapterExtractionData,
    StoredCharacterObservation,
    _AnalysisSourceItem,
    _CharacterJobCancelled,
    _PreparedChapter,
    _bounded_text,
    CHARACTER_EXTRACTION_PROMPT_VERSION,
    CHARACTER_MERGE_PROMPT_VERSION,
    CHARACTER_RELATIONSHIP_PROMPT_VERSION,
    MAX_CHARACTER_EXTRACTION_CHARACTERS,
    MAX_CHARACTER_EXTRACTION_OBSERVATIONS,
    MAX_CHARACTER_EXTRACTION_PAGES,
    MAX_CHARACTER_EXTRACTION_REQUEST_CHARACTERS,
    MAX_CHARACTER_EXTRACTION_TEXT_REQUEST_CHARACTERS,
    MAX_CHARACTER_MERGE_BATCH_ITEMS,
    MAX_CHARACTER_PROFILE_GROUP_ITEMS,
    MAX_CHARACTER_RELATIONSHIP_EVIDENCE_ITEMS,
    T,
)

logger = logging.getLogger(__name__)

def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def character_chapter_source_hash(title: str, content: str) -> str:
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
    return novelmind_system_prompt(
        role="中文小说人物观察提取助手",
        material_scope=(
            "title 与 text 是当前章节的连续原文片段。previous_observations "
            "是同一片段此前分页已返回的派生人物观察，仅用于防止重复提取；你需要"
            "继续从当前 text 中提取尚未返回的人物。"
        ),
        task_rules=(
            "按人物在当前片段中的首次出现顺序，继续提取尚未收录进 "
            "previous_observations 的人物观察："
            "1. 指代同一性判准（Identity Resolution Test）：仅将当前片段内证据"
            "确凿指向同一角色的不同称呼（如本名、字号、职衔、明确外号）合为一条"
            "观察；当神秘人、代词、同名角色或身份不明的称谓存在歧义（或原文刻意"
            "保留悬念尚未揭晓）时，不猜测、不跨人物强行归并。"
            "2. 字段与长度规范："
            "   - name：使用片段中最明确稳定的核心称呼（最多 64 字符，非空字符串）。"
            "   - aliases：只收录本片段明确支持的同一人物其他称呼（最多 3 项，"
            "每项最多 48 字符）；不得包含与 name 完全相同的项；无别名时返回 []。"
            "   - mention_count：只统计当前 text 内能可靠归属于该人物的显式称呼"
            "或明确指代次数；必须是 JSON 整数且落在 1–1000000（不要字符串或小数）。"
            "   - short_description：用事实凝练其在当前片段呈现的身份/阵营、当前"
            "目标或叙事定位（最多 192 字符，非空）。"
            "   - appearance_summary：还原其在本片段中的【具体行动/对话交锋 → "
            "反映出的真实意图/心理状态 → 造成的结果或关系/状态变化】（最多 256 "
            "字符，非空），严禁脱离具体事件只堆砌抽象性格标签。"
            "3. 分页与输出契约：若同一人物没有新观察，不重复 previous_observations。"
            "严格只返回 observations 与 has_more 两个顶层字段；每条 observation 只含 "
            "name、aliases、short_description、mention_count、appearance_summary。"
            "不要复制输入里的 observation_id，不要新增字段。"
            "has_more 必须是 JSON boolean（true/false，不要字符串）。"
            "若仍有未返回人物，has_more=true（此时 observations 不得为空）；"
            "否则为 false。单页最多 8 条。无人物时返回 "
            + '{"observations":[],"has_more":false}。'
            + "合法输出结构示例（内容仅为格式示范）："
            + '{"observations":[{"name":"林默","aliases":["默哥"],'
            + '"short_description":"少年修士，追查失踪线索",'
            + '"mention_count":3,'
            + '"appearance_summary":"追问证人后确认方向并启程"}],'
            + '"has_more":false}。'
        ),
    )


def _ensure_can_continue(
    should_continue: Optional[Callable[[], bool]],
) -> None:
    """失效时抛取消异常，避免继续付费调用。"""
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
    """每次付费请求前检测取消；拒绝重复页面与超出安全上限。"""
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
            history: list[dict[str, object]] = [
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
                    "请从下一行 JSON 的当前章节连续片段提取人物观察（排除 previous_observations "
                    "已收录且无新增信息的人物）：\n"
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
    """新抽取增量落盘，失败后可恢复。"""
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
            if cached is None
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
    """估算值作进度初始 total，不含极端安全上限。"""
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
        # 面向用户的进度估算，不是请求披露所用的组合安全上限；
        # 分页和调和在运行时增加调用，由 record_generation_step 扩大总数。
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
