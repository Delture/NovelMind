"""两个明确的任务链；受理时冻结目的地与授权，重启不会自动重新发送正文。"""
from dataclasses import dataclass
from typing import Callable
from fastapi import BackgroundTasks, Depends
from starlette.concurrency import run_in_threadpool
from backend.app.core.config import Settings, get_settings
from backend.app.models.workspace import Workflow
from backend.app.models.analysis import AnalysisJob
from backend.app.models.rag import RagQuestion
from backend.app.services import summaries, reports, rag
from backend.app.services.tasks import get_llm_provider_factory, get_sync_llm_provider_factory


@dataclass(frozen=True)
class WorkflowExecution:
    """仅驻留内存的已批准执行环境；配置含密钥，绝不写入任务记录或响应。"""
    settings: Settings
    summary_factory: Callable
    report_factory: Callable
    embedding_factory: Callable
    generation_factory: Callable
    vector_store_factory: Callable


def get_workflow_execution(
    settings: Settings = Depends(get_settings),
    summary_factory: Callable = Depends(get_llm_provider_factory),
    report_factory: Callable = Depends(get_sync_llm_provider_factory),
    embedding_factory: Callable = Depends(rag.get_embedding_factory),
    generation_factory: Callable = Depends(rag.get_generation_factory),
    vector_store_factory: Callable = Depends(rag.get_vector_store_factory),
) -> WorkflowExecution:
    """依赖仅创建绑定同一 Settings 的惰性工厂，校验授权前不创建外部客户端。"""
    return WorkflowExecution(settings, summary_factory, report_factory,
                             embedding_factory, generation_factory, vector_store_factory)


async def run_workflow(workflow_id, factory, execution, external, rights):
    """原子领取并顺序执行任务；等待每个 worker 完成后才检查下一步。

    普通服务与 HTTP 路由共享缓存、授权、冲突校验。数据库会话在派发之后释放，
    整条链保留受理时的配置和双授权。失败保留已有结果，重复 worker 无法再次领取。
    """
    with factory() as db:
        claimed = db.query(Workflow).filter(Workflow.id == workflow_id, Workflow.status == "queued").update({"status": "running"})
        db.commit()
        if not claimed:
            return
        row = db.get(Workflow, workflow_id)
        novel_id, kind, question = row.novel_id, row.kind, row.question
    consent = dict(confirm_external_processing=external, confirm_content_rights=rights)
    if kind == "report":
        steps = [
            ("补齐摘要", summaries.start_chapter_summaries,
             dict(request=summaries.StartSummariesRequest(**consent), provider_factory=execution.summary_factory), AnalysisJob),
            ("更新报告", reports.start_report,
             dict(request=reports.StartReportRequest(**consent), settings=execution.settings, provider_factory=execution.report_factory), AnalysisJob),
        ]
    else:
        shared = dict(settings=execution.settings, embedding_factory=execution.embedding_factory,
                      vector_store_factory=execution.vector_store_factory)
        steps = [
            ("准备原文", rag.start_rag_index,
             dict(**shared, request=rag.StartRagIndexRequest(**consent)), AnalysisJob),
            ("回答问题", rag.start_rag_question,
             dict(**shared, generation_factory=execution.generation_factory,
                  request=rag.StartRagQuestionRequest(question=question,
                  confirm_embedding_processing=external, confirm_generation_processing=external,
                  confirm_content_rights=rights)), RagQuestion),
        ]
    try:
        for phase, start, arguments, model in steps:
            background = BackgroundTasks()
            # Session creation, use and closing all happen in the same thread.
            def dispatch():
                """每一步独立事务，服务函数负责提交任务与补偿派发失败。"""
                with factory() as db:
                    row = db.get(Workflow, workflow_id)
                    if row is None or row.status != "running":
                        raise ValueError("任务链已停止。")
                    row.phase = phase
                    db.commit()
                    return start(novel_id=novel_id, db=db, background_tasks=background,
                                 session_factory=factory, **arguments)
            child = await run_in_threadpool(dispatch)
            await background()
            with factory() as db:
                saved = db.get(model, child.id)
                if saved is None or saved.status != "succeeded":
                    raise ValueError("前一步未成功完成；请检查子任务状态后重试。")
        with factory() as db:
            row = db.get(Workflow, workflow_id)
            if row and row.status == "running":
                row.status, row.phase = "succeeded", "已完成"
                db.commit()
    except Exception:
        # 供应商异常可能含请求头、正文或密钥，任务链不持久化异常原文。
        with factory() as db:
            row = db.get(Workflow, workflow_id)
            if row:
                row.status = "failed"
                row.error_message = "任务链未完成，请检查配置及子任务状态后重试。已完成结果已保留。"
                db.commit()
