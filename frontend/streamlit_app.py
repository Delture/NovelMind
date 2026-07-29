"""Streamlit workspace for importing, reading, and analyzing novels."""

from html import escape
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import streamlit as st

from api_client import (
    NovelApiError,
    check_backend,
    delete_novel,
    get_chapter,
    get_chapter_summary,
    get_novel,
    get_summary_estimate,
    get_system_status,
    list_analysis_jobs,
    list_chapters,
    list_novels,
    list_summaries,
    start_chapter_summary,
    start_summaries,
    upload_novel,
)
from ui_styles import APP_CSS


MAX_FILE_BYTES = 20 * 1024 * 1024
LIBRARY_PAGE_SIZE = 10
CHAPTER_PAGE_SIZE = 50
SUMMARY_PAGE_SIZE = 20
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
    """Render a selected novel and switch between summary and reader modes."""
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
        "章节阅读与 DeepSeek 摘要分析",
    )
    render_novel_metrics(novel)
    mode = st.radio(
        "小说工作区",
        options=["章节摘要", "正文阅读"],
        horizontal=True,
        key=f"novel_mode_{novel_id}",
    )
    if mode == "章节摘要":
        render_summary_workspace(novel)
    else:
        render_reader_workspace(novel)


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
