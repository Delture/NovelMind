"""HTTP 依赖集中入口；每个任务捕获受理时的配置和会话工厂。"""

from dataclasses import dataclass
from typing import Callable
from fastapi import Depends, status
from sqlalchemy.orm import Session
from backend.app.core.config import Settings, get_settings
from backend.app.core.database import SessionLocal
from backend.app.services.errors import ServiceError
from backend.app.analyzer.llm import LLMProvider
from backend.app.analyzer.deepseek import DeepSeekProvider, AsyncDeepSeekProvider
from backend.app.rag.embedding import EmbeddingProvider, OpenAIEmbeddingProvider
from backend.app.rag.indexer import VectorStore
from backend.app.rag.vector_store import LocalVectorStore

ProviderFactory = Callable[[], LLMProvider]
EmbeddingFactory = Callable[[], EmbeddingProvider]
LLMFactory = Callable[[], LLMProvider]
VectorStoreFactory = Callable[[], VectorStore]


def get_session_factory() -> Callable[[], Session]:
    """以依赖形式提供会话工厂，便于测试替换为隔离数据库。"""
    return SessionLocal


def get_llm_provider(
    settings: Settings = Depends(get_settings),
) -> LLMProvider:
    """Build the synchronous provider used by non-summary analyzers."""
    if not settings.deepseek_configured:
        raise ServiceError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "尚未配置生成模型 API 密钥，请前往“模型与密钥”设置"
                "（也可设置 DEEPSEEK_API_KEY）。"
            ),
        )
    return DeepSeekProvider(settings)


def get_summary_llm_provider(
    settings: Settings = Depends(get_settings),
) -> LLMProvider:
    """Build the async-capable provider used by summary workers."""
    if not settings.deepseek_configured:
        raise ServiceError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "尚未配置生成模型 API 密钥，请前往“模型与密钥”设置"
                "（也可设置 DEEPSEEK_API_KEY）。"
            ),
        )
    return AsyncDeepSeekProvider(settings)


def get_llm_provider_factory(
    settings: Settings = Depends(get_settings),
) -> ProviderFactory:
    """Defer async summary-client construction until consent succeeds."""
    return lambda: get_summary_llm_provider(settings)


def get_sync_llm_provider_factory(
    settings: Settings = Depends(get_settings),
) -> ProviderFactory:
    """Defer synchronous analysis-client construction until consent."""
    return lambda: get_llm_provider(settings)


def get_embedding_factory(
    settings: Settings = Depends(get_settings),
) -> EmbeddingFactory:
    """Defer embedding-client construction until consent succeeds."""
    return lambda: OpenAIEmbeddingProvider(settings)


def get_generation_factory(
    settings: Settings = Depends(get_settings),
) -> LLMFactory:
    """Defer DeepSeek-client construction until consent succeeds."""
    return lambda: DeepSeekProvider(settings)


def get_vector_store_factory(
    settings: Settings = Depends(get_settings),
) -> VectorStoreFactory:
    """Defer local vector-store initialization until RAG needs it."""
    return lambda: LocalVectorStore(settings.vector_store_path)


@dataclass(frozen=True)
class WorkflowExecution:
    """仅驻留内存的已批准执行环境；配置含密钥，绝不写入任务记录或响应。"""

    settings: Settings
    summary_factory: Callable
    report_factory: Callable
    embedding_factory: Callable
    generation_factory: Callable
    vector_store_factory: Callable


def get_workflow_execution(
    settings: Settings = Depends(get_settings),
    summary_factory: Callable = Depends(get_llm_provider_factory),
    report_factory: Callable = Depends(get_sync_llm_provider_factory),
    embedding_factory: Callable = Depends(get_embedding_factory),
    generation_factory: Callable = Depends(get_generation_factory),
    vector_store_factory: Callable = Depends(get_vector_store_factory),
) -> WorkflowExecution:
    """依赖仅创建绑定同一 Settings 的惰性工厂，校验授权前不创建外部客户端。"""
    return WorkflowExecution(
        settings,
        summary_factory,
        report_factory,
        embedding_factory,
        generation_factory,
        vector_store_factory,
    )
