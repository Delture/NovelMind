"""Tests for RAG consent gates, estimates, and background scheduling."""

from dataclasses import replace
from typing import Callable, Generator
from unittest.mock import Mock

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api import rag as rag_api
from backend.app.api.rag import (
    StartRagIndexRequest,
    StartRagQuestionRequest,
    get_rag_estimate,
    get_rag_index_status,
    start_rag_index,
    start_rag_question,
)
from backend.app.core.config import Settings, load_settings
from backend.app.core.database import Base, create_db_engine
from backend.app.main import app
from backend.app.models.analysis import AnalysisJob
from backend.app.models.novel import Chapter, Novel
from backend.app.models.rag import RagChunkRecord, RagIndex, RagQuestion
from backend.app.rag.chunking import (
    CHUNK_FORMAT_VERSION,
    build_rag_chunks,
    rag_source_hash,
)


@pytest.fixture()
def session_factory() -> Generator[Callable[[], Session], None, None]:
    engine = create_db_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    try:
        yield factory
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def configured_settings() -> Settings:
    return replace(
        load_settings({}),
        deepseek_api_key="deepseek-test-key",
        embedding_api_key="siliconflow-test-key",
        embedding_base_url="https://api.siliconflow.cn/v1",
        vector_store_path="./test-vectors",
    )


def add_novel(db: Session) -> Novel:
    novel = Novel(
        filename="rag-api.txt",
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
    db.commit()
    db.refresh(novel)
    return novel


def add_current_index(
    db: Session,
    novel: Novel,
    settings: Settings,
) -> RagIndex:
    chunks = build_rag_chunks(
        novel_id=novel.id,
        chapters=novel.chapters,
        embedding_model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )
    source_hash = rag_source_hash(
        chunks,
        embedding_model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )
    index = RagIndex(
        novel_id=novel.id,
        source_hash=source_hash,
        collection_name="novelmind_api_current",
        embedding_provider="siliconflow",
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        chunk_version=CHUNK_FORMAT_VERSION,
        chunk_count=len(chunks),
    )
    db.add(index)
    db.flush()
    db.add_all(
        [
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
            for chunk in chunks
        ]
    )
    db.commit()
    db.refresh(index)
    return index


def test_rag_routes_are_registered() -> None:
    paths = app.openapi()["paths"]
    assert "get" in paths["/api/novels/{novel_id}/rag/estimate"]
    assert {"get", "post"} <= set(
        paths["/api/novels/{novel_id}/rag/index"]
    )
    assert {"get", "post"} <= set(
        paths["/api/novels/{novel_id}/rag/questions"]
    )
    assert "get" in paths[
        "/api/novels/{novel_id}/rag/questions/{question_id}"
    ]


def test_rag_estimate_discloses_both_external_destinations(
    session_factory,
) -> None:
    settings = configured_settings()
    with session_factory() as db:
        novel = add_novel(db)
        result = get_rag_estimate(
            novel_id=novel.id,
            force=False,
            db=db,
            settings=settings,
        )
        assert result.chunk_count == 2
        assert result.character_count == 200
        assert result.estimated_new_embedding_count == 2
        assert result.maximum_index_embedding_attempts == 3
        assert result.maximum_question_embedding_attempts == 3
        assert result.maximum_answer_generation_attempts == 15
        assert result.embedding_provider == "siliconflow"
        assert result.embedding_model == "Qwen/Qwen3-Embedding-4B"
        assert result.embedding_dimensions == 1_024
        assert result.embedding_region == "global"
        assert result.sends_chapter_titles_and_original_chunks_to_embedding_provider
        assert result.sends_question_and_retrieved_chunks_to_generation_provider
        assert result.embedding_privacy_url.startswith("https://")
        assert result.generation_privacy_url.startswith("https://")

        add_current_index(db, novel, settings)
        cached = get_rag_estimate(
            novel_id=novel.id,
            force=False,
            db=db,
            settings=settings,
        )
        assert cached.index_current is True
        assert cached.reusable_chunk_count == 2
        assert cached.estimated_new_embedding_count == 0


def test_rag_estimate_tracks_custom_provider_provenance(
    session_factory,
) -> None:
    default_settings = configured_settings()
    custom_settings = replace(
        default_settings,
        generation_provider="openai_compatible",
        deepseek_base_url="https://models.example.com/v1",
        deepseek_model="custom-chat",
        embedding_provider="openai_compatible",
        embedding_base_url="https://embeddings.example.com/v1",
        embedding_model="custom-embedding",
        embedding_dimensions=1_536,
    )
    with session_factory() as db:
        novel = add_novel(db)
        add_current_index(db, novel, default_settings)

        result = get_rag_estimate(
            novel_id=novel.id,
            force=False,
            db=db,
            settings=custom_settings,
        )

    assert result.embedding_provider == "openai_compatible"
    assert result.generation_provider == "openai_compatible"
    assert result.embedding_dimensions == 1_536
    assert result.embedding_region == "custom"
    assert result.index_current is False
    assert result.reusable_chunk_count == 0
    assert result.embedding_terms_url == "https://embeddings.example.com/v1"
    assert result.generation_terms_url == "https://models.example.com/v1"


@pytest.mark.parametrize(
    "payload",
    [
        StartRagIndexRequest(
            confirm_external_processing=False,
            confirm_content_rights=True,
        ),
        StartRagIndexRequest(
            confirm_external_processing=True,
            confirm_content_rights=False,
        ),
        StartRagIndexRequest(
            confirm_external_processing=True,
            confirm_content_rights=True,
            force=True,
            confirm_force_refresh=False,
        ),
    ],
)
def test_index_consent_failures_construct_no_external_client(
    session_factory,
    payload: StartRagIndexRequest,
) -> None:
    settings = configured_settings()
    calls = 0

    def embedding_factory():
        nonlocal calls
        calls += 1
        return Mock()

    with session_factory() as db:
        novel = add_novel(db)
        with pytest.raises(HTTPException) as captured:
            start_rag_index(
                request=payload,
                background_tasks=BackgroundTasks(),
                novel_id=novel.id,
                db=db,
                settings=settings,
                embedding_factory=embedding_factory,
                vector_store_factory=lambda: Mock(),
                session_factory=session_factory,
            )
    assert captured.value.status_code == 400
    assert calls == 0


def test_index_start_persists_one_background_job(
    session_factory,
) -> None:
    settings = configured_settings()
    provider = Mock()
    provider.model = "Qwen/Qwen3-Embedding-4B"
    provider.dimensions = 1_024
    tasks = BackgroundTasks()
    with session_factory() as db:
        novel = add_novel(db)
        response = start_rag_index(
            request=StartRagIndexRequest(
                confirm_external_processing=True,
                confirm_content_rights=True,
            ),
            background_tasks=tasks,
            novel_id=novel.id,
            db=db,
            settings=settings,
            embedding_factory=lambda: provider,
            vector_store_factory=lambda: Mock(),
            session_factory=session_factory,
        )
        assert response.status == "queued"
        assert response.progress_total == 2

        same = start_rag_index(
            request=StartRagIndexRequest(
                confirm_external_processing=True,
                confirm_content_rights=True,
            ),
            background_tasks=BackgroundTasks(),
            novel_id=novel.id,
            db=db,
            settings=settings,
            embedding_factory=Mock(
                side_effect=AssertionError("must stay deferred")
            ),
            vector_store_factory=Mock(
                side_effect=AssertionError("must stay deferred")
            ),
            session_factory=session_factory,
        )
        assert same.id == response.id
    assert len(tasks.tasks) == 1


def test_index_response_failure_terminalizes_job_and_closes_provider(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = configured_settings()
    provider = Mock()
    provider.model = "Qwen/Qwen3-Embedding-4B"
    provider.dimensions = 1_024
    monkeypatch.setattr(
        rag_api,
        "_rag_job_response",
        Mock(side_effect=RuntimeError("response validation failed")),
    )

    with session_factory() as db:
        novel = add_novel(db)
        with pytest.raises(RuntimeError, match="response validation failed"):
            start_rag_index(
                request=StartRagIndexRequest(
                    confirm_external_processing=True,
                    confirm_content_rights=True,
                ),
                background_tasks=BackgroundTasks(),
                novel_id=novel.id,
                db=db,
                settings=settings,
                embedding_factory=lambda: provider,
                vector_store_factory=lambda: Mock(),
                session_factory=session_factory,
            )

        job = db.query(AnalysisJob).one()
        assert job.status == "failed"
        assert job.completed_at is not None
        assert "后台队列" in str(job.error_message)
    provider.close.assert_called_once_with()


@pytest.mark.parametrize(
    "payload",
    [
        StartRagQuestionRequest(
            question="问题",
            confirm_embedding_processing=False,
            confirm_generation_processing=True,
            confirm_content_rights=True,
        ),
        StartRagQuestionRequest(
            question="问题",
            confirm_embedding_processing=True,
            confirm_generation_processing=False,
            confirm_content_rights=True,
        ),
        StartRagQuestionRequest(
            question="问题",
            confirm_embedding_processing=True,
            confirm_generation_processing=True,
            confirm_content_rights=False,
        ),
    ],
)
def test_question_consent_failures_construct_no_provider(
    session_factory,
    payload: StartRagQuestionRequest,
) -> None:
    settings = configured_settings()
    with session_factory() as db:
        novel = add_novel(db)
        with pytest.raises(HTTPException) as captured:
            start_rag_question(
                request=payload,
                background_tasks=BackgroundTasks(),
                novel_id=novel.id,
                db=db,
                settings=settings,
                embedding_factory=Mock(
                    side_effect=AssertionError("must stay deferred")
                ),
                generation_factory=Mock(
                    side_effect=AssertionError("must stay deferred")
                ),
                vector_store_factory=Mock(),
                session_factory=session_factory,
            )
    assert captured.value.status_code == 400


def test_question_requires_a_current_index_before_provider_creation(
    session_factory,
) -> None:
    settings = configured_settings()
    with session_factory() as db:
        novel = add_novel(db)
        with pytest.raises(HTTPException) as captured:
            start_rag_question(
                request=StartRagQuestionRequest(
                    question="钥匙在哪里？",
                    confirm_embedding_processing=True,
                    confirm_generation_processing=True,
                    confirm_content_rights=True,
                ),
                background_tasks=BackgroundTasks(),
                novel_id=novel.id,
                db=db,
                settings=settings,
                embedding_factory=Mock(
                    side_effect=AssertionError("must stay deferred")
                ),
                generation_factory=Mock(
                    side_effect=AssertionError("must stay deferred")
                ),
                vector_store_factory=Mock(),
                session_factory=session_factory,
            )
    assert captured.value.status_code == 409
    assert "索引" in captured.value.detail


def test_question_start_schedules_recoverable_history_row(
    session_factory,
) -> None:
    settings = configured_settings()
    embedding = Mock()
    llm = Mock()
    tasks = BackgroundTasks()
    with session_factory() as db:
        novel = add_novel(db)
        add_current_index(db, novel, settings)
        status_result = get_rag_index_status(
            novel_id=novel.id,
            db=db,
            settings=settings,
        )
        assert status_result.current is True

        response = start_rag_question(
            request=StartRagQuestionRequest(
                question="  第一章是什么？  ",
                top_k=6,
                confirm_embedding_processing=True,
                confirm_generation_processing=True,
                confirm_content_rights=True,
            ),
            background_tasks=tasks,
            novel_id=novel.id,
            db=db,
            settings=settings,
            embedding_factory=lambda: embedding,
            generation_factory=lambda: llm,
            vector_store_factory=lambda: Mock(),
            session_factory=session_factory,
        )
        assert response.status == "queued"
        assert response.question == "第一章是什么？"
        assert response.citations == []
        assert response.retrieved_evidence_count == 0
        assert len(tasks.tasks) == 1


def test_question_response_failure_terminalizes_row_and_closes_providers(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = configured_settings()
    embedding = Mock()
    llm = Mock()
    monkeypatch.setattr(
        rag_api,
        "_question_response",
        Mock(side_effect=RuntimeError("response validation failed")),
    )

    with session_factory() as db:
        novel = add_novel(db)
        add_current_index(db, novel, settings)
        with pytest.raises(RuntimeError, match="response validation failed"):
            start_rag_question(
                request=StartRagQuestionRequest(
                    question="第一章发生了什么？",
                    confirm_embedding_processing=True,
                    confirm_generation_processing=True,
                    confirm_content_rights=True,
                ),
                background_tasks=BackgroundTasks(),
                novel_id=novel.id,
                db=db,
                settings=settings,
                embedding_factory=lambda: embedding,
                generation_factory=lambda: llm,
                vector_store_factory=lambda: Mock(),
                session_factory=session_factory,
            )

        db.expire_all()
        question = db.query(RagQuestion).one()
        assert question.status == "failed"
        assert question.completed_at is not None
        assert "后台响应" in str(question.error_message)
    embedding.close.assert_called_once_with()
    llm.close.assert_called_once_with()
