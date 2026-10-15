"""向量命中只是候选：回查主 SQLite 核对小说、章节、范围和内容哈希后才交给生成模型；
citation_ids 还须属于本次证据白名单，因此损坏索引、过期文本或模型伪造引用都不能成为可信答案。
"""

import hashlib
import json
import logging
import math
from collections.abc import Callable
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    StrictBool,
    field_validator,
    model_validator,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.app.analyzer.llm import LLMProvider, LLMProviderError
from backend.app.analyzer.prompting import novelmind_system_prompt
from backend.app.core.sqlite_retry import retry_sqlite
from backend.app.core.time import utc_now
from backend.app.models.novel import Chapter, Novel
from backend.app.services.execution import executor
from backend.app.models.rag import RagChunkRecord, RagIndex, RagQuestion
from backend.app.rag.chunking import query_embedding_text
from backend.app.rag.embedding import (
    EmbeddingProvider,
    EmbeddingProviderError,
)
from backend.app.rag.indexer import VectorStore
from backend.app.rag.vector_store import VectorCollectionNotFoundError


logger = logging.getLogger(__name__)
RAG_ANSWER_PROMPT_VERSION = "rag-answer-v3"
_CHUNK_ID_PATTERN = r"^chunk_[0-9a-f]{64}$"


class RagAnswerError(RuntimeError):
    pass


class _RagQuestionCancelled(RuntimeError):
    """Internal signal for a deleted or terminalized question."""


class RagAnswerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=12_000,
        ),
    ]
    citation_ids: list[
        Annotated[str, StringConstraints(pattern=_CHUNK_ID_PATTERN)]
    ] = Field(max_length=10)
    insufficient_evidence: StrictBool

    @field_validator("citation_ids")
    @classmethod
    def citation_ids_must_be_unique(
        cls,
        values: list[str],
    ) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("citation_ids must be unique")
        return values

    @model_validator(mode="after")
    def supported_answers_need_a_citation(self) -> "RagAnswerPayload":
        """若模型声称证据充足，至少必须引用一个本次检索片段。"""
        if not self.insufficient_evidence and not self.citation_ids:
            raise ValueError(
                "an answer with sufficient evidence needs a citation"
            )
        return self


class RagEvidence(BaseModel):
    """Exact local evidence sent to DeepSeek and safe to persist."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: Annotated[
        str,
        StringConstraints(pattern=_CHUNK_ID_PATTERN),
    ]
    chapter_index: int = Field(ge=1)
    chapter_title: str = Field(min_length=1, max_length=255)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    distance: float
    excerpt: str = Field(min_length=1, max_length=1_200)

    @model_validator(mode="after")
    def range_must_be_ordered(self) -> "RagEvidence":
        if self.end_char <= self.start_char:
            raise ValueError("invalid evidence character range")
        if not math.isfinite(self.distance):
            raise ValueError("invalid evidence distance")
        return self


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _close_provider(provider: object, description: str) -> None:
    close = getattr(provider, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        logger.exception("Could not close unused %s provider.", description)


def _question_is_running(
    session_factory: Callable[[], Session],
    *,
    question_id: str,
    novel_id: int,
    source_hash: str,
) -> bool:
    with session_factory() as db:
        return (
            db.query(RagQuestion.id)
            .join(Novel, Novel.id == RagQuestion.novel_id)
            .join(RagIndex, RagIndex.novel_id == Novel.id)
            .filter(
                RagQuestion.id == question_id,
                RagQuestion.status == "running",
                Novel.id == novel_id,
                RagIndex.source_hash == source_hash,
            )
            .first()
            is not None
        )


def _ensure_question_running(
    session_factory: Callable[[], Session],
    *,
    question_id: str,
    novel_id: int,
    source_hash: str,
) -> None:
    """条件失效时抛内部取消异常，阻止迟到结果写库。"""
    if executor.is_stopping():
        raise _RagQuestionCancelled()
    if not _question_is_running(
        session_factory,
        question_id=question_id,
        novel_id=novel_id,
        source_hash=source_hash,
    ):
        raise _RagQuestionCancelled()


def _claim_question(db: Session, question_id: str) -> bool:
    """锁重试耗尽则上抛，由 worker 写失败终态；条件 UPDATE 避免重复外部调用。"""

    def write_status():
        try:
            claimed = (
                db.query(RagQuestion)
                .filter(RagQuestion.id == question_id, RagQuestion.status == "queued")
                .update(
                    {
                        RagQuestion.status: "running",
                        RagQuestion.error_message: None,
                        RagQuestion.started_at: utc_now(),
                        RagQuestion.completed_at: None,
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            return claimed == 1
        except OperationalError:
            db.rollback()
            raise

    return retry_sqlite(write_status)


def mark_rag_question_failed_with_sqlite_lock_retry(
    session_factory: Callable[[], Session],
    *,
    question_id: str,
    expected_status: str,
    error_message: str,
) -> bool:
    """expected_status 防止迟到异常覆盖已完成答案；error_message 必须是不含正文/密钥的安全文字。"""

    def write_status():
        with session_factory() as db:
            updated = (
                db.query(RagQuestion)
                .filter(
                    RagQuestion.id == question_id, RagQuestion.status == expected_status
                )
                .update(
                    {
                        RagQuestion.status: "failed",
                        RagQuestion.error_message: error_message,
                        RagQuestion.completed_at: utc_now(),
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if updated == 1:
                return True
            question = db.get(RagQuestion, question_id)
            return question is None or question.status != expected_status

    try:
        return retry_sqlite(write_status)
    except Exception:
        logger.error("Could not terminalize RAG question %s.", question_id)
        return False


def _validate_hit_metadata(
    metadata: object,
    record: RagChunkRecord,
) -> None:
    if not isinstance(metadata, dict):
        raise RagAnswerError("本地向量索引元数据校验失败，请重建索引。")
    expected = {
        "chapter_id": record.chapter_id,
        "chapter_index": record.chapter_index,
        "chunk_index": record.chunk_index,
        "start_char": record.start_char,
        "end_char": record.end_char,
        "content_hash": record.content_hash,
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise RagAnswerError("本地向量索引元数据校验失败，请重建索引。")


# 可信文本从主库重新切片并核验；不直接采用向量库元数据里的文本。
def _load_evidence(
    db: Session,
    *,
    index: RagIndex,
    hits: list,
) -> list[RagEvidence]:
    """任一命中缺记录或内容变化都令整次回答失败，不使用部分可疑证据。"""
    hit_ids = [hit.chunk_id for hit in hits]
    if len(set(hit_ids)) != len(hit_ids):
        raise RagAnswerError("本地向量检索返回重复结果，请重建索引。")
    records = (
        db.query(RagChunkRecord)
        .filter(
            RagChunkRecord.rag_index_id == index.id,
            RagChunkRecord.chunk_id.in_(hit_ids),
        )
        .all()
    )
    by_id = {record.chunk_id: record for record in records}
    if set(by_id) != set(hit_ids):
        raise RagAnswerError("本地向量索引来源校验失败，请重建索引。")
    chapter_ids = {record.chapter_id for record in records}
    chapters = {
        chapter.id: chapter
        for chapter in (
            db.query(Chapter)
            .filter(
                Chapter.novel_id == index.novel_id,
                Chapter.id.in_(chapter_ids),
            )
            .all()
        )
    }
    if set(chapters) != chapter_ids:
        raise RagAnswerError("索引对应的小说章节已变化，请重建索引。")

    evidence: list[RagEvidence] = []
    for hit in hits:
        if not math.isfinite(hit.distance):
            raise RagAnswerError("本地向量检索距离无效，请重建索引。")
        record = by_id[hit.chunk_id]
        _validate_hit_metadata(hit.metadata, record)
        chapter = chapters[record.chapter_id]
        if (
            chapter.chapter_index != record.chapter_index
            or record.end_char > len(chapter.content)
        ):
            raise RagAnswerError("索引对应的小说章节已变化，请重建索引。")
        excerpt = chapter.content[record.start_char:record.end_char]
        if not excerpt or _sha256_text(excerpt) != record.content_hash:
            raise RagAnswerError("索引对应的小说正文已变化，请重建索引。")
        evidence.append(
            RagEvidence(
                chunk_id=record.chunk_id,
                chapter_index=record.chapter_index,
                chapter_title=chapter.title,
                start_char=record.start_char,
                end_char=record.end_char,
                distance=hit.distance,
                excerpt=excerpt,
            )
        )
    return evidence


def _answer_prompt(question: str, evidence: list[RagEvidence]) -> str:
    """组装读者问题与原文证据片段的 JSON 材料。"""
    request_json = json.dumps(
        {
            "question": question,
            "evidence": [item.model_dump() for item in evidence],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        "请严格依据下一行 JSON 中的小说原文片段（evidence）解答读者问题"
        "（question），深入还原文本原意与细节逻辑：\n"
        f"{request_json}"
    )


def _rag_system_prompt() -> str:
    return novelmind_system_prompt(
        role="中文小说原意解析与证据问答助手",
        material_scope=(
            "question 是读者提出的关于小说情节、人物动机、伏笔线索或文本含义的"
            "问题；evidence.excerpt 是从小说中检索出的原文片段（含 chunk_id 与"
            "章节信息）。检索结果可能只覆盖全书局部、存在上下文缺口或包含与问题"
            "无关的片段，不能仅因某片段被检索召回就强行将其用于作答。"
        ),
        task_rules=(
            "严格只返回 answer、citation_ids、insufficient_evidence 三个字段："
            "1. 自然深度的解答（answer）："
            "   - 结论先行：首句直接回答读者问题，再结合原文细节给出完成解答所需的"
            "紧凑说明。当解析人物动机或情节深意时，将表层事件与原文的动作细节、对话"
            "潜台词自然串联。每个关键事实或推断都必须由所引用的片段直接支撑；推断要"
            "用“可推断”“暗示”“可能”等措辞与原文直接事实区分。"
            "   - 无痕融合（Forbidden Meta-Phrases）：行文中直接以小说世界的内部"
            "脉络或章节场景自然叙述（如“在第 N 章的对峙中……”），严禁在 answer "
            "文本中出现“根据检索到的片段”“chunk_x 显示”“evidence 中提到”等暴露"
            "检索机制的生硬汇报话术。"
            "2. 精选引用（citation_ids）："
            "   - 只列出实际支撑 answer 实质内容的 chunk_id，优先保留最核心的证据"
            "片段，绝不引用无关或未使用的陪衬片段。"
            "3. 证据边界裁定（insufficient_evidence）："
            "   - 若检索证据足以完整回答问题，设 insufficient_evidence=false，且 "
            "citation_ids 不得为空。"
            "   - 若检索证据无法回答、只能回答一部分、存在无法裁决的视角冲突，或问题"
            "超出给定原文范围，answer 必须先基于现有片段说明可确认的事实范围，再明确"
            "指出当前片段尚未覆盖的信息缺口（此时仍可引用用于说明已知部分或冲突的 "
            "chunk_id），并将 insufficient_evidence 设为 true。"
        ),
    )


def run_rag_answer_job(
    question_id: str,
    embedding_provider: EmbeddingProvider,
    llm_provider: LLMProvider,
    vector_store: VectorStore,
    session_factory: Callable[[], Session],
) -> None:
    expected_status = "queued"
    try:
        with session_factory() as db:
            if not _claim_question(db, question_id):
                return
            expected_status = "running"
            row = db.get(RagQuestion, question_id)
            if row is None:
                return
            novel_id = row.novel_id
            source_hash = row.index_source_hash
            index = (
                db.query(RagIndex)
                .filter(
                    RagIndex.novel_id == novel_id,
                    RagIndex.source_hash == source_hash,
                )
                .one_or_none()
            )
            if index is None:
                raise RagAnswerError(
                    "当前 RAG 索引已变化，请重新提交问题。"
                )
            check = lambda: _ensure_question_running(
                session_factory,
                question_id=question_id,
                novel_id=novel_id,
                source_hash=source_hash,
            )
            query_result = embedding_provider.embed(
                [query_embedding_text(row.question)],
                before_attempt=check,
            )
            check()
            if len(query_result.vectors) != 1:
                raise RagAnswerError(
                    "问题向量返回数量异常，请稍后重试。"
                )
            try:
                hits = vector_store.query(
                    collection_name=index.collection_name,
                    embedding=query_result.vectors[0],
                    limit=row.top_k,
                )
            except VectorCollectionNotFoundError as error:
                raise RagAnswerError(
                    "本地向量索引不存在，请重建索引后重新提问。"
                ) from error
            if not hits:
                raise RagAnswerError(
                    "本地向量索引没有返回可用证据，请重建索引。"
                )
            evidence = _load_evidence(db, index=index, hits=hits)
            generated = llm_provider.generate_json(
                system_prompt=_rag_system_prompt(),
                user_prompt=_answer_prompt(row.question, evidence),
                response_model=RagAnswerPayload,
                thinking=True,
                reasoning_effort="high",
                before_attempt=check,
            )
            cited = set(generated.value.citation_ids)
            available = {item.chunk_id for item in evidence}
            if not cited <= available:
                raise RagAnswerError(
                    "DeepSeek 返回了无效引用，请重新提问。"
                )
            check()
            updated = (
                db.query(RagQuestion)
                .filter(
                    RagQuestion.id == question_id,
                    RagQuestion.status == "running",
                    RagQuestion.index_source_hash == source_hash,
                )
                .update(
                    {
                        RagQuestion.status: "succeeded",
                        RagQuestion.answer: generated.value.answer,
                        RagQuestion.insufficient_evidence: (
                            generated.value.insufficient_evidence
                        ),
                        RagQuestion.citation_ids: list(
                            generated.value.citation_ids
                        ),
                        RagQuestion.evidence: [
                            item.model_dump()
                            for item in evidence
                        ],
                        RagQuestion.generation_model: generated.model,
                        RagQuestion.completed_at: utc_now(),
                    },
                    synchronize_session=False,
                )
            )
            if updated != 1:
                raise _RagQuestionCancelled()
            db.commit()
    except _RagQuestionCancelled:
        logger.info(
            "RAG question %s stopped because its state or index changed.",
            question_id,
        )
        mark_rag_question_failed_with_sqlite_lock_retry(
            session_factory,
            question_id=question_id,
            expected_status=expected_status,
            error_message=(
                "RAG 索引在问答期间发生变化，请确认索引已就绪后重新提问。"
            ),
        )
    except Exception as error:
        if not isinstance(
            error,
            (
                EmbeddingProviderError,
                LLMProviderError,
                RagAnswerError,
            ),
        ):
            logger.exception("Unexpected RAG answer job failure.")
        error_message = (
            str(error)
            if isinstance(
                error,
                (
                    EmbeddingProviderError,
                    LLMProviderError,
                    RagAnswerError,
                ),
            )
            else "RAG 问答任务执行失败，请查看后端日志后重试。"
        )[:1_000]
        mark_rag_question_failed_with_sqlite_lock_retry(
            session_factory,
            question_id=question_id,
            expected_status=expected_status,
            error_message=error_message,
        )
    finally:
        _close_provider(embedding_provider, "embedding provider")
        _close_provider(llm_provider, "DeepSeek")


# 内存任务无法跨进程恢复，启动时显式结束遗留任务比永久转圈更诚实。
def fail_interrupted_rag_questions(
    session_factory: Callable[[], Session],
) -> int:
    with session_factory() as db:
        rows = (
            db.query(RagQuestion)
            .filter(RagQuestion.status.in_(("queued", "running")))
            .all()
        )
        now = utc_now()
        for row in rows:
            row.status = "failed"
            row.error_message = (
                "应用上次运行时中断了此问答任务，请重新提交问题。"
            )
            row.completed_at = now
        db.commit()
        return len(rows)
