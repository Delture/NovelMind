"""共享确定性测试数据与假提供商；不发送网络请求。"""
import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable, Generator, Optional, Type
from unittest.mock import Mock
import httpx
import pytest
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from backend.app.analyzer.llm import (
    JsonGenerationResult,
    ProviderUnavailableError,
)
from backend.app.analyzer import summaries as summaries_service
from backend.app.analyzer import jobs as jobs_service
from backend.app.services import tasks as summaries_api
from backend.app.analyzer.summaries import (
    ChapterSummaryPayload,
    fail_interrupted_jobs,
    generate_chapter_summary,
    run_summary_job,
)
from backend.app.services.summaries import (
    StartSummariesRequest,
    get_chapter_summary,
    get_analysis_job,
    get_summary_estimate,
    list_analysis_jobs,
    list_chapter_summaries,
    start_chapter_summaries,
    start_single_chapter_summary,
)
from backend.app.core.config import load_settings
from backend.app.core.database import Base, create_db_engine, get_db
from backend.app.core.time import utc_now
from backend.app.main import app
from backend.app.models.analysis import AnalysisJob, ChapterSummary
from backend.app.models.novel import Chapter, Novel

class FakeProvider:
    """Deterministic provider used without any external API calls."""

    model = "deepseek-v4-pro"

    def __init__(
        self,
        *,
        error: Optional[ProviderUnavailableError] = None,
        response_model_name: Optional[str] = None,
    ) -> None:
        self.error = error
        self.response_model_name = response_model_name
        self.calls: list[dict[str, object]] = []

    def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: Type[BaseModel],
        thinking: bool = False,
        reasoning_effort: Optional[str] = None,
        before_attempt: Optional[Callable[[], None]] = None,
    ) -> JsonGenerationResult:
        if before_attempt is not None:
            before_attempt()
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "response_model": response_model,
                "thinking": thinking,
                "reasoning_effort": reasoning_effort,
            }
        )
        if self.error is not None:
            raise self.error
        payload = response_model(
            summary="主角发现了一封关键来信。",
            key_events=["收到来信", "决定启程"],
            plot_progression="故事目标由等待转为主动调查。",
            cliffhanger="来信的署名来自失踪多年的故人。",
        )
        return JsonGenerationResult(
            value=payload,
            model=self.response_model_name or self.model,
            prompt_tokens=120,
            completion_tokens=80,
            cache_hit_tokens=0,
            request_id="req-test",
        )
