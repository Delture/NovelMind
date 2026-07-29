"""Public backend configuration status endpoints."""

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.app.core.config import Settings, get_settings


router = APIRouter(prefix="/system", tags=["system"])


class GenerationStatus(BaseModel):
    """Public generation-provider configuration."""

    provider: str
    model: str
    configured: bool


class EmbeddingStatus(BaseModel):
    """Public embedding-provider configuration."""

    provider: str
    model: Optional[str]
    configured: bool


class SystemStatusResponse(BaseModel):
    """Non-secret backend capability status."""

    status: str
    generation: GenerationStatus
    embedding: EmbeddingStatus


@router.get("/status", response_model=SystemStatusResponse)
def get_system_status(
    settings: Settings = Depends(get_settings),
) -> SystemStatusResponse:
    """Describe configured AI providers without exposing credentials."""
    return SystemStatusResponse(
        status="ok",
        generation=GenerationStatus(
            provider="deepseek",
            model=settings.deepseek_model,
            configured=settings.deepseek_configured,
        ),
        embedding=EmbeddingStatus(
            provider="external_api",
            model=settings.embedding_model,
            configured=settings.embedding_configured,
        ),
    )
