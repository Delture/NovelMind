from typing import Callable, Optional, Type
from pydantic import BaseModel
from backend.app.analyzer.llm import (
    JsonGenerationResult,
    ProviderUnavailableError,
)

class FakeProvider:
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
