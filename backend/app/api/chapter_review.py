from typing import Callable

from fastapi import APIRouter, BackgroundTasks, Depends, Path as PathParameter, status
from sqlalchemy.orm import Session

from backend.app.api.service_route import ServiceRoute
from backend.app.core.config import Settings, get_settings
from backend.app.core.database import get_db
from backend.app.services import chapter_review as service
from backend.app.services.chapter_review import (
    ChapterReviewEstimateResponse,
    StartChapterReviewRequest,
)
from backend.app.services.execution import JobTasks
from backend.app.services.tasks import (
    AnalysisJobResponse,
    ProviderFactory,
    get_llm_provider_factory,
    get_session_factory,
)

router = APIRouter(route_class=ServiceRoute)


@router.get(
    "/novels/{novel_id}/chapters/review/estimate",
    response_model=ChapterReviewEstimateResponse,
)
def get_chapter_review_estimate(
    novel_id: int = PathParameter(..., ge=1),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> ChapterReviewEstimateResponse:
    return service.get_chapter_review_estimate(novel_id=novel_id, settings=settings, db=db)


@router.post(
    "/novels/{novel_id}/chapters/review",
    response_model=AnalysisJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_chapter_review(
    request: StartChapterReviewRequest,
    background_tasks: BackgroundTasks,
    novel_id: int = PathParameter(..., ge=1),
    db: Session = Depends(get_db),
    provider_factory: ProviderFactory = Depends(get_llm_provider_factory),
    session_factory: Callable[[], Session] = Depends(get_session_factory),
    settings: Settings = Depends(get_settings),
) -> AnalysisJobResponse:
    return service.start_chapter_review(
        request=request,
        background_tasks=JobTasks(background_tasks),
        novel_id=novel_id,
        db=db,
        provider_factory=provider_factory,
        session_factory=session_factory,
        settings=settings,
    )
