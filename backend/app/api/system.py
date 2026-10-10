"""配置写入/重置只接受回环地址请求，避免远端借此更换 API 端点诱导程序发送密钥；
状态接口绝不返回密钥正文。
"""

from backend.app.api.service_route import ServiceRoute
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from backend.app.core.config import (
    EMBEDDING_PROVIDERS,
    GENERATION_PROVIDERS,
    ConfigurationError,
    Settings,
    service_destination,
    clear_runtime_model_settings,
    get_settings,
    runtime_model_settings_exist,
    update_runtime_model_settings,
)


router = APIRouter(route_class=ServiceRoute, prefix="/system", tags=["system"])


# 状态模型与可编辑模型分开，避免为了编辑方便把 API Key 加入公开响应。
class GenerationStatus(BaseModel):
    provider: str
    model: str
    configured: bool
    summary_concurrency_mode: str
    summary_initial_concurrency: int
    summary_max_concurrency: int


class EmbeddingStatus(BaseModel):
    provider: str
    model: str
    dimensions: int
    region: str
    configured: bool


class SystemStatusResponse(BaseModel):
    status: str
    generation: GenerationStatus
    embedding: EmbeddingStatus


class ProviderOption(BaseModel):
    id: str
    label: str


class EditableProviderStatus(BaseModel):
    provider: str
    provider_label: str
    model: str
    base_url: str
    configured: bool
    api_key_set: bool


class EditableGenerationStatus(EditableProviderStatus):
    summary_concurrency_mode: str
    summary_initial_concurrency: int
    summary_max_concurrency: int
    summary_provider_concurrency_limit: int


class EditableEmbeddingStatus(EditableProviderStatus):
    dimensions: int


class ModelSettingsResponse(BaseModel):
    generation: EditableGenerationStatus
    embedding: EditableEmbeddingStatus
    generation_provider_options: list[ProviderOption]
    embedding_provider_options: list[ProviderOption]
    local_override: bool


class ProviderSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=100)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: Optional[str] = Field(default=None, max_length=4_096)
    clear_api_key: StrictBool = False


class GenerationSettingsUpdate(ProviderSettingsUpdate):
    summary_concurrency_mode: str = Field(
        default="auto",
        pattern="^(auto|manual)$",
    )
    summary_max_concurrency: int = Field(
        default=2_500,
        ge=1,
        le=2_500,
    )


class EmbeddingSettingsUpdate(ProviderSettingsUpdate):
    dimensions: int = Field(ge=1, le=8_192)


class ModelSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation: GenerationSettingsUpdate
    embedding: EmbeddingSettingsUpdate


def _model_settings_response(
    settings: Settings,
) -> ModelSettingsResponse:
    return ModelSettingsResponse(
        generation=EditableGenerationStatus(
            provider=settings.generation_provider,
            provider_label=GENERATION_PROVIDERS[
                settings.generation_provider
            ],
            model=settings.deepseek_model,
            base_url=settings.deepseek_base_url,
            configured=settings.deepseek_configured,
            api_key_set=settings.deepseek_api_key is not None,
            summary_concurrency_mode=(
                settings.summary_concurrency_mode
            ),
            summary_initial_concurrency=(
                settings.summary_initial_concurrency
            ),
            summary_max_concurrency=settings.summary_max_concurrency,
            summary_provider_concurrency_limit=(
                settings.summary_provider_concurrency_limit
            ),
        ),
        embedding=EditableEmbeddingStatus(
            provider=settings.embedding_provider,
            provider_label=EMBEDDING_PROVIDERS[
                settings.embedding_provider
            ],
            model=settings.embedding_model,
            base_url=settings.embedding_base_url or "",
            dimensions=settings.embedding_dimensions,
            configured=settings.embedding_configured,
            api_key_set=settings.embedding_api_key is not None,
        ),
        generation_provider_options=[
            ProviderOption(id=provider_id, label=label)
            for provider_id, label in GENERATION_PROVIDERS.items()
        ],
        embedding_provider_options=[
            ProviderOption(id=provider_id, label=label)
            for provider_id, label in EMBEDDING_PROVIDERS.items()
        ],
        local_override=runtime_model_settings_exist(),
    )




@router.get("/status", response_model=SystemStatusResponse)
def get_system_status(
    settings: Settings = Depends(get_settings),
) -> SystemStatusResponse:
    return SystemStatusResponse(
        status="ok",
        generation=GenerationStatus(
            provider=settings.generation_provider,
            model=settings.deepseek_model,
            configured=settings.deepseek_configured,
            summary_concurrency_mode=(
                settings.summary_concurrency_mode
            ),
            summary_initial_concurrency=(
                settings.summary_initial_concurrency
            ),
            summary_max_concurrency=(
                settings.summary_effective_concurrency_limit
            ),
        ),
        embedding=EmbeddingStatus(
            provider=settings.embedding_provider,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            region=(
                "global"
                if settings.embedding_provider == "siliconflow"
                else "custom"
            ),
            configured=settings.embedding_configured,
        ),
    )


@router.get("/model-settings", response_model=ModelSettingsResponse)
def get_model_settings(
    settings: Settings = Depends(get_settings),
) -> ModelSettingsResponse:
    return _model_settings_response(settings)


@router.put("/model-settings", response_model=ModelSettingsResponse)
def put_model_settings(
    payload: ModelSettingsUpdate,
    request: Request,
) -> ModelSettingsResponse:
    current = get_settings()
    generation_api_key = (
        payload.generation.api_key.strip()
        if payload.generation.api_key is not None
        else ""
    )
    embedding_api_key = (
        payload.embedding.api_key.strip()
        if payload.embedding.api_key is not None
        else ""
    )
    if payload.generation.clear_api_key and generation_api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="生成模型密钥不能同时替换和清除。",
        )
    if payload.embedding.clear_api_key and embedding_api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="向量模型密钥不能同时替换和清除。",
        )
    try:
        generation_changed = service_destination(payload.generation.provider, payload.generation.base_url) != service_destination(current.generation_provider, current.deepseek_base_url)
        embedding_changed = service_destination(payload.embedding.provider, payload.embedding.base_url) != service_destination(current.embedding_provider, current.embedding_base_url)
    except ConfigurationError:
        raise HTTPException(400, "服务地址格式无效，请检查地址。") from None
    if (
        generation_changed
        and not generation_api_key
        and not payload.generation.clear_api_key
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="切换生成模型供应商或服务地址时需要填写新的 API Key，或明确清除密钥。",
        )
    if (
        embedding_changed
        and not embedding_api_key
        and not payload.embedding.clear_api_key
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="切换向量模型供应商或服务地址时需要填写新的 API Key，或明确清除密钥。",
        )
    generation_key = (
        ""
        if payload.generation.clear_api_key
        else (generation_api_key or current.deepseek_api_key or "")
    )
    embedding_key = (
        ""
        if payload.embedding.clear_api_key
        else (embedding_api_key or current.embedding_api_key or "")
    )
    try:
        updated = update_runtime_model_settings(
            {
                "GENERATION_PROVIDER": payload.generation.provider,
                "DEEPSEEK_API_KEY": generation_key,
                "DEEPSEEK_BASE_URL": payload.generation.base_url,
                "DEEPSEEK_MODEL": payload.generation.model,
                "SUMMARY_CONCURRENCY_MODE": (
                    payload.generation.summary_concurrency_mode
                ),
                "SUMMARY_MAX_CONCURRENCY": str(
                    payload.generation.summary_max_concurrency
                ),
                "EMBEDDING_PROVIDER": payload.embedding.provider,
                "EMBEDDING_API_KEY": embedding_key,
                "EMBEDDING_BASE_URL": payload.embedding.base_url,
                "EMBEDDING_MODEL": payload.embedding.model,
                "EMBEDDING_DIMENSIONS": str(
                    payload.embedding.dimensions
                ),
            }
        )
    except ConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from None
    return _model_settings_response(updated)


@router.delete("/model-settings", response_model=ModelSettingsResponse)
def reset_model_settings(request: Request) -> ModelSettingsResponse:
    try:
        clear_runtime_model_settings()
        restored = get_settings()
    except ConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from None
    return _model_settings_response(restored)


class ModelProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["generation", "embedding"]
    provider: str = Field(min_length=1, max_length=50)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: Optional[str] = Field(default=None, max_length=4096)
    use_saved_key: StrictBool = True
    confirm_connection: StrictBool


def get_model_probe_transport():
    """默认只在用户主动操作时建立外部连接；测试覆盖此依赖。"""
    return None


@router.get("/provider-presets")
def get_provider_presets():
    from backend.app.services.model_probe import PRESETS
    return PRESETS


@router.post("/model-probe")
async def post_model_probe(payload: ModelProbeRequest, request: Request,
                           settings: Settings = Depends(get_settings),
                           transport=Depends(get_model_probe_transport)):
    """不发送正文或生成请求。"""
    from backend.app.services.model_probe import probe_models
    return await probe_models(payload, settings, transport)
