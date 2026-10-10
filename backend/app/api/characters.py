
from backend.app.api.service_route import ServiceRoute
from typing import Callable, Literal, Optional
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Path as PathParameter,
    Query,
    status,
)
from sqlalchemy.orm import Session
from backend.app.core.config import Settings, get_settings
from backend.app.core.database import get_db
from backend.app.services.tasks import (
    AnalysisJobResponse,
    ProviderFactory,
    get_sync_llm_provider_factory,
    get_session_factory,
)
from backend.app.services.execution import JobTasks
from backend.app.services import characters as service
from backend.app.services.characters import (
    StartCharacterAnalysisRequest,
    CharacterEstimateResponse,
    CharacterListResponse,
    CharacterDetailResponse,
    CharacterAppearanceListResponse,
    CharacterRelationshipListResponse,
)

router = APIRouter(route_class=ServiceRoute, tags=["characters"])


@router.get(
    "/novels/{novel_id}/characters/estimate", response_model=CharacterEstimateResponse
)
def get_character_estimate(
    novel_id: int = PathParameter(..., ge=1),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> CharacterEstimateResponse:
    return service.get_character_estimate(novel_id=novel_id, settings=settings, db=db)


@router.post(
    "/novels/{novel_id}/characters",
    response_model=AnalysisJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_character_analysis(
    request: StartCharacterAnalysisRequest,
    background_tasks: BackgroundTasks,
    novel_id: int = PathParameter(..., ge=1),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    provider_factory: ProviderFactory = Depends(get_sync_llm_provider_factory),
    session_factory: Callable[[], Session] = Depends(get_session_factory),
) -> AnalysisJobResponse:
    return service.start_character_analysis(
        request=request,
        background_tasks=JobTasks(background_tasks),
        novel_id=novel_id,
        db=db,
        settings=settings,
        provider_factory=provider_factory,
        session_factory=session_factory,
    )


@router.get("/novels/{novel_id}/characters", response_model=CharacterListResponse)
def list_characters(
    novel_id: int = PathParameter(..., ge=1),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None, max_length=100),
    sort: Literal["mention_count", "chapter_count", "first_chapter"] = Query(
        "mention_count"
    ),
    db: Session = Depends(get_db),
) -> CharacterListResponse:
    return service.list_characters(
        novel_id=novel_id, offset=offset, limit=limit, search=search, sort=sort, db=db
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
    return service.get_character(novel_id=novel_id, character_id=character_id, db=db)


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
    return service.list_character_appearances(
        novel_id=novel_id, character_id=character_id, offset=offset, limit=limit, db=db
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
    return service.list_character_relationships(
        novel_id=novel_id, character_id=character_id, offset=offset, limit=limit, db=db
    )
