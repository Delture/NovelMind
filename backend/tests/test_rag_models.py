"""Database integrity tests for RAG snapshots and answer history."""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base, create_db_engine
from backend.app.models.novel import Chapter, Novel
from backend.app.models.rag import RagChunkRecord, RagIndex, RagQuestion
from backend.app.rag.answering import RAG_ANSWER_PROMPT_VERSION
from backend.app.rag.chunking import CHUNK_FORMAT_VERSION


def make_session():
    engine = create_db_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def add_novel(session) -> Novel:
    novel = Novel(
        filename="models.txt",
        encoding="utf-8",
        chapter_count=1,
        chapters=[
            Chapter(
                chapter_index=1,
                title="第一章",
                content="正文",
                character_count=2,
            )
        ],
    )
    session.add(novel)
    session.flush()
    return novel


def add_index(session, novel: Novel) -> RagIndex:
    index = RagIndex(
        novel_id=novel.id,
        source_hash="a" * 64,
        collection_name=f"novelmind_n{novel.id}_model_123456789012",
        embedding_provider="aliyun_model_studio",
        embedding_model="text-embedding-v4",
        embedding_dimensions=1_024,
        chunk_version=CHUNK_FORMAT_VERSION,
        chunk_count=1,
    )
    session.add(index)
    session.flush()
    session.add(
        RagChunkRecord(
            rag_index_id=index.id,
            chapter_id=novel.chapters[0].id,
            chunk_id="chunk_" + "b" * 64,
            chapter_index=1,
            chunk_index=1,
            start_char=0,
            end_char=2,
            content_hash="c" * 64,
        )
    )
    session.flush()
    return index


def add_question(session, novel: Novel, *, status: str = "queued"):
    row = RagQuestion(
        novel_id=novel.id,
        index_source_hash="a" * 64,
        question="发生了什么？",
        top_k=6,
        status=status,
        citation_ids=[],
        evidence=[],
        generation_model="deepseek-v4-pro",
        prompt_version=RAG_ANSWER_PROMPT_VERSION,
    )
    session.add(row)
    session.flush()
    return row


def test_only_one_active_index_and_question_are_allowed_per_novel() -> None:
    engine, session = make_session()
    try:
        novel = add_novel(session)
        add_index(session, novel)
        session.commit()
        with session.begin():
            session.add(
                RagIndex(
                    novel_id=novel.id,
                    source_hash="d" * 64,
                    collection_name="novelmind_duplicate_index",
                    embedding_provider="aliyun_model_studio",
                    embedding_model="text-embedding-v4",
                    embedding_dimensions=1_024,
                    chunk_version=CHUNK_FORMAT_VERSION,
                    chunk_count=1,
                )
            )
            try:
                session.flush()
            except IntegrityError:
                session.rollback()
        add_question(session, novel)
        session.commit()
        session.add(
            RagQuestion(
                novel_id=novel.id,
                index_source_hash="a" * 64,
                question="另一个问题",
                top_k=6,
                status="running",
                citation_ids=[],
                evidence=[],
                generation_model="deepseek-v4-pro",
                prompt_version=RAG_ANSWER_PROMPT_VERSION,
            )
        )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
        else:
            raise AssertionError("second active question should fail")
    finally:
        session.close()
        engine.dispose()


def test_completed_questions_do_not_block_a_new_active_question() -> None:
    engine, session = make_session()
    try:
        novel = add_novel(session)
        add_question(session, novel, status="succeeded")
        session.commit()
        add_question(session, novel, status="queued")
        session.commit()
        assert session.query(RagQuestion).count() == 2
    finally:
        session.close()
        engine.dispose()


def test_rag_constraints_reject_invalid_ranges_and_question_bounds() -> None:
    engine, session = make_session()
    try:
        novel = add_novel(session)
        index = RagIndex(
            novel_id=novel.id,
            source_hash="a" * 64,
            collection_name="novelmind_invalid_range",
            embedding_provider="aliyun_model_studio",
            embedding_model="text-embedding-v4",
            embedding_dimensions=1_024,
            chunk_version=CHUNK_FORMAT_VERSION,
            chunk_count=1,
        )
        session.add(index)
        session.commit()
        novel_id = novel.id
        session.add(
            RagChunkRecord(
                rag_index_id=index.id,
                chapter_id=novel.chapters[0].id,
                chunk_id="chunk_" + "b" * 64,
                chapter_index=1,
                chunk_index=1,
                start_char=2,
                end_char=2,
                content_hash="c" * 64,
            )
        )
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
        else:
            raise AssertionError("empty chunk range should fail")

        novel = session.get(Novel, novel_id)
        session.add(
            RagQuestion(
                novel_id=novel.id,
                index_source_hash="a" * 64,
                question="问题",
                top_k=2,
                status="queued",
                citation_ids=[],
                evidence=[],
                generation_model="deepseek-v4-pro",
                prompt_version=RAG_ANSWER_PROMPT_VERSION,
            )
        )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
        else:
            raise AssertionError("top_k below three should fail")
    finally:
        session.close()
        engine.dispose()


def test_deleting_novel_cascades_all_rag_rows() -> None:
    engine, session = make_session()
    try:
        novel = add_novel(session)
        add_index(session, novel)
        add_question(session, novel)
        session.commit()
        session.delete(novel)
        session.commit()
        assert session.query(RagIndex).count() == 0
        assert session.query(RagChunkRecord).count() == 0
        assert session.query(RagQuestion).count() == 0
    finally:
        session.close()
        engine.dispose()
