"""Focused tests for the Streamlit RAG workspace."""

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
    module_name = "novelmind_rag_streamlit_under_test"
    spec = importlib.util.spec_from_file_location(
        module_name,
        FRONTEND_DIR / "streamlit_app.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module, fake_streamlit


def rag_estimate(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "novel_id": 7,
        "chapter_count": 3,
        "character_count": 12_345,
        "chunk_count": 12,
        "reusable_chunk_count": 4,
        "estimated_new_embedding_count": 8,
        "maximum_index_embedding_attempts": 6,
        "maximum_question_embedding_attempts": 3,
        "maximum_answer_generation_attempts": 9,
        "index_current": True,
        "embedding_configured": True,
        "generation_configured": True,
        "embedding_provider": "aliyun_model_studio",
        "embedding_model": "text-embedding-v4",
        "embedding_dimensions": 1_024,
        "embedding_region": "cn-beijing",
        "generation_provider": "deepseek",
        "generation_model": "deepseek-v4-pro",
        "sends_chapter_titles_and_original_chunks_to_bailian": True,
        "sends_question_to_bailian": True,
        "sends_question_and_retrieved_chunks_to_deepseek": True,
        "data_handling_notice": "原文会发送至两个已披露的外部服务。",
        "bailian_terms_url": (
            "https://help.aliyun.com/en/model-studio/"
            "bailian-service-notes"
        ),
        "bailian_privacy_url": (
            "https://help.aliyun.com/en/model-studio/privacy-notice"
        ),
        "deepseek_terms_url": (
            "https://cdn.deepseek.com/policies/en-US/"
            "deepseek-open-platform-terms-of-service.html"
        ),
        "deepseek_privacy_url": (
            "https://cdn.deepseek.com/policies/en-US/"
            "deepseek-privacy-policy.html"
        ),
    }
    value.update(overrides)
    return value


def question_item(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "question-123",
        "novel_id": 7,
        "question": "钥匙在哪里？",
        "top_k": 6,
        "status": "succeeded",
        "answer": "钥匙在旧桥下。",
        "insufficient_evidence": False,
        "citations": [
            {
                "chunk_id": "chunk_" + "a" * 64,
                "chapter_index": 1,
                "chapter_title": "雨夜",
                "start_char": 0,
                "end_char": 12,
                "excerpt": "林舟在旧桥下找到钥匙。",
            }
        ],
        "retrieved_evidence_count": 3,
        "generation_model": "deepseek-v4-pro",
        "error_message": None,
        "created_at": "2026-07-29T10:00:00",
        "started_at": "2026-07-29T10:00:01",
        "completed_at": "2026-07-29T10:00:02",
    }
    value.update(overrides)
    return value


def configure_rag_dependencies(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    history: list[dict[str, object]] | None = None,
    estimate: dict[str, object] | None = None,
) -> dict[str, Mock]:
    items = history or []
    clients = {
        "get_rag_estimate": Mock(
            return_value=estimate or rag_estimate()
        ),
        "get_rag_index_status": Mock(
            return_value={
                "novel_id": 7,
                "ready": True,
                "current": True,
                "chunk_count": 12,
                "source_hash": "a" * 64,
                "embedding_provider": "aliyun_model_studio",
                "embedding_model": "text-embedding-v4",
                "embedding_dimensions": 1_024,
                "chunk_version": "rag-chunk-v1",
                "updated_at": "2026-07-29T10:00:00",
            }
        ),
        "list_rag_index_jobs": Mock(
            return_value={"items": [], "total": 0}
        ),
        "list_rag_questions": Mock(
            return_value={
                "items": items,
                "total": len(items),
                "offset": 0,
                "limit": 10,
            }
        ),
        "start_rag_index": Mock(
            return_value={
                "id": "rag-index-job",
                "novel_id": 7,
                "status": "queued",
            }
        ),
        "start_rag_question": Mock(
            return_value=question_item(
                id="new-question",
                status="queued",
                answer=None,
                citations=[],
                retrieved_evidence_count=0,
                insufficient_evidence=None,
            )
        ),
    }
    for name, client in clients.items():
        monkeypatch.setattr(module, name, client, raising=False)
    return clients


def test_novel_detail_routes_to_rag_mode(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    fake.radio_value = "RAG 问答"
    monkeypatch.setattr(
        module,
        "get_novel",
        Mock(
            return_value={
                "id": 7,
                "filename": "story.txt",
                "chapter_count": 3,
                "character_count": 12_345,
                "encoding": "utf-8",
                "created_at": "2026-07-29T10:00:00",
            }
        ),
    )
    rag_workspace = Mock()
    monkeypatch.setattr(module, "render_rag_workspace", rag_workspace)
    monkeypatch.setattr(module, "render_summary_workspace", Mock())
    monkeypatch.setattr(module, "render_reader_workspace", Mock())
    monkeypatch.setattr(module, "render_character_workspace", Mock())

    module.render_novel_detail(7, backend_online=True)

    assert "RAG 问答" in fake.radio_options
    rag_workspace.assert_called_once()


def test_force_index_requires_second_confirmation(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    clients = configure_rag_dependencies(module, monkeypatch)
    fake.checkbox_values.update(
        {
            "rag_index_external_consent_7": True,
            "rag_index_content_rights_7": True,
            "rag_index_force_7": True,
            "rag_index_force_confirm_7": False,
        }
    )
    fake.button_values["start_rag_index_7"] = True

    module.render_rag_workspace({"id": 7})

    start_call = next(
        call
        for call in fake.button_calls
        if call["key"] == "start_rag_index_7"
    )
    assert start_call["disabled"] is True
    clients["start_rag_index"].assert_not_called()


def test_question_requires_three_independent_confirmations(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    clients = configure_rag_dependencies(module, monkeypatch)
    fake.text_area_values["rag_question_text_7"] = "钥匙在哪里？"
    fake.checkbox_values.update(
        {
            "rag_question_embedding_consent_7": True,
            "rag_question_generation_consent_7": True,
            "rag_question_content_rights_7": False,
        }
    )
    fake.button_values["start_rag_question_7"] = True

    module.render_rag_workspace({"id": 7})

    start_call = next(
        call
        for call in fake.button_calls
        if call["key"] == "start_rag_question_7"
    )
    assert start_call["disabled"] is True
    clients["start_rag_question"].assert_not_called()


def test_answer_and_citations_are_rendered_only_as_plain_text(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    malicious_answer = "<script>alert('answer')</script> **bold**"
    malicious_excerpt = "<img src=x onerror=alert('excerpt')>"
    configure_rag_dependencies(
        module,
        monkeypatch,
        history=[
            question_item(
                answer=malicious_answer,
                citations=[
                    {
                        "chunk_id": "chunk_" + "a" * 64,
                        "chapter_index": 1,
                        "chapter_title": "雨夜",
                        "start_char": 0,
                        "end_char": 35,
                        "excerpt": malicious_excerpt,
                    }
                ],
            )
        ],
    )

    module.render_rag_workspace({"id": 7})

    text_messages = [
        body for kind, body in fake.messages if kind == "text"
    ]
    markdown_messages = [
        body for kind, body in fake.messages if kind == "markdown"
    ]
    assert malicious_answer in text_messages
    assert malicious_excerpt in text_messages
    assert all(
        malicious_answer not in body
        and malicious_excerpt not in body
        for body in markdown_messages
    )


def test_untrusted_policy_host_is_rejected_before_link_rendering(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    configure_rag_dependencies(
        module,
        monkeypatch,
        estimate=rag_estimate(
            bailian_privacy_url="https://evil.example/privacy"
        ),
    )

    module.render_rag_workspace({"id": 7})

    assert any(
        kind == "error" and "help.aliyun.com" in body
        for kind, body in fake.messages
    )
    assert all(
        "evil.example" not in body
        for kind, body in fake.messages
        if kind == "markdown"
    )
