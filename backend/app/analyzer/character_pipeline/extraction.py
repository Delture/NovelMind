"""人物分析 extraction 阶段；保留有界抽取、确定性统计与失败原子性。"""
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
