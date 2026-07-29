"""Integration tests for the dependency-light local vector store."""

import sqlite3

import pytest

from backend.app.rag.vector_store import (
    LocalVectorStore,
    VectorCollectionNotFoundError,
)


def test_external_vectors_round_trip_without_source_documents(
    tmp_path,
) -> None:
    path = tmp_path / "vectors"
    store = LocalVectorStore(str(path))
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

    with sqlite3.connect(path / "vectors.sqlite3") as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(vector_entries)"
            ).fetchall()
        }
        metadata = connection.execute(
            "SELECT metadata FROM vector_entries WHERE chunk_id = ?",
            ("chunk_a",),
        ).fetchone()[0]
    assert "document" not in columns
    assert '"chapter_index":1' in metadata


def test_delete_collection_is_idempotent(tmp_path) -> None:
    store = LocalVectorStore(str(tmp_path / "vectors"))
    store.create_collection("novelmind_delete_test")
    store.delete_collection("novelmind_delete_test")
    store.delete_collection("novelmind_delete_test")
    assert store.list_collection_names() == []


def test_store_rejects_wrong_dimensions_and_non_finite_vectors(
    tmp_path,
) -> None:
    store = LocalVectorStore(str(tmp_path / "vectors"))
    store.create_collection("novelmind_strict")
    store.upsert(
        collection_name="novelmind_strict",
        ids=["one"],
        embeddings=[[1.0, 0.0]],
        metadatas=[{"chapter_id": 1}],
    )
    with pytest.raises(ValueError, match="dimensions"):
        store.upsert(
            collection_name="novelmind_strict",
            ids=["two"],
            embeddings=[[1.0, 0.0, 0.0]],
            metadatas=[{"chapter_id": 2}],
        )
    with pytest.raises(ValueError, match="finite"):
        store.upsert(
            collection_name="novelmind_strict",
            ids=["bad"],
            embeddings=[[float("nan"), 0.0]],
            metadatas=[{"chapter_id": 2}],
        )
    with pytest.raises(ValueError, match="non-zero"):
        store.query(
            collection_name="novelmind_strict",
            embedding=[0.0, 0.0],
            limit=1,
        )


def test_missing_collection_read_is_explicit(tmp_path) -> None:
    store = LocalVectorStore(str(tmp_path / "vectors"))
    with pytest.raises(VectorCollectionNotFoundError):
        store.collection_count("novelmind_missing")
