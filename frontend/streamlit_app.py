"""Streamlit frontend for uploading and reading parsed novels."""

from html import escape

import streamlit as st

from api_client import (
    NovelApiError,
    check_backend,
    upload_novel,
)
from ui_styles import APP_CSS


MAX_FILE_BYTES = 20 * 1024 * 1024

st.set_page_config(
    page_title="NovelMind",
    page_icon="📘",
    layout="wide",
    initial_sidebar_state="collapsed",
)
st.markdown(APP_CSS, unsafe_allow_html=True)


def clear_result() -> None:
    """Clear an earlier result when the selected file changes."""
    st.session_state.pop("novel_result", None)


def format_file_size(size: int) -> str:
    """Return a readable file size."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def render_chapter_table(chapters: list[dict]) -> None:
    """Render a safe HTML table for parsed chapter metadata."""
    rows = "".join(
        (
            "<tr>"
            f'<td class="nm-number">{chapter["index"]:03d}</td>'
            f"<td>{escape(str(chapter['title']))}</td>"
            f'<td class="nm-count">{chapter["character_count"]:,}</td>'
            "</tr>"
        )
        for chapter in chapters
    )
    st.markdown(
        (
            '<div class="nm-table-wrap">'
            '<table class="nm-table">'
            "<thead><tr>"
            "<th>序号</th><th>章节标题</th><th>字符数</th>"
            "</tr></thead>"
            f"<tbody>{rows}</tbody>"
            "</table></div>"
        ),
        unsafe_allow_html=True,
    )


backend_online = check_backend()
status_class = "" if backend_online else " is-offline"
status_text = "后端已连接" if backend_online else "后端未连接"

st.markdown(
    f"""
    <div class="nm-topbar">
        <div class="nm-brand">
            <div class="nm-brand-mark">N</div>
            <div>
                <div class="nm-brand-name">NovelMind</div>
                <div class="nm-brand-area">小说结构工作台</div>
            </div>
        </div>
        <div class="nm-status{status_class}">
            <span class="nm-status-dot"></span>
            <span>{status_text}</span>
        </div>
    </div>
    <div class="nm-page-heading">
        <h1>导入小说</h1>
        <p>TXT · UTF-8 / GB18030 · 最大 20 MB</p>
    </div>
    """,
    unsafe_allow_html=True,
)

uploaded_file = st.file_uploader(
    "TXT 文件",
    type=["txt"],
    help="选择需要解析并保存的小说文本。",
    on_change=clear_result,
)

file_size = len(uploaded_file.getvalue()) if uploaded_file else 0
action_column, note_column = st.columns([1, 3])

with action_column:
    submit = st.button(
        "解析并保存",
        disabled=uploaded_file is None,
    )

with note_column:
    if uploaded_file is None:
        note = "尚未选择文件"
    else:
        note = f"{escape(uploaded_file.name)} · {format_file_size(file_size)}"
    st.markdown(
        f'<div class="nm-file-note">{note}</div>',
        unsafe_allow_html=True,
    )

if submit and uploaded_file is not None:
    if file_size > MAX_FILE_BYTES:
        st.error("TXT 文件不能超过 20 MB。")
    else:
        try:
            with st.spinner("正在解析并保存..."):
                st.session_state["novel_result"] = upload_novel(
                    uploaded_file.name,
                    uploaded_file.getvalue(),
                )
        except NovelApiError as error:
            st.error(str(error))

result = st.session_state.get("novel_result")
if result:
    chapters = result["chapters"]
    total_characters = sum(
        chapter["character_count"] for chapter in chapters
    )

    st.markdown(
        (
            '<div class="nm-success">'
            f"《{escape(result['filename'])}》已完成解析并保存"
            "</div>"
        ),
        unsafe_allow_html=True,
    )

    metric_columns = st.columns(4)
    metric_columns[0].metric("小说 ID", result["novel_id"])
    metric_columns[1].metric("章节数", result["chapter_count"])
    metric_columns[2].metric("文本编码", result["encoding"].upper())
    metric_columns[3].metric("正文字符", f"{total_characters:,}")

    list_tab, reader_tab = st.tabs(["章节列表", "正文预览"])

    with list_tab:
        render_chapter_table(chapters)

    with reader_tab:
        selected_index = st.selectbox(
            "选择章节",
            options=range(len(chapters)),
            format_func=lambda index: (
                f"{chapters[index]['index']:03d} · "
                f"{chapters[index]['title']}"
            ),
        )
        selected_chapter = chapters[selected_index]
        st.markdown(
            (
                '<article class="nm-reader">'
                f"<h2>{escape(str(selected_chapter['title']))}</h2>"
                '<div class="nm-reader-body">'
                f"{escape(str(selected_chapter['content']))}"
                "</div></article>"
            ),
            unsafe_allow_html=True,
        )
