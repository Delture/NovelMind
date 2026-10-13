"""只发送缺失块；向量先写入新集合，数量验证成功后才在主 SQLite 事务中替换 RagIndex/RagChunkRecord。
成功批次由构建检查点保留，暂时失败后可只续嵌缺失 chunk IDs。
"""

import logging
import math
from collections.abc import Callable, Sequence
from typing import Optional, Protocol

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.app.analyzer.jobs import (
    claim_analysis_job,
    is_sqlite_lock_error,
    mark_analysis_job_failed_with_sqlite_lock_retry,
)
from backend.app.core.sqlite_retry import retry_sqlite
from backend.app.core.time import utc_now
from backend.app.models.analysis import AnalysisJob
from backend.app.models.novel import Chapter, Novel
from backend.app.models.rag import RagBuildCheckpoint, RagChunkRecord, RagIndex
from backend.app.rag.chunking import (
    CHUNK_FORMAT_VERSION,
    RagChunk,
    build_rag_chunks,
    rag_source_hash,
)
from backend.app.rag.embedding import (
    EmbeddingProvider,
    EmbeddingProviderError,
    EmbeddingUnavailableError,
    MAX_EMBEDDING_BATCH_SIZE,
)
from backend.app.rag.vector_store import VectorSearchHit


logger = logging.getLogger(__name__)
_CACHE_READ_BATCH_SIZE = 500

def _is_transient_vector_store_error(error: BaseException) -> bool:
    """Vector store SQLite locks should keep the checkpoint for resume."""
    if isinstance(error, OperationalError) and is_sqlite_lock_error(error):
        return True
    message = str(error).casefold()
    return "locked" in message or "busy" in message



class RagIndexBuildError(RuntimeError):
    pass


class _RagIndexJobCancelled(RuntimeError):
    """Internal signal that a deleted or terminalized job must stop."""


class VectorStore(Protocol):
    def create_collection(self, name: str) -> None:
        ...

    def delete_collection(self, name: str) -> None:
        ...

    def collection_count(self, name: str) -> int:
        ...

    def upsert(
        self,
        *,
        collection_name: str,
        ids: Sequence[str],
        embeddings: Sequence[Sequence[float]],
        metadatas: Sequence[dict[str, object]],
    ) -> None:
        """ids、embeddings、metadatas 三个序列必须等长；已有 ID 应更新而不是重复插入。"""
        ...

    def get_embeddings(
        self,
        *,
        collection_name: str,
        ids: Sequence[str],
    ) -> dict[str, tuple[float, ...]]:
        ...

    def query(
        self,
        *,
        collection_name: str,
        embedding: Sequence[float],
        limit: int,
    ) -> list[VectorSearchHit]:
        ...


def _close_embedding_provider(provider: EmbeddingProvider) -> None:
    try:
        provider.close()
    except Exception:
        logger.exception("Could not close the embedding provider.")


def _safe_delete_collection(
    vector_store: VectorStore,
    collection_name: Optional[str],
) -> None:
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
    """已删除小说或任务不再继续计费。"""
    if not _job_is_running(
        session_factory,
        job_id=job_id,
        novel_id=novel_id,
    ):
        raise _RagIndexJobCancelled()


def _claim_job(db: Session, job_id: str) -> bool:
    return claim_analysis_job(db, job_id, "rag_index", utc_now()) == 1


def _valid_cached_vector(
    vector: Sequence[float],
    *,
    dimensions: int,
) -> bool:
    return (
        len(vector) == dimensions
        and any(float(value) != 0.0 for value in vector)
        and all(math.isfinite(float(value)) for value in vector)
    )


def _embedding_provider_id(provider: EmbeddingProvider) -> str:
    value = getattr(provider, "provider_id", "openai_compatible")
    return str(value)


def protocol_matches(
    *,
    embedding_provider: str,
    embedding_model: str,
    embedding_dimensions: int,
    chunk_version: str,
    source_hash: str,
    provider: EmbeddingProvider,
    expected_source_hash: str,
) -> bool:
    return (
        embedding_provider == _embedding_provider_id(provider)
        and embedding_model == provider.model
        and embedding_dimensions == provider.dimensions
        and chunk_version == CHUNK_FORMAT_VERSION
        and source_hash == expected_source_hash
    )


def load_valid_vectors_by_ids(
    *,
    vector_store: VectorStore,
    collection_name: str,
    chunk_ids: Sequence[str],
    dimensions: int,
) -> dict[str, tuple[float, ...]]:
    """Only IDs that exist on disk with valid vectors count as completed."""
    cached: dict[str, tuple[float, ...]] = {}
    for offset in range(0, len(chunk_ids), _CACHE_READ_BATCH_SIZE):
        batch_ids = list(chunk_ids[offset:offset + _CACHE_READ_BATCH_SIZE])
        batch = vector_store.get_embeddings(
            collection_name=collection_name,
            ids=batch_ids,
        )
        cached.update(
            {
                chunk_id: vector
                for chunk_id, vector in batch.items()
                if _valid_cached_vector(vector, dimensions=dimensions)
            }
        )
    return cached


def _load_cached_vectors(
    *,
    vector_store: VectorStore,
    current: Optional[RagIndex],
    chunks: Sequence[RagChunk],
    provider: EmbeddingProvider,
    force_refresh: bool,
) -> dict[str, tuple[float, ...]]:
    """force_refresh=True 或任一协议字段不匹配直接返回空字典，保证不会跨配置误用向量。"""
    if (
        force_refresh
        or current is None
        or current.embedding_provider != _embedding_provider_id(provider)
        or current.embedding_model != provider.model
        or current.embedding_dimensions != provider.dimensions
        or current.chunk_version != CHUNK_FORMAT_VERSION
    ):
        return {}

    try:
        return load_valid_vectors_by_ids(
            vector_store=vector_store,
            collection_name=current.collection_name,
            chunk_ids=[chunk.chunk_id for chunk in chunks],
            dimensions=provider.dimensions,
        )
    except Exception:
        logger.warning(
            "Could not reuse vectors from current RAG collection; "
            "missing chunks will be embedded again.",
        )
        return {}


def _persist_progress(
    db: Session,
    *,
    job_id: str,
    current: int,
    total: int,
) -> None:
    """只更新 running 任务并验证边界。"""

    def write_status():
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
        except OperationalError:
            db.rollback()
            raise

    return retry_sqlite(write_status)


def _collection_name(
    *,
    novel_id: int,
    source_hash: str,
    job_id: str,
) -> str:
    return (
        f"novelmind_n{novel_id}_{source_hash[:12]}_"
        f"{job_id.replace('-', '')[:12]}"
    )


def _discard_checkpoint(
    db: Session,
    vector_store: VectorStore,
    checkpoint: Optional[RagBuildCheckpoint],
) -> None:
    if checkpoint is None:
        return
    collection_name = checkpoint.collection_name
    db.delete(checkpoint)
    db.commit()
    _safe_delete_collection(vector_store, collection_name)


def _ensure_collection(vector_store: VectorStore, name: str) -> None:
    try:
        vector_store.collection_count(name)
    except Exception:
        vector_store.create_collection(name)


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
    (
        db.query(RagBuildCheckpoint)
        .filter(RagBuildCheckpoint.novel_id == novel_id)
        .delete(synchronize_session=False)
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
    # None until a checkpoint row is committed; then finally must not delete it
    # on transient failure.
    registered_collection: Optional[str] = None
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
            checkpoint = (
                db.query(RagBuildCheckpoint)
                .filter(RagBuildCheckpoint.novel_id == novel_id)
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
                    if checkpoint is not None:
                        _discard_checkpoint(db, vector_store, checkpoint)
                    _mark_unchanged_succeeded(
                        db,
                        job_id=job_id,
                        chunk_count=len(chunks),
                    )
                    return

            checkpoint_compatible = (
                checkpoint is not None
                and not force_refresh
                and protocol_matches(
                    embedding_provider=checkpoint.embedding_provider,
                    embedding_model=checkpoint.embedding_model,
                    embedding_dimensions=checkpoint.embedding_dimensions,
                    chunk_version=checkpoint.chunk_version,
                    source_hash=checkpoint.source_hash,
                    provider=provider,
                    expected_source_hash=source_hash,
                )
            )
            if checkpoint is not None and not checkpoint_compatible:
                _discard_checkpoint(db, vector_store, checkpoint)
                checkpoint = None

            completed: dict[str, tuple[float, ...]] = {}
            if checkpoint_compatible and checkpoint is not None:
                registered_collection = checkpoint.collection_name
                try:
                    completed = load_valid_vectors_by_ids(
                        vector_store=vector_store,
                        collection_name=registered_collection,
                        chunk_ids=[chunk.chunk_id for chunk in chunks],
                        dimensions=provider.dimensions,
                    )
                except Exception as error:
                    if _is_transient_vector_store_error(error):
                        raise EmbeddingUnavailableError() from error
                    raise RagIndexBuildError(
                        "无法读取构建检查点中的向量，请强制重建索引。"
                    ) from error
            else:
                registered_collection = _collection_name(
                    novel_id=novel_id,
                    source_hash=source_hash,
                    job_id=job_id,
                )
                checkpoint = RagBuildCheckpoint(
                    novel_id=novel_id,
                    source_hash=source_hash,
                    collection_name=registered_collection,
                    embedding_provider=_embedding_provider_id(provider),
                    embedding_model=provider.model,
                    embedding_dimensions=provider.dimensions,
                    chunk_version=CHUNK_FORMAT_VERSION,
                )
                db.add(checkpoint)
                db.commit()

            _ensure_collection(vector_store, registered_collection)

            cached = _load_cached_vectors(
                vector_store=vector_store,
                current=current,
                chunks=chunks,
                provider=provider,
                force_refresh=force_refresh,
            )
            _persist_progress(
                db,
                job_id=job_id,
                current=len(completed),
                total=len(chunks),
            )

            processed = len(completed)
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
                pending = [
                    chunk
                    for chunk in batch
                    if chunk.chunk_id not in completed
                ]
                if not pending:
                    processed = max(processed, offset + len(batch))
                    _persist_progress(
                        db,
                        job_id=job_id,
                        current=processed,
                        total=len(chunks),
                    )
                    continue

                missing = [
                    chunk
                    for chunk in pending
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
                    completed.get(chunk.chunk_id)
                    or cached.get(chunk.chunk_id)
                    or generated[chunk.chunk_id]
                    for chunk in pending
                ]
                vector_store.upsert(
                    collection_name=registered_collection,
                    ids=[chunk.chunk_id for chunk in pending],
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
                        for chunk in pending
                    ],
                )
                for chunk, vector in zip(pending, vectors, strict=True):
                    completed[chunk.chunk_id] = tuple(vector)
                processed = len(completed)
                _persist_progress(
                    db,
                    job_id=job_id,
                    current=processed,
                    total=len(chunks),
                )

            expected_ids = [chunk.chunk_id for chunk in chunks]
            try:
                on_disk = load_valid_vectors_by_ids(
                    vector_store=vector_store,
                    collection_name=registered_collection,
                    chunk_ids=expected_ids,
                    dimensions=provider.dimensions,
                )
            except Exception as error:
                if _is_transient_vector_store_error(error):
                    raise EmbeddingUnavailableError() from error
                raise RagIndexBuildError(
                    "本地向量集合校验失败，旧索引已保留。"
                ) from error
            if set(on_disk) != set(expected_ids):
                raise RagIndexBuildError(
                    "本地向量集合校验失败，旧索引已保留。"
                )
            if vector_store.collection_count(registered_collection) != len(
                chunks
            ):
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
                collection_name=registered_collection,
                chunks=chunks,
                provider=provider,
            )
            committed_collection = registered_collection
            registered_collection = None
            if old_collection != committed_collection:
                _safe_delete_collection(vector_store, old_collection)
    except _RagIndexJobCancelled:
        logger.info(
            "RAG index job %s stopped because its state or source "
            "was removed.",
            job_id,
        )
        if registered_collection is not None and novel_id is not None:
            with session_factory() as db:
                checkpoint = (
                    db.query(RagBuildCheckpoint)
                    .filter(RagBuildCheckpoint.novel_id == novel_id)
                    .one_or_none()
                )
                _discard_checkpoint(db, vector_store, checkpoint)
            registered_collection = None
    except Exception as error:
        if not isinstance(
            error,
            (EmbeddingProviderError, RagIndexBuildError),
        ):
            logger.exception("Unexpected RAG index job failure.")
        retryable = (
            isinstance(error, EmbeddingProviderError)
            and bool(error.retryable)
        )
        if (
            not retryable
            and registered_collection is not None
            and novel_id is not None
        ):
            with session_factory() as db:
                checkpoint = (
                    db.query(RagBuildCheckpoint)
                    .filter(RagBuildCheckpoint.novel_id == novel_id)
                    .one_or_none()
                )
                _discard_checkpoint(db, vector_store, checkpoint)
            registered_collection = None
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
        _close_embedding_provider(provider)
