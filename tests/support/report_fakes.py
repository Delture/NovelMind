from typing import Optional, Type
from backend.app.analyzer.llm import (
    JsonGenerationResult,
)
from backend.app.analyzer.reports import (
    NovelReportPayload,
    ReportEvidenceNote,
    ReportEvidenceNotesPayload,
    ReportSectionPayload,
)

def section(
    title: str,
    *,
    evidence: Optional[list[int]] = None,
) -> ReportSectionPayload:
    return ReportSectionPayload(
        title=title,
        analysis=f"{title}分析",
        evidence_chapter_indices=evidence or [1],
    )


def report_payload(
    *,
    evidence: Optional[list[int]] = None,
) -> NovelReportPayload:
    citations = evidence or [1]
    return NovelReportPayload(
        title="全书分析报告",
        executive_summary=section("执行摘要", evidence=citations),
        plot_structure=section("情节结构", evidence=citations),
        character_dynamics=section("人物动态", evidence=citations),
        pacing=section("节奏", evidence=citations),
        themes=[section("主题", evidence=citations)],
        strengths=[section("优点", evidence=citations)],
        risks=[section("风险", evidence=citations)],
        recommendations=[section("建议", evidence=citations)],
    )


class FakeReportProvider:
    model = "deepseek-v4-pro"

    def __init__(
        self,
        *,
        invalid_final_citation: bool = False,
        failure: Optional[Exception] = None,
    ) -> None:
        self.invalid_final_citation = invalid_final_citation
        self.failure = failure
        self.calls: list[dict[str, object]] = []
        self.closed = False

    def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: Type,
        thinking: bool = False,
        reasoning_effort: Optional[str] = None,
        before_attempt=None,
    ):
        if before_attempt is not None:
            before_attempt()
        if self.failure is not None:
            raise self.failure
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "response_model": response_model,
                "thinking": thinking,
                "reasoning_effort": reasoning_effort,
            }
        )
        if response_model is ReportEvidenceNotesPayload:
            value = ReportEvidenceNotesPayload(
                notes=[
                    ReportEvidenceNote(
                        topic="情节",
                        analysis="证据分析",
                        evidence_chapter_indices=[1],
                    )
                ]
            )
        elif response_model is NovelReportPayload:
            value = report_payload(
                evidence=(
                    [999] if self.invalid_final_citation else [1]
                )
            )
        else:
            raise AssertionError("unexpected response model")
        return JsonGenerationResult(
            value=value,
            model=self.model,
            prompt_tokens=1,
            completion_tokens=1,
            cache_hit_tokens=0,
            request_id="request-test",
        )

    def close(self) -> None:
        self.closed = True
