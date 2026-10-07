"""HTTP 声明；任务链直接调用 services 中同一业务入口。"""
from backend.app.services.tasks import get_session_factory
from collections.abc import Callable
from fastapi import APIRouter, BackgroundTasks, Depends, Path as PathParameter, Query, status
from sqlalchemy.orm import Session
from backend.app.core.config import Settings, get_settings
from backend.app.core.database import get_db
from backend.app.services import rag as service
from backend.app.services.rag import (
    EmbeddingFactory,
    LLMFactory,
    VectorStoreFactory,
    StartRagIndexRequest,
    StartRagQuestionRequest,
    RagJobResponse,
    RagEstimateResponse,
    RagIndexStatusResponse,
    RagQuestionResponse,
    RagQuestionListResponse,
    get_embedding_factory,
    get_generation_factory,
    get_vector_store_factory,
)
router = APIRouter()

@router.get(
    "/novels/{novel_id}/rag/estimate",
    response_model=RagEstimateResponse,
)
def get_rag_estimate(
    novel_id: int = PathParameter(..., ge=1),
    force: bool = Query(False),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    vector_store_factory: VectorStoreFactory = Depends(
        get_vector_store_factory
    ),
) -> RagEstimateResponse:
    """委托共享业务服务处理校验、缓存和派发。"""
    return service.get_rag_estimate(novel_id=novel_id, force=force, db=db, settings=settings, vector_store_factory=vector_store_factory)


@router.get(
    "/novels/{novel_id}/rag/index",
    response_model=RagIndexStatusResponse,
)
def get_rag_index_status(
    novel_id: int = PathParameter(..., ge=1),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    vector_store_factory: VectorStoreFactory = Depends(
        get_vector_store_factory
    ),
) -> RagIndexStatusResponse:
    """委托共享业务服务处理校验、缓存和派发。"""
    return service.get_rag_index_status(novel_id=novel_id, db=db, settings=settings, vector_store_factory=vector_store_factory)


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
    """委托共享业务服务处理校验、缓存和派发。"""
    return service.start_rag_index(request=request, background_tasks=background_tasks, novel_id=novel_id, db=db, settings=settings, embedding_factory=embedding_factory, vector_store_factory=vector_store_factory, session_factory=session_factory)


@router.get(
    "/novels/{novel_id}/rag/index/jobs",
    response_model=list[RagJobResponse],
)
def list_rag_index_jobs(
    novel_id: int = PathParameter(..., ge=1),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
) -> list[RagJobResponse]:
    """委托共享业务服务处理校验、缓存和派发。"""
    return service.list_rag_index_jobs(novel_id=novel_id, limit=limit, db=db)


@router.post(
    "/novels/{novel_id}/rag/questions",
    response_model=RagQuestionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
# 提问也持久化为任务，以便刷新页面或短暂断线后继续读取状态与最终证据。
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
    """委托共享业务服务处理校验、缓存和派发。"""
    return service.start_rag_question(request=request, background_tasks=background_tasks, novel_id=novel_id, db=db, settings=settings, embedding_factory=embedding_factory, generation_factory=generation_factory, vector_store_factory=vector_store_factory, session_factory=session_factory)


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
    """委托共享业务服务处理校验、缓存和派发。"""
    return service.list_rag_questions(novel_id=novel_id, offset=offset, limit=limit, db=db)


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
    """委托共享业务服务处理校验、缓存和派发。"""
    return service.get_rag_question(novel_id=novel_id, question_id=question_id, db=db)
