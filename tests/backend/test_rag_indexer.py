from tests.support.embedding_fakes import FakeProvider
"""Tests for atomic, cache-aware background RAG indexing."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.database import create_db_engine, init_db
from backend.app.models.analysis import AnalysisJob
from backend.app.models.novel import Chapter, Novel
from backend.app.models.rag import RagBuildCheckpoint, RagChunkRecord, RagIndex
from backend.app.rag.embedding import EmbeddingUnavailableError
from backend.app.rag.indexer import run_rag_index_job
from backend.app.rag.embedding import MAX_EMBEDDING_BATCH_SIZE
from backend.app.rag.vector_store import VectorSearchHit


@dataclass


class FakeVectorStore:
    def __init__(self) -> None:
        self.collections: dict[
            str,
            dict[str, tuple[tuple[float, ...], dict[str, object]]],
        ] = {}
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.fail_upsert = False

    def create_collection(self, name: str) -> None:
        if name in self.collections:
            raise RuntimeError("duplicate collection")
        self.collections[name] = {}
        self.created.append(name)

    def delete_collection(self, name: str) -> None:
        self.collections.pop(name, None)
        self.deleted.append(name)

    def collection_count(self, name: str) -> int:
        return len(self.collections[name])

    def upsert(
        self,
        *,
        collection_name: str,
        ids: Sequence[str],
        embeddings: Sequence[Sequence[float]],
        metadatas: Sequence[dict[str, object]],
    ) -> None:
        if self.fail_upsert:
            raise RuntimeError("sensitive vector failure")
        for chunk_id, vector, metadata in zip(
            ids,
            embeddings,
            metadatas,
            strict=True,
        ):
            self.collections[collection_name][chunk_id] = (
                tuple(vector),
                metadata,
            )

    def get_embeddings(
        self,
        *,
        collection_name: str,
        ids: Sequence[str],
    ) -> dict[str, tuple[float, ...]]:
        collection = self.collections[collection_name]
        return {
            chunk_id: collection[chunk_id][0]
            for chunk_id in ids
            if chunk_id in collection
        }

    def query(
        self,
        *,
        collection_name: str,
        embedding: Sequence[float],
        limit: int,
    ) -> list[VectorSearchHit]:
        return []


def make_session_factory():
    engine = create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    init_db(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def seed_job(
    factory,
    *,
    force: bool = False,
) -> tuple[int, str]:
    with factory() as db:
        novel = Novel(
            filename="test.txt",
            encoding="utf-8",
            chapter_count=2,
            chapters=[
                Chapter(
                    chapter_index=1,
                    title="第一章",
                    content="甲" * 100,
                    character_count=100,
                ),
                Chapter(
                    chapter_index=2,
                    title="第二章",
                    content="乙" * 100,
                    character_count=100,
                ),
            ],
        )
        db.add(novel)
        db.flush()
        job = AnalysisJob(
            novel_id=novel.id,
            job_type="rag_index",
            force_refresh=force,
            status="queued",
            progress_current=0,
            progress_total=0,
        )
        db.add(job)
        db.commit()
        return novel.id, job.id


def add_job(factory, novel_id: int, *, force: bool = False) -> str:
    with factory() as db:
        job = AnalysisJob(
            novel_id=novel_id,
            job_type="rag_index",
            force_refresh=force,
            status="queued",
            progress_current=0,
            progress_total=0,
        )
        db.add(job)
        db.commit()
        return job.id


def test_index_job_builds_verified_snapshot_and_closes_provider() -> None:
    engine, factory = make_session_factory()
    novel_id, job_id = seed_job(factory)
    provider = FakeProvider()
    store = FakeVectorStore()
    try:
        run_rag_index_job(job_id, provider, store, factory)
        with factory() as db:
            job = db.get(AnalysisJob, job_id)
            index = db.scalar(
                select(RagIndex).where(RagIndex.novel_id == novel_id)
            )
            chunks = db.scalars(
                select(RagChunkRecord)
                .where(RagChunkRecord.rag_index_id == index.id)
                .order_by(RagChunkRecord.chapter_index)
            ).all()
            assert job.status == "succeeded"
            assert job.progress_current == job.progress_total == 2
            assert index.chunk_count == 2
            assert index.embedding_dimensions == 1_024
            assert len(chunks) == 2
            assert index.collection_name in store.collections
            assert len(store.collections[index.collection_name]) == 2
            assert all(
                item[1].get("content_hash")
                for item in store.collections[
                    index.collection_name
                ].values()
            )
    finally:
        engine.dispose()
    assert len(provider.calls) == 1
    assert provider.closed is True


def test_unchanged_source_reuses_complete_snapshot_without_api_call() -> None:
    engine, factory = make_session_factory()
    novel_id, first_job = seed_job(factory)
    store = FakeVectorStore()
    try:
        run_rag_index_job(first_job, FakeProvider(), store, factory)
        with factory() as db:
            original_collection = db.scalar(
                select(RagIndex.collection_name)
                .where(RagIndex.novel_id == novel_id)
            )
        second_job = add_job(factory, novel_id)
        provider = FakeProvider()
        run_rag_index_job(second_job, provider, store, factory)
        with factory() as db:
            job = db.get(AnalysisJob, second_job)
            current_collection = db.scalar(
                select(RagIndex.collection_name)
                .where(RagIndex.novel_id == novel_id)
            )
        assert job.status == "succeeded"
        assert current_collection == original_collection
        assert provider.calls == []
        assert len(store.created) == 1
        assert provider.closed is True
    finally:
        engine.dispose()

def test_changed_source_reuses_only_unchanged_chunk_vectors() -> None:
    engine, factory = make_session_factory()
    novel_id, first_job = seed_job(factory)
    store = FakeVectorStore()
    try:
        run_rag_index_job(first_job, FakeProvider(), store, factory)
        with factory() as db:
            chapter = db.scalar(
                select(Chapter).where(
                    Chapter.novel_id == novel_id,
                    Chapter.chapter_index == 2,
                )
            )
            chapter.content = "丙" * 100
            chapter.character_count = 100
            db.commit()
        second_job = add_job(factory, novel_id)
        provider = FakeProvider()
        run_rag_index_job(second_job, provider, store, factory)
        assert len(provider.calls) == 1
        assert len(provider.calls[0]) == 1
        assert "丙" * 20 in provider.calls[0][0]
        assert len(store.created) == 2
        assert store.created[0] in store.deleted
        assert store.created[0] not in store.collections
        assert store.created[1] in store.collections
    finally:
        engine.dispose()


def test_transient_embedding_failure_preserves_old_snapshot_and_checkpoint() -> None:
    engine, factory = make_session_factory()
    novel_id, first_job = seed_job(factory)
    store = FakeVectorStore()
    try:
        run_rag_index_job(first_job, FakeProvider(), store, factory)
        with factory() as db:
            original = db.scalar(
                select(RagIndex).where(RagIndex.novel_id == novel_id)
            )
            old_collection = original.collection_name
            old_hash = original.source_hash
            chapter = db.scalar(
                select(Chapter).where(
                    Chapter.novel_id == novel_id,
                    Chapter.chapter_index == 2,
                )
            )
            chapter.content = "变化后的正文"
            chapter.character_count = len(chapter.content)
            db.commit()
        second_job = add_job(factory, novel_id)
        provider = FakeProvider(fail=True)
        run_rag_index_job(second_job, provider, store, factory)
        with factory() as db:
            job = db.get(AnalysisJob, second_job)
            current = db.scalar(
                select(RagIndex).where(RagIndex.novel_id == novel_id)
            )
            checkpoint = db.scalar(
                select(RagBuildCheckpoint).where(
                    RagBuildCheckpoint.novel_id == novel_id
                )
            )
            assert job.status == "failed"
            assert "暂时不可用" in job.error_message
            assert current.collection_name == old_collection
            assert current.source_hash == old_hash
            assert checkpoint is not None
            assert checkpoint.collection_name == store.created[-1]
        assert old_collection in store.collections
        assert store.created[-1] in store.collections
        assert provider.closed is True
    finally:
        engine.dispose()


def test_vector_failure_uses_safe_error_and_preserves_old_snapshot() -> None:
    engine, factory = make_session_factory()
    novel_id, first_job = seed_job(factory)
    store = FakeVectorStore()
    try:
        run_rag_index_job(first_job, FakeProvider(), store, factory)
        with factory() as db:
            old_collection = db.scalar(
                select(RagIndex.collection_name)
                .where(RagIndex.novel_id == novel_id)
            )
            chapter = db.scalar(
                select(Chapter).where(
                    Chapter.novel_id == novel_id,
                    Chapter.chapter_index == 1,
                )
            )
            chapter.content = "新正文"
            chapter.character_count = 3
            db.commit()
        job_id = add_job(factory, novel_id)
        store.fail_upsert = True
        run_rag_index_job(job_id, FakeProvider(), store, factory)
        with factory() as db:
            job = db.get(AnalysisJob, job_id)
            current = db.scalar(
                select(RagIndex).where(RagIndex.novel_id == novel_id)
            )
            assert job.status == "failed"
            assert job.error_message == (
                "RAG 索引任务执行失败，请查看后端日志后重试。"
            )
            assert current.collection_name == old_collection
        assert old_collection in store.collections
    finally:
        engine.dispose()


@dataclass
class FailAfterProvider(FakeProvider):
    fail_after: int = 3

    def embed(self, texts, *, before_attempt=None):
        if before_attempt is not None:
            before_attempt()
        self.calls.append(list(texts))
        if len(self.calls) >= self.fail_after:
            raise EmbeddingUnavailableError()
        vectors = tuple(
            tuple([float(index + 1)] * self.dimensions)
            for index, _ in enumerate(texts)
        )
        from backend.app.rag.embedding import EmbeddingBatchResult
        return EmbeddingBatchResult(
            vectors=vectors,
            model=self.model,
            prompt_tokens=len(texts),
            request_id="fake",
        )


def seed_many_chapters(factory, chapter_count: int = 25):
    with factory() as db:
        novel = Novel(
            filename="big.txt",
            encoding="utf-8",
            chapter_count=chapter_count,
            chapters=[
                Chapter(
                    chapter_index=index,
                    title=f"第{index}章",
                    content=chr(0x4E00 + index) * 100,
                    character_count=100,
                )
                for index in range(1, chapter_count + 1)
            ],
        )
        db.add(novel)
        db.flush()
        job = AnalysisJob(
            novel_id=novel.id,
            job_type="rag_index",
            force_refresh=False,
            status="queued",
            progress_current=0,
            progress_total=0,
        )
        db.add(job)
        db.commit()
        return novel.id, job.id


def test_resume_after_two_batches_embeds_only_missing_chunks() -> None:
    engine, factory = make_session_factory()
    novel_id, first_job = seed_many_chapters(factory, 25)
    store = FakeVectorStore()
    try:
        run_rag_index_job(
            first_job,
            FailAfterProvider(fail_after=3),
            store,
            factory,
        )
        with factory() as db:
            checkpoint = db.scalar(
                select(RagBuildCheckpoint).where(
                    RagBuildCheckpoint.novel_id == novel_id
                )
            )
            assert checkpoint is not None
            temp = checkpoint.collection_name
            assert db.scalar(
                select(RagIndex).where(RagIndex.novel_id == novel_id)
            ) is None
        assert len(store.collections[temp]) == 2 * MAX_EMBEDDING_BATCH_SIZE
        second_job = add_job(factory, novel_id)
        provider = FakeProvider()
        run_rag_index_job(second_job, provider, store, factory)
        with factory() as db:
            job = db.get(AnalysisJob, second_job)
            index = db.scalar(
                select(RagIndex).where(RagIndex.novel_id == novel_id)
            )
            assert job.status == "succeeded"
            assert index is not None
            assert index.chunk_count == 25
            assert db.scalar(
                select(RagBuildCheckpoint).where(
                    RagBuildCheckpoint.novel_id == novel_id
                )
            ) is None
        assert sum(len(batch) for batch in provider.calls) == 5
        assert provider.closed is True
    finally:
        engine.dispose()


def test_partial_checkpoint_is_never_ready() -> None:
    engine, factory = make_session_factory()
    novel_id, job_id = seed_job(factory)
    store = FakeVectorStore()
    try:
        run_rag_index_job(job_id, FakeProvider(fail=True), store, factory)
        with factory() as db:
            assert db.scalar(
                select(RagIndex).where(RagIndex.novel_id == novel_id)
            ) is None
            assert db.scalars(select(RagChunkRecord)).all() == []
            checkpoint = db.scalar(
                select(RagBuildCheckpoint).where(
                    RagBuildCheckpoint.novel_id == novel_id
                )
            )
            assert checkpoint is not None
    finally:
        engine.dispose()


def test_checkpoint_invalidated_on_source_change() -> None:
    engine, factory = make_session_factory()
    novel_id, first_job = seed_job(factory)
    store = FakeVectorStore()
    try:
        run_rag_index_job(first_job, FakeProvider(fail=True), store, factory)
        with factory() as db:
            old_temp = db.scalar(
                select(RagBuildCheckpoint.collection_name).where(
                    RagBuildCheckpoint.novel_id == novel_id
                )
            )
            chapter = db.scalar(
                select(Chapter).where(
                    Chapter.novel_id == novel_id,
                    Chapter.chapter_index == 1,
                )
            )
            chapter.content = "全新正文内容"
            chapter.character_count = len(chapter.content)
            db.commit()
        second_job = add_job(factory, novel_id)
        run_rag_index_job(second_job, FakeProvider(), store, factory)
        with factory() as db:
            assert db.scalar(
                select(RagBuildCheckpoint).where(
                    RagBuildCheckpoint.novel_id == novel_id
                )
            ) is None
            index = db.scalar(
                select(RagIndex).where(RagIndex.novel_id == novel_id)
            )
            assert index is not None
            assert index.collection_name != old_temp
        assert old_temp not in store.collections
    finally:
        engine.dispose()


def test_resume_uses_disk_ids_not_job_progress() -> None:
    engine, factory = make_session_factory()
    novel_id, first_job = seed_many_chapters(factory, 25)
    store = FakeVectorStore()
    try:
        run_rag_index_job(
            first_job,
            FailAfterProvider(fail_after=3),
            store,
            factory,
        )
        with factory() as db:
            checkpoint = db.scalar(
                select(RagBuildCheckpoint).where(
                    RagBuildCheckpoint.novel_id == novel_id
                )
            )
            temp = checkpoint.collection_name
            # Inflate progress so counters alone would look complete.
            failed = db.get(AnalysisJob, first_job)
            failed.progress_current = 25
            failed.progress_total = 25
            db.commit()
        # Remove one persisted id so disk proof requires re-embed.
        removed_id = next(iter(store.collections[temp]))
        del store.collections[temp][removed_id]
        second_job = add_job(factory, novel_id)
        provider = FakeProvider()
        run_rag_index_job(second_job, provider, store, factory)
        # 20 kept on disk + 1 deleted => exactly 6 missing after batching math:
        # fail_after=3 left 20 vectors; delete 1 => 19 on disk; 25-19=6.
        assert sum(len(batch) for batch in provider.calls) == 6
        with factory() as db:
            assert db.get(AnalysisJob, second_job).status == "succeeded"
            assert db.scalar(
                select(RagIndex.chunk_count).where(
                    RagIndex.novel_id == novel_id
                )
            ) == 25
    finally:
        engine.dispose()


def test_old_ready_index_remains_queryable_during_build() -> None:
    engine, factory = make_session_factory()
    novel_id, first_job = seed_job(factory)
    store = FakeVectorStore()
    try:
        run_rag_index_job(first_job, FakeProvider(), store, factory)
        with factory() as db:
            old = db.scalar(
                select(RagIndex).where(RagIndex.novel_id == novel_id)
            )
            old_name = old.collection_name
            old_hash = old.source_hash
        # Force rebuild same source: discard would start new collection.
        second_job = add_job(factory, novel_id, force=True)
        provider = FakeProvider(fail=True)
        run_rag_index_job(second_job, provider, store, factory)
        with factory() as db:
            current = db.scalar(
                select(RagIndex).where(RagIndex.novel_id == novel_id)
            )
            assert current.collection_name == old_name
            assert current.source_hash == old_hash
        assert old_name in store.collections
        assert store.collection_count(old_name) == 2
    finally:
        engine.dispose()


def test_final_validation_requires_expected_ids_on_disk() -> None:
    engine, factory = make_session_factory()
    novel_id, job_id = seed_job(factory)
    store = FakeVectorStore()
    original_upsert = store.upsert

    def corrupt_upsert(**kwargs):
        original_upsert(**kwargs)
        # After writing expected IDs, inject an unexpected ID and drop one.
        collection = store.collections[kwargs["collection_name"]]
        missing = kwargs["ids"][0]
        collection.pop(missing, None)
        collection["x" * 70] = (
            tuple([1.0] * 1_024),
            {},
        )

    store.upsert = corrupt_upsert  # type: ignore[method-assign]
    try:
        run_rag_index_job(job_id, FakeProvider(), store, factory)
        with factory() as db:
            assert db.get(AnalysisJob, job_id).status == "failed"
            assert db.scalar(
                select(RagIndex).where(RagIndex.novel_id == novel_id)
            ) is None
            # Terminal validation failure discards checkpoint.
            assert db.scalar(
                select(RagBuildCheckpoint).where(
                    RagBuildCheckpoint.novel_id == novel_id
                )
            ) is None
    finally:
        engine.dispose()


def test_checkpoint_invalidated_on_model_change() -> None:
    engine, factory = make_session_factory()
    novel_id, first_job = seed_job(factory)
    store = FakeVectorStore()
    try:
        run_rag_index_job(first_job, FakeProvider(fail=True), store, factory)
        with factory() as db:
            old_temp = db.scalar(
                select(RagBuildCheckpoint.collection_name).where(
                    RagBuildCheckpoint.novel_id == novel_id
                )
            )
        second_job = add_job(factory, novel_id)

        class OtherModelProvider(FakeProvider):
            @property
            def model(self) -> str:
                return "other-embedding-model"

        run_rag_index_job(second_job, OtherModelProvider(), store, factory)
        with factory() as db:
            assert db.scalar(
                select(RagBuildCheckpoint).where(
                    RagBuildCheckpoint.novel_id == novel_id
                )
            ) is None
            index = db.scalar(
                select(RagIndex).where(RagIndex.novel_id == novel_id)
            )
            assert index is not None
            assert index.embedding_model == "other-embedding-model"
            assert index.collection_name != old_temp
        assert old_temp not in store.collections
    finally:
        engine.dispose()


def test_transient_collection_count_lock_keeps_checkpoint() -> None:
    engine, factory = make_session_factory()
    novel_id, job_id = seed_job(factory)
    store = FakeVectorStore()
    original_count = store.collection_count

    def flaky_count(name: str) -> int:
        if name in store.collections and store.collections[name]:
            raise RuntimeError("database is locked")
        return original_count(name)

    try:
        # First succeed creating some vectors via normal path then fail at end.
        calls = {"n": 0}
        original_upsert = store.upsert

        def upsert_then_lock(**kwargs):
            original_upsert(**kwargs)
            calls["n"] += 1
            if calls["n"] >= 1:
                store.collection_count = flaky_count  # type: ignore[method-assign]

        store.upsert = upsert_then_lock  # type: ignore[method-assign]
        run_rag_index_job(job_id, FakeProvider(), store, factory)
        with factory() as db:
            job = db.get(AnalysisJob, job_id)
            checkpoint = db.scalar(
                select(RagBuildCheckpoint).where(
                    RagBuildCheckpoint.novel_id == novel_id
                )
            )
            assert job.status == "failed"
            assert "暂时不可用" in (job.error_message or "")
            assert checkpoint is not None
            assert checkpoint.collection_name in store.collections
    finally:
        engine.dispose()
