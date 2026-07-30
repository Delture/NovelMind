"""Focused tests for the Streamlit analysis-report workspace."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

from backend.tests.test_streamlit_character_ui import FakeStreamlit
from frontend import api_client


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = PROJECT_ROOT / "frontend"


@pytest.fixture
def streamlit_module(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ModuleType, FakeStreamlit]:
    fake_streamlit = FakeStreamlit()
    monkeypatch.setitem(sys.modules, "streamlit", fake_streamlit)
    monkeypatch.setitem(sys.modules, "api_client", api_client)
    ui_styles = importlib.import_module("frontend.ui_styles")
    monkeypatch.setitem(sys.modules, "ui_styles", ui_styles)
    module_name = "novelmind_report_streamlit_under_test"
    spec = importlib.util.spec_from_file_location(
        module_name,
        FRONTEND_DIR / "streamlit_app.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module, fake_streamlit


def report_estimate(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "novel_id": 7,
        "chapter_count": 3,
        "summary_count": 3,
        "character_count": 5,
        "includes_character_analysis": True,
        "estimated_generation_steps": 2,
        "maximum_request_attempts": 18,
        "estimated_force_generation_steps": 2,
        "maximum_force_request_attempts": 18,
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "sends_original_text": False,
        "sends_chapter_titles": True,
        "sends_derived_summaries": True,
        "sends_derived_character_data": True,
        "data_handling_notice": "派生摘要和人物数据会发送至 DeepSeek。",
        "terms_url": (
            "https://cdn.deepseek.com/policies/en-US/"
            "deepseek-open-platform-terms-of-service.html"
        ),
        "privacy_url": (
            "https://cdn.deepseek.com/policies/en-US/"
            "deepseek-privacy-policy.html"
        ),
        "up_to_date": False,
    }
    value.update(overrides)
    return value


def report_section(
    title: str,
    analysis: str,
) -> dict[str, object]:
    return {
        "title": title,
        "analysis": analysis,
        "evidence_chapter_indices": [1, 2],
    }


def saved_report(
    *,
    malicious_text: str = "结构稳定",
) -> dict[str, object]:
    return {
        "novel_id": 7,
        "report": {
            "title": malicious_text,
            "executive_summary": report_section(
                "执行摘要",
                malicious_text,
            ),
            "plot_structure": report_section("情节结构", "层层推进"),
            "character_dynamics": report_section("人物动态", "关系清晰"),
            "pacing": report_section("节奏", "张弛有度"),
            "themes": [report_section("成长", "主题证据")],
            "strengths": [report_section("优点", "优点证据")],
            "risks": [report_section("风险", "风险证据")],
            "recommendations": [report_section("建议", "建议证据")],
        },
        "model": "deepseek-v4-pro",
        "prompt_version": "novel-report-v2",
        "summary_count": 3,
        "character_count": 5,
        "generation_steps": 2,
        "includes_character_analysis": True,
        "up_to_date": True,
        "created_at": "2026-07-29T10:00:00",
        "updated_at": "2026-07-29T10:00:02",
    }


def configure_report_dependencies(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    estimate: dict[str, object] | None = None,
    report: dict[str, object] | None = None,
) -> dict[str, Mock]:
    clients = {
        "get_system_status": Mock(
            return_value={
                "generation": {
                    "provider": "deepseek",
                    "model": "deepseek-v4-pro",
                    "configured": True,
                }
            }
        ),
        "list_analysis_jobs": Mock(
            return_value={"items": [], "total": 0}
        ),
        "get_report_estimate": Mock(
            return_value=estimate or report_estimate()
        ),
        "get_report": Mock(return_value=report or saved_report()),
        "start_report": Mock(
            return_value={
                "id": "report-job",
                "novel_id": 7,
                "job_type": "report",
                "status": "queued",
            }
        ),
    }
    for name, client in clients.items():
        monkeypatch.setattr(module, name, client, raising=False)
    return clients


def test_novel_detail_routes_to_report_mode(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    fake.radio_value = "分析报告"
    monkeypatch.setattr(
        module,
        "get_novel",
        Mock(
            return_value={
                "id": 7,
                "filename": "story.txt",
                "chapter_count": 3,
                "character_count": 100,
                "encoding": "utf-8",
                "created_at": "2026-07-29T10:00:00",
            }
        ),
    )
    report_workspace = Mock()
    monkeypatch.setattr(module, "render_report_workspace", report_workspace)
    monkeypatch.setattr(module, "render_summary_workspace", Mock())
    monkeypatch.setattr(module, "render_reader_workspace", Mock())
    monkeypatch.setattr(module, "render_character_workspace", Mock())
    monkeypatch.setattr(module, "render_rag_workspace", Mock())

    module.render_novel_detail(7, backend_online=True)

    assert fake.radio_options[-1] == "分析报告"
    report_workspace.assert_called_once()


def test_force_report_requires_second_confirmation(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    clients = configure_report_dependencies(module, monkeypatch)
    fake.checkbox_values.update(
        {
            "report_external_consent_7": True,
            "report_content_rights_7": True,
            "report_force_7": True,
            "report_force_confirm_7": False,
        }
    )
    fake.button_values["start_report_7"] = True

    module.render_report_workspace({"id": 7})

    start_call = next(
        call
        for call in fake.button_calls
        if call["key"] == "start_report_7"
    )
    assert start_call["disabled"] is True
    clients["start_report"].assert_not_called()


def test_report_model_output_is_rendered_only_as_plain_text(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    malicious = "<script>alert('report')</script> **bold**"
    configure_report_dependencies(
        module,
        monkeypatch,
        report=saved_report(malicious_text=malicious),
    )

    module.render_report_workspace({"id": 7})

    text_messages = [
        body for kind, body in fake.messages if kind == "text"
    ]
    markdown_messages = [
        body for kind, body in fake.messages if kind == "markdown"
    ]
    assert malicious in text_messages
    assert all(malicious not in body for body in markdown_messages)


def test_report_accepts_empty_optional_evidence_sections(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    report = saved_report()
    for key in ("themes", "strengths", "risks", "recommendations"):
        report["report"][key] = []
    configure_report_dependencies(
        module,
        monkeypatch,
        report=report,
    )

    module.render_report_workspace({"id": 7})

    assert not any(kind == "error" for kind, _ in fake.messages)


def test_report_rejects_out_of_range_evidence_before_rendering(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    invalid = saved_report()
    invalid["report"]["pacing"]["evidence_chapter_indices"] = [99]
    configure_report_dependencies(
        module,
        monkeypatch,
        report=invalid,
    )

    module.render_report_workspace({"id": 7})

    assert any(
        kind == "error" and "超出章节范围" in body
        for kind, body in fake.messages
    )


def test_report_rejects_untrusted_policy_host(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    configure_report_dependencies(
        module,
        monkeypatch,
        estimate=report_estimate(
            privacy_url="https://evil.example/privacy"
        ),
    )

    module.render_report_workspace({"id": 7})

    assert any(
        kind == "error" and "cdn.deepseek.com" in body
        for kind, body in fake.messages
    )
    assert all(
        "evil.example" not in body
        for kind, body in fake.messages
        if kind == "markdown"
    )
