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
from backend.app.models.rag import RagChunkRecord, RagIndex
from backend.app.rag.indexer import run_rag_index_job
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


def test_embedding_failure_preserves_old_snapshot_and_cleans_partial() -> None:
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
            assert job.status == "failed"
            assert "暂时不可用" in job.error_message
            assert current.collection_name == old_collection
            assert current.source_hash == old_hash
        assert old_collection in store.collections
        assert store.created[-1] not in store.collections
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
