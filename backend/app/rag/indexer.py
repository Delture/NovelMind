"""可复用旧向量并原子切换快照的 RAG 建索引任务。

执行器 claim queued 任务后确定性分块，从旧索引按 content_hash 复用未变化向量，
只发送缺失块。向量先写入新集合；数量验证成功后才在主 SQLite 事务中替换
RagIndex/RagChunkRecord。失败会删除未启用集合并保留旧快照。
"""

import logging
import math
import time
from collections.abc import Callable, Sequence
from typing import Optional, Protocol

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.app.analyzer.jobs import (
    ANALYSIS_JOB_SQLITE_MAX_ATTEMPTS,
    ANALYSIS_JOB_SQLITE_RETRY_BASE_SECONDS,
    is_sqlite_lock_error,
    mark_analysis_job_failed_with_sqlite_lock_retry,
)
from backend.app.core.time import utc_now
from backend.app.models.analysis import AnalysisJob
from backend.app.models.novel import Chapter, Novel
from backend.app.models.rag import RagChunkRecord, RagIndex
from backend.app.rag.chunking import (
    CHUNK_FORMAT_VERSION,
    RagChunk,
    build_rag_chunks,
    rag_source_hash,
)
from backend.app.rag.embedding import (
    EmbeddingProvider,
    EmbeddingProviderError,
    MAX_EMBEDDING_BATCH_SIZE,
)
from backend.app.rag.vector_store import VectorSearchHit


logger = logging.getLogger(__name__)
_CACHE_READ_BATCH_SIZE = 500


class RagIndexBuildError(RuntimeError):
    """A deterministic, user-readable index construction error."""


class _RagIndexJobCancelled(RuntimeError):
    """Internal signal that a deleted or terminalized job must stop."""


class VectorStore(Protocol):
    """索引器需要的最小存储协议，便于测试替换具体实现。"""

    def create_collection(self, name: str) -> None:
        """创建空的逻辑向量集合；同名已存在应由实现安全处理。"""
        ...

    def delete_collection(self, name: str) -> None:
        """删除指定集合及其中向量；索引器只传尚未启用或已过期的名称。"""
        ...

    def collection_count(self, name: str) -> int:
        """返回集合向量数；集合不存在时实现应返回 0 或抛明确未找到错误。"""
        ...

    def upsert(
        self,
        *,
        collection_name: str,
        ids: Sequence[str],
        embeddings: Sequence[Sequence[float]],
        metadatas: Sequence[dict[str, object]],
    ) -> None:
        """按相同位置批量写入 ID、向量和元数据。

        ``collection_name`` 定位集合；ids、embeddings、metadatas 三个序列必须等长，
        同一位置描述同一分块。已有 ID 应更新而不是重复插入；方法无返回值。
        """
        ...

    def get_embeddings(
        self,
        *,
        collection_name: str,
        ids: Sequence[str],
    ) -> dict[str, tuple[float, ...]]:
        """读取指定 ID 中实际存在的向量，返回 ID 到不可变向量元组的映射。"""
        ...

    def query(
        self,
        *,
        collection_name: str,
        embedding: Sequence[float],
        limit: int,
    ) -> list[VectorSearchHit]:
        """以查询向量搜索集合，按相关性返回不超过 limit 个命中。"""
        ...


def _close_embedding_provider(provider: EmbeddingProvider) -> None:
    """关闭索引任务持有的 embedding HTTP 客户端；异常只记录日志。"""
    try:
        provider.close()
    except Exception:
        logger.exception("Could not close the embedding provider.")


def _safe_delete_collection(
    vector_store: VectorStore,
    collection_name: Optional[str],
) -> None:
    """删除可选集合名；名称为空或存储删除失败时安全返回并记录。"""
    if not collection_name:
        return
    try:
        vector_store.delete_collection(collection_name)
    except Exception:
        logger.exception(
            "Could not clean up RAG vector collection %s.",
            collection_name,
        )


def _job_is_running(
    session_factory: Callable[[], Session],
    *,
    job_id: str,
    novel_id: int,
) -> bool:
    """检查任务仍在 running 且属于目标小说；每次外部批次前用于取消检测。"""
    with session_factory() as db:
        return (
            db.query(AnalysisJob.id)
            .join(Novel, Novel.id == AnalysisJob.novel_id)
            .filter(
                AnalysisJob.id == job_id,
                AnalysisJob.job_type == "rag_index",
                AnalysisJob.status == "running",
                Novel.id == novel_id,
            )
            .first()
            is not None
        )


def _ensure_job_running(
    session_factory: Callable[[], Session],
    *,
    job_id: str,
    novel_id: int,
) -> None:
    """若任务不再可继续则抛内部取消异常，防止已删除小说或任务继续计费。"""
    if not _job_is_running(
        session_factory,
        job_id=job_id,
        novel_id=novel_id,
    ):
        raise _RagIndexJobCancelled()


def _claim_job(db: Session, job_id: str) -> bool:
    """有界锁重试地把索引任务从 queued 原子领取为 running。"""
    for attempt in range(1, ANALYSIS_JOB_SQLITE_MAX_ATTEMPTS + 1):
        try:
            claimed = (
                db.query(AnalysisJob)
                .filter(
                    AnalysisJob.id == job_id,
                    AnalysisJob.job_type == "rag_index",
                    AnalysisJob.status == "queued",
                )
                .update(
                    {
                        AnalysisJob.status: "running",
                        AnalysisJob.progress_current: 0,
                        AnalysisJob.error_message: None,
                        AnalysisJob.started_at: utc_now(),
                        AnalysisJob.completed_at: None,
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            return claimed == 1
        except OperationalError as error:
            db.rollback()
            if (
                not is_sqlite_lock_error(error)
                or attempt == ANALYSIS_JOB_SQLITE_MAX_ATTEMPTS
            ):
                raise
            time.sleep(
                ANALYSIS_JOB_SQLITE_RETRY_BASE_SECONDS
                * (2 ** (attempt - 1))
            )
    return False


# 模型、维度、分块协议和内容哈希全部一致时，旧向量才可安全复用。
def _valid_cached_vector(
    vector: Sequence[float],
    *,
    dimensions: int,
) -> bool:
    """验证缓存向量长度等于 dimensions，且每个值可转为有限 float。"""
    return (
        len(vector) == dimensions
        and any(float(value) != 0.0 for value in vector)
        and all(math.isfinite(float(value)) for value in vector)
    )


def _embedding_provider_id(provider: EmbeddingProvider) -> str:
    """Return provider provenance, preserving compatibility with test fakes."""
    value = getattr(provider, "provider_id", "openai_compatible")
    return str(value)


def _load_cached_vectors(
    *,
    vector_store: VectorStore,
    current: Optional[RagIndex],
    chunks: Sequence[RagChunk],
    provider: EmbeddingProvider,
    force_refresh: bool,
) -> dict[str, tuple[float, ...]]:
    """在来源/模型/维度/分块版本一致时，从当前集合批量读取可复用向量。

    ``force_refresh=True`` 或任一协议字段不匹配直接返回空字典，保证不会跨配置
    误用向量。返回值只包含当前 chunks 中仍存在且维度有效的 ID。
    """
    if (
        force_refresh
        or current is None
        or current.embedding_provider != _embedding_provider_id(provider)
        or current.embedding_model != provider.model
        or current.embedding_dimensions != provider.dimensions
        or current.chunk_version != CHUNK_FORMAT_VERSION
    ):
        return {}

    cached: dict[str, tuple[float, ...]] = {}
    for offset in range(0, len(chunks), _CACHE_READ_BATCH_SIZE):
        ids = [
            chunk.chunk_id
            for chunk in chunks[offset:offset + _CACHE_READ_BATCH_SIZE]
        ]
        try:
            batch = vector_store.get_embeddings(
                collection_name=current.collection_name,
                ids=ids,
            )
        except Exception:
            logger.warning(
                "Could not reuse vectors from current RAG collection; "
                "missing chunks will be embedded again.",
            )
            return {}
        cached.update(
            {
                chunk_id: vector
                for chunk_id, vector in batch.items()
                if _valid_cached_vector(
                    vector,
                    dimensions=provider.dimensions,
                )
            }
        )
    return cached


def _persist_progress(
    db: Session,
    *,
    job_id: str,
    current: int,
    total: int,
) -> None:
    """用短事务更新索引任务进度。

    ``db`` 是当前会话，``job_id`` 定位任务，``current/total`` 是要写入的绝对值。
    只更新 running 任务并验证边界；SQLite 写锁执行有界退避，失败继续抛出。
    """
    for attempt in range(1, ANALYSIS_JOB_SQLITE_MAX_ATTEMPTS + 1):
        try:
            updated = (
                db.query(AnalysisJob)
                .filter(
                    AnalysisJob.id == job_id,
                    AnalysisJob.job_type == "rag_index",
                    AnalysisJob.status == "running",
                )
                .update(
                    {
                        AnalysisJob.progress_current: current,
                        AnalysisJob.progress_total: total,
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if updated != 1:
                raise _RagIndexJobCancelled()
            return
        except OperationalError as error:
            db.rollback()
            if (
                not is_sqlite_lock_error(error)
                or attempt == ANALYSIS_JOB_SQLITE_MAX_ATTEMPTS
            ):
                raise
            time.sleep(
                ANALYSIS_JOB_SQLITE_RETRY_BASE_SECONDS
                * (2 ** (attempt - 1))
            )


def _collection_name(
    *,
    novel_id: int,
    source_hash: str,
    job_id: str,
) -> str:
    """用小说 ID、来源哈希前缀和任务 UUID 生成合法且不会冲突的集合名。"""
    return (
        f"novelmind_n{novel_id}_{source_hash[:12]}_"
        f"{job_id.replace('-', '')[:12]}"
    )


# 主库指针和 chunk 定位在同一事务中切换，避免读到半套索引。
def _replace_current_snapshot(
    db: Session,
    *,
    job_id: str,
    novel_id: int,
    source_hash: str,
    collection_name: str,
    chunks: Sequence[RagChunk],
    provider: EmbeddingProvider,
) -> Optional[str]:
    """Atomically switch SQLite provenance after vectors are complete."""
    current = (
        db.query(RagIndex)
        .filter(RagIndex.novel_id == novel_id)
        .one_or_none()
    )
    old_collection = current.collection_name if current else None
    now = utc_now()
    if current is None:
        current = RagIndex(
            novel_id=novel_id,
            source_hash=source_hash,
            collection_name=collection_name,
            embedding_provider=_embedding_provider_id(provider),
            embedding_model=provider.model,
            embedding_dimensions=provider.dimensions,
            chunk_version=CHUNK_FORMAT_VERSION,
            chunk_count=len(chunks),
            created_at=now,
            updated_at=now,
        )
        db.add(current)
        db.flush()
    else:
        (
            db.query(RagChunkRecord)
            .filter(RagChunkRecord.rag_index_id == current.id)
            .delete(synchronize_session=False)
        )

    current.source_hash = source_hash
    current.collection_name = collection_name
    current.embedding_provider = _embedding_provider_id(provider)
    current.embedding_model = provider.model
    current.embedding_dimensions = provider.dimensions
    current.chunk_version = CHUNK_FORMAT_VERSION
    current.chunk_count = len(chunks)
    current.updated_at = now
    db.add_all(
        [
            RagChunkRecord(
                rag_index_id=current.id,
                chapter_id=chunk.chapter_id,
                chunk_id=chunk.chunk_id,
                chapter_index=chunk.chapter_index,
                chunk_index=chunk.chunk_index,
                start_char=chunk.start_char,
                end_char=chunk.end_char,
                content_hash=chunk.content_hash,
            )
            for chunk in chunks
        ]
    )
    updated_job = (
        db.query(AnalysisJob)
        .filter(
            AnalysisJob.id == job_id,
            AnalysisJob.job_type == "rag_index",
            AnalysisJob.status == "running",
        )
        .update(
            {
                AnalysisJob.status: "succeeded",
                AnalysisJob.progress_current: len(chunks),
                AnalysisJob.progress_total: len(chunks),
                AnalysisJob.completed_at: now,
            },
            synchronize_session=False,
        )
    )
    if updated_job != 1:
        raise _RagIndexJobCancelled()
    db.commit()
    return old_collection


def _mark_unchanged_succeeded(
    db: Session,
    *,
    job_id: str,
    chunk_count: int,
) -> None:
    """当现有索引完全当前时，不调用 embedding，直接把任务标记 succeeded。"""
    updated = (
        db.query(AnalysisJob)
        .filter(
            AnalysisJob.id == job_id,
            AnalysisJob.job_type == "rag_index",
            AnalysisJob.status == "running",
        )
        .update(
            {
                AnalysisJob.status: "succeeded",
                AnalysisJob.progress_current: chunk_count,
                AnalysisJob.progress_total: chunk_count,
                AnalysisJob.completed_at: utc_now(),
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if updated != 1:
        raise _RagIndexJobCancelled()


# 重复派发也只有一个执行者能把 queued 原子 claim 为 running。
def run_rag_index_job(
    job_id: str,
    provider: EmbeddingProvider,
    vector_store: VectorStore,
    session_factory: Callable[[], Session],
) -> None:
    """Build a versioned index and preserve the old snapshot on failure."""
    new_collection: Optional[str] = None
    novel_id: Optional[int] = None
    try:
        with session_factory() as db:
            try:
                if not _claim_job(db, job_id):
                    return
            except Exception as error:
                db.rollback()
                is_lock = (
                    isinstance(error, OperationalError)
                    and is_sqlite_lock_error(error)
                )
                message = (
                    "RAG 索引任务无法获取本地数据库写锁，请重试。"
                    if is_lock
                    else "RAG 索引任务启动失败，请查看后端日志后重试。"
                )
                mark_analysis_job_failed_with_sqlite_lock_retry(
                    session_factory,
                    job_id,
                    "rag_index",
                    message,
                    expected_status="queued",
                    failure_context="claim failure",
                )
                if not is_lock:
                    logger.exception("Could not claim RAG index job.")
                return

            job = db.get(AnalysisJob, job_id)
            if job is None:
                return
            novel_id = job.novel_id
            force_refresh = bool(job.force_refresh)
            chapters = (
                db.query(Chapter)
                .filter(Chapter.novel_id == novel_id)
                .order_by(Chapter.chapter_index)
                .all()
            )
            chunks = build_rag_chunks(
                novel_id=novel_id,
                chapters=chapters,
                embedding_model=provider.model,
                dimensions=provider.dimensions,
            )
            if not chunks:
                raise RagIndexBuildError(
                    "小说没有可建立 RAG 索引的正文。"
                )
            source_hash = rag_source_hash(
                chunks,
                embedding_model=provider.model,
                dimensions=provider.dimensions,
            )
            current = (
                db.query(RagIndex)
                .filter(RagIndex.novel_id == novel_id)
                .one_or_none()
            )
            if (
                not force_refresh
                and current is not None
                and current.source_hash == source_hash
                and current.embedding_provider
                == _embedding_provider_id(provider)
                and current.embedding_model == provider.model
                and current.embedding_dimensions == provider.dimensions
                and current.chunk_version == CHUNK_FORMAT_VERSION
                and current.chunk_count == len(chunks)
            ):
                try:
                    verified_count = vector_store.collection_count(
                        current.collection_name
                    )
                except Exception:
                    verified_count = -1
                if verified_count == len(chunks):
                    _mark_unchanged_succeeded(
                        db,
                        job_id=job_id,
                        chunk_count=len(chunks),
                    )
                    return

            cached = _load_cached_vectors(
                vector_store=vector_store,
                current=current,
                chunks=chunks,
                provider=provider,
                force_refresh=force_refresh,
            )
            new_collection = _collection_name(
                novel_id=novel_id,
                source_hash=source_hash,
                job_id=job_id,
            )
            vector_store.create_collection(new_collection)
            _persist_progress(
                db,
                job_id=job_id,
                current=0,
                total=len(chunks),
            )

            processed = 0
            for offset in range(
                0,
                len(chunks),
                MAX_EMBEDDING_BATCH_SIZE,
            ):
                _ensure_job_running(
                    session_factory,
                    job_id=job_id,
                    novel_id=novel_id,
                )
                batch = chunks[
                    offset:offset + MAX_EMBEDDING_BATCH_SIZE
                ]
                missing = [
                    chunk
                    for chunk in batch
                    if chunk.chunk_id not in cached
                ]
                generated: dict[str, tuple[float, ...]] = {}
                if missing:
                    result = provider.embed(
                        [chunk.embedding_text for chunk in missing],
                        before_attempt=lambda: _ensure_job_running(
                            session_factory,
                            job_id=job_id,
                            novel_id=novel_id,
                        ),
                    )
                    generated = {
                        chunk.chunk_id: vector
                        for chunk, vector in zip(
                            missing,
                            result.vectors,
                            strict=True,
                        )
                    }
                vectors = [
                    cached.get(chunk.chunk_id)
                    or generated[chunk.chunk_id]
                    for chunk in batch
                ]
                vector_store.upsert(
                    collection_name=new_collection,
                    ids=[chunk.chunk_id for chunk in batch],
                    embeddings=vectors,
                    metadatas=[
                        {
                            "chapter_id": chunk.chapter_id,
                            "chapter_index": chunk.chapter_index,
                            "chunk_index": chunk.chunk_index,
                            "start_char": chunk.start_char,
                            "end_char": chunk.end_char,
                            "content_hash": chunk.content_hash,
                        }
                        for chunk in batch
                    ],
                )
                processed += len(batch)
                _persist_progress(
                    db,
                    job_id=job_id,
                    current=processed,
                    total=len(chunks),
                )

            if vector_store.collection_count(new_collection) != len(chunks):
                raise RagIndexBuildError(
                    "本地向量集合校验失败，旧索引已保留。"
                )
            _ensure_job_running(
                session_factory,
                job_id=job_id,
                novel_id=novel_id,
            )
            old_collection = _replace_current_snapshot(
                db,
                job_id=job_id,
                novel_id=novel_id,
                source_hash=source_hash,
                collection_name=new_collection,
                chunks=chunks,
                provider=provider,
            )
            committed_collection = new_collection
            new_collection = None
            if old_collection != committed_collection:
                _safe_delete_collection(vector_store, old_collection)
    except _RagIndexJobCancelled:
        logger.info(
            "RAG index job %s stopped because its state or source "
            "was removed.",
            job_id,
        )
    except Exception as error:
        if not isinstance(
            error,
            (EmbeddingProviderError, RagIndexBuildError),
        ):
            logger.exception("Unexpected RAG index job failure.")
        error_message = (
            str(error)
            if isinstance(
                error,
                (EmbeddingProviderError, RagIndexBuildError),
            )
            else "RAG 索引任务执行失败，请查看后端日志后重试。"
        )[:1_000]
        mark_analysis_job_failed_with_sqlite_lock_retry(
            session_factory,
            job_id,
            "rag_index",
            error_message,
            expected_status="running",
            failure_context="execution failure",
        )
    finally:
        _safe_delete_collection(vector_store, new_collection)
        _close_embedding_provider(provider)
