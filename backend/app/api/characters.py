from backend.app.services.tasks import _close_provider, fail_dispatch
"""人物分析估算/启动，以及当前人物快照的本地查询接口。

写路径会调用外部模型，必须确认数据范围和内容权利；读路径只查询本地 SQLite。
把昂贵生成与便宜读取分开，既便于测试，也避免每次打开人物页都产生 API 费用。
"""

import logging
from datetime import datetime
from typing import Callable, Literal, Optional
from uuid import uuid4

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Path as PathParameter,
    Query,
    status,
)
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError
from sqlalchemy import exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.analyzer.characters import (
    CHARACTER_EXTRACTION_PROMPT_VERSION,
    CHARACTER_MERGE_PROMPT_VERSION,
    CHARACTER_RELATIONSHIP_PROMPT_VERSION,
    StoredChapterExtractionData,
    character_analysis_source_hash,
    character_chapter_source_hash,
    estimate_character_extraction_steps,
    maximum_character_generation_steps,
    run_character_job,
)
from backend.app.analyzer.deepseek import MAX_JSON_GENERATION_ATTEMPTS
from backend.app.analyzer.llm import LLMProvider
from backend.app.services.tasks import (
    AnalysisJobResponse,
    ProviderFactory,
    _job_response,
    get_sync_llm_provider_factory,
    get_session_factory,
)
from backend.app.core.config import GENERATION_PROVIDERS, Settings, get_settings
from backend.app.core.database import get_db
from backend.app.models.analysis import AnalysisJob
from backend.app.models.character import (
    ChapterCharacterExtraction,
    Character,
    CharacterAnalysis,
    CharacterAppearance,
    CharacterRelationship,
)
from backend.app.models.novel import Chapter, Novel


router = APIRouter(tags=["characters"])
logger = logging.getLogger(__name__)

DEEPSEEK_TERMS_URL = (
    "https://cdn.deepseek.com/policies/en-US/"
    "deepseek-open-platform-terms-of-service.html"
)
DEEPSEEK_PRIVACY_URL = (
    "https://cdn.deepseek.com/policies/en-US/"
    "deepseek-privacy-policy.html"
)


# Pydantic 负责无数据库参与的类型检查；跨表归属和活动任务冲突仍由业务层验证。
class StartCharacterAnalysisRequest(BaseModel):
    """Explicit consent and refresh options for whole-novel analysis."""

    model_config = ConfigDict(extra="forbid")

    confirm_external_processing: StrictBool
    confirm_content_rights: StrictBool
    force: StrictBool = False


class CharacterEstimateResponse(BaseModel):
    """External work and disclosure shown before generation starts."""

    novel_id: int
    chapter_count: int
    character_count: int
    cached_chapter_count: int
    pending_chapter_count: int
    estimated_extraction_steps: int
    estimated_merge_steps: int
    estimated_relationship_steps: int
    estimated_generation_steps: int
    maximum_request_attempts: int
    estimated_force_generation_steps: int
    maximum_force_request_attempts: int
    provider: str
    model: str
    sends_original_text: bool
    sends_chapter_titles: bool
    sends_derived_character_data: bool
    data_handling_notice: str
    terms_url: str
    privacy_url: str
    up_to_date: bool


class CharacterListItemResponse(BaseModel):
    """One canonical character with deterministic local statistics."""

    id: int
    canonical_name: str
    aliases: list[str]
    short_description: str
    first_chapter_index: int
    last_chapter_index: int
    chapter_count: int
    mention_count: int
    relationship_count: int


class CharacterListResponse(BaseModel):
    """Paginated characters and snapshot-level totals."""

    items: list[CharacterListItemResponse]
    total: int
    offset: int
    limit: int
    analysis_updated_at: Optional[datetime]
    analyzed_chapter_count: int
    character_count: int
    relationship_count: int


class CharacterDetailResponse(CharacterListItemResponse):
    """One character plus snapshot provenance safe for display."""

    model: str
    updated_at: datetime


class CharacterAppearanceResponse(BaseModel):
    """One character's aggregate appearance in a chapter."""

    id: int
    chapter_index: int
    chapter_title: str
    mention_count: int
    summary: str


class CharacterAppearanceListResponse(BaseModel):
    """Paginated appearance records."""

    items: list[CharacterAppearanceResponse]
    total: int
    offset: int
    limit: int


class CharacterRelationshipResponse(BaseModel):
    """One model-inferred relationship using local character IDs."""

    id: int
    source_character_id: int
    source_character_name: str
    target_character_id: int
    target_character_name: str
    directed: bool
    relation_type: str
    description: str
    evidence_chapter_indices: list[int]


class CharacterRelationshipListResponse(BaseModel):
    """Paginated relationships involving one character."""

    items: list[CharacterRelationshipResponse]
    total: int
    offset: int
    limit: int


def _active_character_job(
    db: Session,
    novel_id: int,
) -> Optional[AnalysisJob]:
    """查询该小说 queued/running 的人物任务；没有时返回 ``None``。"""
    return (
        db.query(AnalysisJob)
        .filter(
            AnalysisJob.novel_id == novel_id,
            AnalysisJob.job_type == "characters",
            AnalysisJob.status.in_(("queued", "running")),
        )
        .order_by(AnalysisJob.created_at.desc())
        .first()
    )


def _mark_committed_character_job_failed(
    *,
    job_id: str,
    db: Session,
    session_factory: Callable[[], Session],
) -> bool:
    """通过共享补偿结束未派发任务。"""
    return fail_dispatch(db=db, session_factory=session_factory, job_id=job_id, job_type="characters", message="人物分析任务未能加入后台队列，请重试。")


# 保证“已经持久化”与“确实交给执行器”之间的窗口可被补偿，而非永久排队。
def _schedule_character_job(
    *,
    novel_id: int,
    progress_total: int,
    request: StartCharacterAnalysisRequest,
    background_tasks: BackgroundTasks,
    db: Session,
    provider: LLMProvider,
    session_factory: Callable[[], Session],
) -> AnalysisJobResponse:
    """持久化并移交人物任务，返回供前端轮询的任务响应。

    ``progress_total`` 来自本地估算，``request`` 带授权/force 选择，provider 在
    成功移交后归 worker 所有；移交前异常会关闭它并补偿任务状态。
    """
    provider_transferred = False
    try:
        active_job = _active_character_job(db, novel_id)
        if active_job is not None:
            if bool(active_job.force_refresh) != bool(request.force):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "该小说已有不同刷新模式的人物分析任务正在运行，"
                        "请等待其完成后重试。"
                    ),
                )
            return _job_response(active_job)

        job = AnalysisJob(
            id=str(uuid4()),
            novel_id=novel_id,
            job_type="characters",
            target_chapter_index=None,
            force_refresh=request.force,
            status="queued",
            progress_current=0,
            progress_total=progress_total,
        )
        job_id = job.id
        db.add(job)
        try:
            db.commit()
        except IntegrityError as error:
            db.rollback()
            if db.get(Novel, novel_id) is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="未找到指定小说。",
                ) from error
            if (
                "UNIQUE constraint failed: "
                "analysis_jobs.novel_id, analysis_jobs.job_type"
                not in str(error.orig)
            ):
                raise
            active_job = _active_character_job(db, novel_id)
            if active_job is None:
                raise
            if bool(active_job.force_refresh) != bool(request.force):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "该小说已有不同刷新模式的人物分析任务正在运行，"
                        "请等待其完成后重试。"
                    ),
                )
            return _job_response(active_job)
        except Exception:
            db.rollback()
            raise

        try:
            db.refresh(job)
            response = _job_response(job)
        except Exception:
            if not _mark_committed_character_job_failed(
                job_id=job_id,
                db=db,
                session_factory=session_factory,
            ):
                logger.error(
                    "Committed character job %s remained queued after "
                    "response preparation failed.",
                    job_id,
                )
            raise

        try:
            background_tasks.add_task(
                run_character_job,
                job_id,
                provider,
                session_factory,
            )
        except Exception:
            if not _mark_committed_character_job_failed(
                job_id=job_id,
                db=db,
                session_factory=session_factory,
            ):
                logger.error(
                    "Committed character job %s remained queued after "
                    "background dispatch failed.",
                    job_id,
                )
            raise
        provider_transferred = True
        return response
    finally:
        if not provider_transferred:
            _close_provider(provider)


def _build_character_estimate(
    novel: Novel,
    *,
    db: Session,
    settings: Settings,
) -> CharacterEstimateResponse:
    """生成授权页所需的人物分析本地估算。

    ``novel`` 确定范围，``db`` 读取章节与逐章缓存，``settings`` 提供当前模型和
    提示限制。返回章节数、字符数、可复用章数、预计步骤及安全上限；函数不构造
    provider，因而不会发送正文或产生 API 费用。
    """
    rows = (
        db.query(Chapter, ChapterCharacterExtraction)
        .outerjoin(
            ChapterCharacterExtraction,
            ChapterCharacterExtraction.chapter_id == Chapter.id,
        )
        .filter(Chapter.novel_id == novel.id)
        .order_by(Chapter.chapter_index)
        .all()
    )
    chapter_count = len(rows)
    text_character_count = 0
    cached_chapter_count = 0
    cached_observation_count = 0
    extraction_steps = 0
    force_extraction_steps = 0
    current_extractions: list[dict[str, object]] = []

    for chapter, extraction in rows:
        text_character_count += int(chapter.character_count)
        expected_hash = character_chapter_source_hash(
            chapter.title,
            chapter.content,
        )
        chapter_extraction_steps = estimate_character_extraction_steps(
            chapter.content
        )
        force_extraction_steps += chapter_extraction_steps
        has_current_metadata = (
            extraction is not None
            and extraction.source_hash == expected_hash
            and extraction.model == settings.deepseek_model
            and extraction.prompt_version
            == CHARACTER_EXTRACTION_PROMPT_VERSION
        )
        validated_data: Optional[StoredChapterExtractionData] = None
        if has_current_metadata:
            try:
                validated_data = (
                    StoredChapterExtractionData.model_validate(
                        extraction.extraction_data
                    )
                )
            except ValidationError:
                logger.warning(
                    "Ignoring invalid cached character extraction for "
                    "chapter %s while estimating work.",
                    extraction.chapter_id,
                )
        if validated_data is not None:
            cached_chapter_count += 1
            cached_observation_count += len(
                validated_data.observations
            )
            current_extractions.append(
                {
                    "chapter_index": int(chapter.chapter_index),
                    "source_hash": extraction.source_hash,
                    "extraction_data": validated_data.model_dump(
                        mode="json"
                    ),
                }
            )
        else:
            extraction_steps += chapter_extraction_steps

    analysis = (
        db.query(CharacterAnalysis)
        .filter(CharacterAnalysis.novel_id == novel.id)
        .one_or_none()
    )
    all_extractions_current = cached_chapter_count == chapter_count
    expected_analysis_hash = (
        character_analysis_source_hash(current_extractions)
        if all_extractions_current
        else None
    )
    up_to_date = bool(
        analysis is not None
        and expected_analysis_hash is not None
        and analysis.source_hash == expected_analysis_hash
        and analysis.model == settings.deepseek_model
        and analysis.extraction_prompt_version
        == CHARACTER_EXTRACTION_PROMPT_VERSION
        and analysis.merge_prompt_version
        == CHARACTER_MERGE_PROMPT_VERSION
        and analysis.relationship_prompt_version
        == CHARACTER_RELATIONSHIP_PROMPT_VERSION
        and analysis.chapter_count == chapter_count
    )
    merge_steps = 0 if up_to_date else 1
    relationship_steps = 0 if up_to_date else 1
    generation_steps = (
        extraction_steps + merge_steps + relationship_steps
    )
    force_generation_steps = force_extraction_steps + 2
    request_attempt_multiplier = (
        settings.deepseek_max_attempts
        * MAX_JSON_GENERATION_ATTEMPTS
    )
    maximum_generation_steps = maximum_character_generation_steps(
        extraction_steps,
        cached_observation_count=cached_observation_count,
        rebuild_required=not up_to_date,
    )
    maximum_force_generation_steps = (
        maximum_character_generation_steps(force_extraction_steps)
    )

    return CharacterEstimateResponse(
        novel_id=novel.id,
        chapter_count=chapter_count,
        character_count=text_character_count,
        cached_chapter_count=cached_chapter_count,
        pending_chapter_count=chapter_count - cached_chapter_count,
        estimated_extraction_steps=extraction_steps,
        estimated_merge_steps=merge_steps,
        estimated_relationship_steps=relationship_steps,
        estimated_generation_steps=generation_steps,
        maximum_request_attempts=(
            maximum_generation_steps * request_attempt_multiplier
        ),
        estimated_force_generation_steps=force_generation_steps,
        maximum_force_request_attempts=(
            maximum_force_generation_steps
            * request_attempt_multiplier
        ),
        provider=settings.generation_provider,
        model=settings.deepseek_model,
        sends_original_text=True,
        sends_chapter_titles=True,
        sends_derived_character_data=True,
        data_handling_notice=(
            (
                "章节标题、正文以及由正文生成的人物候选与档案将发送至 "
                "DeepSeek，并可能在中国处理。根据 DeepSeek 当前条款，"
                "输入可能被缓存、留存并用于服务改进或模型优化；请仅提交"
                "你有权处理的内容。"
            )
            if settings.generation_provider == "deepseek"
            else (
                "章节标题、正文以及派生人物数据将发送至 "
                f"{GENERATION_PROVIDERS[settings.generation_provider]}。"
                "请仅提交你有权处理的内容，并以所选供应商条款为准。"
            )
        ),
        terms_url=(
            DEEPSEEK_TERMS_URL
            if settings.generation_provider == "deepseek"
            else settings.deepseek_base_url
        ),
        privacy_url=(
            DEEPSEEK_PRIVACY_URL
            if settings.generation_provider == "deepseek"
            else settings.deepseek_base_url
        ),
        up_to_date=up_to_date,
    )


@router.get(
    "/novels/{novel_id}/characters/estimate",
    response_model=CharacterEstimateResponse,
)
def get_character_estimate(
    novel_id: int = PathParameter(..., ge=1),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> CharacterEstimateResponse:
    """Estimate external character-analysis work before consent."""
    novel = db.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到指定小说。",
        )
    return _build_character_estimate(
        novel,
        db=db,
        settings=settings,
    )


@router.post(
    "/novels/{novel_id}/characters",
    response_model=AnalysisJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
# 整书分析可能包含许多模型调用，本接口不能占住浏览器连接等待完成。
def start_character_analysis(
    request: StartCharacterAnalysisRequest,
    background_tasks: BackgroundTasks,
    novel_id: int = PathParameter(..., ge=1),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    provider_factory: ProviderFactory = Depends(
        get_sync_llm_provider_factory
    ),
    session_factory: Callable[[], Session] = Depends(get_session_factory),
) -> AnalysisJobResponse:
    """Start one whole-novel DeepSeek character-analysis job."""
    novel = db.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到指定小说。",
        )
    if not request.confirm_external_processing:
        provider_label = GENERATION_PROVIDERS[settings.generation_provider]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "人物分析会把小说标题、正文和派生人物数据发送至 "
                f"{provider_label} API，请先明确确认。"
            ),
        )
    if not request.confirm_content_rights:
        provider_label = GENERATION_PROVIDERS[settings.generation_provider]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"请确认你有权把该小说提交给 {provider_label} API，"
                "并已了解其数据处理条款。"
            ),
        )
    active_job = _active_character_job(db, novel_id)
    if active_job is not None:
        if bool(active_job.force_refresh) != bool(request.force):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "该小说已有不同刷新模式的人物分析任务正在运行，"
                    "请等待其完成后重试。"
                ),
            )
        return _job_response(active_job)
    active_report = (
        db.query(AnalysisJob.id)
        .filter(
            AnalysisJob.novel_id == novel_id,
            AnalysisJob.job_type == "report",
            AnalysisJob.status.in_(("queued", "running")),
        )
        .first()
    )
    if active_report is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="报告生成期间不能更新人物分析，请等待报告任务完成。",
        )
    estimate = _build_character_estimate(
        novel,
        db=db,
        settings=settings,
    )
    provider = provider_factory()
    return _schedule_character_job(
        novel_id=novel_id,
        progress_total=(
            estimate.estimated_force_generation_steps
            if request.force
            else estimate.estimated_generation_steps
        ),
        request=request,
        background_tasks=background_tasks,
        db=db,
        provider=provider,
        session_factory=session_factory,
    )


def _get_character_for_novel(
    db: Session,
    *,
    novel_id: int,
    character_id: int,
) -> tuple[CharacterAnalysis, Character]:
    """按小说和人物 ID 联表查找，返回 ``(当前快照, 人物)``；不存在抛 404。"""
    row = (
        db.query(CharacterAnalysis, Character)
        .join(Character, Character.analysis_id == CharacterAnalysis.id)
        .filter(
            CharacterAnalysis.novel_id == novel_id,
            Character.id == character_id,
        )
        .one_or_none()
    )
    if row is None:
        if db.get(Novel, novel_id) is None:
            detail = "未找到指定小说。"
        else:
            detail = "未找到指定人物。"
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=detail,
        )
    return row


def _relationship_counts(
    db: Session,
    *,
    analysis_id: int,
    character_ids: list[int],
) -> dict[int, int]:
    """批量返回 ``character_id -> 参与关系数``；无关系人物也保留值 0。"""
    counts = {character_id: 0 for character_id in character_ids}
    if not character_ids:
        return counts
    outgoing = (
        db.query(
            CharacterRelationship.source_character_id,
            func.count(CharacterRelationship.id),
        )
        .filter(
            CharacterRelationship.analysis_id == analysis_id,
            CharacterRelationship.source_character_id.in_(character_ids),
        )
        .group_by(CharacterRelationship.source_character_id)
        .all()
    )
    incoming = (
        db.query(
            CharacterRelationship.target_character_id,
            func.count(CharacterRelationship.id),
        )
        .filter(
            CharacterRelationship.analysis_id == analysis_id,
            CharacterRelationship.target_character_id.in_(character_ids),
        )
        .group_by(CharacterRelationship.target_character_id)
        .all()
    )
    for character_id, count in [*outgoing, *incoming]:
        counts[int(character_id)] += int(count)
    return counts


def _character_item(
    character: Character,
    *,
    relationship_count: int,
) -> CharacterListItemResponse:
    """把人物 ORM 行和预计算关系数映射成稳定列表响应，不暴露内部关系对象。"""
    return CharacterListItemResponse(
        id=character.id,
        canonical_name=character.canonical_name,
        aliases=list(character.aliases),
        short_description=character.short_description,
        first_chapter_index=character.first_chapter_index,
        last_chapter_index=character.last_chapter_index,
        chapter_count=character.chapter_count,
        mention_count=character.mention_count,
        relationship_count=relationship_count,
    )


@router.get(
    "/novels/{novel_id}/characters",
    response_model=CharacterListResponse,
)
def list_characters(
    novel_id: int = PathParameter(..., ge=1),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None, max_length=100),
    sort: Literal[
        "mention_count",
        "chapter_count",
        "first_chapter",
    ] = Query("mention_count"),
    db: Session = Depends(get_db),
) -> CharacterListResponse:
    """Return current characters without loading novel bodies."""
    novel = db.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到指定小说。",
        )
    analysis = (
        db.query(CharacterAnalysis)
        .filter(CharacterAnalysis.novel_id == novel_id)
        .one_or_none()
    )
    if analysis is None:
        return CharacterListResponse(
            items=[],
            total=0,
            offset=offset,
            limit=limit,
            analysis_updated_at=None,
            analyzed_chapter_count=0,
            character_count=0,
            relationship_count=0,
        )

    query = db.query(Character).filter(
        Character.analysis_id == analysis.id
    )
    normalized_search = (search or "").strip()
    if normalized_search:
        escaped = (
            normalized_search.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        aliases = func.json_each(
            Character.aliases
        ).table_valued("key", "value")
        alias_matches = exists(
            select(1)
            .select_from(aliases)
            .where(aliases.c.value.ilike(pattern, escape="\\"))
        )
        query = query.filter(
            or_(
                Character.canonical_name.ilike(pattern, escape="\\"),
                alias_matches,
            )
        )
    total = query.count()
    if sort == "chapter_count":
        ordering = (
            Character.chapter_count.desc(),
            Character.mention_count.desc(),
        )
    elif sort == "first_chapter":
        ordering = (
            Character.first_chapter_index.asc(),
            Character.mention_count.desc(),
        )
    else:
        ordering = (
            Character.mention_count.desc(),
            Character.chapter_count.desc(),
        )
    characters = (
        query.order_by(
            *ordering,
            Character.canonical_name.asc(),
            Character.id.asc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )
    counts = _relationship_counts(
        db,
        analysis_id=analysis.id,
        character_ids=[character.id for character in characters],
    )
    relationship_total = (
        db.query(func.count(CharacterRelationship.id))
        .filter(CharacterRelationship.analysis_id == analysis.id)
        .scalar()
        or 0
    )
    character_total = (
        db.query(func.count(Character.id))
        .filter(Character.analysis_id == analysis.id)
        .scalar()
        or 0
    )
    return CharacterListResponse(
        items=[
            _character_item(
                character,
                relationship_count=counts[character.id],
            )
            for character in characters
        ],
        total=total,
        offset=offset,
        limit=limit,
        analysis_updated_at=analysis.updated_at,
        analyzed_chapter_count=analysis.chapter_count,
        character_count=character_total,
        relationship_count=relationship_total,
    )


@router.get(
    "/novels/{novel_id}/characters/{character_id}",
    response_model=CharacterDetailResponse,
)
def get_character(
    novel_id: int = PathParameter(..., ge=1),
    character_id: int = PathParameter(..., ge=1),
    db: Session = Depends(get_db),
) -> CharacterDetailResponse:
    """Return one character from the current novel snapshot."""
    analysis, character = _get_character_for_novel(
        db,
        novel_id=novel_id,
        character_id=character_id,
    )
    count = _relationship_counts(
        db,
        analysis_id=analysis.id,
        character_ids=[character.id],
    )[character.id]
    item = _character_item(character, relationship_count=count)
    return CharacterDetailResponse(
        **item.model_dump(),
        model=analysis.model,
        updated_at=analysis.updated_at,
    )


@router.get(
    "/novels/{novel_id}/characters/{character_id}/appearances",
    response_model=CharacterAppearanceListResponse,
)
def list_character_appearances(
    novel_id: int = PathParameter(..., ge=1),
    character_id: int = PathParameter(..., ge=1),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> CharacterAppearanceListResponse:
    """Return chapter appearances for one current character."""
    _, character = _get_character_for_novel(
        db,
        novel_id=novel_id,
        character_id=character_id,
    )
    base_query = (
        db.query(CharacterAppearance)
        .filter(CharacterAppearance.character_id == character.id)
    )
    total = base_query.count()
    rows = (
        db.query(CharacterAppearance, Chapter)
        .join(Chapter, Chapter.id == CharacterAppearance.chapter_id)
        .filter(CharacterAppearance.character_id == character.id)
        .order_by(Chapter.chapter_index, CharacterAppearance.id)
        .offset(offset)
        .limit(limit)
        .all()
    )
    return CharacterAppearanceListResponse(
        items=[
            CharacterAppearanceResponse(
                id=appearance.id,
                chapter_index=chapter.chapter_index,
                chapter_title=chapter.title,
                mention_count=appearance.mention_count,
                summary=appearance.summary,
            )
            for appearance, chapter in rows
        ],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/novels/{novel_id}/characters/{character_id}/relationships",
    response_model=CharacterRelationshipListResponse,
)
def list_character_relationships(
    novel_id: int = PathParameter(..., ge=1),
    character_id: int = PathParameter(..., ge=1),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> CharacterRelationshipListResponse:
    """Return current relationships involving one character."""
    analysis, character = _get_character_for_novel(
        db,
        novel_id=novel_id,
        character_id=character_id,
    )
    involvement = or_(
        CharacterRelationship.source_character_id == character.id,
        CharacterRelationship.target_character_id == character.id,
    )
    base_query = db.query(CharacterRelationship).filter(
        CharacterRelationship.analysis_id == analysis.id,
        involvement,
    )
    total = base_query.count()
    rows = (
        base_query.order_by(
            CharacterRelationship.source_character_id,
            CharacterRelationship.target_character_id,
            CharacterRelationship.id,
        )
        .offset(offset)
        .limit(limit)
        .all()
    )
    character_ids = {
        value
        for relationship in rows
        for value in (
            relationship.source_character_id,
            relationship.target_character_id,
        )
    }
    names = dict(
        db.query(Character.id, Character.canonical_name)
        .filter(
            Character.analysis_id == analysis.id,
            Character.id.in_(character_ids),
        )
        .all()
    ) if character_ids else {}
    return CharacterRelationshipListResponse(
        items=[
            CharacterRelationshipResponse(
                id=relationship.id,
                source_character_id=relationship.source_character_id,
                source_character_name=names[
                    relationship.source_character_id
                ],
                target_character_id=relationship.target_character_id,
                target_character_name=names[
                    relationship.target_character_id
                ],
                directed=bool(relationship.directed),
                relation_type=relationship.relation_type,
                description=relationship.description,
                evidence_chapter_indices=list(
                    relationship.evidence_chapter_indices
                ),
            )
            for relationship in rows
        ],
        total=total,
        offset=offset,
        limit=limit,
    )
