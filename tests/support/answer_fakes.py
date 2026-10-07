"""共享确定性测试数据与假提供商；不发送网络请求。"""
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
