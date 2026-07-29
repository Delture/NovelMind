"""Tests for evidence-validated external RAG answering."""

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.analyzer.llm import JsonGenerationResult
from backend.app.core.database import create_db_engine, init_db
from backend.app.models.novel import Chapter, Novel
from backend.app.models.rag import RagChunkRecord, RagIndex, RagQuestion
from backend.app.rag.answering import (
    RAG_ANSWER_PROMPT_VERSION,
    RagAnswerPayload,
    fail_interrupted_rag_questions,
    run_rag_answer_job,
)
from backend.app.rag.chunking import (
    CHUNK_FORMAT_VERSION,
    build_rag_chunks,
    rag_source_hash,
)
from backend.app.rag.embedding import EmbeddingBatchResult
from backend.app.rag.vector_store import VectorSearchHit


@dataclass
class FakeEmbeddingProvider:
    closed: bool = False

    def __post_init__(self) -> None:
        self.calls: list[list[str]] = []

    @property
    def model(self) -> str:
        return "text-embedding-v4"

    @property
    def dimensions(self) -> int:
        return 1_024

    def embed(self, texts, *, before_attempt=None):
        if before_attempt:
            before_attempt()
        self.calls.append(list(texts))
        return EmbeddingBatchResult(
            vectors=(tuple([1.0] * 1_024),),
            model=self.model,
            prompt_tokens=5,
            request_id="embed-request",
        )

    def close(self) -> None:
        self.closed = True


class FakeLLMProvider:
    def __init__(self, payload: RagAnswerPayload) -> None:
        self.payload = payload
        self.calls: list[dict] = []
        self.closed = False

    @property
    def model(self) -> str:
        return "deepseek-v4-pro"

    def generate_json(self, **kwargs):
        before_attempt = kwargs.get("before_attempt")
        if before_attempt:
            before_attempt()
        self.calls.append(kwargs)
        return JsonGenerationResult(
            value=self.payload,
            model=self.model,
            prompt_tokens=30,
            completion_tokens=10,
            cache_hit_tokens=0,
            request_id="llm-request",
        )

    def close(self) -> None:
        self.closed = True


class FakeVectorStore:
    def __init__(self, hits: list[VectorSearchHit]) -> None:
        self.hits = hits
        self.calls: list[dict] = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        return self.hits


def make_session_factory():
    engine = create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    init_db(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def seed_question(factory) -> tuple[int, str, str, dict[str, object]]:
    with factory() as db:
        novel = Novel(
            filename="rag.txt",
            encoding="utf-8",
            chapter_count=1,
            chapters=[
                Chapter(
                    chapter_index=1,
                    title="雨夜",
                    content="林舟在旧桥下找到了铜钥匙。",
                    character_count=14,
                )
            ],
        )
        db.add(novel)
        db.flush()
        chunks = build_rag_chunks(
            novel_id=novel.id,
            chapters=novel.chapters,
            embedding_model="text-embedding-v4",
            dimensions=1_024,
        )
        source_hash = rag_source_hash(
            chunks,
            embedding_model="text-embedding-v4",
            dimensions=1_024,
        )
        index = RagIndex(
            novel_id=novel.id,
            source_hash=source_hash,
            collection_name="novelmind_answer_test",
            embedding_provider="aliyun_model_studio",
            embedding_model="text-embedding-v4",
            embedding_dimensions=1_024,
            chunk_version=CHUNK_FORMAT_VERSION,
            chunk_count=1,
        )
        db.add(index)
        db.flush()
        chunk = chunks[0]
        db.add(
            RagChunkRecord(
                rag_index_id=index.id,
                chapter_id=chunk.chapter_id,
                chunk_id=chunk.chunk_id,
                chapter_index=chunk.chapter_index,
                chunk_index=chunk.chunk_index,
                start_char=chunk.start_char,
                end_char=chunk.end_char,
                content_hash=chunk.content_hash,
            )
        )
        question = RagQuestion(
            novel_id=novel.id,
            index_source_hash=source_hash,
            question="铜钥匙在哪里找到？",
            top_k=3,
            status="queued",
            citation_ids=[],
            evidence=[],
            generation_model="deepseek-v4-pro",
            prompt_version=RAG_ANSWER_PROMPT_VERSION,
        )
        db.add(question)
        db.commit()
        metadata = {
            "chapter_id": chunk.chapter_id,
            "chapter_index": chunk.chapter_index,
            "chunk_index": chunk.chunk_index,
            "start_char": chunk.start_char,
            "end_char": chunk.end_char,
            "content_hash": chunk.content_hash,
        }
        return novel.id, question.id, chunk.chunk_id, metadata


def test_rag_answer_persists_only_valid_retrieved_citations() -> None:
    engine, factory = make_session_factory()
    _, question_id, chunk_id, metadata = seed_question(factory)
    embedding = FakeEmbeddingProvider()
    llm = FakeLLMProvider(
        RagAnswerPayload(
            answer="铜钥匙是在旧桥下找到的。",
            citation_ids=[chunk_id],
            insufficient_evidence=False,
        )
    )
    store = FakeVectorStore(
        [
            VectorSearchHit(
                chunk_id=chunk_id,
                distance=0.05,
                metadata=metadata,
            )
        ]
    )
    try:
        run_rag_answer_job(
            question_id,
            embedding,
            llm,
            store,
            factory,
        )
        with factory() as db:
            row = db.get(RagQuestion, question_id)
            assert row.status == "succeeded"
            assert row.answer == "铜钥匙是在旧桥下找到的。"
            assert row.citation_ids == [chunk_id]
            assert row.insufficient_evidence is False
            assert row.evidence[0]["chunk_id"] == chunk_id
            assert row.evidence[0]["chapter_title"] == "雨夜"
            assert "旧桥下" in row.evidence[0]["excerpt"]
        assert len(embedding.calls) == 1
        assert embedding.calls[0][0].endswith("铜钥匙在哪里找到？")
        assert len(llm.calls) == 1
        assert llm.calls[0]["thinking"] is True
        assert llm.calls[0]["reasoning_effort"] == "high"
        assert chunk_id in llm.calls[0]["user_prompt"]
        assert "旧桥下" in llm.calls[0]["user_prompt"]
        system_prompt = llm.calls[0]["system_prompt"]
        assert "检索结果可能不完整" in system_prompt
        assert "可推断" in system_prompt
        assert "引用无关片段" in system_prompt
        request_data = json.loads(
            llm.calls[0]["user_prompt"].split("\n", 1)[1]
        )
        assert request_data["question"] == "铜钥匙在哪里找到？"
        assert request_data["evidence"][0]["chunk_id"] == chunk_id
        assert embedding.closed is True
        assert llm.closed is True
    finally:
        engine.dispose()


def test_model_cannot_cite_a_chunk_outside_retrieval() -> None:
    engine, factory = make_session_factory()
    _, question_id, chunk_id, metadata = seed_question(factory)
    invented = "chunk_" + "f" * 64
    llm = FakeLLMProvider(
        RagAnswerPayload(
            answer="虚构答案",
            citation_ids=[invented],
            insufficient_evidence=False,
        )
    )
    try:
        run_rag_answer_job(
            question_id,
            FakeEmbeddingProvider(),
            llm,
            FakeVectorStore(
                [
                    VectorSearchHit(
                        chunk_id=chunk_id,
                        distance=0.1,
                        metadata=metadata,
                    )
                ]
            ),
            factory,
        )
        with factory() as db:
            row = db.get(RagQuestion, question_id)
            assert row.status == "failed"
            assert row.answer is None
            assert "无效引用" in row.error_message
    finally:
        engine.dispose()


def test_changed_source_is_detected_before_llm_generation() -> None:
    engine, factory = make_session_factory()
    novel_id, question_id, chunk_id, metadata = seed_question(factory)
    with factory() as db:
        chapter = db.scalar(
            select(Chapter).where(Chapter.novel_id == novel_id)
        )
        chapter.content = "正文已被替换。"
        chapter.character_count = len(chapter.content)
        db.commit()
    llm = FakeLLMProvider(
        RagAnswerPayload(
            answer="不会使用",
            citation_ids=[chunk_id],
            insufficient_evidence=False,
        )
    )
    try:
        run_rag_answer_job(
            question_id,
            FakeEmbeddingProvider(),
            llm,
            FakeVectorStore(
                [
                    VectorSearchHit(
                        chunk_id=chunk_id,
                        distance=0.1,
                        metadata=metadata,
                    )
                ]
            ),
            factory,
        )
        with factory() as db:
            row = db.get(RagQuestion, question_id)
            assert row.status == "failed"
            assert "已变化" in row.error_message
        assert llm.calls == []
    finally:
        engine.dispose()


def test_startup_terminalizes_interrupted_rag_questions() -> None:
    engine, factory = make_session_factory()
    _, question_id, _, _ = seed_question(factory)
    try:
        assert fail_interrupted_rag_questions(factory) == 1
        assert fail_interrupted_rag_questions(factory) == 0
        with factory() as db:
            row = db.get(RagQuestion, question_id)
            assert row.status == "failed"
            assert "上次运行时中断" in row.error_message
    finally:
        engine.dispose()
