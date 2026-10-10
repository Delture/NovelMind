from backend.app.api.service_route import ServiceRoute
from backend.app.services.tasks import (
    AnalysisJobResponse,
    ProviderFactory,
    get_session_factory,
    get_llm_provider_factory,
)
from typing import Callable, Optional
from fastapi import APIRouter, BackgroundTasks, Depends, Path as PathParameter, Query, status
from sqlalchemy.orm import Session
from backend.app.core.config import Settings, get_settings
from backend.app.core.database import get_db
from backend.app.services.execution import JobTasks
from backend.app.services import summaries as service
from backend.app.services.summaries import (
    StartSummariesRequest,
    ChapterSummaryResponse,
    ChapterSummaryListResponse,
    SummaryEstimateResponse,
    AnalysisJobListResponse,
)
router = APIRouter(route_class=ServiceRoute)

@router.post(
    "/novels/{novel_id}/summaries",
    response_model=AnalysisJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_chapter_summaries(
    request: StartSummariesRequest,
    background_tasks: BackgroundTasks,
    novel_id: int = PathParameter(..., ge=1),
    db: Session = Depends(get_db),
    provider_factory: ProviderFactory = Depends(get_llm_provider_factory),
    session_factory: Callable[[], Session] = Depends(get_session_factory),
) -> AnalysisJobResponse:
    return service.start_chapter_summaries(request=request, background_tasks=JobTasks(background_tasks), novel_id=novel_id, db=db, provider_factory=provider_factory, session_factory=session_factory)


@router.post(
    "/novels/{novel_id}/chapters/{chapter_index}/summary",
    response_model=AnalysisJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_single_chapter_summary(
    request: StartSummariesRequest,
    background_tasks: BackgroundTasks,
    novel_id: int = PathParameter(..., ge=1),
    chapter_index: int = PathParameter(..., ge=1),
    db: Session = Depends(get_db),
    provider_factory: ProviderFactory = Depends(get_llm_provider_factory),
    session_factory: Callable[[], Session] = Depends(get_session_factory),
) -> AnalysisJobResponse:
    return service.start_single_chapter_summary(request=request, background_tasks=JobTasks(background_tasks), novel_id=novel_id, chapter_index=chapter_index, db=db, provider_factory=provider_factory, session_factory=session_factory)


@router.get(
    "/novels/{novel_id}/summaries",
    response_model=ChapterSummaryListResponse,
)
def list_chapter_summaries(
    novel_id: int = PathParameter(..., ge=1),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> ChapterSummaryListResponse:
    return service.list_chapter_summaries(novel_id=novel_id, offset=offset, limit=limit, db=db)


@router.get(
    "/novels/{novel_id}/chapters/{chapter_index}/summary",
    response_model=ChapterSummaryResponse,
)
def get_chapter_summary(
    novel_id: int = PathParameter(..., ge=1),
    chapter_index: int = PathParameter(..., ge=1),
    db: Session = Depends(get_db),
) -> ChapterSummaryResponse:
    return service.get_chapter_summary(novel_id=novel_id, chapter_index=chapter_index, db=db)


@router.get(
    "/novels/{novel_id}/summaries/estimate",
    response_model=SummaryEstimateResponse,
)
def get_summary_estimate(
    novel_id: int = PathParameter(..., ge=1),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> SummaryEstimateResponse:
    return service.get_summary_estimate(novel_id=novel_id, settings=settings, db=db)


@router.get(
    "/novels/{novel_id}/analysis-jobs",
    response_model=AnalysisJobListResponse,
)
def list_analysis_jobs(
    novel_id: int = PathParameter(..., ge=1),
    job_type: Optional[str] = Query(
        None,
        min_length=1,
        max_length=40,
    ),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> AnalysisJobListResponse:
    return service.list_analysis_jobs(novel_id=novel_id, job_type=job_type, limit=limit, db=db)


@router.get(
    "/analysis-jobs/{job_id}",
    response_model=AnalysisJobResponse,
)
def get_analysis_job(
    job_id: str,
    db: Session = Depends(get_db),
) -> AnalysisJobResponse:
    return service.get_analysis_job(job_id=job_id, db=db)
