
from dataclasses import replace

from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api.novels import delete_novel
from backend.app.core.config import load_settings
from backend.app.core.database import create_db_engine, init_db
from backend.app.models.novel import Chapter, Novel
from backend.app.models.rag import RagBuildCheckpoint, RagIndex
from backend.app.rag.chunking import CHUNK_FORMAT_VERSION
from backend.app.rag.maintenance import cleanup_orphan_rag_collections
from backend.app.rag.vector_store import LocalVectorStore


def make_database():
    engine = create_db_engine("sqlite://", poolclass=StaticPool)
    init_db(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def add_index(db, collection_name: str) -> Novel:
    novel = Novel(
        filename="cleanup.txt",
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
    db.add(novel)
    db.flush()
    db.add(
        RagIndex(
            novel_id=novel.id,
            source_hash="a" * 64,
            collection_name=collection_name,
            embedding_provider="siliconflow",
            embedding_model="Qwen/Qwen3-Embedding-4B",
            embedding_dimensions=1_024,
            chunk_version=CHUNK_FORMAT_VERSION,
            chunk_count=1,
        )
    )
    db.commit()
    return novel


def test_startup_cleanup_removes_only_unreferenced_managed_collections(
    tmp_path,
) -> None:
    engine, factory = make_database()
    path = tmp_path / "vectors"
    store = LocalVectorStore(str(path))
    active = "novelmind_n1_active_123456789012"
    orphan = "novelmind_n2_orphan_123456789012"
    unrelated = "third_party_collection"
    for name in (active, orphan, unrelated):
        store.create_collection(name)
    try:
        with factory() as db:
            add_index(db, active)
        assert cleanup_orphan_rag_collections(
            factory,
            str(path),
        ) == 1
        assert store.list_collection_names() == [active, unrelated]
    finally:
        engine.dispose()

def test_cleanup_missing_path_does_not_create_storage(tmp_path) -> None:
    engine, factory = make_database()
    missing = tmp_path / "missing"
    try:
        assert cleanup_orphan_rag_collections(
            factory,
            str(missing),
        ) == 0
        assert missing.exists() is False
    finally:
        engine.dispose()


def test_delete_novel_removes_current_collection(
    tmp_path,
) -> None:
    engine, factory = make_database()
    path = tmp_path / "vectors"
    collection_name = "novelmind_n1_delete_123456789012"
    store = LocalVectorStore(str(path))
    store.create_collection(collection_name)
    settings = replace(
        load_settings({}),
        vector_store_path=str(path),
    )
    try:
        with factory() as db:
            novel = add_index(db, collection_name)
            response = delete_novel(
                novel_id=novel.id,
                db=db,
                settings=settings,
            )
            assert response.status_code == 204
            assert db.get(Novel, novel.id) is None
        assert collection_name not in store.list_collection_names()
    finally:
        engine.dispose()


def test_startup_cleanup_preserves_current_and_valid_checkpoint(tmp_path) -> None:
    engine, factory = make_database()
    path = tmp_path / "vectors"
    store = LocalVectorStore(str(path))
    active = "novelmind_n1_active_123456789012"
    checkpoint_name = "novelmind_n1_check_123456789012"
    orphan = "novelmind_n2_orphan_123456789012"
    for name in (active, checkpoint_name, orphan):
        store.create_collection(name)
    try:
        with factory() as db:
            novel = add_index(db, active)
            from backend.app.rag.chunking import build_rag_chunks, rag_source_hash
            chapters = list(novel.chapters)
            chunks = build_rag_chunks(
                novel_id=novel.id,
                chapters=chapters,
                embedding_model="Qwen/Qwen3-Embedding-4B",
                dimensions=1_024,
            )
            source_hash = rag_source_hash(
                chunks,
                embedding_model="Qwen/Qwen3-Embedding-4B",
                dimensions=1_024,
            )
            db.add(
                RagBuildCheckpoint(
                    novel_id=novel.id,
                    source_hash=source_hash,
                    collection_name=checkpoint_name,
                    embedding_provider="siliconflow",
                    embedding_model="Qwen/Qwen3-Embedding-4B",
                    embedding_dimensions=1_024,
                    chunk_version=CHUNK_FORMAT_VERSION,
                )
            )
            db.commit()
        assert cleanup_orphan_rag_collections(factory, str(path)) == 1
        names = store.list_collection_names()
        assert active in names
        assert checkpoint_name in names
        assert orphan not in names
    finally:
        engine.dispose()
