
from threading import Event, Lock


class ProviderAudit:
    """Pause the first source-processing call without relying on timing sleeps."""

    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.entered = Event()
        self.release = Event()
        self.lock = Lock()
        self.paused = False
        self.constructed = []
        self.calls = []

    def record_call(self, role, settings, *, pause=False):
        with self.lock:
            self.calls.append((role, settings))
            should_pause = pause and not self.paused
            if should_pause:
                self.paused = True
        if should_pause:
            self.entered.set()
            if not self.release.wait(10):
                raise AssertionError("Review test did not release its workflow barrier")

    def generation(self, role, settings):
        self.constructed.append((role, settings))
        return GenerationFake(self, role, settings)

    def embedding(self, settings):
        self.constructed.append(("embedding", settings))
        return EmbeddingFake(self, settings)


class GenerationFake:
    def __init__(self, audit, role, settings):
        self.audit, self.role, self.settings = audit, role, settings
        self.model = settings.deepseek_model

    def generate_json(self, *, response_model, before_attempt=None, **kwargs):
        from backend.app.analyzer.llm import JsonGenerationResult
        from backend.app.models.rag import RagChunkRecord

        if before_attempt:
            before_attempt()
        self.audit.record_call(self.role, self.settings, pause=self.role == "summary")
        section = dict(title="来信", analysis="主角收到来信后出发。", evidence_chapter_indices=[1])
        name = response_model.__name__
        if name == "ChapterSummaryPayload":
            payload = dict(summary="主角收到来信。", key_events=["收到来信"],
                           plot_progression="主角开始调查。", cliffhanger="来信来源尚不明确。")
        elif name == "ReportEvidenceNotesPayload":
            payload = dict(notes=[dict(topic="来信", analysis="主角收到来信。", evidence_chapter_indices=[1])])
        elif name == "NovelReportPayload":
            payload = dict(title="来信分析", executive_summary=section, plot_structure=section,
                           character_dynamics=section, pacing=section, themes=[section],
                           strengths=[section], risks=[section], recommendations=[section])
        elif name == "RagAnswerPayload":
            with self.audit.session_factory() as db:
                chunk = db.query(RagChunkRecord).first()
                assert chunk is not None
                payload = dict(answer="主角收到来信。", citation_ids=[chunk.chunk_id], insufficient_evidence=False)
        else:
            raise AssertionError(f"Unexpected fake response contract: {name}")
        return JsonGenerationResult(value=response_model.model_validate(payload), model=self.model,
                                    prompt_tokens=1, completion_tokens=1, cache_hit_tokens=0,
                                    request_id="review-fake")

    def close(self):
        pass


class EmbeddingFake:
    def __init__(self, audit, settings):
        self.audit, self.settings = audit, settings
        self.provider_id = settings.embedding_provider
        self.model, self.dimensions = settings.embedding_model, settings.embedding_dimensions

    def embed(self, texts, *, before_attempt=None):
        from backend.app.rag.embedding import EmbeddingBatchResult

        if before_attempt:
            before_attempt()
        self.audit.record_call("embedding", self.settings, pause=True)
        return EmbeddingBatchResult(vectors=tuple(tuple([1.0] * self.dimensions) for _ in texts),
                                    model=self.model, prompt_tokens=len(texts), request_id="review-fake")

    def close(self):
        pass
