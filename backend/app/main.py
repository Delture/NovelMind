"""NovelMind 后端的总入口。

初学者可以把本文件理解成后端的“总装配车间”：各业务模块已经分别写在
``api/``、``analyzer/``、``rag/`` 等目录中，这里不实现具体业务，只负责：

1. 在进程启动时准备数据库并修复上次异常退出留下的运行中状态；
2. 创建唯一的 :class:`FastAPI` 应用对象；
3. 把六组路由统一挂到 ``/api`` 下；
4. 暴露一个极简健康检查，供启动器判断服务是否真正就绪。

这种“入口只做组装”的结构很重要。随着项目变大，如果把上传、数据库查询、
模型调用全部堆进入口文件，任何修改都会互相影响，测试也很难隔离。
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from backend.app.api.characters import router as characters_router
from backend.app.api.novels import router as novels_router
from backend.app.api.rag import router as rag_router
from backend.app.api.reports import router as reports_router
from backend.app.api.summaries import router as summaries_router
from backend.app.api.system import router as system_router
from backend.app.analyzer.summaries import fail_interrupted_jobs
from backend.app.rag.answering import fail_interrupted_rag_questions
from backend.app.rag.maintenance import cleanup_orphan_rag_collections
from backend.app.core.config import settings
from backend.app.core.database import SessionLocal, init_db


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """管理 FastAPI 进程的生命周期，并在接收请求前完成恢复工作。

    ``yield`` 之前是启动阶段，之后本可放关闭阶段逻辑。这里的后台任务由当前
    Python 进程执行，不是 Redis/Celery 一类持久任务队列；进程退出后任务不会
    自动续跑。因此重启时必须把遗留的 ``queued/running`` 记录标为失败，避免
    界面永远显示“处理中”。孤立向量集合也在这里清理。
    """
    # 建表与迁移必须先完成，后面的恢复查询才能安全访问最新表结构。
    init_db()
    # 每项恢复都使用新的数据库会话；这里只传会话工厂，不长期持有会话。
    fail_interrupted_jobs(SessionLocal)
    fail_interrupted_rag_questions(SessionLocal)
    cleanup_orphan_rag_collections(
        SessionLocal,
        settings.vector_store_path,
    )
    yield


# ``app`` 是 ``uvicorn backend.app.main:app`` 中冒号右侧所指的对象。
app = FastAPI(title=settings.app_name, lifespan=lifespan)
# 路由模块只声明各自的相对路径；总入口统一添加 ``/api``，可以避免每个模块
# 重复约定公共前缀，也使健康检查继续保留在更简短的 ``/health``。
app.include_router(characters_router, prefix="/api")
app.include_router(novels_router, prefix="/api")
app.include_router(rag_router, prefix="/api")
app.include_router(reports_router, prefix="/api")
app.include_router(summaries_router, prefix="/api")
app.include_router(system_router, prefix="/api")


@app.get("/health")
def health_check() -> dict[str, str]:
    """返回不依赖业务数据的存活信号，供启动器和人工排障使用。"""
    return {"status": "ok", "project": "NovelMind"}
