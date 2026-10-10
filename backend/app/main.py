
from contextlib import asynccontextmanager
from typing import AsyncIterator

from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.app.api.characters import router as characters_router
from backend.app.api.novels import router as novels_router
from backend.app.api.chapter_review import router as chapter_review_router
from backend.app.api.rag import router as rag_router
from backend.app.api.reports import router as reports_router
from backend.app.api.summaries import router as summaries_router
from backend.app.api.system import router as system_router
from backend.app.api.workspace import router as workspace_router, fail_interrupted_workflows
from backend.app.core.local_web import LocalOriginMiddleware
from backend.app.analyzer.summaries import fail_interrupted_jobs
from backend.app.rag.answering import fail_interrupted_rag_questions
from backend.app.rag.maintenance import cleanup_orphan_rag_collections
from backend.app.core.config import settings
from backend.app.core.database import SessionLocal, init_db
from backend.app.services.execution import executor


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """后台任务由当前进程执行，退出后不会续跑；重启时必须把遗留的 queued/running 记录标为失败，
    避免界面永远处理中。孤立向量集合也在这里清理。
    """
    # 建表与迁移必须先完成，后面的恢复查询才能安全访问最新表结构。
    executor.start()
    init_db()
    fail_interrupted_jobs(SessionLocal)
    fail_interrupted_rag_questions(SessionLocal)
    fail_interrupted_workflows(SessionLocal)
    cleanup_orphan_rag_collections(
        SessionLocal,
        settings.vector_store_path,
    )
    try:
        yield
    finally:
        await executor.shutdown()


app = FastAPI(title=settings.app_name, lifespan=lifespan)


app.add_middleware(LocalOriginMiddleware)
app.include_router(characters_router, prefix="/api")
app.include_router(novels_router, prefix="/api")
app.include_router(chapter_review_router, prefix="/api")
app.include_router(rag_router, prefix="/api")
app.include_router(reports_router, prefix="/api")
app.include_router(summaries_router, prefix="/api")
app.include_router(system_router, prefix="/api")
app.include_router(workspace_router, prefix="/api")


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok", "project": "NovelMind"}


web_files = StaticFiles(directory=Path(__file__).resolve().parents[2] / "frontend" / "web", html=True)


@app.get("/{path:path}", include_in_schema=False)
async def web_asset(path: str, request: Request):
    return await web_files.get_response(path or "index.html", request.scope)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, error: RequestValidationError):
    """只返回字段位置和错误类别，防止密码框原始输入进入验证响应。"""
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": list(item["loc"]), "type": item["type"], "msg": "请求字段无效。"}
        for item in error.errors()
    ]})
