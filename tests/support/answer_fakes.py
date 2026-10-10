from backend.app.analyzer.llm import JsonGenerationResult
from backend.app.rag.answering import (
    RagAnswerPayload,
)

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
