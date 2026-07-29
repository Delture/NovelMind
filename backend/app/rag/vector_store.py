"""Local Chroma storage that never invokes a local embedding model."""

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import chromadb
from chromadb.config import Settings as ChromaSettings


@dataclass(frozen=True)
class VectorSearchHit:
    """One Chroma result containing only local provenance metadata."""

    chunk_id: str
    distance: float
    metadata: Mapping[str, object]


class ChromaVectorStore:
    """Persist externally generated vectors with telemetry disabled."""

    def __init__(self, path: str) -> None:
        resolved = Path(path).expanduser().resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=str(resolved),
            settings=ChromaSettings(
                anonymized_telemetry=False,
                is_persistent=True,
                persist_directory=str(resolved),
            ),
        )

    def create_collection(self, name: str) -> None:
        """Create one versioned cosine collection without an embedder."""
        self._client.create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
            embedding_function=None,
        )

    def delete_collection(self, name: str) -> None:
        """Delete one collection; missing names are ignored."""
        try:
            self._client.delete_collection(name=name)
        except Exception as error:
            if error.__class__.__name__ not in {
                "NotFoundError",
                "InvalidCollectionException",
            }:
                raise

    def collection_count(self, name: str) -> int:
        """Return the number of vectors in one collection."""
        return int(
            self._client.get_collection(
                name=name,
                embedding_function=None,
            ).count()
        )

    def list_collection_names(self) -> list[str]:
        """Return local collection names without loading vectors."""
        return sorted(
            str(collection.name)
            for collection in self._client.list_collections()
        )

    def upsert(
        self,
        *,
        collection_name: str,
        ids: Sequence[str],
        embeddings: Sequence[Sequence[float]],
        metadatas: Sequence[Mapping[str, object]],
    ) -> None:
        """Write external vectors and provenance, never source documents."""
        if not ids:
            return
        collection = self._client.get_collection(
            name=collection_name,
            embedding_function=None,
        )
        collection.upsert(
            ids=list(ids),
            embeddings=[list(vector) for vector in embeddings],
            metadatas=[dict(metadata) for metadata in metadatas],
        )

    def get_embeddings(
        self,
        *,
        collection_name: str,
        ids: Sequence[str],
    ) -> dict[str, tuple[float, ...]]:
        """Read cached vectors by stable chunk ID."""
        if not ids:
            return {}
        collection = self._client.get_collection(
            name=collection_name,
            embedding_function=None,
        )
        response = collection.get(
            ids=list(ids),
            include=["embeddings"],
        )
        response_ids = response.get("ids") or []
        response_embeddings = response.get("embeddings")
        if response_embeddings is None:
            return {}
        return {
            str(chunk_id): tuple(float(value) for value in embedding)
            for chunk_id, embedding in zip(
                response_ids,
                response_embeddings,
                strict=True,
            )
        }

    def query(
        self,
        *,
        collection_name: str,
        embedding: Sequence[float],
        limit: int,
    ) -> list[VectorSearchHit]:
        """Search by an external query vector."""
        collection = self._client.get_collection(
            name=collection_name,
            embedding_function=None,
        )
        response = collection.query(
            query_embeddings=[list(embedding)],
            n_results=limit,
            include=["distances", "metadatas"],
        )
        ids = (response.get("ids") or [[]])[0]
        distances = (response.get("distances") or [[]])[0]
        metadatas = (response.get("metadatas") or [[]])[0]
        return [
            VectorSearchHit(
                chunk_id=str(chunk_id),
                distance=float(distance),
                metadata=dict(metadata or {}),
            )
            for chunk_id, distance, metadata in zip(
                ids,
                distances,
                metadatas,
                strict=True,
            )
        ]
