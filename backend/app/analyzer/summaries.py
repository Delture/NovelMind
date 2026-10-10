"""各章并发请求，但 SQLite 写入由协调器串行完成以降低锁竞争。
自动并发在稳定成功后增加，遇限流/服务错误则减半；每章成功即增量落盘，重试可复用来源哈希一致的结果。
"""

import asyncio
import hashlib
import json
import logging
import math
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from typing import Any, Callable, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.app.analyzer.jobs import (
    claim_analysis_job,
    advance_analysis_job,
    is_sqlite_lock_error,
    mark_analysis_job_failed_with_sqlite_lock_retry,
)
from backend.app.analyzer.llm import LLMProvider, LLMProviderError
from backend.app.analyzer.prompting import novelmind_system_prompt
from backend.app.core.sqlite_retry import retry_sqlite
from backend.app.core.time import utc_now
from backend.app.models.analysis import AnalysisJob, ChapterSummary
from backend.app.models.novel import Chapter, Novel


logger = logging.getLogger(__name__)

SUMMARY_PROMPT_VERSION = "chapter-summary-v3"
MAX_DIRECT_SUMMARY_CHARACTERS = 60_000
SUMMARY_REDUCTION_BATCH_SIZE = 12
SUMMARY_JOB_SQLITE_MAX_ATTEMPTS = 4
SUMMARY_JOB_SQLITE_RETRY_BASE_SECONDS = 0.05
SUMMARY_AUTO_RAMP_SUCCESS_COUNT = 32
SUMMARY_AUTO_RETRY_LIMIT = 2


class _SummaryJobCancelled(RuntimeError):
    """Stop a job whose persisted source or running state disappeared."""


@dataclass(frozen=True)
class _SummaryWorkItem:
    """Immutable chapter data safe to send through concurrent workers."""

    chapter_id: int
    title: str
    content: str
    source_hash: str
    retry_count: int = 0


class ChapterSummaryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=4_000)
    key_events: list[str] = Field(default_factory=list, max_length=12)
    plot_progression: str = Field(min_length=1, max_length=2_000)
    cliffhanger: Optional[str] = Field(default=None, max_length=1_000)

    @field_validator("summary", "plot_progression")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("summary text cannot be blank")
        return normalized

    @field_validator("cliffhanger")
    @classmethod
    def normalize_optional_text(
        cls,
        value: Optional[str],
    ) -> Optional[str]:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("key_events")
    @classmethod
    def normalize_key_events(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            event = value.strip()
            if not event:
                raise ValueError("key events cannot be blank")
            if len(event) > 500:
                raise ValueError("a key event cannot exceed 500 characters")
            if event not in normalized:
                normalized.append(event)
        return normalized


def _split_text(text: str) -> list[str]:
    chunks: list[str] = []
    current_parts: list[str] = []
    current_length = 0

    for paragraph in text.splitlines(keepends=True):
        if len(paragraph) > MAX_DIRECT_SUMMARY_CHARACTERS:
            if current_parts:
                chunks.append("".join(current_parts).strip())
                current_parts = []
                current_length = 0
            for start in range(0, len(paragraph), MAX_DIRECT_SUMMARY_CHARACTERS):
                chunks.append(
                    paragraph[
                        start : start + MAX_DIRECT_SUMMARY_CHARACTERS
                    ].strip()
                )
            continue

        if (
            current_parts
            and current_length + len(paragraph)
            > MAX_DIRECT_SUMMARY_CHARACTERS
        ):
            chunks.append("".join(current_parts).strip())
            current_parts = []
            current_length = 0
        current_parts.append(paragraph)
        current_length += len(paragraph)

    if current_parts:
        chunks.append("".join(current_parts).strip())
    return [chunk for chunk in chunks if chunk]


def _summary_chunks(content: str) -> list[str]:
    chunks = _split_text(content)
    return chunks or [content]


def estimate_summary_generation_steps(content: str) -> int:
    pending_count = len(_summary_chunks(content))
    generation_steps = pending_count
    while pending_count > 1:
        pending_count = math.ceil(
            pending_count / SUMMARY_REDUCTION_BATCH_SIZE
        )
        generation_steps += pending_count
    return generation_steps


def chapter_summary_source_hash(title: str, content: str) -> str:
    """标题、正文或提示规则变化都会使旧摘要失效。"""
    source_material = json.dumps(
        {
            "title": title,
            "content": content,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(
        source_material.encode("utf-8")
    ).hexdigest()


def _summary_system_prompt() -> str:
    return novelmind_system_prompt(
        role="中文小说长篇分块消化助手",
        material_scope=(
            "映射阶段的 text 是小说原文连续片段；归并阶段的 "
            "partial_analyses 是模型派生摘要，不是原文。当前片段缺少前文时，"
            "不得声称某人物、规则或线索在全书“首次出现”。"
        ),
        task_rules=(
            "本任务是状态提取，不是评价、续写或市场判断。按正文事件顺序"
            "提取后续分析所需的最小充分状态：优先保留不可逆事件，关键选择"
            "及代价，明确因果，伏笔植入或回收，规则新增或修正，以及关系、"
            "身份或阵营的实质变化；其次保留冲突阶段结果、重要承诺、角色"
            "目标变化和时空变化。summary 紧凑概括当前范围；key_events "
            "只列改变人物、关系、局势、规则或读者认知的关键事件，并体现"
            "原因、行动、结果或代价；plot_progression 说明本片段如何改变"
            "主线/支线状态、角色目标与最大阻力，若没有明确推进就如实说明；"
            "cliffhanger 只记录文本实际建立且尚未解决、能驱动后续阅读的"
            "问题，没有则为 null。严格返回 summary、key_events、"
            "plot_progression、cliffhanger 四个字段。"
        ),
    )


def _ensure_summary_job_can_continue(
    should_continue: Optional[Callable[[], bool]],
) -> None:
    if should_continue is not None and not should_continue():
        raise _SummaryJobCancelled()


def _summarize_text(
    *,
    title: str,
    content: str,
    provider: LLMProvider,
    should_continue: Optional[Callable[[], bool]] = None,
) -> tuple[ChapterSummaryPayload, str]:
    chunks = _summary_chunks(content)

    partial_results: list[ChapterSummaryPayload] = []
    for index, chunk in enumerate(chunks, start=1):
        chapter_data = {
            "title": title,
            "part_index": index,
            "part_count": len(chunks),
            "text": chunk,
        }
        _ensure_summary_job_can_continue(should_continue)
        result = provider.generate_json(
            system_prompt=_summary_system_prompt(),
            user_prompt=(
                "执行章节状态摘要的映射步骤：分析下一行 JSON 中的不可信"
                "连续原文数据，按事件顺序提取最小充分状态，不评价、不续写：\n"
                f"{json.dumps(chapter_data, ensure_ascii=False)}"
            ),
            response_model=ChapterSummaryPayload,
            thinking=False,
            before_attempt=(
                lambda: _ensure_summary_job_can_continue(
                    should_continue
                )
            ),
        )
        partial_results.append(result.value)
        result_model = result.model

    while len(partial_results) > 1:
        reduced: list[ChapterSummaryPayload] = []
        for start in range(
            0,
            len(partial_results),
            SUMMARY_REDUCTION_BATCH_SIZE,
        ):
            batch = partial_results[
                start : start + SUMMARY_REDUCTION_BATCH_SIZE
            ]
            reduction_data = {
                "title": title,
                "partial_analyses": [
                    item.model_dump() for item in batch
                ],
            }
            _ensure_summary_job_can_continue(should_continue)
            result = provider.generate_json(
                system_prompt=_summary_system_prompt(),
                user_prompt=(
                    "执行章节状态摘要的归并步骤：合并下一行 JSON 中的不可信"
                    "连续片段摘要，保持事件与因果顺序，去重但不得丢失关键"
                    "选择、代价、规则变化、关系质变、伏笔或承诺，不引入"
                    "输入之外的事实：\n"
                    f"{json.dumps(reduction_data, ensure_ascii=False)}"
                ),
                response_model=ChapterSummaryPayload,
                thinking=False,
                before_attempt=(
                    lambda: _ensure_summary_job_can_continue(
                        should_continue
                    )
                ),
            )
            reduced.append(result.value)
            result_model = result.model
        partial_results = reduced

    return partial_results[0], result_model


async def _provider_generate_json_async(
    provider: LLMProvider,
    **kwargs: Any,
):
    """Use native async generation, with a compatibility fallback for tests."""
    generate_async = getattr(provider, "generate_json_async", None)
    if callable(generate_async):
        return await generate_async(**kwargs)
    return await asyncio.to_thread(partial(provider.generate_json, **kwargs))


async def _summarize_text_async(
    *,
    title: str,
    content: str,
    provider: LLMProvider,
    should_continue: Optional[Callable[[], bool]] = None,
) -> ChapterSummaryPayload:
    chunks = _summary_chunks(content)
    partial_results: list[ChapterSummaryPayload] = []
    for index, chunk in enumerate(chunks, start=1):
        chapter_data = {
            "title": title,
            "part_index": index,
            "part_count": len(chunks),
            "text": chunk,
        }
        _ensure_summary_job_can_continue(should_continue)
        result = await _provider_generate_json_async(
            provider,
            system_prompt=_summary_system_prompt(),
            user_prompt=(
                "执行章节状态摘要的映射步骤：分析下一行 JSON 中的不可信"
                "连续原文数据，按事件顺序提取最小充分状态，不评价、不续写：\n"
                f"{json.dumps(chapter_data, ensure_ascii=False)}"
            ),
            response_model=ChapterSummaryPayload,
            thinking=False,
            before_attempt=(
                lambda: _ensure_summary_job_can_continue(
                    should_continue
                )
            ),
        )
        partial_results.append(result.value)

    while len(partial_results) > 1:
        reduced: list[ChapterSummaryPayload] = []
        for start in range(
            0,
            len(partial_results),
            SUMMARY_REDUCTION_BATCH_SIZE,
        ):
            batch = partial_results[
                start : start + SUMMARY_REDUCTION_BATCH_SIZE
            ]
            reduction_data = {
                "title": title,
                "partial_analyses": [
                    item.model_dump() for item in batch
                ],
            }
            _ensure_summary_job_can_continue(should_continue)
            result = await _provider_generate_json_async(
                provider,
                system_prompt=_summary_system_prompt(),
                user_prompt=(
                    "执行章节状态摘要的归并步骤：合并下一行 JSON 中的不可信"
                    "连续片段摘要，保持事件与因果顺序，去重但不得丢失关键"
                    "选择、代价、规则变化、关系质变、伏笔或承诺，不引入"
                    "输入之外的事实：\n"
                    f"{json.dumps(reduction_data, ensure_ascii=False)}"
                ),
                response_model=ChapterSummaryPayload,
                thinking=False,
                before_attempt=(
                    lambda: _ensure_summary_job_can_continue(
                        should_continue
                    )
                ),
            )
            reduced.append(result.value)
        partial_results = reduced

    return partial_results[0]


def _summary_is_current(
    existing: Optional[ChapterSummary],
    *,
    source_hash: str,
    provider_model: str,
) -> bool:
    return bool(
        existing is not None
        and existing.source_hash == source_hash
        and existing.prompt_version == SUMMARY_PROMPT_VERSION
        and existing.model == provider_model
    )


def _persist_summary_payload(
# 保存结果和推进任务进度应处于同一短事务，避免“结果有了但进度没变”。
    db: Session,
    *,
    chapter_id: int,
    source_hash: str,
    payload: ChapterSummaryPayload,
    provider_model: str,
) -> ChapterSummary:
    """不自行 commit，由调用者把结果与任务进度放进同一事务。"""
    existing = (
        db.query(ChapterSummary)
        .filter(ChapterSummary.chapter_id == chapter_id)
        .one_or_none()
    )
    now = utc_now()
    if existing is None:
        existing = ChapterSummary(
            chapter_id=chapter_id,
            created_at=now,
        )
        db.add(existing)
    existing.summary = payload.summary
    existing.key_events = payload.key_events
    existing.plot_progression = payload.plot_progression
    existing.cliffhanger = payload.cliffhanger
    existing.model = provider_model
    existing.prompt_version = SUMMARY_PROMPT_VERSION
    existing.source_hash = source_hash
    existing.updated_at = now
    db.flush()
    return existing


def generate_chapter_summary(
    db: Session,
    chapter: Chapter,
    provider: LLMProvider,
    force: bool = False,
    should_continue: Optional[Callable[[], bool]] = None,
) -> tuple[ChapterSummary, bool]:
    source_hash = chapter_summary_source_hash(
        chapter.title,
        chapter.content,
    )
    existing = (
        db.query(ChapterSummary)
        .filter(ChapterSummary.chapter_id == chapter.id)
        .one_or_none()
    )
    if not force and _summary_is_current(
        existing,
        source_hash=source_hash,
        provider_model=provider.model,
    ):
        assert existing is not None
        return existing, False

    payload, _ = _summarize_text(
        title=chapter.title,
        content=chapter.content,
        provider=provider,
        should_continue=should_continue,
    )
    _ensure_summary_job_can_continue(should_continue)
    stored = _persist_summary_payload(
        db,
        chapter_id=chapter.id,
        source_hash=source_hash,
        payload=payload,
        provider_model=provider.model,
    )
    return stored, True


from backend.app.core.sqlite_retry import sleep_before_retry as _sleep_before_sqlite_retry


def _claim_summary_job(
    db: Session,
    job_id: str,
    claimed_at: datetime,
) -> int:
    return claim_analysis_job(db, job_id, "chapter_summaries", claimed_at, sleep=_sleep_before_sqlite_retry)


def _mark_queued_job_failed_after_claim_error(
    session_factory: Callable[[], Session],
    job_id: str,
    error_message: str,
) -> bool:
    return mark_analysis_job_failed_with_sqlite_lock_retry(
        session_factory,
        job_id,
        "chapter_summaries",
        error_message,
        expected_status="queued",
        failure_context="claim failure",
    )


def _mark_running_job_failed_after_execution_error(
    session_factory: Callable[[], Session],
    job_id: str,
    error_message: str,
) -> bool:
    return mark_analysis_job_failed_with_sqlite_lock_retry(
        session_factory,
        job_id,
        "chapter_summaries",
        error_message,
        expected_status="running",
        failure_context="execution failure",
    )


def _summary_job_has_live_source(
    session_factory: Callable[[], Session],
    *,
    job_id: str,
    novel_id: int,
    chapter_id: int,
) -> bool:
    with session_factory() as status_db:
        active_job = (
            status_db.query(AnalysisJob.id)
            .join(Novel, Novel.id == AnalysisJob.novel_id)
            .join(Chapter, Chapter.novel_id == Novel.id)
            .filter(
                AnalysisJob.id == job_id,
                AnalysisJob.job_type == "chapter_summaries",
                AnalysisJob.status == "running",
                Novel.id == novel_id,
                Chapter.id == chapter_id,
            )
            .first()
        )
        return active_job is not None


async def _close_provider_async(provider: LLMProvider) -> None:
    aclose = getattr(provider, "aclose", None)
    try:
        if callable(aclose):
            await aclose()
            return
        close = getattr(provider, "close", None)
        if callable(close):
            close()
    except Exception:
        logger.exception("Could not close the summary provider.")


def run_summary_job(
    job_id: str,
    provider: LLMProvider,
    session_factory: Callable[[], Session],
) -> None:
    """Execute the async scheduler from FastAPI's background thread."""
    asyncio.run(
        _run_summary_job_and_close(job_id, provider, session_factory)
    )


async def _run_summary_job_and_close(
    job_id: str,
    provider: LLMProvider,
    session_factory: Callable[[], Session],
) -> None:
    try:
        await _run_summary_job(job_id, provider, session_factory)
    finally:
        await _close_provider_async(provider)


def _advance_summary_job(db: Session, job_id: str) -> bool:
    return advance_analysis_job(db, job_id)


async def _cancel_summary_tasks(
    active: dict[asyncio.Task, _SummaryWorkItem],
) -> None:
    tasks = list(active)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    active.clear()


async def _run_concurrent_summary_generation(
# 这里是生产者/协调器边界：网络请求可以并行，数据库写操作仍集中串行。
    *,
    db: Session,
    job_id: str,
    novel_id: int,
    provider: LLMProvider,
    pending: deque[_SummaryWorkItem],
    session_factory: Callable[[], Session],
) -> None:
    """不可恢复错误取消其余 task，已提交的逐章缓存继续保留。"""
    mode = str(
        getattr(provider, "summary_concurrency_mode", "manual")
    )
    max_concurrency = max(
        1,
        int(getattr(provider, "summary_max_concurrency", 1)),
    )
    current_concurrency = min(
        max_concurrency,
        max(
            1,
            int(getattr(provider, "summary_initial_concurrency", 1)),
        ),
        max(1, len(pending)),
    )
    successes_since_ramp = 0
    active: dict[asyncio.Task, _SummaryWorkItem] = {}

    try:
        while pending or active:
            while pending and len(active) < current_concurrency:
                item = pending.popleft()
                continuation_check = partial(
                    _summary_job_has_live_source,
                    session_factory,
                    job_id=job_id,
                    novel_id=novel_id,
                    chapter_id=item.chapter_id,
                )
                task = asyncio.create_task(
                    _summarize_text_async(
                        title=item.title,
                        content=item.content,
                        provider=provider,
                        should_continue=continuation_check,
                    )
                )
                active[task] = item

            if not active:
                break
            completed, _ = await asyncio.wait(
                tuple(active),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in completed:
                item = active.pop(task)
                try:
                    payload = task.result()
                except _SummaryJobCancelled:
                    raise
                except LLMProviderError as error:
                    if (
                        mode == "auto"
                        and error.retryable
                        and item.retry_count < SUMMARY_AUTO_RETRY_LIMIT
                    ):
                        pending.appendleft(
                            replace(
                                item,
                                retry_count=item.retry_count + 1,
                            )
                        )
                        reduced = max(1, current_concurrency // 2)
                        if reduced != current_concurrency:
                            logger.warning(
                                "Summary concurrency reduced from %s to %s "
                                "after a retryable provider failure.",
                                current_concurrency,
                                reduced,
                            )
                        current_concurrency = reduced
                        successes_since_ramp = 0
                        continue
                    raise

                if db.get(Chapter, item.chapter_id) is None:
                    raise _SummaryJobCancelled()
                def persist_step(item=item, payload=payload):
                    """短事务失败后回滚，由共享写锁策略重试。"""
                    try:
                        _persist_summary_payload(db, chapter_id=item.chapter_id, source_hash=item.source_hash, payload=payload, provider_model=provider.model)
                        if not _advance_summary_job(db, job_id):
                            raise _SummaryJobCancelled()
                        return
                    except OperationalError:
                        db.rollback()
                        raise
                retry_sqlite(persist_step, sleep=_sleep_before_sqlite_retry)

                if mode == "auto" and current_concurrency < max_concurrency:
                    successes_since_ramp += 1
                    if (
                        successes_since_ramp
                        >= SUMMARY_AUTO_RAMP_SUCCESS_COUNT
                    ):
                        increased = min(
                            max_concurrency,
                            max(
                                current_concurrency + 8,
                                math.ceil(current_concurrency * 1.25),
                            ),
                        )
                        if increased != current_concurrency:
                            logger.info(
                                "Summary concurrency increased from %s "
                                "to %s after stable completions.",
                                current_concurrency,
                                increased,
                            )
                        current_concurrency = increased
                        successes_since_ramp = 0
    except BaseException:
        await _cancel_summary_tasks(active)
        raise


async def _run_summary_job(
    job_id: str,
    provider: LLMProvider,
    session_factory: Callable[[], Session],
) -> None:
    with session_factory() as db:
        claimed_at = utc_now()
        try:
            claimed = _claim_summary_job(db, job_id, claimed_at)
        except OperationalError as error:
            db.rollback()
            is_lock_error = is_sqlite_lock_error(error)
            if is_lock_error:
                logger.error(
                    "Could not claim summary job %s after %s attempts.",
                    job_id,
                    SUMMARY_JOB_SQLITE_MAX_ATTEMPTS,
                )
            else:
                logger.exception("Could not claim summary job %s.", job_id)
            failure_message = (
                "摘要任务无法获取本地数据库写锁，请重试。"
                if is_lock_error
                else "摘要任务启动失败，请查看后端日志后重试。"
            )
            if not _mark_queued_job_failed_after_claim_error(
                session_factory,
                job_id,
                failure_message,
            ):
                logger.error(
                    "Summary job %s could not be transitioned out "
                    "of queued state.",
                    job_id,
                )
            return
        except Exception:
            db.rollback()
            logger.exception("Could not claim summary job %s.", job_id)
            if not _mark_queued_job_failed_after_claim_error(
                session_factory,
                job_id,
                "摘要任务启动失败，请查看后端日志后重试。",
            ):
                logger.error(
                    "Summary job %s could not be transitioned out "
                    "of queued state.",
                    job_id,
                )
            return

        if claimed != 1:
            if db.get(AnalysisJob, job_id) is None:
                logger.warning("Summary job %s no longer exists.", job_id)
            return

        continuation_check: Optional[Callable[[], bool]] = None
        try:
            job_scope = (
                db.query(
                    AnalysisJob.novel_id,
                    AnalysisJob.target_chapter_index,
                    AnalysisJob.force_refresh,
                )
                .filter(AnalysisJob.id == job_id)
                .one_or_none()
            )
            if job_scope is None:
                return
            novel_id, target_chapter_index, force_refresh = job_scope
            chapter_query = db.query(Chapter.id).filter(
                Chapter.novel_id == novel_id
            )
            if target_chapter_index is not None:
                chapter_query = chapter_query.filter(
                    Chapter.chapter_index == target_chapter_index
                )
            chapter_ids = [
                chapter_id
                for (chapter_id,) in (
                    chapter_query.order_by(Chapter.chapter_index).all()
                )
            ]
            initialized = (
                db.query(AnalysisJob)
                .filter(
                    AnalysisJob.id == job_id,
                    AnalysisJob.status == "running",
                )
                .update(
                    {
                        AnalysisJob.progress_total: len(chapter_ids),
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if initialized != 1:
                return

            pending: deque[_SummaryWorkItem] = deque()
            for chapter_id in chapter_ids:
                still_running = (
                    db.query(AnalysisJob.id)
                    .filter(
                        AnalysisJob.id == job_id,
                        AnalysisJob.status == "running",
                    )
                    .scalar()
                )
                if still_running is None:
                    db.rollback()
                    return
                chapter = db.get(Chapter, chapter_id)
                if chapter is None:
                    raise RuntimeError("摘要任务中的章节已被删除。")
                source_hash = chapter_summary_source_hash(
                    chapter.title,
                    chapter.content,
                )
                existing = (
                    db.query(ChapterSummary)
                    .filter(ChapterSummary.chapter_id == chapter_id)
                    .one_or_none()
                )
                if not force_refresh and _summary_is_current(
                    existing,
                    source_hash=source_hash,
                    provider_model=provider.model,
                ):
                    if not _advance_summary_job(db, job_id):
                        return
                    continue
                pending.append(
                    _SummaryWorkItem(
                        chapter_id=chapter_id,
                        title=chapter.title,
                        content=chapter.content,
                        source_hash=source_hash,
                    )
                )

            await _run_concurrent_summary_generation(
                db=db,
                job_id=job_id,
                novel_id=novel_id,
                provider=provider,
                pending=pending,
                session_factory=session_factory,
            )

            (
                db.query(AnalysisJob)
                .filter(
                    AnalysisJob.id == job_id,
                    AnalysisJob.status == "running",
                )
                .update(
                    {
                        AnalysisJob.status: "succeeded",
                        AnalysisJob.completed_at: utc_now(),
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
        except _SummaryJobCancelled:
            db.rollback()
            logger.info(
                "Summary job %s stopped because its running state or "
                "source was removed.",
                job_id,
            )
            return
        except Exception as error:
            db.rollback()
            if continuation_check is not None:
                try:
                    if not continuation_check():
                        logger.info(
                            "Summary job %s stopped because its running "
                            "state or source was removed.",
                            job_id,
                        )
                        return
                except Exception:
                    logger.exception(
                        "Could not verify summary job %s cancellation.",
                        job_id,
                    )
            sqlite_lock_failure = (
                isinstance(error, OperationalError)
                and is_sqlite_lock_error(error)
            )
            if sqlite_lock_failure:
                logger.error(
                    "Summary job %s could not persist local progress "
                    "because the SQLite database remained locked.",
                    job_id,
                )
            elif not isinstance(error, LLMProviderError):
                logger.exception("Unexpected chapter summary job failure.")
            error_message = (
                str(error)
                if isinstance(error, LLMProviderError)
                else (
                    "摘要任务无法获取本地数据库写锁，请重试。"
                    if sqlite_lock_failure
                    else "章节摘要任务执行失败，请查看后端日志后重试。"
                )
            )[:1_000]
            if not _mark_running_job_failed_after_execution_error(
                session_factory,
                job_id,
                error_message,
            ):
                logger.error(
                    "Summary job %s could not be transitioned out "
                    "of running state.",
                    job_id,
                )


def fail_interrupted_jobs(
# 进程内 BackgroundTasks 无法跨重启恢复，所以启动时必须显式结束遗留活动任务。
    session_factory: Callable[[], Session],
) -> int:
    with session_factory() as db:
        jobs = (
            db.query(AnalysisJob)
            .filter(AnalysisJob.status.in_(("queued", "running")))
            .all()
        )
        now = utc_now()
        for job in jobs:
            job.status = "failed"
            job.error_message = (
                "应用上次运行时中断了此任务，请重新开始。"
            )
            job.completed_at = now
        db.commit()
        return len(jobs)
