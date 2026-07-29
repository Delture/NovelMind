"""Integration tests for local Chroma storage with external vectors."""

import chromadb
from chromadb.config import Settings as ChromaSettings

from backend.app.rag.vector_store import ChromaVectorStore


def test_external_vectors_round_trip_without_source_documents(tmp_path) -> None:
    path = tmp_path / "vectors"
    store = ChromaVectorStore(str(path))
    collection_name = "novelmind_test_vectors"
    store.create_collection(collection_name)
    store.upsert(
        collection_name=collection_name,
        ids=["chunk_a", "chunk_b"],
        embeddings=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        metadatas=[
            {"chapter_index": 1, "start_char": 0, "end_char": 10},
            {"chapter_index": 2, "start_char": 2, "end_char": 12},
        ],
    )

    assert store.collection_count(collection_name) == 2
    cached = store.get_embeddings(
        collection_name=collection_name,
        ids=["chunk_b", "missing", "chunk_a"],
    )
    assert cached == {
        "chunk_a": (1.0, 0.0, 0.0),
        "chunk_b": (0.0, 1.0, 0.0),
    }
    hits = store.query(
        collection_name=collection_name,
        embedding=[0.9, 0.1, 0.0],
        limit=2,
    )
    assert [hit.chunk_id for hit in hits] == ["chunk_a", "chunk_b"]
    assert hits[0].metadata["chapter_index"] == 1

    raw_client = chromadb.PersistentClient(
        path=str(path),
        settings=ChromaSettings(
            anonymized_telemetry=False,
            is_persistent=True,
            persist_directory=str(path.resolve()),
        ),
    )
    raw = raw_client.get_collection(
        collection_name,
        embedding_function=None,
    ).get(include=["documents"])
    assert raw["documents"] == [None, None]


def test_delete_collection_is_idempotent(tmp_path) -> None:
    store = ChromaVectorStore(str(tmp_path / "vectors"))
    store.create_collection("novelmind_delete_test")
    store.delete_collection("novelmind_delete_test")
    store.delete_collection("novelmind_delete_test")
