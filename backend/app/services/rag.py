"""两条路径分别披露外发数据并校验授权。
索引可用性不仅检查 SQLite 指针，还要验证向量集合真实存在且数量匹配。
"""

from backend.app.services.execution import TaskDispatcher
from backend.app.services.errors import ServiceError
from backend.app.services.tasks import (
    fail_dispatch,
)

import logging
import math
from collections.abc import Callable
from datetime import datetime
from typing import Optional
from uuid import uuid4

from starlette import status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.analyzer.deepseek import MAX_JSON_GENERATION_ATTEMPTS
from backend.app.analyzer.llm import LLMProvider
from backend.app.core.config import (
    EMBEDDING_PROVIDERS,
    GENERATION_PROVIDERS,
    Settings,
)
from backend.app.models.analysis import AnalysisJob
from backend.app.models.novel import Chapter, Novel
from backend.app.models.rag import RagBuildCheckpoint, RagChunkRecord, RagIndex, RagQuestion
from backend.app.rag.answering import (
    RAG_ANSWER_PROMPT_VERSION,
    RagEvidence,
    mark_rag_question_failed_with_sqlite_lock_retry,
    run_rag_answer_job,
)
from backend.app.rag.chunking import CHUNK_FORMAT_VERSION, build_rag_chunks, rag_source_hash
from backend.app.rag.embedding import (
    EmbeddingProvider,
    MAX_EMBEDDING_BATCH_SIZE,
)
from backend.app.rag.indexer import (
    VectorStore,
    load_valid_vectors_by_ids,
    run_rag_index_job,
)


logger = logging.getLogger(__name__)

EmbeddingFactory = Callable[[], EmbeddingProvider]
LLMFactory = Callable[[], LLMProvider]
VectorStoreFactory = Callable[[], VectorStore]

SILICONFLOW_TERMS_URL = (
    "https://docs.siliconflow.cn/cn/legals/terms-of-service"
)
SILICONFLOW_PRIVACY_URL = (
    "https://docs.siliconflow.cn/cn/legals/privacy-policy"
)
DEEPSEEK_TERMS_URL = (
    "https://cdn.deepseek.com/policies/en-US/"
    "deepseek-open-platform-terms-of-service.html"
)
DEEPSEEK_PRIVACY_URL = "https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html"


# Index and ask use different consent/force fields, so they keep separate request models.
class StartRagIndexRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_external_processing: StrictBool
    confirm_content_rights: StrictBool
    force: StrictBool = False
    confirm_force_refresh: StrictBool = False


class StartRagQuestionRequest(BaseModel):
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
    sends_chapter_titles_and_original_chunks_to_embedding_provider: bool
    sends_question_to_embedding_provider: bool
    sends_question_and_retrieved_chunks_to_generation_provider: bool
    data_handling_notice: str
    embedding_terms_url: str
    embedding_privacy_url: str
    generation_terms_url: str
    generation_privacy_url: str


class RagIndexStatusResponse(BaseModel):
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
    chunk_id: str
    chapter_index: int
    chapter_title: str
    start_char: int
    end_char: int
    excerpt: str


class RagQuestionResponse(BaseModel):
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










def _get_novel(db: Session, novel_id: int) -> Novel:
    novel = db.get(Novel, novel_id)
    if novel is None:
        raise ServiceError(
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
    """阻止同小说并行问答争用资源。"""
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
    """估算、状态和启动接口共享本函数，避免三条路径采用不同的数据范围。"""
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


# 数据库元数据只是一份声明；磁盘集合的存在与实际向量数才是可用性的证据。
def _vector_collection_is_complete(
    current: Optional[RagIndex],
    vector_store_factory: object,
) -> bool:
    """Treat persisted RAG metadata as ready only when vectors still exist."""
    if current is None:
        return False
    # Direct unit-level calls may omit FastAPI dependency resolution. The
    # production route always receives a callable factory.
    if not callable(vector_store_factory):
        return True
    try:
        vector_store = vector_store_factory()
        return (
            vector_store.collection_count(current.collection_name)
            == current.chunk_count
        )
    except Exception:
        logger.warning(
            "RAG vector collection %s is missing or unreadable.",
            current.collection_name,
        )
        return False


def _build_estimate(
    db: Session,
    *,
    novel_id: int,
    force: bool,
    settings: Settings,
    vector_store_factory: Optional[VectorStoreFactory] = None,
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
        and current.embedding_provider == settings.embedding_provider
        and current.embedding_model == settings.embedding_model
        and current.embedding_dimensions == settings.embedding_dimensions
        and current.chunk_version == CHUNK_FORMAT_VERSION
    )
    vector_collection_valid = _vector_collection_is_complete(
        current,
        vector_store_factory,
    )
    new_ids = {chunk.chunk_id for chunk in chunks}
    reusable_ids: set[str] = set()
    if not force and new_ids:
        if current is not None and current_compatible and vector_collection_valid:
            reusable_ids.update(
                chunk_id
                for (chunk_id,) in db.query(RagChunkRecord.chunk_id).filter(
                    RagChunkRecord.rag_index_id == current.id,
                    RagChunkRecord.chunk_id.in_(new_ids),
                )
            )
        checkpoint = (
            db.query(RagBuildCheckpoint)
            .filter(RagBuildCheckpoint.novel_id == novel_id)
            .one_or_none()
        )
        checkpoint_compatible = (
            checkpoint is not None
            and checkpoint.embedding_provider == settings.embedding_provider
            and checkpoint.embedding_model == settings.embedding_model
            and checkpoint.embedding_dimensions == settings.embedding_dimensions
            and checkpoint.chunk_version == CHUNK_FORMAT_VERSION
            and checkpoint.source_hash == source_hash
        )
        if (
            checkpoint is not None
            and checkpoint_compatible
            and callable(vector_store_factory)
        ):
            checkpoint_name = checkpoint.collection_name
            try:
                store = vector_store_factory()
                reusable_ids.update(
                    load_valid_vectors_by_ids(
                        vector_store=store,
                        collection_name=checkpoint_name,
                        chunk_ids=list(new_ids),
                        dimensions=settings.embedding_dimensions,
                    )
                )
            except Exception:
                logger.warning(
                    "RAG build checkpoint collection %s is unreadable.",
                    checkpoint_name,
                )
    reusable = len(reusable_ids)
    estimated_new = len(chunks) - reusable
    maximum_batches = math.ceil(
        len(chunks) / MAX_EMBEDDING_BATCH_SIZE
    )
    embedding_protocol_attempts = (
        2 if settings.embedding_provider == "openai_compatible" else 1
    )
    index_current = bool(
        not force
        and current_compatible
        and current is not None
        and current.source_hash == source_hash
        and current.chunk_count == len(chunks)
        and vector_collection_valid
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
            maximum_batches
            * settings.embedding_max_attempts
            * embedding_protocol_attempts
        ),
        maximum_question_embedding_attempts=(
            settings.embedding_max_attempts * embedding_protocol_attempts
        ),
        maximum_answer_generation_attempts=(
            settings.deepseek_max_attempts
            * MAX_JSON_GENERATION_ATTEMPTS
        ),
        index_current=index_current,
        embedding_configured=settings.embedding_configured,
        generation_configured=settings.deepseek_configured,
        embedding_provider=settings.embedding_provider,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        embedding_region=(
            "global"
            if settings.embedding_provider == "siliconflow"
            else "custom"
        ),
        generation_provider=settings.generation_provider,
        generation_model=settings.deepseek_model,
        sends_chapter_titles_and_original_chunks_to_embedding_provider=True,
        sends_question_to_embedding_provider=True,
        sends_question_and_retrieved_chunks_to_generation_provider=True,
        data_handling_notice=(
            "建立索引和提问时，原文片段或问题会发送至 "
            f"{EMBEDDING_PROVIDERS[settings.embedding_provider]}；"
            "生成答案时，问题与检索片段会发送至 "
            f"{GENERATION_PROVIDERS[settings.generation_provider]}。"
            "请在操作前核对所选供应商的条款。"
        ),
        embedding_terms_url=(
            SILICONFLOW_TERMS_URL
            if settings.embedding_provider == "siliconflow"
            else (settings.embedding_base_url or "")
        ),
        embedding_privacy_url=(
            SILICONFLOW_PRIVACY_URL
            if settings.embedding_provider == "siliconflow"
            else (settings.embedding_base_url or "")
        ),
        generation_terms_url=(
            DEEPSEEK_TERMS_URL
            if settings.generation_provider == "deepseek"
            else settings.deepseek_base_url
        ),
        generation_privacy_url=(
            DEEPSEEK_PRIVACY_URL
            if settings.generation_provider == "deepseek"
            else settings.deepseek_base_url
        ),
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
) -> bool:
    return fail_dispatch(db=db, session_factory=session_factory, job_id=job_id, job_type="rag_index", message="索引任务未能加入后台队列，请重试。")


def _schedule_index(
    *,
    novel_id: int,
    chunk_count: int,
    request: StartRagIndexRequest,
    background_tasks: TaskDispatcher,
    db: Session,
    embedding_provider: EmbeddingProvider,
    vector_store: VectorStore,
    session_factory: Callable[[], Session],
) -> RagJobResponse:
    """成功后 worker 负责资源；移交前失败则本函数关闭资源并补偿任务。"""
    transferred = False
    try:
        active = _active_index_job(db, novel_id)
        if active is not None:
            if bool(active.force_refresh) != bool(request.force):
                raise ServiceError(
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
                raise ServiceError(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="未找到指定小说。",
                ) from error
            active = _active_index_job(db, novel_id)
            if active is None:
                raise
            if bool(active.force_refresh) != bool(request.force):
                raise ServiceError(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="该小说已有不同刷新模式的 RAG 索引任务正在运行。",
                ) from error
            return _rag_job_response(active)
        try:
            db.refresh(job)
            response = _rag_job_response(job)
        except Exception:
            if not _mark_undispatched_index_failed(
                job_id=job_id,
                db=db,
                session_factory=session_factory,
            ):
                logger.error(
                    "Committed RAG index job %s remained queued after "
                    "response preparation failed.",
                    job_id,
                )
            raise
        try:
            background_tasks.add_task(
                run_rag_index_job,
                job_id,
                embedding_provider,
                vector_store,
                session_factory,
            )
        except Exception:
            if not _mark_undispatched_index_failed(
                job_id=job_id,
                db=db,
                session_factory=session_factory,
            ):
                logger.error(
                    "RAG index job %s remained queued after dispatch "
                    "failed.",
                    job_id,
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
        raise ServiceError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="本地 RAG 问答证据记录损坏，请删除并重新导入小说。",
        ) from error
    by_id = {item.chunk_id: item for item in evidence}
    citation_ids = list(row.citation_ids or [])
    if len(set(citation_ids)) != len(citation_ids) or not (
        set(citation_ids) <= set(by_id)
    ):
        raise ServiceError(
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


def get_rag_estimate(novel_id: int, force: bool, db: Session, settings: Settings, vector_store_factory: VectorStoreFactory) -> RagEstimateResponse:
    return _build_estimate(
        db,
        novel_id=novel_id,
        force=force,
        settings=settings,
        vector_store_factory=vector_store_factory,
    )


def get_rag_index_status(novel_id: int, db: Session, settings: Settings, vector_store_factory: VectorStoreFactory) -> RagIndexStatusResponse:
    _get_novel(db, novel_id)
    _, chunks, source_hash = _source_state(db, novel_id, settings)
    current = (
        db.query(RagIndex)
        .filter(RagIndex.novel_id == novel_id)
        .one_or_none()
    )
    vector_collection_valid = _vector_collection_is_complete(
        current,
        vector_store_factory,
    )
    is_current = bool(
        current is not None
        and current is not None
        and current.source_hash == source_hash
        and current.embedding_provider == settings.embedding_provider
        and current.embedding_model == settings.embedding_model
        and current.embedding_dimensions == settings.embedding_dimensions
        and current.chunk_version == CHUNK_FORMAT_VERSION
        and current.chunk_count == len(chunks)
        and vector_collection_valid
    )
    return RagIndexStatusResponse(
        novel_id=novel_id,
        ready=current is not None and vector_collection_valid,
        current=is_current,
        chunk_count=current.chunk_count if current else 0,
        source_hash=current.source_hash if current else None,
        embedding_provider=(
            current.embedding_provider
            if current
            else settings.embedding_provider
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


def start_rag_index(request: StartRagIndexRequest, background_tasks: TaskDispatcher, novel_id: int, db: Session, settings: Settings, embedding_factory: EmbeddingFactory, vector_store_factory: VectorStoreFactory, session_factory: Callable[[], Session]) -> RagJobResponse:
    """Start external embedding only after every required confirmation."""
    _get_novel(db, novel_id)
    if not request.confirm_external_processing:
        embedding_label = EMBEDDING_PROVIDERS[settings.embedding_provider]
        raise ServiceError(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "必须明确同意将章节标题和原文片段发送至"
                f"{embedding_label}。"
            ),
        )
    if not request.confirm_content_rights:
        raise ServiceError(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="必须确认你有权将该小说内容交给外部服务处理。",
        )
    if request.force and not request.confirm_force_refresh:
        raise ServiceError(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="强制重建前必须再次确认会重新发送全部原文片段。",
        )
    from backend.app.services.chapter_review import ensure_no_active_chapter_review
    ensure_no_active_chapter_review(db, novel_id)
    active = _active_index_job(db, novel_id)
    if active is not None:
        if bool(active.force_refresh) != bool(request.force):
            raise ServiceError(
                status_code=status.HTTP_409_CONFLICT,
                detail="该小说已有不同刷新模式的 RAG 索引任务正在运行。",
            )
        return _rag_job_response(active)
    if not settings.embedding_configured:
        embedding_label = EMBEDDING_PROVIDERS[settings.embedding_provider]
        raise ServiceError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"尚未配置 {embedding_label} embedding API，请设置 "
                "EMBEDDING_API_KEY 和工作空间 EMBEDDING_BASE_URL。"
            ),
        )
    estimate = _build_estimate(
        db,
        novel_id=novel_id,
        force=request.force,
        settings=settings,
        vector_store_factory=vector_store_factory,
    )
    if estimate.chunk_count == 0:
        raise ServiceError(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="小说没有可建立 RAG 索引的正文。",
        )
    embedding_provider = embedding_factory()
    try:
        vector_store = vector_store_factory()
    except Exception as error:
        _close_unused(embedding_provider, "embedding provider")
        raise ServiceError(
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


def list_rag_index_jobs(novel_id: int, limit: int, db: Session) -> list[RagJobResponse]:
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


def start_rag_question(request: StartRagQuestionRequest, background_tasks: TaskDispatcher, novel_id: int, db: Session, settings: Settings, embedding_factory: EmbeddingFactory, generation_factory: LLMFactory, vector_store_factory: VectorStoreFactory, session_factory: Callable[[], Session]) -> RagQuestionResponse:
    """Create a recoverable RAG answer task after three confirmations."""
    _get_novel(db, novel_id)
    if not request.confirm_embedding_processing:
        embedding_label = EMBEDDING_PROVIDERS[settings.embedding_provider]
        raise ServiceError(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"必须明确同意将问题发送至 {embedding_label}"
                "生成查询向量。"
            ),
        )
    if not request.confirm_generation_processing:
        generation_label = GENERATION_PROVIDERS[settings.generation_provider]
        raise ServiceError(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "必须明确同意将问题和检索片段发送至"
                f"{generation_label}。"
            ),
        )
    if not request.confirm_content_rights:
        raise ServiceError(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="必须确认你有权将相关小说内容交给外部服务处理。",
        )
    active = _active_question(db, novel_id)
    if active is not None:
        if (
            active.question != request.question
            or active.top_k != request.top_k
        ):
            raise ServiceError(
                status_code=status.HTTP_409_CONFLICT,
                detail="该小说已有另一个 RAG 问答任务正在运行。",
            )
        return _question_response(active)
    if not settings.embedding_configured:
        embedding_label = EMBEDDING_PROVIDERS[settings.embedding_provider]
        raise ServiceError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"尚未配置 {embedding_label} embedding API。",
        )
    if not settings.deepseek_configured:
        generation_label = GENERATION_PROVIDERS[settings.generation_provider]
        raise ServiceError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"尚未配置 {generation_label} API 密钥。",
        )
    _, chunks, expected_hash = _source_state(db, novel_id, settings)
    index = (
        db.query(RagIndex)
        .filter(
            RagIndex.novel_id == novel_id,
            RagIndex.source_hash == expected_hash,
            RagIndex.embedding_provider == settings.embedding_provider,
            RagIndex.embedding_model == settings.embedding_model,
            RagIndex.embedding_dimensions == settings.embedding_dimensions,
            RagIndex.chunk_version == CHUNK_FORMAT_VERSION,
            RagIndex.chunk_count == len(chunks),
        )
        .one_or_none()
    )
    if index is None:
        raise ServiceError(
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
        _close_unused(llm_provider, "generation provider")
        raise ServiceError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "本地向量库初始化失败，请检查 VECTOR_STORE_PATH。"
            ),
        ) from error
    try:
        vector_count = vector_store.collection_count(index.collection_name)
    except Exception:
        vector_count = -1
    if isinstance(vector_count, int) and vector_count != index.chunk_count:
        _close_unused(embedding_provider, "embedding provider")
        _close_unused(llm_provider, "generation provider")
        raise ServiceError(
            status_code=status.HTTP_409_CONFLICT,
            detail="本地向量索引缺失或不完整，请先重建 RAG 索引。",
        )

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
                raise ServiceError(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="该小说已有另一个 RAG 问答任务正在运行。",
                ) from error
            return _question_response(active)
        try:
            db.refresh(row)
            response = _question_response(row)
        except Exception:
            db.rollback()
            if not mark_rag_question_failed_with_sqlite_lock_retry(
                session_factory,
                question_id=row.id,
                expected_status="queued",
                error_message=(
                    "RAG 问答任务未能准备后台响应，请重试。"
                ),
            ):
                logger.error(
                    "Committed RAG question %s remained queued after "
                    "response preparation failed.",
                    row.id,
                )
            raise
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
            if not mark_rag_question_failed_with_sqlite_lock_retry(
                session_factory,
                question_id=row.id,
                expected_status="queued",
                error_message=(
                    "RAG 问答任务未能加入后台队列，请重试。"
                ),
            ):
                logger.error(
                    "RAG question %s remained queued after dispatch "
                    "failed.",
                    row.id,
                )
            raise
        transferred = True
        return response
    finally:
        if not transferred:
            _close_unused(embedding_provider, "embedding provider")
            _close_unused(llm_provider, "generation provider")


def list_rag_questions(novel_id: int, offset: int, limit: int, db: Session) -> RagQuestionListResponse:
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


def get_rag_question(novel_id: int, question_id: str, db: Session) -> RagQuestionResponse:
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
        raise ServiceError(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到指定 RAG 问答任务。",
        )
    return _question_response(row)

from backend.app.services.dependencies import get_session_factory as get_session_factory, get_embedding_factory as get_embedding_factory, get_generation_factory as get_generation_factory, get_vector_store_factory as get_vector_store_factory
