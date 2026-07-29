"""Focused tests for the Streamlit character-analysis workspace."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

from frontend import api_client


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = PROJECT_ROOT / "frontend"


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
    """Small Streamlit test double for the character workspace."""

    def __init__(self) -> None:
        super().__init__("streamlit")
        self.session_state: dict[str, object] = {}
        self.checkbox_values: dict[str, bool] = {}
        self.checkbox_calls: list[dict[str, object]] = []
        self.button_values: dict[str, bool] = {}
        self.button_calls: list[dict[str, object]] = []
        self.text_input_values: dict[str, str] = {}
        self.text_input_calls: list[dict[str, object]] = []
        self.selectbox_values: dict[str, object] = {}
        self.selectbox_calls: list[dict[str, object]] = []
        self.radio_value: object | None = None
        self.radio_options: list[object] = []
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

    def text_input(
        self,
        label: str,
        key: str,
        **__: object,
    ) -> str:
        self.text_input_calls.append({"label": label, "key": key})
        return self.text_input_values.get(key, "")

    def selectbox(
        self,
        label: str,
        options: object,
        index: int = 0,
        key: str | None = None,
        **__: object,
    ) -> object:
        values = list(options)  # type: ignore[arg-type]
        self.selectbox_calls.append(
            {
                "label": label,
                "key": key,
                "options": values,
            }
        )
        return self.selectbox_values.get(key or label, values[index])

    def radio(
        self,
        _: str,
        options: object,
        **__: object,
    ) -> object:
        self.radio_options = list(options)  # type: ignore[arg-type]
        return (
            self.radio_value
            if self.radio_value is not None
            else self.radio_options[0]
        )

    def rerun(self) -> None:
        self.messages.append(("rerun", "requested"))


@pytest.fixture
def streamlit_module(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ModuleType, FakeStreamlit]:
    fake_streamlit = FakeStreamlit()
    monkeypatch.setitem(sys.modules, "streamlit", fake_streamlit)
    monkeypatch.setitem(sys.modules, "api_client", api_client)
    ui_styles = importlib.import_module("frontend.ui_styles")
    monkeypatch.setitem(sys.modules, "ui_styles", ui_styles)
    monkeypatch.setattr(api_client, "check_backend", lambda: False)

    module_name = "novelmind_character_streamlit_under_test"
    spec = importlib.util.spec_from_file_location(
        module_name,
        FRONTEND_DIR / "streamlit_app.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module, fake_streamlit


def character_item(**overrides: object) -> dict[str, object]:
    """Return one complete character list/detail item."""
    item: dict[str, object] = {
        "id": 11,
        "canonical_name": "林舟",
        "aliases": ["小舟"],
        "short_description": "谨慎但可靠的主角。",
        "first_chapter_index": 1,
        "last_chapter_index": 3,
        "chapter_count": 3,
        "mention_count": 8,
        "relationship_count": 1,
    }
    item.update(overrides)
    return item


def configure_character_dependencies(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    job: dict[str, object] | None = None,
) -> dict[str, Mock]:
    """Install complete, valid API responses for character UI tests."""
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
        "get_character_estimate": Mock(
            return_value={
                "novel_id": 7,
                "chapter_count": 3,
                "character_count": 12_345,
                "cached_chapter_count": 1,
                "pending_chapter_count": 2,
                "estimated_extraction_steps": 2,
                "estimated_merge_steps": 1,
                "estimated_relationship_steps": 1,
                "estimated_generation_steps": 4,
                "maximum_request_attempts": 12,
                "estimated_force_generation_steps": 6,
                "maximum_force_request_attempts": 18,
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
                "sends_original_text": True,
                "sends_chapter_titles": True,
                "sends_derived_character_data": True,
                "data_handling_notice": (
                    "原文和派生人物观察、档案将由 DeepSeek 处理。"
                ),
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
        ),
        "list_analysis_jobs": Mock(
            return_value={
                "items": [] if job is None else [job],
                "total": 0 if job is None else 1,
            }
        ),
        "list_characters": Mock(
            return_value={
                "items": [character_item()],
                "total": 1,
                "offset": 0,
                "limit": 20,
                "analysis_updated_at": "2026-07-29T10:00:00",
                "analyzed_chapter_count": 3,
                "character_count": 1,
                "relationship_count": 1,
            }
        ),
        "get_character": Mock(
            return_value={
                **character_item(),
                "model": "deepseek-v4-pro",
                "updated_at": "2026-07-29T10:00:00",
            }
        ),
        "list_character_appearances": Mock(
            return_value={
                "items": [
                    {
                        "id": 21,
                        "chapter_index": 1,
                        "chapter_title": "第一章",
                        "mention_count": 3,
                        "summary": "林舟第一次登场。",
                    }
                ],
                "total": 1,
                "offset": 0,
                "limit": 50,
            }
        ),
        "list_character_relationships": Mock(
            return_value={
                "items": [
                    {
                        "id": 31,
                        "source_character_id": 11,
                        "source_character_name": "林舟",
                        "target_character_id": 12,
                        "target_character_name": "苏晚",
                        "directed": False,
                        "relation_type": "盟友",
                        "description": "二人在危机中互相帮助。",
                        "evidence_chapter_indices": [1, 2],
                    }
                ],
                "total": 1,
                "offset": 0,
                "limit": 50,
            }
        ),
        "start_character_analysis": Mock(
            return_value={
                "id": "job-characters",
                "job_type": "characters",
                "status": "queued",
            }
        ),
        "get_chapter": Mock(
            side_effect=AssertionError(
                "人物统计页面不得额外读取章节正文"
            )
        ),
    }
    for name, client in clients.items():
        monkeypatch.setattr(module, name, client, raising=False)
    return clients


def test_novel_detail_exposes_third_character_mode(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    fake.radio_value = "人物统计"
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
    character_workspace = Mock()
    monkeypatch.setattr(
        module,
        "render_character_workspace",
        character_workspace,
        raising=False,
    )
    summary_workspace = Mock()
    reader_workspace = Mock()
    monkeypatch.setattr(module, "render_summary_workspace", summary_workspace)
    monkeypatch.setattr(module, "render_reader_workspace", reader_workspace)

    module.render_novel_detail(7, backend_online=True)

    assert fake.radio_options == ["章节摘要", "正文阅读", "人物统计"]
    character_workspace.assert_called_once()
    summary_workspace.assert_not_called()
    reader_workspace.assert_not_called()


def test_character_actions_require_both_consents_and_force_confirmation(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    clients = configure_character_dependencies(module, monkeypatch)
    fake.checkbox_values.update(
        {
            "character_external_consent_7": True,
            "character_content_rights_7": True,
            "character_force_7": True,
            "character_force_confirm_7": False,
        }
    )
    fake.button_values["start_character_analysis_7"] = True

    module.render_character_workspace({"id": 7, "filename": "story.txt"})

    start_call = next(
        call
        for call in fake.button_calls
        if call["key"] == "start_character_analysis_7"
    )
    assert start_call["disabled"] is True
    clients["start_character_analysis"].assert_not_called()
    checkbox_labels = [
        str(call["label"]) for call in fake.checkbox_calls
    ]
    assert any("DeepSeek" in label and "原文" in label for label in checkbox_labels)
    assert any("有权" in label for label in checkbox_labels)
    combined = "\n".join(message for _, message in fake.messages)
    assert "派生人物观察" in combined
    assert "DeepSeek 开放平台服务条款" in combined
    assert "DeepSeek 隐私政策" in combined
    assert "全量重算预计生成步骤 6" in combined
    assert "最多请求尝试 18" in combined
    assert "费用" not in combined
    assert "价格" not in combined
    clients["get_chapter"].assert_not_called()


def test_confirmed_force_starts_character_job(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    clients = configure_character_dependencies(module, monkeypatch)
    fake.checkbox_values.update(
        {
            "character_external_consent_7": True,
            "character_content_rights_7": True,
            "character_force_7": True,
            "character_force_confirm_7": True,
        }
    )
    fake.button_values["start_character_analysis_7"] = True

    module.render_character_workspace({"id": 7, "filename": "story.txt"})

    clients["start_character_analysis"].assert_called_once_with(
        7,
        confirm_external_processing=True,
        confirm_content_rights=True,
        force=True,
    )


def test_character_jobs_and_results_use_server_side_filters_and_pagination(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    job = {
        "id": "job-running",
        "status": "running",
        "target_chapter_index": None,
        "progress_current": 2,
        "progress_total": 4,
        "error_message": None,
    }
    clients = configure_character_dependencies(
        module,
        monkeypatch,
        job=job,
    )
    fake.session_state["character_page_7"] = 2
    fake.session_state["character_search_snapshot_7"] = "林"
    fake.text_input_values["character_search_7"] = "林"
    fake.selectbox_values["character_sort_7"] = "first_chapter"
    clients["list_characters"].return_value = {
        "items": [],
        "total": 50,
        "offset": 40,
        "limit": 20,
        "analysis_updated_at": "2026-07-29T10:00:00",
        "analyzed_chapter_count": 3,
        "character_count": 50,
        "relationship_count": 12,
    }

    module.render_character_workspace({"id": 7, "filename": "story.txt"})

    clients["list_analysis_jobs"].assert_called_once_with(
        7,
        job_type="characters",
        limit=1,
    )
    clients["list_characters"].assert_called_once_with(
        7,
        offset=40,
        limit=20,
        search="林",
        sort="first_chapter",
    )
    assert fake.progress_values == [50]
    assert any(
        call["key"] == "start_character_analysis_7"
        and call["disabled"] is True
        for call in fake.button_calls
    )


def test_character_detail_lists_are_server_paginated(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    clients = configure_character_dependencies(module, monkeypatch)
    fake.session_state["selected_character_id_7"] = 11
    fake.session_state["character_appearance_page_7_11"] = 2
    fake.session_state["character_relationship_page_7_11"] = 1
    clients["list_character_appearances"].return_value.update(
        {"total": 130, "offset": 100, "limit": 50}
    )
    clients["list_character_relationships"].return_value.update(
        {"total": 80, "offset": 50, "limit": 50}
    )

    module.render_character_workspace({"id": 7, "filename": "story.txt"})

    clients["list_character_appearances"].assert_called_once_with(
        7,
        11,
        offset=100,
        limit=50,
    )
    clients["list_character_relationships"].assert_called_once_with(
        7,
        11,
        offset=50,
        limit=50,
    )
    assert any(
        call["key"] == "character_appearance_previous_7_11"
        for call in fake.button_calls
    )
    assert any(
        call["key"] == "character_relationship_previous_7_11"
        for call in fake.button_calls
    )


def test_character_model_text_is_always_literal_plain_text(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, fake = streamlit_module
    clients = configure_character_dependencies(module, monkeypatch)
    fake.session_state["selected_character_id_7"] = 11
    model_text = {
        "canonical_name": (
            "林舟 ![头像](https://attacker.invalid/avatar.png)"
        ),
        "alias": "<script src=https://attacker.invalid/alias.js></script>",
        "description": (
            "主角 [远程链接](https://attacker.invalid/profile)"
        ),
        "chapter_title": (
            "第一章 ![像素](https://attacker.invalid/chapter.gif)"
        ),
        "appearance": (
            "出场 <img src=https://attacker.invalid/appearance.png>"
        ),
        "source_name": (
            "林舟 <iframe src=https://attacker.invalid/source></iframe>"
        ),
        "target_name": (
            "苏晚 ![关系图](https://attacker.invalid/target.png)"
        ),
        "relation_type": (
            "盟友 [类型](https://attacker.invalid/type)"
        ),
        "relationship": (
            "互助 <script src=https://attacker.invalid/relation.js></script>"
        ),
    }
    unsafe_item = character_item(
        canonical_name=model_text["canonical_name"],
        aliases=[model_text["alias"]],
        short_description=model_text["description"],
    )
    clients["list_characters"].return_value["items"] = [unsafe_item]
    clients["get_character"].return_value = {
        **unsafe_item,
        "model": "deepseek-v4-pro",
        "updated_at": "2026-07-29T10:00:00",
    }
    clients["list_character_appearances"].return_value["items"] = [
        {
            "id": 21,
            "chapter_index": 1,
            "chapter_title": model_text["chapter_title"],
            "mention_count": 3,
            "summary": model_text["appearance"],
        }
    ]
    clients["list_character_relationships"].return_value["items"] = [
        {
            "id": 31,
            "source_character_id": 11,
            "source_character_name": model_text["source_name"],
            "target_character_id": 12,
            "target_character_name": model_text["target_name"],
            "directed": False,
            "relation_type": model_text["relation_type"],
            "description": model_text["relationship"],
            "evidence_chapter_indices": [1, 2],
        }
    ]

    module.render_character_workspace({"id": 7, "filename": "story.txt"})

    rendered_text = [
        message for level, message in fake.messages if level == "text"
    ]
    for value in model_text.values():
        assert value in rendered_text

    rich_messages = [
        message
        for level, message in fake.messages
        if level != "text"
    ]
    widget_labels = [
        str(call["label"])
        for call in (
            fake.checkbox_calls
            + fake.button_calls
            + fake.text_input_calls
            + fake.selectbox_calls
        )
    ]
    for value in model_text.values():
        assert all(value not in message for message in rich_messages)
        assert all(value not in label for label in widget_labels)
        assert all(value not in label for label in fake.expander_labels)


@pytest.mark.parametrize(
    ("payload_name", "field", "invalid_value"),
    [
        ("estimate", "sends_derived_character_data", "true"),
        ("list", "aliases", "小舟"),
        ("detail", "short_description", None),
        ("appearance", "summary", ["不是字符串"]),
        ("relationship", "directed", "false"),
    ],
)
def test_invalid_character_payload_is_user_facing_and_blocks_start(
    streamlit_module: tuple[ModuleType, FakeStreamlit],
    monkeypatch: pytest.MonkeyPatch,
    payload_name: str,
    field: str,
    invalid_value: object,
) -> None:
    module, fake = streamlit_module
    clients = configure_character_dependencies(module, monkeypatch)
    fake.session_state["selected_character_id_7"] = 11
    if payload_name == "estimate":
        clients["get_character_estimate"].return_value[field] = invalid_value
    elif payload_name == "list":
        clients["list_characters"].return_value["items"][0][field] = (
            invalid_value
        )
    elif payload_name == "detail":
        clients["get_character"].return_value[field] = invalid_value
    elif payload_name == "appearance":
        clients["list_character_appearances"].return_value["items"][0][
            field
        ] = invalid_value
    else:
        clients["list_character_relationships"].return_value["items"][0][
            field
        ] = invalid_value
    fake.checkbox_values.update(
        {
            "character_external_consent_7": True,
            "character_content_rights_7": True,
        }
    )
    fake.button_values["start_character_analysis_7"] = True

    module.render_character_workspace({"id": 7, "filename": "story.txt"})

    assert any(
        level == "error" and "数据格式" in message
        for level, message in fake.messages
    )
    if payload_name == "estimate":
        clients["start_character_analysis"].assert_not_called()
