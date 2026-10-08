"""HTTP 声明；任务链直接调用 services 中同一业务入口。"""
from backend.app.api.service_route import ServiceRoute
from backend.app.services.tasks import (
    AnalysisJobResponse,
    ProviderFactory,
    get_session_factory,
    get_sync_llm_provider_factory,
)
from typing import Callable
from fastapi import APIRouter, BackgroundTasks, Depends, Path as PathParameter, status
from sqlalchemy.orm import Session
from backend.app.core.config import Settings, get_settings
from backend.app.core.database import get_db
from backend.app.services.execution import JobTasks
from backend.app.services import reports as service
from backend.app.services.reports import StartReportRequest, ReportEstimateResponse, ReportResponse
router = APIRouter(route_class=ServiceRoute)

@router.get(
    "/novels/{novel_id}/report/estimate",
    response_model=ReportEstimateResponse,
)
def get_report_estimate(
    novel_id: int = PathParameter(..., ge=1),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> ReportEstimateResponse:
    """委托共享业务服务处理校验、缓存和派发。"""
    return service.get_report_estimate(novel_id=novel_id, settings=settings, db=db)


@router.post(
    "/novels/{novel_id}/report",
    response_model=AnalysisJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
# 路由只做授权、来源准备和任务创建；昂贵生成在后台执行。
def start_report(
    request: StartReportRequest,
    background_tasks: BackgroundTasks,
    novel_id: int = PathParameter(..., ge=1),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
    provider_factory: ProviderFactory = Depends(
        get_sync_llm_provider_factory
    ),
    session_factory: Callable[[], Session] = Depends(get_session_factory),
) -> AnalysisJobResponse:
    """委托共享业务服务处理校验、缓存和派发。"""
    return service.start_report(request=request, background_tasks=JobTasks(background_tasks), novel_id=novel_id, settings=settings, db=db, provider_factory=provider_factory, session_factory=session_factory)


@router.get(
    "/novels/{novel_id}/report",
    response_model=ReportResponse,
)
def get_report(
    novel_id: int = PathParameter(..., ge=1),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> ReportResponse:
    """委托共享业务服务处理校验、缓存和派发。"""
    return service.get_report(novel_id=novel_id, settings=settings, db=db)
