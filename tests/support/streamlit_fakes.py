from __future__ import annotations
"""兼容界面共用的确定性 Streamlit 替身。"""
import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock
import pytest
from legacy.streamlit import api_client

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

    def write(self, body: object) -> None:
        self.root.write(body)


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
        self.text_area_values: dict[str, str] = {}
        self.text_area_calls: list[dict[str, object]] = []
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

    def container(self, **__: object) -> FakeBlock:
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

    def text_area(
        self,
        label: str,
        key: str,
        **__: object,
    ) -> str:
        self.text_area_calls.append({"label": label, "key": key})
        return self.text_area_values.get(key, "")

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
