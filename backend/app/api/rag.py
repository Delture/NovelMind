"""RAG index, consent, question, and evidence-history endpoints."""

import logging
import math
from collections.abc import Callable
from datetime import datetime
from typing import Optional
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
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
)
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.analyzer.deepseek import (
    MAX_JSON_GENERATION_ATTEMPTS,
    DeepSeekProvider,
)
from backend.app.analyzer.jobs import (
    mark_analysis_job_failed_with_sqlite_lock_retry,
)
from backend.app.analyzer.llm import LLMProvider
from backend.app.core.config import Settings, get_settings
from backend.app.core.database import SessionLocal, get_db
from backend.app.core.time import utc_now
from backend.app.models.analysis import AnalysisJob
from backend.app.models.novel import Chapter, Novel
from backend.app.models.rag import RagChunkRecord, RagIndex, RagQuestion
from backend.app.rag.answering import (
    RAG_ANSWER_PROMPT_VERSION,
    RagEvidence,
    run_rag_answer_job,
)
from backend.app.rag.chunking import (
    CHUNK_FORMAT_VERSION,
    build_rag_chunks,
    rag_source_hash,
)
from backend.app.rag.embedding import (
    BailianEmbeddingProvider,
    EmbeddingProvider,
    MAX_EMBEDDING_BATCH_SIZE,
)
from backend.app.rag.indexer import (
    VectorStore,
    run_rag_index_job,
)
from backend.app.rag.vector_store import LocalVectorStore


router = APIRouter(tags=["rag"])
logger = logging.getLogger(__name__)

EmbeddingFactory = Callable[[], EmbeddingProvider]
LLMFactory = Callable[[], LLMProvider]
VectorStoreFactory = Callable[[], VectorStore]

BAILIAN_TERMS_URL = (
    "https://help.aliyun.com/en/model-studio/bailian-service-notes"
)
BAILIAN_PRIVACY_URL = (
    "https://help.aliyun.com/en/model-studio/privacy-notice"
)
DEEPSEEK_TERMS_URL = (
    "https://cdn.deepseek.com/policies/en-US/"
    "deepseek-open-platform-terms-of-service.html"
)
DEEPSEEK_PRIVACY_URL = "https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html"


class StartRagIndexRequest(BaseModel):
    """Consent and refresh controls for sending source chunks to Bailian."""

    model_config = ConfigDict(extra="forbid")

    confirm_external_processing: StrictBool
    confirm_content_rights: StrictBool
    force: StrictBool = False
    confirm_force_refresh: StrictBool = False


class StartRagQuestionRequest(BaseModel):
    """Question plus explicit consent for both external providers."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2_000)
    top_k: int = Field(default=6, ge=3, le=10)
    confirm_embedding_processing: StrictBool
    confirm_generation_processing: StrictBool
    confirm_content_rights: StrictBool

    @field_validator("question")
    @classmethod
    def question_cannot_be_whitespace(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("question cannot be blank")
        return stripped


class RagJobResponse(BaseModel):
    """Persistent RAG indexing job progress."""

    id: str
    novel_id: int
    status: str
    force: bool
    progress_current: int
    progress_total: int
    error_message: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]


class RagEstimateResponse(BaseModel):
    """Bounded external-work estimate shown before consent."""

    novel_id: int
    chapter_count: int
    character_count: int
    chunk_count: int
    reusable_chunk_count: int
    estimated_new_embedding_count: int
    maximum_index_embedding_attempts: int
    maximum_question_embedding_attempts: int
    maximum_answer_generation_attempts: int
    index_current: bool
    embedding_configured: bool
    generation_configured: bool
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    embedding_region: str
    generation_provider: str
    generation_model: str
    sends_chapter_titles_and_original_chunks_to_bailian: bool
    sends_question_to_bailian: bool
    sends_question_and_retrieved_chunks_to_deepseek: bool
    data_handling_notice: str
    bailian_terms_url: str
    bailian_privacy_url: str
    deepseek_terms_url: str
    deepseek_privacy_url: str


class RagIndexStatusResponse(BaseModel):
    """Current index readiness and non-secret provenance."""

    novel_id: int
    ready: bool
    current: bool
    chunk_count: int
    source_hash: Optional[str]
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    chunk_version: str
    updated_at: Optional[datetime]


class RagCitationResponse(BaseModel):
    """One exact source citation selected by DeepSeek."""

    chunk_id: str
    chapter_index: int
    chapter_title: str
    start_char: int
    end_char: int
    excerpt: str


class RagQuestionResponse(BaseModel):
    """One persisted question, answer state, and selected citations."""

    id: str
    novel_id: int
    question: str
    top_k: int
    status: str
    answer: Optional[str]
    insufficient_evidence: Optional[bool]
    citations: list[RagCitationResponse]
    retrieved_evidence_count: int
    generation_model: str
    error_message: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]


class RagQuestionListResponse(BaseModel):
    items: list[RagQuestionResponse]
    total: int
    offset: int
    limit: int


def get_session_factory() -> Callable[[], Session]:
    return SessionLocal


def get_embedding_factory(
    settings: Settings = Depends(get_settings),
) -> EmbeddingFactory:
    """Defer embedding-client construction until consent succeeds."""
    return lambda: BailianEmbeddingProvider(settings)


def get_generation_factory(
    settings: Settings = Depends(get_settings),
) -> LLMFactory:
    """Defer DeepSeek-client construction until consent succeeds."""
    return lambda: DeepSeekProvider(settings)


def get_vector_store_factory(
    settings: Settings = Depends(get_settings),
) -> VectorStoreFactory:
    """Defer local vector-store initialization until RAG needs it."""
    return lambda: LocalVectorStore(settings.vector_store_path)


def _get_novel(db: Session, novel_id: int) -> Novel:
    novel = db.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到指定小说。",
        )
    return novel


def _rag_job_response(job: AnalysisJob) -> RagJobResponse:
    return RagJobResponse(
        id=job.id,
        novel_id=job.novel_id,
        status=job.status,
        force=bool(job.force_refresh),
        progress_current=job.progress_current,
        progress_total=job.progress_total,
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


def _active_index_job(
    db: Session,
    novel_id: int,
) -> Optional[AnalysisJob]:
    return (
        db.query(AnalysisJob)
        .filter(
            AnalysisJob.novel_id == novel_id,
            AnalysisJob.job_type == "rag_index",
            AnalysisJob.status.in_(("queued", "running")),
        )
        .order_by(AnalysisJob.created_at.desc())
        .first()
    )


def _active_question(
    db: Session,
    novel_id: int,
) -> Optional[RagQuestion]:
    return (
        db.query(RagQuestion)
        .filter(
            RagQuestion.novel_id == novel_id,
            RagQuestion.status.in_(("queued", "running")),
        )
        .order_by(RagQuestion.created_at.desc())
        .first()
    )


def _source_state(
    db: Session,
    novel_id: int,
    settings: Settings,
):
    chapters = (
        db.query(Chapter)
        .filter(Chapter.novel_id == novel_id)
        .order_by(Chapter.chapter_index)
        .all()
    )
    chunks = build_rag_chunks(
        novel_id=novel_id,
        chapters=chapters,
        embedding_model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )
    source_hash = rag_source_hash(
        chunks,
        embedding_model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )
    return chapters, chunks, source_hash


def _build_estimate(
    db: Session,
    *,
    novel_id: int,
    force: bool,
    settings: Settings,
) -> RagEstimateResponse:
    _get_novel(db, novel_id)
    chapters, chunks, source_hash = _source_state(
        db,
        novel_id,
        settings,
    )
    current = (
        db.query(RagIndex)
        .filter(RagIndex.novel_id == novel_id)
        .one_or_none()
    )
    current_compatible = (
        current is not None
        and current.embedding_model == settings.embedding_model
        and current.embedding_dimensions == settings.embedding_dimensions
        and current.chunk_version == CHUNK_FORMAT_VERSION
    )
    reusable = 0
    if not force and current_compatible:
        new_ids = {chunk.chunk_id for chunk in chunks}
        reusable = (
            db.query(func.count(RagChunkRecord.id))
            .filter(
                RagChunkRecord.rag_index_id == current.id,
                RagChunkRecord.chunk_id.in_(new_ids),
            )
            .scalar()
            or 0
        ) if new_ids else 0
    estimated_new = len(chunks) - int(reusable)
    maximum_batches = math.ceil(
        len(chunks) / MAX_EMBEDDING_BATCH_SIZE
    )
    index_current = bool(
        not force
        and current_compatible
        and current.source_hash == source_hash
        and current.chunk_count == len(chunks)
    )
    return RagEstimateResponse(
        novel_id=novel_id,
        chapter_count=len(chapters),
        character_count=sum(
            int(chapter.character_count)
            for chapter in chapters
        ),
        chunk_count=len(chunks),
        reusable_chunk_count=int(reusable),
        estimated_new_embedding_count=estimated_new,
        maximum_index_embedding_attempts=(
            maximum_batches * settings.embedding_max_attempts
        ),
        maximum_question_embedding_attempts=(
            settings.embedding_max_attempts
        ),
        maximum_answer_generation_attempts=(
            settings.deepseek_max_attempts
            * MAX_JSON_GENERATION_ATTEMPTS
        ),
        index_current=index_current,
        embedding_configured=settings.embedding_configured,
        generation_configured=settings.deepseek_configured,
        embedding_provider="aliyun_model_studio",
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        embedding_region="cn-beijing",
        generation_provider="deepseek",
        generation_model=settings.deepseek_model,
        sends_chapter_titles_and_original_chunks_to_bailian=True,
        sends_question_to_bailian=True,
        sends_question_and_retrieved_chunks_to_deepseek=True,
        data_handling_notice=(
            "建立索引时，章节标题和原文片段会发送至阿里云百炼北京区"
            "工作空间生成向量；提问时，问题会发送至百炼，随后问题与"
            "本地检索到的原文片段会发送至 DeepSeek 生成答案。"
            "请在每次操作前核对官方条款与隐私政策。"
        ),
        bailian_terms_url=BAILIAN_TERMS_URL,
        bailian_privacy_url=BAILIAN_PRIVACY_URL,
        deepseek_terms_url=DEEPSEEK_TERMS_URL,
        deepseek_privacy_url=DEEPSEEK_PRIVACY_URL,
    )


def _close_unused(provider: object, description: str) -> None:
    close = getattr(provider, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            logger.exception("Could not close unused %s.", description)


def _mark_undispatched_index_failed(
    *,
    job_id: str,
    db: Session,
    session_factory: Callable[[], Session],
) -> None:
    db.rollback()
    mark_analysis_job_failed_with_sqlite_lock_retry(
        session_factory,
        job_id,
        "rag_index",
        "RAG 索引任务未能加入后台队列，请重试。",
        expected_status="queued",
        failure_context="background dispatch failure",
    )


def _schedule_index(
    *,
    novel_id: int,
    chunk_count: int,
    request: StartRagIndexRequest,
    background_tasks: BackgroundTasks,
    db: Session,
    embedding_provider: EmbeddingProvider,
    vector_store: VectorStore,
    session_factory: Callable[[], Session],
) -> RagJobResponse:
    transferred = False
    try:
        active = _active_index_job(db, novel_id)
        if active is not None:
            if bool(active.force_refresh) != bool(request.force):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "该小说已有不同刷新模式的 RAG 索引任务正在运行。"
                    ),
                )
            return _rag_job_response(active)

        job = AnalysisJob(
            id=str(uuid4()),
            novel_id=novel_id,
            job_type="rag_index",
            target_chapter_index=None,
            force_refresh=request.force,
            status="queued",
            progress_current=0,
            progress_total=chunk_count,
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
            active = _active_index_job(db, novel_id)
            if active is None:
                raise
            if bool(active.force_refresh) != bool(request.force):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="该小说已有不同刷新模式的 RAG 索引任务正在运行。",
                ) from error
            return _rag_job_response(active)
        db.refresh(job)
        response = _rag_job_response(job)
        try:
            background_tasks.add_task(
                run_rag_index_job,
                job_id,
                embedding_provider,
                vector_store,
                session_factory,
            )
        except Exception:
            _mark_undispatched_index_failed(
                job_id=job_id,
                db=db,
                session_factory=session_factory,
            )
            raise
        transferred = True
        return response
    finally:
        if not transferred:
            _close_unused(embedding_provider, "embedding provider")


def _question_response(row: RagQuestion) -> RagQuestionResponse:
    try:
        evidence = [
            RagEvidence.model_validate(item)
            for item in list(row.evidence or [])
        ]
    except ValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="本地 RAG 问答证据记录损坏，请删除并重新导入小说。",
        ) from error
    by_id = {item.chunk_id: item for item in evidence}
    citation_ids = list(row.citation_ids or [])
    if len(set(citation_ids)) != len(citation_ids) or not (
        set(citation_ids) <= set(by_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="本地 RAG 问答引用记录损坏，请删除并重新导入小说。",
        )
    citations = [
        RagCitationResponse(
            chunk_id=by_id[chunk_id].chunk_id,
            chapter_index=by_id[chunk_id].chapter_index,
            chapter_title=by_id[chunk_id].chapter_title,
            start_char=by_id[chunk_id].start_char,
            end_char=by_id[chunk_id].end_char,
            excerpt=by_id[chunk_id].excerpt,
        )
        for chunk_id in citation_ids
    ]
    return RagQuestionResponse(
        id=row.id,
        novel_id=row.novel_id,
        question=row.question,
        top_k=row.top_k,
        status=row.status,
        answer=row.answer,
        insufficient_evidence=(
            bool(row.insufficient_evidence)
            if row.insufficient_evidence is not None
            else None
        ),
        citations=citations,
        retrieved_evidence_count=len(evidence),
        generation_model=row.generation_model,
        error_message=row.error_message,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


@router.get(
    "/novels/{novel_id}/rag/estimate",
    response_model=RagEstimateResponse,
)
def get_rag_estimate(
    novel_id: int = PathParameter(..., ge=1),
    force: bool = Query(False),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> RagEstimateResponse:
    """Estimate local chunks and worst-case paid attempts."""
    return _build_estimate(
        db,
        novel_id=novel_id,
        force=force,
        settings=settings,
    )


@router.get(
    "/novels/{novel_id}/rag/index",
    response_model=RagIndexStatusResponse,
)
def get_rag_index_status(
    novel_id: int = PathParameter(..., ge=1),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> RagIndexStatusResponse:
    _get_novel(db, novel_id)
    _, chunks, source_hash = _source_state(db, novel_id, settings)
    current = (
        db.query(RagIndex)
        .filter(RagIndex.novel_id == novel_id)
        .one_or_none()
    )
    is_current = bool(
        current is not None
        and current.source_hash == source_hash
        and current.embedding_model == settings.embedding_model
        and current.embedding_dimensions == settings.embedding_dimensions
        and current.chunk_version == CHUNK_FORMAT_VERSION
        and current.chunk_count == len(chunks)
    )
    return RagIndexStatusResponse(
        novel_id=novel_id,
        ready=current is not None,
        current=is_current,
        chunk_count=current.chunk_count if current else 0,
        source_hash=current.source_hash if current else None,
        embedding_provider=(
            current.embedding_provider
            if current
            else "aliyun_model_studio"
        ),
        embedding_model=(
            current.embedding_model
            if current
            else settings.embedding_model
        ),
        embedding_dimensions=(
            current.embedding_dimensions
            if current
            else settings.embedding_dimensions
        ),
        chunk_version=(
            current.chunk_version
            if current
            else CHUNK_FORMAT_VERSION
        ),
        updated_at=current.updated_at if current else None,
    )


@router.post(
    "/novels/{novel_id}/rag/index",
    response_model=RagJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_rag_index(
    request: StartRagIndexRequest,
    background_tasks: BackgroundTasks,
    novel_id: int = PathParameter(..., ge=1),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    embedding_factory: EmbeddingFactory = Depends(get_embedding_factory),
    vector_store_factory: VectorStoreFactory = Depends(
        get_vector_store_factory
    ),
    session_factory: Callable[[], Session] = Depends(get_session_factory),
) -> RagJobResponse:
    """Start external embedding only after every required confirmation."""
    _get_novel(db, novel_id)
    if not request.confirm_external_processing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="必须明确同意将章节标题和原文片段发送至阿里云百炼。",
        )
    if not request.confirm_content_rights:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="必须确认你有权将该小说内容交给外部服务处理。",
        )
    if request.force and not request.confirm_force_refresh:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="强制重建前必须再次确认会重新发送全部原文片段。",
        )
    active = _active_index_job(db, novel_id)
    if active is not None:
        if bool(active.force_refresh) != bool(request.force):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该小说已有不同刷新模式的 RAG 索引任务正在运行。",
            )
        return _rag_job_response(active)
    if not settings.embedding_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "尚未配置阿里云百炼北京区 embedding API，请设置 "
                "EMBEDDING_API_KEY 和工作空间 EMBEDDING_BASE_URL。"
            ),
        )
    estimate = _build_estimate(
        db,
        novel_id=novel_id,
        force=request.force,
        settings=settings,
    )
    if estimate.chunk_count == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="小说没有可建立 RAG 索引的正文。",
        )
    embedding_provider = embedding_factory()
    try:
        vector_store = vector_store_factory()
    except Exception as error:
        _close_unused(embedding_provider, "embedding provider")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "本地向量库初始化失败，请检查 VECTOR_STORE_PATH。"
            ),
        ) from error
    return _schedule_index(
        novel_id=novel_id,
        chunk_count=estimate.chunk_count,
        request=request,
        background_tasks=background_tasks,
        db=db,
        embedding_provider=embedding_provider,
        vector_store=vector_store,
        session_factory=session_factory,
    )


@router.get(
    "/novels/{novel_id}/rag/index/jobs",
    response_model=list[RagJobResponse],
)
def list_rag_index_jobs(
    novel_id: int = PathParameter(..., ge=1),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
) -> list[RagJobResponse]:
    _get_novel(db, novel_id)
    jobs = (
        db.query(AnalysisJob)
        .filter(
            AnalysisJob.novel_id == novel_id,
            AnalysisJob.job_type == "rag_index",
        )
        .order_by(
            AnalysisJob.created_at.desc(),
            AnalysisJob.id.desc(),
        )
        .limit(limit)
        .all()
    )
    return [_rag_job_response(job) for job in jobs]


@router.post(
    "/novels/{novel_id}/rag/questions",
    response_model=RagQuestionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_rag_question(
    request: StartRagQuestionRequest,
    background_tasks: BackgroundTasks,
    novel_id: int = PathParameter(..., ge=1),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    embedding_factory: EmbeddingFactory = Depends(get_embedding_factory),
    generation_factory: LLMFactory = Depends(get_generation_factory),
    vector_store_factory: VectorStoreFactory = Depends(
        get_vector_store_factory
    ),
    session_factory: Callable[[], Session] = Depends(get_session_factory),
) -> RagQuestionResponse:
    """Create a recoverable RAG answer task after three confirmations."""
    _get_novel(db, novel_id)
    if not request.confirm_embedding_processing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="必须明确同意将问题发送至阿里云百炼生成查询向量。",
        )
    if not request.confirm_generation_processing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="必须明确同意将问题和检索片段发送至 DeepSeek。",
        )
    if not request.confirm_content_rights:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="必须确认你有权将相关小说内容交给外部服务处理。",
        )
    active = _active_question(db, novel_id)
    if active is not None:
        if (
            active.question != request.question
            or active.top_k != request.top_k
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该小说已有另一个 RAG 问答任务正在运行。",
            )
        return _question_response(active)
    if not settings.embedding_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="尚未配置阿里云百炼北京区 embedding API。",
        )
    if not settings.deepseek_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="尚未配置 DeepSeek API 密钥。",
        )
    _, chunks, expected_hash = _source_state(db, novel_id, settings)
    index = (
        db.query(RagIndex)
        .filter(
            RagIndex.novel_id == novel_id,
            RagIndex.source_hash == expected_hash,
            RagIndex.embedding_model == settings.embedding_model,
            RagIndex.embedding_dimensions == settings.embedding_dimensions,
            RagIndex.chunk_version == CHUNK_FORMAT_VERSION,
            RagIndex.chunk_count == len(chunks),
        )
        .one_or_none()
    )
    if index is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="RAG 索引尚未建立或已过期，请先完成索引。",
        )

    embedding_provider = embedding_factory()
    try:
        llm_provider = generation_factory()
    except Exception:
        _close_unused(embedding_provider, "embedding provider")
        raise
    try:
        vector_store = vector_store_factory()
    except Exception as error:
        _close_unused(embedding_provider, "embedding provider")
        _close_unused(llm_provider, "DeepSeek provider")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "本地向量库初始化失败，请检查 VECTOR_STORE_PATH。"
            ),
        ) from error

    transferred = False
    try:
        row = RagQuestion(
            id=str(uuid4()),
            novel_id=novel_id,
            index_source_hash=index.source_hash,
            question=request.question,
            top_k=request.top_k,
            status="queued",
            citation_ids=[],
            evidence=[],
            generation_model=settings.deepseek_model,
            prompt_version=RAG_ANSWER_PROMPT_VERSION,
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError as error:
            db.rollback()
            active = _active_question(db, novel_id)
            if active is None:
                raise
            if (
                active.question != request.question
                or active.top_k != request.top_k
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="该小说已有另一个 RAG 问答任务正在运行。",
                ) from error
            return _question_response(active)
        db.refresh(row)
        response = _question_response(row)
        try:
            background_tasks.add_task(
                run_rag_answer_job,
                row.id,
                embedding_provider,
                llm_provider,
                vector_store,
                session_factory,
            )
        except Exception:
            db.rollback()
            with session_factory() as failure_db:
                queued = failure_db.get(RagQuestion, row.id)
                if queued is not None and queued.status == "queued":
                    queued.status = "failed"
                    queued.error_message = (
                        "RAG 问答任务未能加入后台队列，请重试。"
                    )
                    queued.completed_at = utc_now()
                    failure_db.commit()
            raise
        transferred = True
        return response
    finally:
        if not transferred:
            _close_unused(embedding_provider, "embedding provider")
            _close_unused(llm_provider, "DeepSeek provider")


@router.get(
    "/novels/{novel_id}/rag/questions",
    response_model=RagQuestionListResponse,
)
def list_rag_questions(
    novel_id: int = PathParameter(..., ge=1),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> RagQuestionListResponse:
    _get_novel(db, novel_id)
    query = db.query(RagQuestion).filter(
        RagQuestion.novel_id == novel_id
    )
    total = query.count()
    rows = (
        query.order_by(
            RagQuestion.created_at.desc(),
            RagQuestion.id.desc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )
    return RagQuestionListResponse(
        items=[_question_response(row) for row in rows],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/novels/{novel_id}/rag/questions/{question_id}",
    response_model=RagQuestionResponse,
)
def get_rag_question(
    novel_id: int = PathParameter(..., ge=1),
    question_id: str = PathParameter(
        ...,
        min_length=36,
        max_length=36,
    ),
    db: Session = Depends(get_db),
) -> RagQuestionResponse:
    _get_novel(db, novel_id)
    row = (
        db.query(RagQuestion)
        .filter(
            RagQuestion.id == question_id,
            RagQuestion.novel_id == novel_id,
        )
        .one_or_none()
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到指定 RAG 问答任务。",
        )
    return _question_response(row)
