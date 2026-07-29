"""Streamlit workspace for importing, reading, and analyzing novels."""

from html import escape
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import streamlit as st

from api_client import (
    NovelApiError,
    check_backend,
    delete_novel,
    get_character,
    get_character_estimate,
    get_chapter,
    get_chapter_summary,
    get_novel,
    get_summary_estimate,
    get_system_status,
    list_analysis_jobs,
    list_character_appearances,
    list_character_relationships,
    list_characters,
    list_chapters,
    list_novels,
    list_summaries,
    start_character_analysis,
    start_chapter_summary,
    start_summaries,
    upload_novel,
)
from ui_styles import APP_CSS


MAX_FILE_BYTES = 20 * 1024 * 1024
LIBRARY_PAGE_SIZE = 10
CHAPTER_PAGE_SIZE = 50
SUMMARY_PAGE_SIZE = 20
CHARACTER_PAGE_SIZE = 20
CHARACTER_DETAIL_PAGE_SIZE = 50
ACTIVE_JOB_STATUSES = {"queued", "running"}


class UiPayloadError(ValueError):
    """An unexpected backend response that should be explained in the UI."""


def _render_plain_text(value: str) -> None:
    """Render untrusted API/model output without Markdown or HTML parsing."""
    st.text(value)


def _require_object(value: Any, context: str) -> Dict[str, Any]:
    """Return a JSON object or raise a stable, user-facing validation error."""
    if not isinstance(value, dict):
        raise UiPayloadError(f"{context}应为对象")
    return value


def _require_list(
    payload: Dict[str, Any],
    key: str,
    context: str,
) -> list[Any]:
    """Return one required list field."""
    value = payload.get(key)
    if not isinstance(value, list):
        raise UiPayloadError(f"{context}.{key} 应为数组")
    return value


def _require_int(
    payload: Dict[str, Any],
    key: str,
    context: str,
    *,
    minimum: int = 0,
) -> int:
    """Return one required integer field, rejecting booleans."""
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise UiPayloadError(f"{context}.{key} 应为整数")
    if value < minimum:
        raise UiPayloadError(
            f"{context}.{key} 不能小于 {minimum}"
        )
    return value


def _optional_int(
    payload: Dict[str, Any],
    key: str,
    context: str,
    *,
    minimum: int = 0,
) -> Optional[int]:
    """Return one optional integer field."""
    if key not in payload:
        raise UiPayloadError(f"{context}.{key} 为必需字段")
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise UiPayloadError(f"{context}.{key} 应为整数或 null")
    if value < minimum:
        raise UiPayloadError(
            f"{context}.{key} 不能小于 {minimum}"
        )
    return value


def _require_text(
    payload: Dict[str, Any],
    key: str,
    context: str,
    *,
    allow_empty: bool = False,
) -> str:
    """Return one required string field."""
    value = payload.get(key)
    if not isinstance(value, str):
        raise UiPayloadError(f"{context}.{key} 应为字符串")
    if not allow_empty and not value.strip():
        raise UiPayloadError(f"{context}.{key} 不能为空")
    return value


def _optional_text(
    payload: Dict[str, Any],
    key: str,
    context: str,
) -> Optional[str]:
    """Return one optional string field."""
    if key not in payload:
        raise UiPayloadError(f"{context}.{key} 为必需字段")
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise UiPayloadError(f"{context}.{key} 应为字符串或 null")
    return value


def _require_bool(
    payload: Dict[str, Any],
    key: str,
    context: str,
) -> bool:
    """Return one required boolean field."""
    value = payload.get(key)
    if not isinstance(value, bool):
        raise UiPayloadError(f"{context}.{key} 应为布尔值")
    return value


def _require_text_list(
    payload: Dict[str, Any],
    key: str,
    context: str,
) -> list[str]:
    """Return one required list containing only non-empty strings."""
    values = _require_list(payload, key, context)
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value.strip():
            raise UiPayloadError(
                f"{context}.{key}[{index}] 应为非空字符串"
            )
    return values


def _require_int_list(
    payload: Dict[str, Any],
    key: str,
    context: str,
    *,
    minimum: int = 0,
) -> list[int]:
    """Return one required list containing bounded integers."""
    values = _require_list(payload, key, context)
    for index, value in enumerate(values):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < minimum
        ):
            raise UiPayloadError(
                f"{context}.{key}[{index}] 应为不小于 "
                f"{minimum} 的整数"
            )
    return values


def _require_deepseek_url(
    payload: Dict[str, Any],
    key: str,
    context: str,
) -> str:
    """Return a safe HTTPS link hosted on the DeepSeek domain."""
    value = _require_text(payload, key, context)
    try:
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").lower()
    except ValueError as error:
        raise UiPayloadError(
            f"{context}.{key} 不是有效链接"
        ) from error
    if (
        parsed.scheme != "https"
        or not hostname
        or (
            hostname != "deepseek.com"
            and not hostname.endswith(".deepseek.com")
        )
        or parsed.username is not None
        or parsed.password is not None
        or any(character.isspace() for character in value)
        or any(character in value for character in "()")
    ):
        raise UiPayloadError(
            f"{context}.{key} 应为 DeepSeek 官方 HTTPS 链接"
        )
    return value


def _show_payload_error(context: str, error: UiPayloadError) -> None:
    """Explain a response-contract problem without exposing a traceback."""
    st.error(
        f"{context}：后端返回的数据格式不正确（{error}）。"
        "请刷新页面；若问题持续，请重启后端。"
    )


def _validate_novel(
    value: Any,
    context: str = "小说信息",
) -> Dict[str, Any]:
    """Validate fields used by library cards and the novel workspace."""
    novel = _require_object(value, context)
    _require_int(novel, "id", context, minimum=1)
    _require_text(novel, "filename", context)
    _require_int(novel, "chapter_count", context)
    _require_int(novel, "character_count", context)
    _require_text(novel, "encoding", context)
    _require_text(novel, "created_at", context)
    return novel


def _validate_chapter_metadata(
    value: Any,
    context: str,
) -> Dict[str, Any]:
    """Validate chapter fields used by selectors."""
    chapter = _require_object(value, context)
    _require_int(chapter, "index", context, minimum=1)
    _require_text(chapter, "title", context)
    return chapter


def initialize_session_state() -> None:
    """Create stable application state without overwriting user choices."""
    defaults = {
        "view": "library",
        "library_page": 0,
        "selected_novel_id": None,
        "reader_chapter_page": 0,
        "reader_chapter_index": None,
        "delete_candidate_id": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def clear_result() -> None:
    """Clear an earlier upload notice when the selected file changes."""
    st.session_state.pop("upload_result", None)


def format_file_size(size: int) -> str:
    """Return a readable file size."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def last_page_index(total: int, page_size: int) -> int:
    """Return the final zero-based page index for a collection."""
    if total <= 0:
        return 0
    return (total - 1) // page_size


def render_topbar(backend_online: bool) -> None:
    """Render the common product header and backend state."""
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
        """,
        unsafe_allow_html=True,
    )


def render_page_heading(title: str, description: str) -> None:
    """Render one consistent page heading."""
    st.markdown(
        (
            '<div class="nm-page-heading">'
            f"<h1>{escape(title)}</h1>"
            f"<p>{escape(description)}</p>"
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def select_novel(novel_id: int) -> None:
    """Open one novel and reset reader-specific state."""
    st.session_state["selected_novel_id"] = novel_id
    st.session_state["reader_chapter_page"] = 0
    st.session_state["reader_chapter_index"] = None
    st.session_state["view"] = "library"
    st.session_state.pop("delete_candidate_id", None)


def return_to_library() -> None:
    """Close the current novel while preserving the library page."""
    st.session_state["selected_novel_id"] = None
    st.session_state["reader_chapter_page"] = 0
    st.session_state["reader_chapter_index"] = None
    st.session_state.pop("delete_candidate_id", None)


def render_navigation() -> None:
    """Render navigation compatible with Streamlit 1.12."""
    st.sidebar.markdown("### 工作区")
    if st.sidebar.button(
        "资料库",
        key="nav_library",
        disabled=st.session_state["view"] == "library",
    ):
        st.session_state["view"] = "library"
        st.session_state.pop("delete_candidate_id", None)
    if st.sidebar.button(
        "导入小说",
        key="nav_upload",
        disabled=st.session_state["view"] == "upload",
    ):
        st.session_state["view"] = "upload"
        st.session_state.pop("delete_candidate_id", None)


def render_upload_page(backend_online: bool) -> None:
    """Render TXT upload and move successful imports into the library."""
    render_page_heading(
        "导入小说",
        "TXT · UTF-8 / GB18030 · 最大 20 MB",
    )
    if not backend_online:
        st.error("后端未连接。请先启动 FastAPI 服务，再上传小说。")

    uploaded_file = st.file_uploader(
        "TXT 文件",
        type=["txt"],
        help="选择需要解析并保存的小说文本。",
        on_change=clear_result,
        key="novel_upload",
    )
    file_size = len(uploaded_file.getvalue()) if uploaded_file else 0
    action_column, note_column = st.columns([1, 3])

    with action_column:
        submit = st.button(
            "解析并保存",
            key="submit_upload",
            disabled=uploaded_file is None or not backend_online,
        )

    with note_column:
        if uploaded_file is None:
            note = "尚未选择文件"
        else:
            note = (
                f"{escape(uploaded_file.name)} · "
                f"{format_file_size(file_size)}"
            )
        st.markdown(
            f'<div class="nm-file-note">{note}</div>',
            unsafe_allow_html=True,
        )

    if not submit or uploaded_file is None:
        return
    if file_size > MAX_FILE_BYTES:
        st.error("TXT 文件不能超过 20 MB。")
        return

    try:
        with st.spinner("正在解析并保存..."):
            result = upload_novel(
                uploaded_file.name,
                uploaded_file.getvalue(),
            )
    except NovelApiError as error:
        st.error(str(error))
        return

    try:
        upload_result = _require_object(result, "上传结果")
        novel_id = _require_int(
            upload_result,
            "novel_id",
            "上传结果",
            minimum=1,
        )
        filename = _require_text(
            upload_result,
            "filename",
            "上传结果",
        )
    except UiPayloadError as error:
        _show_payload_error("无法确认上传结果", error)
        return

    select_novel(novel_id)
    st.session_state["library_page"] = 0
    st.session_state["flash_message"] = (
        f"《{filename}》已完成解析并保存。"
    )
    st.rerun()


def render_library_card(
    novel: Dict[str, Any],
    total: int,
) -> None:
    """Render one saved novel without loading any chapter body."""
    try:
        novel = _validate_novel(novel, "资料库小说")
    except UiPayloadError as error:
        _show_payload_error("无法显示资料库小说", error)
        return

    novel_id = _require_int(
        novel,
        "id",
        "资料库小说",
        minimum=1,
    )
    filename = _require_text(novel, "filename", "资料库小说")
    st.markdown(
        (
            '<div class="nm-table-wrap" style="padding: 1rem;">'
            f"<strong>{escape(filename)}</strong><br>"
            '<span style="color: #5f6368; font-size: 13px;">'
            f"{novel['chapter_count']:,} 章 · "
            f"{novel['character_count']:,} 字符 · "
            f"{escape(novel['encoding'].upper())} · "
            f"{escape(novel['created_at'].replace('T', ' ')[:16])}"
            "</span></div>"
        ),
        unsafe_allow_html=True,
    )

    open_column, delete_column = st.columns([3, 1])
    with open_column:
        if st.button(
            "打开小说",
            key=f"open_novel_{novel_id}",
        ):
            select_novel(novel_id)
            st.rerun()
    with delete_column:
        if st.button(
            "删除",
            key=f"delete_novel_{novel_id}",
        ):
            st.session_state["delete_candidate_id"] = novel_id

    if st.session_state.get("delete_candidate_id") != novel_id:
        return

    st.warning(
        f"确认永久删除《{filename}》？此操作仅删除本机 SQLite 数据"
        "（小说、章节和分析结果），且无法撤销；"
        "不影响 DeepSeek 已处理、缓存或留存的数据。"
    )
    confirm_column, cancel_column = st.columns(2)
    with confirm_column:
        confirmed = st.button(
            "确认永久删除",
            key=f"confirm_delete_novel_{novel_id}",
        )
    with cancel_column:
        cancelled = st.button(
            "取消",
            key=f"cancel_delete_novel_{novel_id}",
        )

    if cancelled:
        st.session_state.pop("delete_candidate_id", None)
        st.rerun()
        return
    if not confirmed:
        return

    try:
        delete_novel(novel_id)
    except NovelApiError as error:
        if error.status_code == 404:
            if st.session_state.get("selected_novel_id") == novel_id:
                return_to_library()
            st.session_state.pop("delete_candidate_id", None)
            st.session_state["flash_warning"] = (
                "这部小说已不存在，资料库已刷新。"
            )
            st.warning("这部小说已不存在，资料库将刷新。")
            st.rerun()
        else:
            st.error(str(error))
        return

    if st.session_state.get("selected_novel_id") == novel_id:
        return_to_library()
    st.session_state.pop("delete_candidate_id", None)
    remaining = max(0, total - 1)
    page = max(0, int(st.session_state.get("library_page", 0)))
    st.session_state["library_page"] = min(
        page,
        last_page_index(remaining, LIBRARY_PAGE_SIZE),
    )
    st.session_state["flash_message"] = f"《{filename}》已永久删除。"
    st.rerun()


def render_library_page(backend_online: bool) -> None:
    """Render the persisted novel library or the selected novel."""
    selected_novel_id = st.session_state.get("selected_novel_id")
    if selected_novel_id is not None:
        render_novel_detail(int(selected_novel_id), backend_online)
        return

    render_page_heading("资料库", "从本地 SQLite 重新打开已导入的小说")
    if not backend_online:
        st.error("后端未连接，暂时无法读取资料库。")
        return

    page = max(0, int(st.session_state.get("library_page", 0)))
    try:
        payload = list_novels(
            offset=page * LIBRARY_PAGE_SIZE,
            limit=LIBRARY_PAGE_SIZE,
        )
    except NovelApiError as error:
        st.error(str(error))
        return

    try:
        payload = _require_object(payload, "资料库响应")
        total = _require_int(payload, "total", "资料库响应")
        items = _require_list(payload, "items", "资料库响应")
        novels = [
            _validate_novel(item, f"资料库响应.items[{index}]")
            for index, item in enumerate(items)
        ]
    except UiPayloadError as error:
        _show_payload_error("无法读取资料库", error)
        return

    final_page = last_page_index(total, LIBRARY_PAGE_SIZE)
    if page > final_page:
        st.session_state["library_page"] = final_page
        st.session_state.pop("delete_candidate_id", None)
        st.rerun()
        return

    if not novels:
        st.info("资料库还是空的。请先导入一部 TXT 小说。")
        if st.button("去导入小说", key="empty_library_upload"):
            st.session_state["view"] = "upload"
            st.session_state.pop("delete_candidate_id", None)
            st.rerun()
        return

    st.caption(f"共 {total:,} 部小说")
    for novel in novels:
        render_library_card(novel, total)

    previous_column, page_column, next_column = st.columns([1, 2, 1])
    with previous_column:
        if st.button(
            "上一页",
            key="library_previous",
            disabled=page == 0,
        ):
            st.session_state["library_page"] = page - 1
            st.session_state.pop("delete_candidate_id", None)
            st.rerun()
    with page_column:
        st.markdown(
            f"<div style='text-align:center'>第 {page + 1} / "
            f"{final_page + 1} 页</div>",
            unsafe_allow_html=True,
        )
    with next_column:
        if st.button(
            "下一页",
            key="library_next",
            disabled=page >= final_page,
        ):
            st.session_state["library_page"] = page + 1
            st.session_state.pop("delete_candidate_id", None)
            st.rerun()


def render_novel_metrics(novel: Dict[str, Any]) -> None:
    """Render aggregate metadata for one selected novel."""
    columns = st.columns(4)
    columns[0].metric("小说 ID", novel["id"])
    columns[1].metric("章节数", f"{int(novel['chapter_count']):,}")
    columns[2].metric("文本编码", str(novel["encoding"]).upper())
    columns[3].metric(
        "正文字符",
        f"{int(novel['character_count']):,}",
    )


def render_novel_detail(novel_id: int, backend_online: bool) -> None:
    """Render a selected novel and switch between its workspace modes."""
    if st.button("← 返回资料库", key=f"back_to_library_{novel_id}"):
        return_to_library()
        st.rerun()
        return
    if not backend_online:
        st.error("后端未连接，暂时无法打开小说。")
        return

    try:
        novel = get_novel(novel_id)
    except NovelApiError as error:
        st.error(str(error))
        if error.status_code == 404:
            return_to_library()
        return
    try:
        novel = _validate_novel(novel)
    except UiPayloadError as error:
        _show_payload_error("无法打开小说", error)
        return

    render_page_heading(
        novel["filename"],
        "章节阅读、DeepSeek 摘要与人物统计",
    )
    render_novel_metrics(novel)
    mode = st.radio(
        "小说工作区",
        options=["章节摘要", "正文阅读", "人物统计"],
        horizontal=True,
        key=f"novel_mode_{novel_id}",
    )
    if mode == "章节摘要":
        render_summary_workspace(novel)
    elif mode == "正文阅读":
        render_reader_workspace(novel)
    else:
        render_character_workspace(novel)


def _summary_start_error(error: NovelApiError) -> None:
    """Show actionable feedback for summary task creation failures."""
    if error.status_code == 409:
        st.warning(
            "已有其他范围的摘要任务正在运行，"
            "请等待该摘要任务完成后再试。"
        )
    elif error.status_code == 503:
        st.error(
            "DeepSeek 尚未配置。请在本地 .env 中设置 "
            "DEEPSEEK_API_KEY，并重启后端。"
        )
    else:
        st.error(str(error))


def _render_generation_status(
    novel_id: int,
) -> tuple[bool, Optional[Dict[str, Any]]]:
    """Render provider state and the recoverable latest summary job."""
    generation_configured = False
    try:
        system_status = get_system_status()
        system_status = _require_object(system_status, "系统状态")
        generation = _require_object(
            system_status.get("generation"),
            "系统状态.generation",
        )
        generation_configured = _require_bool(
            generation,
            "configured",
            "系统状态.generation",
        )
        provider = _require_text(
            generation,
            "provider",
            "系统状态.generation",
        ).title()
        model = _require_text(
            generation,
            "model",
            "系统状态.generation",
        )
        if generation_configured:
            st.success(f"{provider} 已配置 · {model}")
        else:
            st.warning(
                f"{provider} 未配置 · {model}。"
                "请先在本地 .env 中设置 DEEPSEEK_API_KEY。"
            )
    except NovelApiError as error:
        st.warning(f"暂时无法读取 DeepSeek 配置状态：{error}")
    except UiPayloadError as error:
        _show_payload_error("无法读取 DeepSeek 配置状态", error)

    latest_job: Optional[Dict[str, Any]] = None
    jobs_available = True
    try:
        jobs = list_analysis_jobs(
            novel_id,
            job_type="chapter_summaries",
            limit=1,
        )
        jobs = _require_object(jobs, "摘要任务列表")
        items = _require_list(jobs, "items", "摘要任务列表")
        _require_int(jobs, "total", "摘要任务列表")
        if items:
            latest_job = _require_object(
                items[0],
                "摘要任务列表.items[0]",
            )
            _require_text(
                latest_job,
                "id",
                "摘要任务列表.items[0]",
            )
            _require_text(
                latest_job,
                "status",
                "摘要任务列表.items[0]",
            )
            _require_int(
                latest_job,
                "progress_current",
                "摘要任务列表.items[0]",
            )
            _require_int(
                latest_job,
                "progress_total",
                "摘要任务列表.items[0]",
            )
            _optional_int(
                latest_job,
                "target_chapter_index",
                "摘要任务列表.items[0]",
                minimum=1,
            )
            _optional_text(
                latest_job,
                "error_message",
                "摘要任务列表.items[0]",
            )
    except NovelApiError as error:
        st.warning(f"暂时无法恢复最近摘要任务：{error}")
        jobs_available = False
    except UiPayloadError as error:
        _show_payload_error("无法恢复最近摘要任务", error)
        latest_job = None
        jobs_available = False

    if latest_job is not None:
        status_value = latest_job["status"]
        status_labels = {
            "queued": "等待执行",
            "running": "正在执行",
            "succeeded": "已完成",
            "failed": "执行失败",
        }
        current = latest_job["progress_current"]
        total = latest_job["progress_total"]
        target_chapter_index = latest_job.get("target_chapter_index")
        scope = (
            "全书"
            if target_chapter_index is None
            else f"第 {target_chapter_index} 章"
        )
        percent = (
            0
            if total == 0
            else (current * 100 + total // 2) // total
        )
        percent = max(0, min(100, percent))
        st.subheader("最近摘要任务")
        st.write(
            f"{scope} · {status_labels.get(status_value, status_value)} · "
            f"{current:,} / {total:,} 章"
        )
        st.progress(percent)
        if status_value == "failed":
            st.error(
                str(
                    latest_job.get("error_message")
                    or "摘要任务失败，请重新启动任务。"
                )
            )
    if st.button("刷新任务状态", key=f"refresh_summary_job_{novel_id}"):
        st.rerun()

    return generation_configured and jobs_available, latest_job


def _render_summary_estimate(novel_id: int) -> bool:
    """Render external-processing disclosure and return availability."""
    try:
        estimate = get_summary_estimate(novel_id)
    except NovelApiError as error:
        st.error(f"无法读取摘要估算：{error}")
        return False
    try:
        estimate = _require_object(estimate, "摘要估算")
        chapter_count = _require_int(
            estimate,
            "chapter_count",
            "摘要估算",
        )
        character_count = _require_int(
            estimate,
            "character_count",
            "摘要估算",
        )
        generation_steps = _require_int(
            estimate,
            "estimated_generation_steps",
            "摘要估算",
        )
        maximum_attempts = _require_int(
            estimate,
            "maximum_request_attempts",
            "摘要估算",
        )
        provider = _require_text(
            estimate,
            "provider",
            "摘要估算",
        )
        if provider.lower() != "deepseek":
            raise UiPayloadError("摘要估算.provider 应为 deepseek")
        model = _require_text(
            estimate,
            "model",
            "摘要估算",
        )
        sends_original_text = _require_bool(
            estimate,
            "sends_original_text",
            "摘要估算",
        )
        sends_chapter_titles = _require_bool(
            estimate,
            "sends_chapter_titles",
            "摘要估算",
        )
        data_handling_notice = _require_text(
            estimate,
            "data_handling_notice",
            "摘要估算",
        )
        terms_url = _require_deepseek_url(
            estimate,
            "terms_url",
            "摘要估算",
        )
        privacy_url = _require_deepseek_url(
            estimate,
            "privacy_url",
            "摘要估算",
        )
    except UiPayloadError as error:
        _show_payload_error("无法读取摘要估算", error)
        return False

    st.subheader("全书摘要估算")
    columns = st.columns(4)
    columns[0].metric("章节", chapter_count)
    columns[1].metric(
        "正文字符",
        f"{character_count:,}",
    )
    columns[2].metric(
        "生成步骤",
        generation_steps,
    )
    columns[3].metric(
        "最多请求尝试",
        maximum_attempts,
    )
    st.caption(f"目标 API：DeepSeek · {model}")
    sent_content = []
    if sends_chapter_titles:
        sent_content.append("章节标题")
    if sends_original_text:
        sent_content.append("章节正文")
    if sent_content:
        st.caption(f"将发送至 DeepSeek：{'、'.join(sent_content)}")
    st.warning(data_handling_notice)
    st.markdown(
        f"[DeepSeek 开放平台服务条款]({terms_url}) · "
        f"[DeepSeek 隐私政策]({privacy_url})"
    )
    return True


def _load_summary_chapter(
    novel_id: int,
) -> Optional[Dict[str, Any]]:
    """Select one chapter using metadata only, never its original body."""
    page_key = f"summary_chapter_page_{novel_id}"
    page = max(0, int(st.session_state.get(page_key, 0)))
    try:
        payload = list_chapters(
            novel_id,
            offset=page * CHAPTER_PAGE_SIZE,
            limit=CHAPTER_PAGE_SIZE,
        )
    except NovelApiError as error:
        st.error(f"无法读取章节列表：{error}")
        return None

    try:
        payload = _require_object(payload, "章节列表")
        total = _require_int(payload, "total", "章节列表")
        items = _require_list(payload, "items", "章节列表")
        chapters = [
            _validate_chapter_metadata(
                chapter,
                f"章节列表.items[{index}]",
            )
            for index, chapter in enumerate(items)
        ]
    except UiPayloadError as error:
        _show_payload_error("无法读取章节列表", error)
        return None

    final_page = last_page_index(total, CHAPTER_PAGE_SIZE)
    if page > final_page:
        st.session_state[page_key] = final_page
        st.rerun()
        return None

    if not chapters:
        st.info("这部小说没有可分析的章节。")
        return None

    options = [chapter["index"] for chapter in chapters]
    selected_index = st.selectbox(
        "选择要查看或生成摘要的章节",
        options=options,
        format_func=lambda index: next(
            (
                f"{index:03d} · {chapter['title']}"
                for chapter in chapters
                if chapter["index"] == index
            ),
            str(index),
        ),
        key=f"summary_chapter_select_{novel_id}_{page}",
    )
    selected = next(
        chapter
        for chapter in chapters
        if chapter["index"] == selected_index
    )

    if final_page > 0:
        previous_column, page_column, next_column = st.columns([1, 2, 1])
        with previous_column:
            if st.button(
                "上一批章节",
                key=f"summary_chapter_previous_{novel_id}",
                disabled=page == 0,
            ):
                st.session_state[page_key] = page - 1
                st.rerun()
        with page_column:
            st.write(f"章节页 {page + 1} / {final_page + 1}")
        with next_column:
            if st.button(
                "下一批章节",
                key=f"summary_chapter_next_{novel_id}",
                disabled=page >= final_page,
            ):
                st.session_state[page_key] = page + 1
                st.rerun()
    return selected


def _render_selected_chapter_summary(
    novel_id: int,
    chapter: Dict[str, Any],
) -> None:
    """Render one selected saved summary without loading original text."""
    chapter_index = int(chapter["index"])
    st.subheader("当前章节摘要")
    try:
        summary = get_chapter_summary(novel_id, chapter_index)
    except NovelApiError as error:
        if error.status_code == 404:
            st.info("该章节尚未生成摘要。")
        else:
            st.error(str(error))
        return

    try:
        summary = _require_object(summary, "章节摘要")
        summary_text = _require_text(
            summary,
            "summary",
            "章节摘要",
        )
        key_events = _require_list(
            summary,
            "key_events",
            "章节摘要",
        )
        if any(not isinstance(event, str) for event in key_events):
            raise UiPayloadError("章节摘要.key_events 只能包含字符串")
        plot_progression = _require_text(
            summary,
            "plot_progression",
            "章节摘要",
        )
        cliffhanger = _optional_text(
            summary,
            "cliffhanger",
            "章节摘要",
        )
        model = _require_text(summary, "model", "章节摘要")
        updated_at = _require_text(
            summary,
            "updated_at",
            "章节摘要",
        )
    except UiPayloadError as error:
        _show_payload_error("无法显示章节摘要", error)
        return

    _render_plain_text(summary_text)
    if key_events:
        st.caption("关键事件")
        for event in key_events:
            _render_plain_text(event)
    st.caption("剧情推进")
    _render_plain_text(plot_progression)
    if cliffhanger:
        st.caption("悬念")
        _render_plain_text(cliffhanger)
    st.caption(f"模型：{model} · 更新：{updated_at}")


def _render_summary_actions(
    novel_id: int,
    chapter: Optional[Dict[str, Any]],
    generation_configured: bool,
    latest_job: Optional[Dict[str, Any]],
    estimate_available: bool,
) -> None:
    """Render consent-gated full-book and single-chapter task actions."""
    st.subheader("生成摘要")
    consent = st.checkbox(
        "我明确同意将小说章节标题与正文发送至 DeepSeek API",
        key=f"summary_consent_{novel_id}",
    )
    content_rights_confirmed = st.checkbox(
        (
            "我确认有权将该小说（含标题与正文）提交给外部服务，"
            "并理解 DeepSeek 可能依据其条款缓存、留存或用于服务改进，"
            "且数据可能在中国境内处理和存储"
        ),
        key=f"summary_content_rights_{novel_id}",
    )
    force = st.checkbox(
        "强制重新生成已有摘要（force）",
        key=f"summary_force_{novel_id}",
    )
    force_confirmed = False
    if force:
        st.warning(
            "强制重新生成会覆盖当前已有摘要，并再次发送相关正文。"
        )
        force_confirmed = st.checkbox(
            "我确认覆盖已有摘要并再次发送正文",
            key=f"summary_force_confirm_{novel_id}",
        )

    active_job = (
        latest_job is not None
        and str(latest_job.get("status")) in ACTIVE_JOB_STATUSES
    )
    actions_disabled = (
        not generation_configured
        or not estimate_available
        or not consent
        or not content_rights_confirmed
        or active_job
        or (force and not force_confirmed)
    )

    full_column, chapter_column = st.columns(2)
    with full_column:
        start_all = st.button(
            "生成全书缺失摘要" if not force else "重新生成全书摘要",
            key=f"start_all_summaries_{novel_id}",
            disabled=actions_disabled,
        )
    with chapter_column:
        chapter_index = (
            int(chapter["index"]) if chapter is not None else None
        )
        start_one = st.button(
            "生成本章摘要" if not force else "重新生成本章摘要",
            key=(
                f"start_chapter_summary_{novel_id}_"
                f"{chapter_index if chapter_index is not None else 'none'}"
            ),
            disabled=actions_disabled or chapter is None,
        )

    if start_all:
        try:
            job = start_summaries(
                novel_id,
                confirm_external_processing=consent,
                confirm_content_rights=content_rights_confirmed,
                force=force,
            )
        except NovelApiError as error:
            _summary_start_error(error)
        else:
            try:
                job = _require_object(job, "摘要任务")
                job_id = _require_text(job, "id", "摘要任务")
            except UiPayloadError as error:
                _show_payload_error("无法确认摘要任务", error)
                return
            st.success(f"摘要任务已创建：{job_id}")
            st.rerun()

    if start_one and chapter is not None:
        try:
            job = start_chapter_summary(
                novel_id,
                int(chapter["index"]),
                confirm_external_processing=consent,
                confirm_content_rights=content_rights_confirmed,
                force=force,
            )
        except NovelApiError as error:
            _summary_start_error(error)
        else:
            try:
                job = _require_object(job, "章节摘要任务")
                job_id = _require_text(
                    job,
                    "id",
                    "章节摘要任务",
                )
            except UiPayloadError as error:
                _show_payload_error("无法确认章节摘要任务", error)
                return
            st.success(f"本章摘要任务已创建：{job_id}")
            st.rerun()


def _render_saved_summaries(novel_id: int) -> None:
    """Render saved summaries with server-side pagination."""
    page_key = f"summary_page_{novel_id}"
    page = max(0, int(st.session_state.get(page_key, 0)))
    try:
        payload = list_summaries(
            novel_id,
            offset=page * SUMMARY_PAGE_SIZE,
            limit=SUMMARY_PAGE_SIZE,
        )
    except NovelApiError as error:
        st.error(f"无法读取已有摘要：{error}")
        return

    try:
        payload = _require_object(payload, "摘要列表")
        completed = _require_int(payload, "completed", "摘要列表")
        total = _require_int(payload, "total", "摘要列表")
        items = _require_list(payload, "items", "摘要列表")
        summaries = []
        for index, item in enumerate(items):
            context = f"摘要列表.items[{index}]"
            summary = _require_object(item, context)
            _require_int(
                summary,
                "chapter_index",
                context,
                minimum=1,
            )
            _require_text(summary, "chapter_title", context)
            _require_text(summary, "summary", context)
            events = _require_list(summary, "key_events", context)
            if any(not isinstance(event, str) for event in events):
                raise UiPayloadError(
                    f"{context}.key_events 只能包含字符串"
                )
            _require_text(summary, "plot_progression", context)
            _optional_text(summary, "cliffhanger", context)
            summaries.append(summary)
    except UiPayloadError as error:
        _show_payload_error("无法读取已有摘要", error)
        return

    final_page = last_page_index(completed, SUMMARY_PAGE_SIZE)
    if page > final_page:
        st.session_state[page_key] = final_page
        st.rerun()
        return

    st.subheader(f"已有摘要 · {completed:,} / {total:,} 章")
    if not summaries:
        st.info("尚未保存任何章节摘要。")
        return

    for summary in summaries:
        chapter_index = int(summary["chapter_index"])
        with st.expander(f"第 {chapter_index:03d} 章摘要"):
            st.caption("章节标题")
            _render_plain_text(summary["chapter_title"])
            _render_plain_text(summary["summary"])
            events = summary.get("key_events", [])
            if events:
                st.caption("关键事件")
                for event in events:
                    _render_plain_text(event)
            st.caption("剧情推进")
            _render_plain_text(summary["plot_progression"])
            if summary.get("cliffhanger"):
                st.caption("悬念")
                _render_plain_text(summary["cliffhanger"])

    if final_page > 0:
        previous_column, page_column, next_column = st.columns([1, 2, 1])
        with previous_column:
            if st.button(
                "上一页摘要",
                key=f"summary_previous_{novel_id}",
                disabled=page == 0,
            ):
                st.session_state[page_key] = page - 1
                st.rerun()
        with page_column:
            st.write(f"摘要页 {page + 1} / {final_page + 1}")
        with next_column:
            if st.button(
                "下一页摘要",
                key=f"summary_next_{novel_id}",
                disabled=page >= final_page,
            ):
                st.session_state[page_key] = page + 1
                st.rerun()


def render_summary_workspace(novel: Dict[str, Any]) -> None:
    """Render disclosure, actions, progress, and saved summaries."""
    try:
        novel = _require_object(novel, "小说信息")
        novel_id = _require_int(
            novel,
            "id",
            "小说信息",
            minimum=1,
        )
    except UiPayloadError as error:
        _show_payload_error("无法打开摘要工作区", error)
        return
    st.markdown("---")
    st.subheader("DeepSeek 章节摘要")
    generation_configured, latest_job = _render_generation_status(
        novel_id
    )
    estimate_available = _render_summary_estimate(novel_id)
    chapter = _load_summary_chapter(novel_id)
    if chapter is not None:
        _render_selected_chapter_summary(novel_id, chapter)
    _render_summary_actions(
        novel_id,
        chapter,
        generation_configured,
        latest_job,
        estimate_available,
    )
    _render_saved_summaries(novel_id)


def _character_start_error(error: NovelApiError) -> None:
    """Show actionable feedback for character task creation failures."""
    if error.status_code == 409:
        st.warning(
            "已有分析任务正在运行，请等待该任务完成后再启动人物统计。"
        )
    elif error.status_code == 503:
        st.error(
            "DeepSeek 尚未配置。请在本地 .env 中设置 "
            "DEEPSEEK_API_KEY，并重启后端。"
        )
    else:
        st.error(str(error))


def _render_character_generation_status(
    novel_id: int,
) -> tuple[bool, Optional[Dict[str, Any]]]:
    """Render provider state and the latest character-analysis job."""
    generation_configured = False
    try:
        system_status = get_system_status()
        system_status = _require_object(system_status, "系统状态")
        generation = _require_object(
            system_status.get("generation"),
            "系统状态.generation",
        )
        generation_configured = _require_bool(
            generation,
            "configured",
            "系统状态.generation",
        )
        provider = _require_text(
            generation,
            "provider",
            "系统状态.generation",
        )
        model = _require_text(
            generation,
            "model",
            "系统状态.generation",
        )
        if provider.lower() != "deepseek":
            raise UiPayloadError(
                "系统状态.generation.provider 应为 deepseek"
            )
        if generation_configured:
            st.success("DeepSeek 人物分析已配置")
            st.caption("当前生成模型")
            _render_plain_text(model)
        else:
            st.warning(
                "DeepSeek 未配置。请先在本地 .env 中设置 "
                "DEEPSEEK_API_KEY。"
            )
    except NovelApiError as error:
        st.warning(f"暂时无法读取 DeepSeek 配置状态：{error}")
    except UiPayloadError as error:
        _show_payload_error("无法读取 DeepSeek 配置状态", error)

    latest_job: Optional[Dict[str, Any]] = None
    jobs_available = True
    try:
        jobs = list_analysis_jobs(
            novel_id,
            job_type="characters",
            limit=1,
        )
        jobs = _require_object(jobs, "人物统计任务列表")
        items = _require_list(
            jobs,
            "items",
            "人物统计任务列表",
        )
        _require_int(jobs, "total", "人物统计任务列表")
        if items:
            context = "人物统计任务列表.items[0]"
            latest_job = _require_object(items[0], context)
            _require_text(latest_job, "id", context)
            status = _require_text(latest_job, "status", context)
            if status not in {
                "queued",
                "running",
                "succeeded",
                "failed",
            }:
                raise UiPayloadError(
                    f"{context}.status 不是受支持的任务状态"
                )
            _require_int(
                latest_job,
                "progress_current",
                context,
            )
            _require_int(
                latest_job,
                "progress_total",
                context,
            )
            _optional_int(
                latest_job,
                "target_chapter_index",
                context,
                minimum=1,
            )
            _optional_text(
                latest_job,
                "error_message",
                context,
            )
    except NovelApiError as error:
        st.warning(f"暂时无法恢复最近人物统计任务：{error}")
        jobs_available = False
    except UiPayloadError as error:
        _show_payload_error("无法恢复最近人物统计任务", error)
        latest_job = None
        jobs_available = False

    if latest_job is not None:
        status_value = latest_job["status"]
        status_labels = {
            "queued": "等待执行",
            "running": "正在执行",
            "succeeded": "已完成",
            "failed": "执行失败",
        }
        current = latest_job["progress_current"]
        total = latest_job["progress_total"]
        percent = (
            0
            if total == 0
            else (current * 100 + total // 2) // total
        )
        percent = max(0, min(100, percent))
        st.subheader("最近人物统计任务")
        st.write(
            f"{status_labels[status_value]} · "
            f"{current:,} / {total:,} 步"
        )
        st.progress(percent)
        if status_value == "failed":
            st.error(
                str(
                    latest_job.get("error_message")
                    or "人物统计任务失败，请重新启动任务。"
                )
            )

    if st.button(
        "刷新人物任务状态",
        key=f"refresh_character_job_{novel_id}",
    ):
        st.rerun()

    return generation_configured and jobs_available, latest_job


def _render_character_estimate(
    novel_id: int,
) -> tuple[bool, Optional[int], Optional[int]]:
    """Render the complete character-analysis processing disclosure."""
    try:
        estimate = get_character_estimate(novel_id)
    except NovelApiError as error:
        st.error(f"无法读取人物统计估算：{error}")
        return False, None, None

    try:
        estimate = _require_object(estimate, "人物统计估算")
        response_novel_id = _require_int(
            estimate,
            "novel_id",
            "人物统计估算",
            minimum=1,
        )
        if response_novel_id != novel_id:
            raise UiPayloadError("人物统计估算.novel_id 与当前小说不符")
        chapter_count = _require_int(
            estimate,
            "chapter_count",
            "人物统计估算",
        )
        character_count = _require_int(
            estimate,
            "character_count",
            "人物统计估算",
        )
        cached_chapters = _require_int(
            estimate,
            "cached_chapter_count",
            "人物统计估算",
        )
        pending_chapters = _require_int(
            estimate,
            "pending_chapter_count",
            "人物统计估算",
        )
        extraction_steps = _require_int(
            estimate,
            "estimated_extraction_steps",
            "人物统计估算",
        )
        merge_steps = _require_int(
            estimate,
            "estimated_merge_steps",
            "人物统计估算",
        )
        relationship_steps = _require_int(
            estimate,
            "estimated_relationship_steps",
            "人物统计估算",
        )
        generation_steps = _require_int(
            estimate,
            "estimated_generation_steps",
            "人物统计估算",
        )
        maximum_attempts = _require_int(
            estimate,
            "maximum_request_attempts",
            "人物统计估算",
        )
        force_generation_steps = _require_int(
            estimate,
            "estimated_force_generation_steps",
            "人物统计估算",
        )
        force_maximum_attempts = _require_int(
            estimate,
            "maximum_force_request_attempts",
            "人物统计估算",
        )
        provider = _require_text(
            estimate,
            "provider",
            "人物统计估算",
        )
        if provider.lower() != "deepseek":
            raise UiPayloadError(
                "人物统计估算.provider 应为 deepseek"
            )
        model = _require_text(
            estimate,
            "model",
            "人物统计估算",
        )
        sends_original_text = _require_bool(
            estimate,
            "sends_original_text",
            "人物统计估算",
        )
        sends_chapter_titles = _require_bool(
            estimate,
            "sends_chapter_titles",
            "人物统计估算",
        )
        sends_derived_data = _require_bool(
            estimate,
            "sends_derived_character_data",
            "人物统计估算",
        )
        data_handling_notice = _require_text(
            estimate,
            "data_handling_notice",
            "人物统计估算",
        )
        terms_url = _require_deepseek_url(
            estimate,
            "terms_url",
            "人物统计估算",
        )
        privacy_url = _require_deepseek_url(
            estimate,
            "privacy_url",
            "人物统计估算",
        )
        up_to_date = _require_bool(
            estimate,
            "up_to_date",
            "人物统计估算",
        )
    except UiPayloadError as error:
        _show_payload_error("无法读取人物统计估算", error)
        return False, None, None

    st.subheader("全书人物统计估算")
    columns = st.columns(4)
    columns[0].metric("章节", chapter_count)
    columns[1].metric("正文字符", f"{character_count:,}")
    columns[2].metric("已缓存章节", cached_chapters)
    columns[3].metric("待分析章节", pending_chapters)
    step_columns = st.columns(4)
    step_columns[0].metric("抽取步骤", extraction_steps)
    step_columns[1].metric("合并步骤", merge_steps)
    step_columns[2].metric("关系步骤", relationship_steps)
    step_columns[3].metric("生成步骤", generation_steps)
    st.caption("最多请求尝试")
    st.write(f"{maximum_attempts:,}")
    st.caption("目标模型")
    _render_plain_text(model)
    sent_content = []
    if sends_chapter_titles:
        sent_content.append("章节标题")
    if sends_original_text:
        sent_content.append("章节原文")
    if sends_derived_data:
        sent_content.append("派生人物观察与人物档案")
    st.warning(
        "人物统计会把章节原文以及分析过程中产生的派生人物观察、"
        "人物档案发送至 DeepSeek。派生内容可能在人物合并和"
        "关系分析步骤中再次发送。"
    )
    if sent_content:
        st.caption(f"将发送至 DeepSeek：{'、'.join(sent_content)}")
    st.warning(data_handling_notice)
    st.markdown(
        f"[DeepSeek 开放平台服务条款]({terms_url}) · "
        f"[DeepSeek 隐私政策]({privacy_url})"
    )
    if up_to_date:
        st.info("当前人物统计已覆盖最新章节；可按需强制重新分析。")
    return True, force_generation_steps, force_maximum_attempts


def _render_character_actions(
    novel_id: int,
    generation_configured: bool,
    latest_job: Optional[Dict[str, Any]],
    estimate_available: bool,
    force_generation_steps: Optional[int],
    force_maximum_attempts: Optional[int],
) -> None:
    """Render dual-consent and force-confirmation task controls."""
    st.subheader("生成人物统计")
    external_consent = st.checkbox(
        (
            "我明确同意将小说原文和派生人物观察、人物档案"
            "发送至 DeepSeek API"
        ),
        key=f"character_external_consent_{novel_id}",
    )
    content_rights = st.checkbox(
        (
            "我确认有权将该小说原文及相关派生内容提交给外部服务，"
            "并理解 DeepSeek 可能依据其条款缓存、留存或用于服务改进，"
            "且数据可能在中国境内处理和存储"
        ),
        key=f"character_content_rights_{novel_id}",
    )
    force = st.checkbox(
        "强制重新生成人物统计（force）",
        key=f"character_force_{novel_id}",
    )
    force_confirmed = False
    if force:
        st.warning(
            "强制重新分析会覆盖当前人物档案、出场记录和关系，"
            "并再次发送原文与派生人物数据。"
        )
        if (
            force_generation_steps is not None
            and force_maximum_attempts is not None
        ):
            st.warning(
                "全量重算预计生成步骤 "
                f"{force_generation_steps:,}；最多请求尝试 "
                f"{force_maximum_attempts:,}。"
            )
        force_confirmed = st.checkbox(
            "我确认覆盖现有人物统计并再次发送相关数据",
            key=f"character_force_confirm_{novel_id}",
        )

    active_job = (
        latest_job is not None
        and str(latest_job.get("status")) in ACTIVE_JOB_STATUSES
    )
    disabled = (
        not generation_configured
        or not estimate_available
        or not external_consent
        or not content_rights
        or active_job
        or (force and not force_confirmed)
    )
    start_clicked = st.button(
        "分析缺失章节" if not force else "重新分析全书人物",
        key=f"start_character_analysis_{novel_id}",
        disabled=disabled,
    )
    if not start_clicked:
        return

    try:
        job = start_character_analysis(
            novel_id,
            confirm_external_processing=external_consent,
            confirm_content_rights=content_rights,
            force=force,
        )
    except NovelApiError as error:
        _character_start_error(error)
        return

    try:
        job = _require_object(job, "人物统计任务")
        job_id = _require_text(job, "id", "人物统计任务")
    except UiPayloadError as error:
        _show_payload_error("无法确认人物统计任务", error)
        return
    st.success(f"人物统计任务已创建：{job_id}")
    st.rerun()


def _validate_character_item(
    value: Any,
    context: str,
    *,
    detail: bool = False,
) -> Dict[str, Any]:
    """Validate one list item or complete character profile."""
    character = _require_object(value, context)
    _require_int(character, "id", context, minimum=1)
    _require_text(character, "canonical_name", context)
    _require_text_list(character, "aliases", context)
    _require_text(character, "short_description", context)
    first_chapter = _require_int(
        character,
        "first_chapter_index",
        context,
        minimum=1,
    )
    last_chapter = _require_int(
        character,
        "last_chapter_index",
        context,
        minimum=1,
    )
    if last_chapter < first_chapter:
        raise UiPayloadError(
            f"{context}.last_chapter_index 不能早于首次出场章节"
        )
    _require_int(character, "chapter_count", context, minimum=1)
    _require_int(character, "mention_count", context, minimum=1)
    _require_int(character, "relationship_count", context)
    if detail:
        _require_text(character, "model", context)
        _require_text(character, "updated_at", context)
    return character


def _render_character_card(
    novel_id: int,
    character: Dict[str, Any],
) -> bool:
    """Render one character with fixed labels and a fixed action title."""
    with st.expander("人物条目"):
        st.caption("规范姓名")
        _render_plain_text(character["canonical_name"])
        st.caption("别名")
        aliases = character["aliases"]
        if aliases:
            for alias in aliases:
                _render_plain_text(alias)
        else:
            _render_plain_text("无")
        st.caption("人物简介")
        _render_plain_text(character["short_description"])
        st.caption("统计")
        st.write(
            f"首次第 {character['first_chapter_index']:,} 章 · "
            f"末次第 {character['last_chapter_index']:,} 章 · "
            f"覆盖 {character['chapter_count']:,} 章 · "
            f"提及 {character['mention_count']:,} 次 · "
            f"关系 {character['relationship_count']:,} 条"
        )
        return st.button(
            "查看人物详情",
            key=(
                f"select_character_{novel_id}_"
                f"{character['id']}"
            ),
        )


def _validate_character_appearance(
    value: Any,
    context: str,
) -> Dict[str, Any]:
    """Validate one persisted character appearance."""
    appearance = _require_object(value, context)
    _require_int(appearance, "id", context, minimum=1)
    _require_int(
        appearance,
        "chapter_index",
        context,
        minimum=1,
    )
    _require_text(appearance, "chapter_title", context)
    _require_int(
        appearance,
        "mention_count",
        context,
        minimum=1,
    )
    _require_text(appearance, "summary", context)
    return appearance


def _validate_character_relationship(
    value: Any,
    context: str,
) -> Dict[str, Any]:
    """Validate one persisted relationship involving a character."""
    relationship = _require_object(value, context)
    _require_int(relationship, "id", context, minimum=1)
    _require_int(
        relationship,
        "source_character_id",
        context,
        minimum=1,
    )
    _require_text(
        relationship,
        "source_character_name",
        context,
    )
    _require_int(
        relationship,
        "target_character_id",
        context,
        minimum=1,
    )
    _require_text(
        relationship,
        "target_character_name",
        context,
    )
    _require_bool(relationship, "directed", context)
    _require_text(relationship, "relation_type", context)
    _require_text(relationship, "description", context)
    _require_int_list(
        relationship,
        "evidence_chapter_indices",
        context,
        minimum=1,
    )
    return relationship


def _render_character_appearances(
    novel_id: int,
    character_id: int,
) -> None:
    """Render one server-paginated page of character appearances."""
    page_key = f"character_appearance_page_{novel_id}_{character_id}"
    page = max(0, int(st.session_state.get(page_key, 0)))
    try:
        payload = list_character_appearances(
            novel_id,
            character_id,
            offset=page * CHARACTER_DETAIL_PAGE_SIZE,
            limit=CHARACTER_DETAIL_PAGE_SIZE,
        )
    except NovelApiError as error:
        st.error(f"无法读取人物出场记录：{error}")
        return

    try:
        payload = _require_object(payload, "人物出场记录")
        total = _require_int(payload, "total", "人物出场记录")
        response_offset = _require_int(
            payload,
            "offset",
            "人物出场记录",
        )
        response_limit = _require_int(
            payload,
            "limit",
            "人物出场记录",
            minimum=1,
        )
        if (
            response_offset != page * CHARACTER_DETAIL_PAGE_SIZE
            or response_limit != CHARACTER_DETAIL_PAGE_SIZE
        ):
            raise UiPayloadError("人物出场记录分页范围与请求不符")
        raw_items = _require_list(
            payload,
            "items",
            "人物出场记录",
        )
        appearances = [
            _validate_character_appearance(
                item,
                f"人物出场记录.items[{index}]",
            )
            for index, item in enumerate(raw_items)
        ]
    except UiPayloadError as error:
        _show_payload_error("无法读取人物出场记录", error)
        return

    final_page = last_page_index(total, CHARACTER_DETAIL_PAGE_SIZE)
    if page > final_page:
        st.session_state[page_key] = final_page
        st.rerun()
        return

    st.subheader("出场记录")
    st.write(f"共 {total:,} 条")
    if not appearances:
        st.info("暂时没有人物出场记录。")
        return
    for appearance in appearances:
        with st.expander("出场记录条目"):
            st.caption("章节序号")
            st.write(f"{appearance['chapter_index']:,}")
            st.caption("章节标题")
            _render_plain_text(appearance["chapter_title"])
            st.caption("本章提及次数")
            st.write(f"{appearance['mention_count']:,}")
            st.caption("出场摘要")
            _render_plain_text(appearance["summary"])
    if final_page > 0:
        previous_column, page_column, next_column = st.columns(
            [1, 2, 1]
        )
        with previous_column:
            if st.button(
                "上一页出场记录",
                key=(
                    f"character_appearance_previous_"
                    f"{novel_id}_{character_id}"
                ),
                disabled=page == 0,
            ):
                st.session_state[page_key] = page - 1
                st.rerun()
        with page_column:
            st.write(f"出场页 {page + 1} / {final_page + 1}")
        with next_column:
            if st.button(
                "下一页出场记录",
                key=(
                    f"character_appearance_next_"
                    f"{novel_id}_{character_id}"
                ),
                disabled=page >= final_page,
            ):
                st.session_state[page_key] = page + 1
                st.rerun()


def _render_character_relationships(
    novel_id: int,
    character_id: int,
) -> None:
    """Render one server-paginated page of character relationships."""
    page_key = (
        f"character_relationship_page_{novel_id}_{character_id}"
    )
    page = max(0, int(st.session_state.get(page_key, 0)))
    try:
        payload = list_character_relationships(
            novel_id,
            character_id,
            offset=page * CHARACTER_DETAIL_PAGE_SIZE,
            limit=CHARACTER_DETAIL_PAGE_SIZE,
        )
    except NovelApiError as error:
        st.error(f"无法读取人物关系：{error}")
        return

    try:
        payload = _require_object(payload, "人物关系")
        total = _require_int(payload, "total", "人物关系")
        response_offset = _require_int(
            payload,
            "offset",
            "人物关系",
        )
        response_limit = _require_int(
            payload,
            "limit",
            "人物关系",
            minimum=1,
        )
        if (
            response_offset != page * CHARACTER_DETAIL_PAGE_SIZE
            or response_limit != CHARACTER_DETAIL_PAGE_SIZE
        ):
            raise UiPayloadError("人物关系分页范围与请求不符")
        raw_items = _require_list(payload, "items", "人物关系")
        relationships = [
            _validate_character_relationship(
                item,
                f"人物关系.items[{index}]",
            )
            for index, item in enumerate(raw_items)
        ]
    except UiPayloadError as error:
        _show_payload_error("无法读取人物关系", error)
        return

    final_page = last_page_index(total, CHARACTER_DETAIL_PAGE_SIZE)
    if page > final_page:
        st.session_state[page_key] = final_page
        st.rerun()
        return

    st.subheader("人物关系")
    st.write(f"共 {total:,} 条")
    if not relationships:
        st.info("暂时没有人物关系记录。")
        return
    for relationship in relationships:
        with st.expander("人物关系条目"):
            st.caption("关系起点")
            _render_plain_text(
                relationship["source_character_name"]
            )
            st.caption("关系终点")
            _render_plain_text(
                relationship["target_character_name"]
            )
            st.caption("关系方向")
            st.write(
                "有向关系"
                if relationship["directed"]
                else "无向关系"
            )
            st.caption("关系类型")
            _render_plain_text(relationship["relation_type"])
            st.caption("关系说明")
            _render_plain_text(relationship["description"])
            st.caption("证据章节")
            evidence = relationship["evidence_chapter_indices"]
            st.write("、".join(str(index) for index in evidence))
    if final_page > 0:
        previous_column, page_column, next_column = st.columns(
            [1, 2, 1]
        )
        with previous_column:
            if st.button(
                "上一页人物关系",
                key=(
                    f"character_relationship_previous_"
                    f"{novel_id}_{character_id}"
                ),
                disabled=page == 0,
            ):
                st.session_state[page_key] = page - 1
                st.rerun()
        with page_column:
            st.write(f"关系页 {page + 1} / {final_page + 1}")
        with next_column:
            if st.button(
                "下一页人物关系",
                key=(
                    f"character_relationship_next_"
                    f"{novel_id}_{character_id}"
                ),
                disabled=page >= final_page,
            ):
                st.session_state[page_key] = page + 1
                st.rerun()


def _render_selected_character(
    novel_id: int,
    character_id: int,
) -> None:
    """Render a selected profile, appearances, and relationships."""
    try:
        character = get_character(novel_id, character_id)
    except NovelApiError as error:
        if error.status_code == 404:
            st.warning("该人物档案已不存在，请重新选择。")
            st.session_state.pop(
                f"selected_character_id_{novel_id}",
                None,
            )
        else:
            st.error(f"无法读取人物详情：{error}")
        return

    try:
        character = _validate_character_item(
            character,
            "人物详情",
            detail=True,
        )
        if character["id"] != character_id:
            raise UiPayloadError("人物详情.id 与当前选择不符")
    except UiPayloadError as error:
        _show_payload_error("无法读取人物详情", error)
        return

    st.subheader("人物详情")
    st.caption("规范姓名")
    _render_plain_text(character["canonical_name"])
    st.caption("别名")
    aliases = character["aliases"]
    if aliases:
        for alias in aliases:
            _render_plain_text(alias)
    else:
        _render_plain_text("无")
    st.caption("人物简介")
    _render_plain_text(character["short_description"])
    st.caption("统计")
    st.write(
        f"首次第 {character['first_chapter_index']:,} 章 · "
        f"末次第 {character['last_chapter_index']:,} 章 · "
        f"覆盖 {character['chapter_count']:,} 章 · "
        f"提及 {character['mention_count']:,} 次 · "
        f"关系 {character['relationship_count']:,} 条"
    )
    st.caption("生成模型")
    _render_plain_text(character["model"])
    st.caption("更新时间")
    _render_plain_text(character["updated_at"])
    _render_character_appearances(novel_id, character_id)
    _render_character_relationships(novel_id, character_id)


def _render_character_results(novel_id: int) -> None:
    """Render searchable, sorted, server-paginated character results."""
    search_key = f"character_search_{novel_id}"
    search_snapshot_key = f"character_search_snapshot_{novel_id}"
    page_key = f"character_page_{novel_id}"
    search = st.text_input(
        "搜索人物姓名或别名",
        key=search_key,
    ).strip()
    sort = st.selectbox(
        "人物排序",
        options=[
            "mention_count",
            "chapter_count",
            "first_chapter",
        ],
        format_func=lambda value: {
            "mention_count": "按提及次数",
            "chapter_count": "按覆盖章节",
            "first_chapter": "按首次出场",
        }[value],
        key=f"character_sort_{novel_id}",
    )
    previous_search = str(
        st.session_state.get(search_snapshot_key, "")
    )
    page = max(0, int(st.session_state.get(page_key, 0)))
    if search != previous_search:
        page = 0
        st.session_state[page_key] = 0
        st.session_state[search_snapshot_key] = search

    try:
        payload = list_characters(
            novel_id,
            offset=page * CHARACTER_PAGE_SIZE,
            limit=CHARACTER_PAGE_SIZE,
            search=search,
            sort=str(sort),
        )
    except NovelApiError as error:
        if error.status_code == 404:
            st.info("尚未生成可用的人物统计。")
        else:
            st.error(f"无法读取人物列表：{error}")
        return

    try:
        payload = _require_object(payload, "人物列表")
        total = _require_int(payload, "total", "人物列表")
        _require_int(payload, "offset", "人物列表")
        _require_int(payload, "limit", "人物列表", minimum=1)
        analysis_updated_at = _optional_text(
            payload,
            "analysis_updated_at",
            "人物列表",
        )
        analyzed_chapters = _require_int(
            payload,
            "analyzed_chapter_count",
            "人物列表",
        )
        character_count = _require_int(
            payload,
            "character_count",
            "人物列表",
        )
        relationship_count = _require_int(
            payload,
            "relationship_count",
            "人物列表",
        )
        raw_items = _require_list(payload, "items", "人物列表")
        characters = [
            _validate_character_item(
                item,
                f"人物列表.items[{index}]",
            )
            for index, item in enumerate(raw_items)
        ]
    except UiPayloadError as error:
        _show_payload_error("无法读取人物列表", error)
        return

    final_page = last_page_index(total, CHARACTER_PAGE_SIZE)
    if page > final_page:
        st.session_state[page_key] = final_page
        st.rerun()
        return

    st.subheader("人物列表")
    st.write(
        f"分析章节 {analyzed_chapters:,} · "
        f"人物 {character_count:,} · "
        f"关系 {relationship_count:,}"
    )
    if analysis_updated_at is not None:
        st.caption("分析更新时间")
        _render_plain_text(analysis_updated_at)
    if not characters:
        if search:
            st.info("没有找到匹配的人物。")
        else:
            st.info("尚未保存任何人物统计。")
    for character in characters:
        if _render_character_card(novel_id, character):
            st.session_state[
                f"selected_character_id_{novel_id}"
            ] = character["id"]
            st.rerun()
            return

    if final_page > 0:
        previous_column, page_column, next_column = st.columns(
            [1, 2, 1]
        )
        with previous_column:
            if st.button(
                "上一页人物",
                key=f"character_previous_{novel_id}",
                disabled=page == 0,
            ):
                st.session_state[page_key] = page - 1
                st.rerun()
        with page_column:
            st.write(f"人物页 {page + 1} / {final_page + 1}")
        with next_column:
            if st.button(
                "下一页人物",
                key=f"character_next_{novel_id}",
                disabled=page >= final_page,
            ):
                st.session_state[page_key] = page + 1
                st.rerun()

    selected_character_id = st.session_state.get(
        f"selected_character_id_{novel_id}"
    )
    if (
        isinstance(selected_character_id, bool)
        or not isinstance(selected_character_id, int)
        or selected_character_id < 1
    ):
        return
    _render_selected_character(novel_id, selected_character_id)


def render_character_workspace(novel: Dict[str, Any]) -> None:
    """Render character disclosure, task controls, results, and detail."""
    try:
        novel = _require_object(novel, "小说信息")
        novel_id = _require_int(
            novel,
            "id",
            "小说信息",
            minimum=1,
        )
    except UiPayloadError as error:
        _show_payload_error("无法打开人物统计工作区", error)
        return

    st.markdown("---")
    st.subheader("DeepSeek 人物统计")
    generation_configured, latest_job = (
        _render_character_generation_status(novel_id)
    )
    (
        estimate_available,
        force_generation_steps,
        force_maximum_attempts,
    ) = _render_character_estimate(novel_id)
    _render_character_actions(
        novel_id,
        generation_configured,
        latest_job,
        estimate_available,
        force_generation_steps,
        force_maximum_attempts,
    )
    _render_character_results(novel_id)


def render_reader_workspace(novel: Dict[str, Any]) -> None:
    """Render chapter content only while the reader mode is active."""
    try:
        novel = _require_object(novel, "小说信息")
        novel_id = _require_int(
            novel,
            "id",
            "小说信息",
            minimum=1,
        )
    except UiPayloadError as error:
        _show_payload_error("无法打开正文阅读", error)
        return
    page = max(0, int(st.session_state.get("reader_chapter_page", 0)))
    try:
        payload = list_chapters(
            novel_id,
            offset=page * CHAPTER_PAGE_SIZE,
            limit=CHAPTER_PAGE_SIZE,
        )
    except NovelApiError as error:
        st.error(str(error))
        return

    try:
        payload = _require_object(payload, "章节列表")
        total = _require_int(payload, "total", "章节列表")
        items = _require_list(payload, "items", "章节列表")
        chapters = [
            _validate_chapter_metadata(
                chapter,
                f"章节列表.items[{index}]",
            )
            for index, chapter in enumerate(items)
        ]
    except UiPayloadError as error:
        _show_payload_error("无法读取章节列表", error)
        return

    final_page = last_page_index(total, CHAPTER_PAGE_SIZE)
    if page > final_page:
        st.session_state["reader_chapter_page"] = final_page
        st.rerun()
        return

    if not chapters:
        st.info("这部小说没有可阅读的章节。")
        return

    options = [chapter["index"] for chapter in chapters]
    selected_index = st.selectbox(
        "选择章节",
        options=options,
        format_func=lambda index: next(
            (
                f"{index:03d} · {chapter['title']}"
                for chapter in chapters
                if chapter["index"] == index
            ),
            str(index),
        ),
        key=f"reader_chapter_select_{novel_id}_{page}",
    )
    try:
        chapter = get_chapter(novel_id, int(selected_index))
    except NovelApiError as error:
        st.error(str(error))
        return

    try:
        chapter = _require_object(chapter, "章节正文")
        chapter_title = _require_text(
            chapter,
            "title",
            "章节正文",
        )
        chapter_content = _require_text(
            chapter,
            "content",
            "章节正文",
            allow_empty=True,
        )
    except UiPayloadError as error:
        _show_payload_error("无法显示章节正文", error)
        return

    st.markdown(
        (
            '<article class="nm-reader">'
            f"<h2>{escape(chapter_title)}</h2>"
            '<div class="nm-reader-body">'
            f"{escape(chapter_content)}"
            "</div></article>"
        ),
        unsafe_allow_html=True,
    )

    if final_page > 0:
        previous_column, page_column, next_column = st.columns([1, 2, 1])
        with previous_column:
            if st.button(
                "上一批章节",
                key=f"reader_previous_{novel_id}",
                disabled=page == 0,
            ):
                st.session_state["reader_chapter_page"] = page - 1
                st.rerun()
        with page_column:
            st.write(f"章节页 {page + 1} / {final_page + 1}")
        with next_column:
            if st.button(
                "下一批章节",
                key=f"reader_next_{novel_id}",
                disabled=page >= final_page,
            ):
                st.session_state["reader_chapter_page"] = page + 1
                st.rerun()


def main() -> None:
    """Run the NovelMind Streamlit application."""
    st.set_page_config(
        page_title="NovelMind",
        page_icon="📘",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(APP_CSS, unsafe_allow_html=True)
    initialize_session_state()
    backend_online = check_backend()
    render_topbar(backend_online)
    render_navigation()

    flash_message = st.session_state.pop("flash_message", None)
    if flash_message:
        st.success(str(flash_message))
    flash_warning = st.session_state.pop("flash_warning", None)
    if flash_warning:
        st.warning(str(flash_warning))

    if st.session_state["view"] == "upload":
        render_upload_page(backend_online)
    else:
        render_library_page(backend_online)


if __name__ == "__main__":
    main()
