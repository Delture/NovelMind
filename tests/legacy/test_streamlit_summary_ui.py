from __future__ import annotations
"""Focused tests for the Streamlit chapter-summary workspace."""


import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

from legacy.streamlit import api_client


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = PROJECT_ROOT / "legacy" / "streamlit"


class FakeBlock:
    """Context block that delegates Streamlit calls to the fake root."""

    def __init__(self, root: "FakeStreamlit") -> None:
        self.root = root

    def __enter__(self) -> "FakeBlock":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def metric(self, label: str, value: object, **_: object) -> None:
        self.root.metrics.append((label, value))

    def button(self, label: str, **kwargs: object) -> bool:
        return self.root.button(label, **kwargs)

    def markdown(self, body: str, **kwargs: object) -> None:
        self.root.markdown(body, **kwargs)


class FakeStreamlit(ModuleType):
    """Small Streamlit 1.60-compatible test double."""

    def __init__(self) -> None:
        super().__init__("streamlit")
        self.session_state: dict[str, object] = {}
        self.checkbox_values: dict[str, bool] = {}
        self.checkbox_calls: list[dict[str, object]] = []
        self.button_values: dict[str, bool] = {}
        self.button_calls: list[dict[str, object]] = []
        self.metrics: list[tuple[str, object]] = []
        self.messages: list[tuple[str, str]] = []
        self.progress_values: list[int] = []
        self.expander_labels: list[str] = []
        self.sidebar = FakeBlock(self)

    def set_page_config(self, **_: object) -> None:
        return None

    def markdown(self, body: str, **_: object) -> None:
        self.messages.append(("markdown", str(body)))

    def write(self, body: object) -> None:
        self.messages.append(("write", str(body)))

    def text(self, body: object) -> None:
        self.messages.append(("text", str(body)))

    def caption(self, body: object) -> None:
        self.messages.append(("caption", str(body)))

    def subheader(self, body: object) -> None:
        self.messages.append(("subheader", str(body)))

    def success(self, body: object) -> None:
        self.messages.append(("success", str(body)))

    def warning(self, body: object) -> None:
        self.messages.append(("warning", str(body)))

    def error(self, body: object) -> None:
        self.messages.append(("error", str(body)))

    def info(self, body: object) -> None:
        self.messages.append(("info", str(body)))

    def progress(self, value: int) -> None:
        self.progress_values.append(value)

    def columns(self, spec: object) -> list[FakeBlock]:
        count = spec if isinstance(spec, int) else len(spec)  # type: ignore[arg-type]
        return [FakeBlock(self) for _ in range(count)]

    def expander(self, label: str, **__: object) -> FakeBlock:
        self.expander_labels.append(label)
        return FakeBlock(self)

    def container(self, **__: object) -> FakeBlock:
        return FakeBlock(self)

    def spinner(self, *_: object, **__: object) -> FakeBlock:
        return FakeBlock(self)

    def file_uploader(self, *_: object, **__: object) -> None:
        return None

    def checkbox(
        self,
        label: str,
        key: str,
        **__: object,
    ) -> bool:
        self.checkbox_calls.append({"label": label, "key": key})
        return self.checkbox_values.get(key, False)

    def button(
        self,
        label: str,
        key: str | None = None,
        disabled: bool = False,
        **_: object,
    ) -> bool:
        self.button_calls.append(
            {
                "label": label,
                "key": key,
                "disabled": disabled,
            }
        )
        return (
            False
            if disabled
            else self.button_values.get(key or label, False)
        )

    def selectbox(
        self,
        _: str,
        options: object,
        index: int = 0,
        **__: object,
    ) -> object:
        return list(options)[index]  # type: ignore[arg-type]

    def rerun(self) -> None:
        self.messages.append(("rerun", "requested"))


@pytest.fixture
def streamlit_module(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ModuleType, FakeStreamlit]:
    fake_streamlit = FakeStreamlit()
    monkeypatch.setitem(sys.modules, "streamlit", fake_streamlit)
    monkeypatch.setitem(sys.modules, "api_client", api_client)
    ui_styles = importlib.import_module("legacy.streamlit.ui_styles")
    monkeypatch.setitem(sys.modules, "ui_styles", ui_styles)
    monkeypatch.setattr(api_client, "check_backend", lambda: False)

    module_name = "novelmind_streamlit_under_test"
    spec = importlib.util.spec_from_file_location(
        module_name,
        FRONTEND_DIR / "streamlit_app.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module, fake_streamlit


def configure_summary_dependencies(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    job: dict | None = None,
) -> dict[str, Mock]:
    clients = {
        "get_system_status": Mock(
            return_value={
                "status": "ok",
                "generation": {
                    "provider": "deepseek",
                    "model": "deepseek-v4-pro",
                    "configured": True,
                },
            }
        ),
        "get_summary_estimate": Mock(
            return_value={
                "chapter_count": 3,
                "character_count": 12_345,
                "estimated_generation_steps": 5,
                "maximum_request_attempts": 45,
                "concurrency_mode": "auto",
                "initial_concurrency": 64,
                "maximum_concurrency": 500,
                "effective_concurrency": 3,
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
                "sends_original_text": True,
                "sends_chapter_titles": True,
                "data_handling_notice": (
                    "章节标题和正文将由 DeepSeek 在中国处理；"
                    "请仅提交你有权处理的内容。"
                ),
                "terms_url": (
                    "https://cdn.deepseek.com/policies/en-US/"
                    "deepseek-open-platform-terms-of-service.html"
                ),
                "privacy_url": (
                    "https://cdn.deepseek.com/policies/en-US/"
                    "deepseek-privacy-policy.html"
                ),
            }
        ),
        "list_analysis_jobs": Mock(
            return_value={
                "items": [] if job is None else [job],
                "total": 0 if job is None else 1,
            }
        ),
        "list_chapters": Mock(
            return_value={
                "items": [
                    {
                        "id": 11,
                        "index": 1,
                        "title": "第一章",
                        "character_count": 123,
                    }
                ],
                "total": 1,
                "offset": 0,
                "limit": 50,
            }
        ),
        "get_chapter_summary": Mock(
            side_effect=api_client.NovelApiError(
                "该章节尚未生成摘要。",
                status_code=404,
            )
        ),
        "list_summaries": Mock(
            return_value={
                "items": [],
                "total": 3,
                "completed": 0,
                "offset": 0,
                "limit": 20,
            }
        ),
        "start_summaries": Mock(
            return_value={"id": "job-all", "status": "queued"}
        ),
        "start_chapter_summary": Mock(
            return_value={"id": "job-one", "status": "queued"}
        ),
        "delete_novel": Mock(return_value=None),
        "get_chapter": Mock(
            side_effect=AssertionError(
                "摘要页面刷新不得加载章节正文"
            )
        ),
    }
    for name, client in clients.items():
        monkeypatch.setattr(module, name, client)
    return clients


def test_summary_actions_require_consent_and_force_confirmation(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    clients = configure_summary_dependencies(module, monkeypatch)
    fake.session_state["summary_force_7"] = True
    fake.checkbox_values.update(
        {
            "summary_consent_7": True,
            "summary_content_rights_7": True,
            "summary_force_7": True,
            "summary_force_confirm_7": False,
        }
    )

    module.render_summary_workspace({"id": 7, "filename": "story.txt"})

    action_calls = {
        call["key"]: call for call in fake.button_calls
    }
    assert (
        action_calls["start_all_summaries_7"]["label"]
        == "重新生成全书摘要"
    )
    assert action_calls["start_all_summaries_7"]["disabled"] is True
    assert action_calls["start_chapter_summary_7_1"]["disabled"] is True
    clients["start_summaries"].assert_not_called()
    clients["start_chapter_summary"].assert_not_called()


def test_summary_actions_are_disabled_without_external_consent(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    clients = configure_summary_dependencies(module, monkeypatch)
    fake.button_values["start_all_summaries_7"] = True
    fake.button_values["start_chapter_summary_7_1"] = True

    module.render_summary_workspace({"id": 7, "filename": "story.txt"})

    action_calls = {
        call["key"]: call for call in fake.button_calls
    }
    assert action_calls["start_all_summaries_7"]["disabled"] is True
    assert action_calls["start_chapter_summary_7_1"]["disabled"] is True
    clients["start_summaries"].assert_not_called()
    clients["start_chapter_summary"].assert_not_called()


def test_long_novel_can_jump_directly_to_a_chapter_range(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
) -> None:
    module, fake = streamlit_module
    fake.selectbox = Mock(return_value=3)

    changed = module.render_chapter_range_jump(
        total=348,
        page=0,
        page_size=50,
        state_key="summary_chapter_page_7",
        widget_key="summary_chapter_range_7",
    )

    assert changed is True
    assert fake.session_state["summary_chapter_page_7"] == 3
    assert ("rerun", "requested") in fake.messages


def test_summary_actions_require_independent_content_rights_confirmation(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    clients = configure_summary_dependencies(module, monkeypatch)
    fake.checkbox_values["summary_consent_7"] = True
    fake.button_values["start_all_summaries_7"] = True

    module.render_summary_workspace({"id": 7, "filename": "story.txt"})

    action_calls = {
        call["key"]: call for call in fake.button_calls
    }
    assert action_calls["start_all_summaries_7"]["disabled"] is True
    assert any(
        call["key"] == "summary_content_rights_7"
        and "有权" in str(call["label"])
        and "中国境内" in str(call["label"])
        for call in fake.checkbox_calls
    )
    clients["start_summaries"].assert_not_called()


def test_summary_workspace_discloses_estimate_and_never_loads_body(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    clients = configure_summary_dependencies(module, monkeypatch)

    module.render_summary_workspace({"id": 7, "filename": "story.txt"})

    assert ("章节", 3) in fake.metrics
    assert ("正文字符", "12,345") in fake.metrics
    assert ("生成步骤", 5) in fake.metrics
    assert ("最多请求尝试", 45) in fake.metrics
    assert any(
        "章节标题和正文将由 DeepSeek 在中国处理" in message
        for _, message in fake.messages
    )
    disclosure_markdown = "\n".join(
        message
        for level, message in fake.messages
        if level == "markdown"
    )
    assert "DeepSeek 开放平台服务条款" in disclosure_markdown
    assert "deepseek-open-platform-terms-of-service.html" in (
        disclosure_markdown
    )
    assert "DeepSeek 隐私政策" in disclosure_markdown
    assert "deepseek-privacy-policy.html" in disclosure_markdown
    clients["get_chapter"].assert_not_called()
    clients["list_summaries"].assert_called_once_with(
        7,
        offset=0,
        limit=20,
    )


@pytest.mark.parametrize(
    "estimate_payload",
    [
        {},
        {
            "chapter_count": "not-a-number",
            "character_count": 12_345,
            "estimated_generation_steps": 5,
            "maximum_request_attempts": 45,
        },
    ],
)
def test_invalid_estimate_payload_is_user_facing(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
    estimate_payload: dict,
) -> None:
    module, fake = streamlit_module
    clients = configure_summary_dependencies(module, monkeypatch)
    clients["get_summary_estimate"].return_value = estimate_payload

    module.render_summary_workspace({"id": 7, "filename": "story.txt"})

    assert any(
        level == "error" and "数据格式" in message
        for level, message in fake.messages
    )
    clients["start_summaries"].assert_not_called()


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("sends_chapter_titles", "true"),
        ("data_handling_notice", None),
        ("terms_url", "javascript:alert(1)"),
        ("terms_url", "https://[deepseek.com/policy"),
        ("terms_url", "https://deepseek.com／@evil.example"),
        ("terms_url", "https://deepseek.com/policy("),
        ("privacy_url", 123),
        ("provider", "other-provider"),
        ("model", ""),
    ],
)
def test_invalid_disclosure_payload_is_user_facing(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    invalid_value: object,
) -> None:
    module, fake = streamlit_module
    clients = configure_summary_dependencies(module, monkeypatch)
    estimate = dict(clients["get_summary_estimate"].return_value)
    estimate[field] = invalid_value
    clients["get_summary_estimate"].return_value = estimate

    module.render_summary_workspace({"id": 7, "filename": "story.txt"})

    assert any(
        level == "error" and "数据格式" in message
        for level, message in fake.messages
    )
    clients["start_summaries"].assert_not_called()


def test_saved_summaries_use_server_side_pagination(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    clients = configure_summary_dependencies(module, monkeypatch)
    fake.session_state["summary_page_7"] = 2
    clients["list_summaries"].return_value = {
        "items": [],
        "total": 50,
        "completed": 45,
        "offset": 40,
        "limit": 20,
    }

    module.render_summary_workspace({"id": 7, "filename": "story.txt"})

    clients["list_summaries"].assert_called_once_with(
        7,
        offset=40,
        limit=20,
    )
    clients["get_chapter"].assert_not_called()


def test_selected_summary_renders_model_fields_as_literal_plain_text(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    clients = configure_summary_dependencies(module, monkeypatch)
    model_fields = {
        "summary": (
            "摘要 ![远程图片](https://attacker.invalid/summary.png) "
            "<img src=https://attacker.invalid/html.png>"
        ),
        "key_events": [
            "事件 [远程链接](https://attacker.invalid/event) "
            "<a href=https://attacker.invalid/html>HTML</a>"
        ],
        "plot_progression": (
            "推进 **加粗** <script src=https://attacker.invalid/x.js></script>"
        ),
        "cliffhanger": (
            "悬念 ![像素](https://attacker.invalid/pixel.gif)"
        ),
    }
    clients["get_chapter_summary"].side_effect = None
    clients["get_chapter_summary"].return_value = {
        **model_fields,
        "model": "deepseek-v4-pro",
        "updated_at": "2026-07-29T10:00:00",
    }

    module._render_selected_chapter_summary(7, {"index": 1})

    clients["get_chapter_summary"].assert_called_once_with(7, 1)
    assert [
        message for level, message in fake.messages if level == "text"
    ] == [
        model_fields["summary"],
        model_fields["key_events"][0],
        model_fields["plot_progression"],
        model_fields["cliffhanger"],
    ]
    rich_messages = [
        message
        for level, message in fake.messages
        if level in {"markdown", "write"}
    ]
    for value in (
        model_fields["summary"],
        *model_fields["key_events"],
        model_fields["plot_progression"],
        model_fields["cliffhanger"],
    ):
        assert all(value not in message for message in rich_messages)


def test_paginated_summaries_render_model_fields_as_literal_plain_text(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    clients = configure_summary_dependencies(module, monkeypatch)
    model_fields = {
        "chapter_title": (
            "第一章 ![远程标题图](https://attacker.invalid/title.png)"
        ),
        "summary": (
            "分页摘要 ![远程图片](https://attacker.invalid/page.png)"
        ),
        "key_events": [
            "分页事件 [远程链接](https://attacker.invalid/page-link)"
        ],
        "plot_progression": (
            "分页推进 <iframe src=https://attacker.invalid/frame></iframe>"
        ),
        "cliffhanger": (
            "分页悬念 <img src=https://attacker.invalid/pixel.png>"
        ),
    }
    clients["list_summaries"].return_value = {
        "items": [
            {
                "chapter_index": 1,
                **model_fields,
            }
        ],
        "total": 1,
        "completed": 1,
        "offset": 0,
        "limit": 20,
    }

    module._render_saved_summaries(7)

    clients["list_summaries"].assert_called_once_with(
        7,
        offset=0,
        limit=20,
    )
    assert [
        message for level, message in fake.messages if level == "text"
    ] == [
        model_fields["chapter_title"],
        model_fields["summary"],
        model_fields["key_events"][0],
        model_fields["plot_progression"],
        model_fields["cliffhanger"],
    ]
    assert fake.expander_labels == ["第 001 章摘要"]
    rich_messages = [
        message
        for level, message in fake.messages
        if level in {"markdown", "write"}
    ]
    for value in (
        model_fields["chapter_title"],
        model_fields["summary"],
        *model_fields["key_events"],
        model_fields["plot_progression"],
        model_fields["cliffhanger"],
    ):
        assert all(value not in message for message in rich_messages)


def test_confirmed_force_can_start_full_summary_job(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    clients = configure_summary_dependencies(module, monkeypatch)
    fake.checkbox_values.update(
        {
            "summary_consent_7": True,
            "summary_content_rights_7": True,
            "summary_force_7": True,
            "summary_force_confirm_7": True,
        }
    )
    fake.button_values["start_all_summaries_7"] = True

    module.render_summary_workspace({"id": 7, "filename": "story.txt"})

    clients["start_summaries"].assert_called_once_with(
        7,
        confirm_external_processing=True,
        confirm_content_rights=True,
        force=True,
    )


def test_conflicting_summary_scope_is_explained(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    clients = configure_summary_dependencies(module, monkeypatch)
    clients["start_chapter_summary"].side_effect = (
        api_client.NovelApiError("摘要任务范围冲突。", status_code=409)
    )
    fake.checkbox_values["summary_consent_7"] = True
    fake.checkbox_values["summary_content_rights_7"] = True
    fake.button_values["start_chapter_summary_7_1"] = True

    module.render_summary_workspace({"id": 7, "filename": "story.txt"})

    assert any(
        level == "warning" and "摘要任务" in message and "完成" in message
        for level, message in fake.messages
    )
    clients["start_chapter_summary"].assert_called_once_with(
        7,
        1,
        confirm_external_processing=True,
        confirm_content_rights=True,
        force=False,
    )


def test_running_and_failed_jobs_are_recovered_from_backend(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    running_job = {
        "id": "job-running",
        "status": "running",
        "target_chapter_index": None,
        "progress_current": 2,
        "progress_total": 5,
        "error_message": None,
    }
    clients = configure_summary_dependencies(
        module,
        monkeypatch,
        job=running_job,
    )

    module.render_summary_workspace({"id": 7, "filename": "story.txt"})

    assert fake.progress_values == [40]
    assert any(
        level == "write" and "全书" in message
        for level, message in fake.messages
    )
    clients["list_analysis_jobs"].assert_called_once_with(
        7,
        job_type="chapter_summaries",
        limit=1,
    )
    assert any(
        call["key"] == "start_all_summaries_7"
        and call["disabled"] is True
        for call in fake.button_calls
    )

    fake.messages.clear()
    fake.progress_values.clear()
    failed_job = {
        "id": "job-failed",
        "status": "failed",
        "target_chapter_index": None,
        "progress_current": 2,
        "progress_total": 5,
        "error_message": "请求频率过高，请稍后重试。",
    }
    monkeypatch.setattr(
        module,
        "list_analysis_jobs",
        Mock(return_value={"items": [failed_job], "total": 1}),
    )

    module.render_summary_workspace({"id": 7, "filename": "story.txt"})

    assert any(
        level == "error" and "请求频率过高" in message
        for level, message in fake.messages
    )


def test_extremely_large_job_progress_does_not_overflow(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    huge_progress = 10**1000
    job = {
        "id": "job-running",
        "status": "running",
        "target_chapter_index": None,
        "progress_current": huge_progress,
        "progress_total": huge_progress,
        "error_message": None,
    }
    configure_summary_dependencies(module, monkeypatch, job=job)

    module.render_summary_workspace({"id": 7, "filename": "story.txt"})

    assert fake.progress_values == [100]


def test_recent_single_chapter_job_displays_target_scope(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    job = {
        "id": "job-running",
        "status": "running",
        "target_chapter_index": 12,
        "progress_current": 0,
        "progress_total": 1,
        "error_message": None,
    }
    configure_summary_dependencies(module, monkeypatch, job=job)

    module.render_summary_workspace({"id": 7, "filename": "story.txt"})

    assert any(
        level == "write" and "第 12 章" in message
        for level, message in fake.messages
    )


def test_invalid_job_payload_is_user_facing_and_blocks_new_jobs(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    clients = configure_summary_dependencies(module, monkeypatch)
    clients["list_analysis_jobs"].return_value = {
        "items": [{}],
        "total": 1,
    }
    fake.checkbox_values.update(
        {
            "summary_consent_7": True,
            "summary_content_rights_7": True,
        }
    )
    fake.button_values["start_all_summaries_7"] = True

    module.render_summary_workspace({"id": 7, "filename": "story.txt"})

    assert any(
        level == "error" and "数据格式" in message
        for level, message in fake.messages
    )
    clients["start_summaries"].assert_not_called()
    assert any(
        call["key"] == "start_all_summaries_7"
        and call["disabled"] is True
        for call in fake.button_calls
    )


def test_navigation_switches_to_upload_without_touching_widget_state(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
) -> None:
    module, fake = streamlit_module
    fake.session_state["view"] = "library"
    fake.session_state["novel_mode_7"] = "正文阅读"
    fake.button_values["nav_upload"] = True

    module.render_navigation()

    assert fake.session_state["view"] == "upload"
    assert fake.session_state["novel_mode_7"] == "正文阅读"
    assert ("rerun", "requested") in fake.messages


def test_empty_library_is_user_facing_and_does_not_load_chapters(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    list_novels = Mock(
        return_value={
            "items": [],
            "total": 0,
            "offset": 0,
            "limit": 10,
        }
    )
    get_chapter = Mock(
        side_effect=AssertionError("空资料库不得读取正文")
    )
    monkeypatch.setattr(module, "list_novels", list_novels)
    monkeypatch.setattr(module, "get_chapter", get_chapter)
    fake.session_state["selected_novel_id"] = None
    fake.session_state["library_page"] = 0

    module.render_library_page(backend_online=True)

    assert any(
        level == "info" and "资料库还是空的" in message
        for level, message in fake.messages
    )
    get_chapter.assert_not_called()


def test_invalid_library_payload_is_user_facing(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    monkeypatch.setattr(
        module,
        "list_novels",
        Mock(return_value={"items": "not-a-list", "total": 1}),
    )
    fake.session_state["selected_novel_id"] = None

    module.render_library_page(backend_online=True)

    assert any(
        level == "error" and "数据格式" in message
        for level, message in fake.messages
    )


def test_reader_loads_only_selected_body_and_escapes_html(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    list_chapters = Mock(
        return_value={
            "items": [
                {
                    "id": 11,
                    "index": 1,
                    "title": "<script>title</script>",
                    "character_count": 20,
                }
            ],
            "total": 1,
            "offset": 0,
            "limit": 50,
        }
    )
    get_chapter = Mock(
        return_value={
            "id": 11,
            "novel_id": 7,
            "index": 1,
            "title": "<script>title</script>",
            "content": "<img src=x onerror=alert(1)>",
            "character_count": 31,
        }
    )
    monkeypatch.setattr(module, "list_chapters", list_chapters)
    monkeypatch.setattr(module, "get_chapter", get_chapter)

    module.render_reader_workspace({"id": 7, "filename": "story.txt"})

    get_chapter.assert_called_once_with(7, 1)
    reader_html = "\n".join(
        message for level, message in fake.messages if level == "markdown"
    )
    assert "<script>title</script>" not in reader_html
    assert "&lt;script&gt;title&lt;/script&gt;" in reader_html
    assert "<img src=x" not in reader_html
    assert "&lt;img src=x" in reader_html


def test_invalid_reader_body_payload_is_user_facing(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    monkeypatch.setattr(
        module,
        "list_chapters",
        Mock(
            return_value={
                "items": [{"index": 1, "title": "第一章"}],
                "total": 1,
            }
        ),
    )
    monkeypatch.setattr(module, "get_chapter", Mock(return_value={}))

    module.render_reader_workspace({"id": 7})

    assert any(
        level == "error" and "数据格式" in message
        for level, message in fake.messages
    )


def test_library_delete_requires_confirmation(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    delete_novel = Mock()
    monkeypatch.setattr(module, "delete_novel", delete_novel)
    fake.button_values["delete_novel_7"] = True
    novel = {
        "id": 7,
        "filename": "story.txt",
        "chapter_count": 1,
        "character_count": 10,
        "encoding": "utf-8",
        "created_at": "2026-07-29T10:00:00",
    }

    module.render_library_card(novel, total=11)

    delete_novel.assert_not_called()
    assert fake.session_state["delete_candidate_id"] == 7
    assert any(
        level == "warning"
        and "仅删除本机 SQLite 数据" in message
        and "不影响 DeepSeek 已处理、缓存或留存的数据" in message
        for level, message in fake.messages
    )


def test_confirmed_delete_clamps_page_and_clears_selection(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    delete_novel = Mock()
    monkeypatch.setattr(module, "delete_novel", delete_novel)
    fake.session_state.update(
        {
            "delete_candidate_id": 7,
            "selected_novel_id": 7,
            "library_page": 1,
        }
    )
    fake.button_values["confirm_delete_novel_7"] = True
    novel = {
        "id": 7,
        "filename": "story.txt",
        "chapter_count": 1,
        "character_count": 10,
        "encoding": "utf-8",
        "created_at": "2026-07-29T10:00:00",
    }

    module.render_library_card(novel, total=11)

    delete_novel.assert_called_once_with(7)
    assert fake.session_state["library_page"] == 0
    assert fake.session_state["selected_novel_id"] is None
    assert "delete_candidate_id" not in fake.session_state
    assert any(level == "rerun" for level, _ in fake.messages)


def test_delete_not_found_is_friendly_and_refreshes_library(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    delete_novel = Mock(
        side_effect=api_client.NovelApiError(
            "未找到指定小说。",
            status_code=404,
        )
    )
    monkeypatch.setattr(module, "delete_novel", delete_novel)
    fake.session_state["delete_candidate_id"] = 7
    fake.session_state["library_page"] = 0
    fake.button_values["confirm_delete_novel_7"] = True
    novel = {
        "id": 7,
        "filename": "story.txt",
        "chapter_count": 1,
        "character_count": 10,
        "encoding": "utf-8",
        "created_at": "2026-07-29T10:00:00",
    }

    module.render_library_card(novel, total=1)

    assert "delete_candidate_id" not in fake.session_state
    assert any(
        level == "warning" and "已不存在" in message
        for level, message in fake.messages
    )
    assert any(level == "rerun" for level, _ in fake.messages)
