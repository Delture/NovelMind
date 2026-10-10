from dataclasses import dataclass
from backend.app.rag.embedding import (
    EmbeddingBatchResult,
    EmbeddingUnavailableError,
)

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
