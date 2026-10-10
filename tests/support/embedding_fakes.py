from collections.abc import Sequence
from dataclasses import dataclass
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from backend.app.core.database import create_db_engine, init_db
from backend.app.models.analysis import AnalysisJob
from backend.app.models.novel import Chapter, Novel
from backend.app.models.rag import RagChunkRecord, RagIndex
from backend.app.rag.embedding import (
    EmbeddingBatchResult,
    EmbeddingUnavailableError,
)
from backend.app.rag.indexer import run_rag_index_job
from backend.app.rag.vector_store import VectorSearchHit

@dataclass
class FakeProvider:
    fail: bool = False
    closed: bool = False

    def __post_init__(self) -> None:
        self.calls: list[list[str]] = []

    @property
    def model(self) -> str:
        return "Qwen/Qwen3-Embedding-4B"

    @property
    def dimensions(self) -> int:
        return 1_024

    def embed(self, texts, *, before_attempt=None):
        if before_attempt is not None:
            before_attempt()
        self.calls.append(list(texts))
        if self.fail:
            raise EmbeddingUnavailableError()
        vectors = tuple(
            tuple([float(index + 1)] * self.dimensions)
            for index, _ in enumerate(texts)
        )
        return EmbeddingBatchResult(
            vectors=vectors,
            model=self.model,
            prompt_tokens=len(texts),
            request_id="fake",
        )

    def close(self) -> None:
        self.closed = True
