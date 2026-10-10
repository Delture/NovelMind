
from backend.app.api.service_route import ServiceRoute
import logging
from uuid import uuid4
from typing import Literal, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, StrictBool, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert
from backend.app.services.execution import executor

from backend.app.api.novels import get_novel_or_404
from backend.app.core.database import get_db
from backend.app.services.workflows import run_workflow, WorkflowExecution, get_workflow_execution
from backend.app.core.time import utc_now
from backend.app.core.config import Settings, get_settings, privacy_scope
from backend.app.models.workspace import WorkspaceSetting, ReadingState, Workflow
from backend.app.models.analysis import AnalysisJob
from backend.app.models.rag import RagQuestion
from backend.app.models.novel import Novel

router = APIRouter(route_class=ServiceRoute, tags=["workspace"])
logger = logging.getLogger(__name__)


class Privacy(BaseModel):
    """本机授权选择；默认关闭，前端仍向业务接口传递明确确认字段。"""

    model_config = ConfigDict(extra="forbid")
    decided: StrictBool = False
    external: StrictBool = False
    rights: StrictBool = False


class Position(BaseModel):
    chapter: int = Field(default=1, ge=1, le=10000)
    paragraph: int = Field(default=-1, ge=-1, le=2000000)
    offset: int = Field(default=0, ge=0, le=20000000)


class Bookmark(Position):
    id: str = Field(max_length=80)
    snippet: str = Field(default="", max_length=100)
    label: str = Field(default="", max_length=80)
    note: str = Field(default="", max_length=500)


class Preferences(BaseModel):
    theme: Literal["paper", "sepia", "dark"] = "paper"
    font: Literal["serif", "sans", "pingfang"] = "serif"
    size: float = Field(default=18, ge=16, le=28)
    line: float = Field(default=1.9, ge=1.6, le=2.2)
    width: float = Field(default=720, ge=560, le=880)
    gap: float = Field(default=22, ge=12, le=36)
    sidebar: bool = True

class ReaderState(BaseModel):
    """防止任意大 JSON 写入数据库。"""

    prefs: Preferences = Field(default_factory=Preferences)
    position: Position = Field(default_factory=Position)
    bookmarks: list[Bookmark] = Field(default_factory=list, max_length=500)


class ReadingPosition(BaseModel):
    """小体积位置与偏好更新，不携带或覆盖其他标签页的书签。"""
    prefs: Preferences
    position: Position
    # 客户端缓存的目录章数；目录复核合并后旧阅读器提交会因不匹配被拒绝。
    expected_chapter_count: int | None = Field(default=None, ge=1, le=10000)


def _reading_for_update(db: Session, novel_id: int) -> tuple[Novel, ReadingState]:
    """先获取 SQLite 写锁再读 JSON，串行合并不同条目的修改。"""
    novel = get_novel_or_404(db, novel_id)
    db.execute(insert(ReadingState).values(novel_id=novel_id, value=ReaderState().model_dump())
               .on_conflict_do_nothing(index_elements=["novel_id"]))
    row = db.query(ReadingState).filter(ReadingState.novel_id == novel_id).populate_existing().one()
    return novel, row


@router.put("/novels/{novel_id}/reading/position", response_model=ReadingPosition)
def save_position(novel_id: int, value: ReadingPosition, db: Session = Depends(get_db)):
    novel, row = _reading_for_update(db, novel_id)
    if (
        value.expected_chapter_count is not None
        and value.expected_chapter_count != novel.chapter_count
    ):
        raise HTTPException(409, "章节目录已更新，请重新打开阅读器。")
    if value.position.chapter > novel.chapter_count:
        raise HTTPException(422, "阅读位置超出章节范围。")
    payload = value.model_dump(exclude={"expected_chapter_count"})
    row.value = {**row.value, **payload}
    db.commit()
    return value


@router.put("/novels/{novel_id}/reading/bookmarks/{bookmark_id}", response_model=Bookmark)
def save_bookmark(novel_id: int, bookmark_id: str, value: Bookmark, db: Session = Depends(get_db)):
    novel, row = _reading_for_update(db, novel_id)
    if value.id != bookmark_id or value.chapter > novel.chapter_count:
        raise HTTPException(422, "书签标识或位置无效。")
    bookmarks = [b for b in row.value["bookmarks"] if b["id"] != bookmark_id]
    if len(bookmarks) >= 500:
        raise HTTPException(422, "最多保存500个书签。")
    row.value = {**row.value, "bookmarks": [*bookmarks, value.model_dump()]}
    db.commit()
    return value


@router.delete("/novels/{novel_id}/reading/bookmarks/{bookmark_id}", status_code=204)
def delete_bookmark(novel_id: int, bookmark_id: str, db: Session = Depends(get_db)):
    novel, row = _reading_for_update(db, novel_id)
    row.value = {**row.value, "bookmarks": [b for b in row.value["bookmarks"] if b["id"] != bookmark_id]}
    db.commit()


@router.get("/workspace/privacy", response_model=Privacy)
def get_privacy(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    row = db.get(WorkspaceSetting, "privacy")
    if row is None or row.value.get("scope") != privacy_scope(settings):
        return Privacy()
    return Privacy.model_validate({k: row.value[k] for k in ("decided", "external", "rights")})


@router.put("/workspace/privacy", response_model=Privacy)
def save_privacy(value: Privacy, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    db.merge(WorkspaceSetting(key="privacy", value={**value.model_dump(), "scope": privacy_scope(settings)}))
    db.commit()
    return value


@router.get("/novels/{novel_id}/reading", response_model=ReaderState)
def get_reading(novel_id: int, db: Session = Depends(get_db)):
    get_novel_or_404(db, novel_id)
    row = db.get(ReadingState, novel_id)
    if row is None:
        return ReaderState()
    data = dict(row.value)
    prefs = dict(data.get("prefs") or {})
    # Dirty stored fonts from older clients; writes stay Literal-strict.
    if not isinstance(prefs.get("font"), str) or prefs["font"] not in {"serif", "sans", "pingfang"}:
        prefs["font"] = "serif"
        data["prefs"] = prefs
    return ReaderState.model_validate(data)


@router.put("/novels/{novel_id}/reading", response_model=ReaderState)
def save_reading(novel_id: int, value: ReaderState, db: Session = Depends(get_db)):
    novel = get_novel_or_404(db, novel_id)
    if any(p.chapter > novel.chapter_count for p in [value.position, *value.bookmarks]):
        raise HTTPException(422, "阅读位置超出章节范围。")
    db.merge(ReadingState(novel_id=novel_id, value=value.model_dump()))
    db.commit()
    return value


def workflow_data(row):
    """问题正文不进入任务列表。"""
    return {**{k: getattr(row, k) for k in
            ("id", "novel_id", "kind", "status", "phase", "error_message")}, "created_at": row.created_at.isoformat()}


@router.get("/workspace/tasks")
def tasks(db: Session = Depends(get_db)):
    """后端持续执行，浏览器只轮询记录。"""
    names = {key: name for key, name in db.query(Novel.id, Novel.filename).all()}
    rows = []
    for job in db.query(AnalysisJob).order_by(AnalysisJob.created_at.desc()).limit(100):
        rows.append(dict(id=job.id, novel_id=job.novel_id, kind=job.job_type,
                         status=job.status, progress_current=job.progress_current,
                         progress_total=job.progress_total, error_message=job.error_message,
                         created_at=job.created_at.isoformat()))
    for question in db.query(RagQuestion).order_by(RagQuestion.created_at.desc()).limit(30):
        rows.append(dict(id=question.id, novel_id=question.novel_id, kind="question",
                         status=question.status, error_message=question.error_message,
                         created_at=question.created_at.isoformat()))
    for row in db.query(Workflow).order_by(Workflow.created_at.desc()).limit(30):
        rows.append(workflow_data(row))
    for task in rows:
        task["filename"] = names.get(task["novel_id"], "")
    return sorted(rows, key=lambda r: r["created_at"], reverse=True)


@router.post("/workspace/tasks/{task_id}/cancel", status_code=204)
def cancel_task(task_id: str, db: Session = Depends(get_db)):
    """结束活动状态并取消等待中的 Future；运行中 worker 在下一安全点停止。"""
    row = next((value for model in (AnalysisJob, RagQuestion, Workflow)
                if (value := db.get(model, task_id)) is not None), None)
    if row is None:
        raise HTTPException(404, "未找到指定任务。")
    ids = [task_id, *executor.child_ids(task_id)]
    for model in (AnalysisJob, RagQuestion, Workflow):
        values: dict[Any, Any] = {"status": "failed", "error_message": "用户已取消任务，已完成结果已保留。"}
        if model is not Workflow:
            values["completed_at"] = utc_now()
        db.query(model).filter(model.id.in_(ids), model.status.in_(("queued", "running"))).update({**values})
    db.commit()
    executor.cancel(task_id)


class WorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["report", "ask"]
    question: str = Field(default="", max_length=2000)
    confirm_external_processing: StrictBool
    confirm_content_rights: StrictBool


from backend.app.services.dependencies import get_session_factory as get_workflow_session_factory


def fail_interrupted_workflows(factory):
    """重启后将未完成任务链明确标为失败，避免意外恢复付费操作。"""
    with factory() as db:
        for row in db.query(Workflow).filter(Workflow.status.in_(["queued", "running"])):
            row.status = "failed"
            row.error_message = "服务已重启；可重新发起操作，已完成的摘要和索引会被复用。"
        db.commit()


@router.post("/novels/{novel_id}/workflows", status_code=202)
def start_workflow(novel_id: int, value: WorkflowRequest, background: BackgroundTasks,
                   db: Session = Depends(get_db), factory=Depends(get_workflow_session_factory),
                   execution: WorkflowExecution = Depends(get_workflow_execution)):
    """不从阅读或页面加载中隐式触发；异常中断不会自动续跑。数据库唯一索引阻止重复启动。"""
    get_novel_or_404(db, novel_id)
    if not value.confirm_external_processing or not value.confirm_content_rights:
        raise HTTPException(400, "请先确认外部处理和内容处理权利。")
    if value.kind == "ask" and not value.question.strip():
        raise HTTPException(422, "请输入问题。")
    job_types = ("chapter_summaries", "report", "characters") if value.kind == "report" else ("rag_index",)
    conflict = db.query(AnalysisJob.id).filter(AnalysisJob.novel_id == novel_id,
        AnalysisJob.job_type.in_((*job_types, "chapter_review")), AnalysisJob.status.in_(("queued", "running"))).first()
    if conflict or (value.kind == "ask" and db.query(RagQuestion.id).filter(
            RagQuestion.novel_id == novel_id, RagQuestion.status.in_(("queued", "running"))).first()):
        raise HTTPException(409, "这本书已有相关任务正在运行，请等待完成后再开始任务链。")
    row = Workflow(id=str(uuid4()), novel_id=novel_id, kind=value.kind,
                   question=value.question.strip(), created_at=utc_now())
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "这本书已有任务链正在运行，请等待完成。") from None
    db.refresh(row)
    result = workflow_data(row)
    try:
        background.add_task(run_workflow, row.id, factory, execution,
                            value.confirm_external_processing, value.confirm_content_rights)
    except Exception:
        row.status = "failed"
        row.error_message = "任务链未能加入后台队列，请重试。"
        db.commit()
        raise
    return result
