"""两个明确的任务链；受理时冻结目的地与授权，重启不会自动重新发送正文。"""

import asyncio
import time
from backend.app.core.sqlite_retry import retry_sqlite
from backend.app.services.execution import JobTasks, executor
from starlette.concurrency import run_in_threadpool
from backend.app.models.workspace import Workflow
from backend.app.core.time import utc_now
from backend.app.models.analysis import AnalysisJob
from backend.app.models.rag import RagQuestion
from backend.app.services import summaries, reports, rag


WORKFLOW_STEP_TIMEOUT = 3600


def _update_workflow(factory, workflow_id, values, states=("queued", "running")):
    def update():
        with factory() as db:
            count = (
                db.query(Workflow)
                .filter(Workflow.id == workflow_id, Workflow.status.in_(states))
                .update(values)
            )
            db.commit()
            return count

    return retry_sqlite(update)


async def _wait_for_child(factory, model, child_id, workflow_id):
    def status():
        with factory() as db:
            row = db.get(model, child_id)
            parent = db.get(Workflow, workflow_id)
            if parent is None or parent.status != "running":
                raise ValueError("任务链已停止。")
            return row.status if row else "failed"

    deadline = time.monotonic() + WORKFLOW_STEP_TIMEOUT
    while True:
        state = await run_in_threadpool(status)
        if state not in {"queued", "running"}:
            if state != "succeeded":
                raise ValueError("前一步未成功完成。")
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("等待子任务超时。")
        await asyncio.sleep(0.2)


def _cancel_late_child(factory, workflow_id, model, child_id):
    """取消可能在派发事务间到达；新创建的子任务不能在父任务停止后开始。"""
    with factory() as db:
        parent = db.get(Workflow, workflow_id)
        if parent is not None and parent.status == "running":
            return
        db.query(model).filter(
            model.id == child_id, model.status.in_(("queued", "running"))
        ).update(
            {
                "status": "failed",
                "error_message": "所属任务链已停止，已完成结果已保留。",
                "completed_at": utc_now(),
            }
        )
        db.commit()
    executor.cancel(child_id)


async def run_workflow(workflow_id, factory, execution, external, rights):
    """领取、派发和终态落盘共享同一异常边界；迟到异常不覆盖终态。"""
    claimed = False
    try:
        claimed = await run_in_threadpool(
            _update_workflow, factory, workflow_id, {"status": "running"}, ("queued",)
        )
        if not claimed:
            return
        with factory() as db:
            row = db.get(Workflow, workflow_id)
            novel_id, kind, question = row.novel_id, row.kind, row.question
        consent = dict(
            confirm_external_processing=external, confirm_content_rights=rights
        )
        if kind == "report":
            steps = [
                (
                    "补齐摘要",
                    summaries.start_chapter_summaries,
                    dict(
                        request=summaries.StartSummariesRequest(**consent),
                        provider_factory=execution.summary_factory,
                    ),
                    AnalysisJob,
                ),
                (
                    "更新报告",
                    reports.start_report,
                    dict(
                        request=reports.StartReportRequest(**consent),
                        settings=execution.settings,
                        provider_factory=execution.report_factory,
                    ),
                    AnalysisJob,
                ),
            ]
        else:
            shared = dict(
                settings=execution.settings,
                embedding_factory=execution.embedding_factory,
                vector_store_factory=execution.vector_store_factory,
            )
            steps = [
                (
                    "准备原文",
                    rag.start_rag_index,
                    dict(**shared, request=rag.StartRagIndexRequest(**consent)),
                    AnalysisJob,
                ),
                (
                    "回答问题",
                    rag.start_rag_question,
                    dict(
                        **shared,
                        generation_factory=execution.generation_factory,
                        request=rag.StartRagQuestionRequest(
                            question=question,
                            confirm_embedding_processing=external,
                            confirm_generation_processing=external,
                            confirm_content_rights=rights,
                        ),
                    ),
                    RagQuestion,
                ),
            ]
        for phase, start, arguments, model in steps:
            background = JobTasks(parent_id=workflow_id)

            def dispatch(
                phase=phase, start=start, arguments=arguments, background=background
            ):
                with factory() as db:
                    row = db.get(Workflow, workflow_id)
                    if row is None or row.status != "running":
                        raise ValueError("任务链已停止。")
                    row.phase = phase
                    db.commit()
                    return start(
                        novel_id=novel_id,
                        db=db,
                        background_tasks=background,
                        session_factory=factory,
                        **arguments,
                    )

            child = await run_in_threadpool(dispatch)
            if background.handles:
                await run_in_threadpool(
                    _cancel_late_child, factory, workflow_id, model, child.id
                )
            await background()
            await _wait_for_child(factory, model, child.id, workflow_id)
        await run_in_threadpool(
            _update_workflow,
            factory,
            workflow_id,
            {"status": "succeeded", "phase": "已完成"},
            ("running",),
        )
    except (Exception, asyncio.CancelledError):
        await run_in_threadpool(
            _update_workflow,
            factory,
            workflow_id,
            {
                "status": "failed",
                "error_message": "任务链未完成，请检查配置及子任务状态后重试。已完成结果已保留。",
            },
            ("running",) if claimed else ("queued",),
        )


from backend.app.services.dependencies import (
    WorkflowExecution as WorkflowExecution,
    get_workflow_execution as get_workflow_execution,
)
