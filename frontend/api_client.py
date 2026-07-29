"""HTTP client used by the Streamlit frontend."""

import os
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv


load_dotenv()

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")


class NovelApiError(Exception):
    """A user-facing error returned by the NovelMind backend."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code


def _parse_response(
    response: requests.Response,
    fallback_error: str,
) -> Dict[str, Any]:
    """Validate a JSON object response and raise readable API errors."""
    try:
        result = response.json()
    except ValueError as error:
        status_code = (
            response.status_code if response.status_code >= 400 else None
        )
        raise NovelApiError(
            "后端返回了无法解析的响应。",
            status_code=status_code,
        ) from error

    if response.status_code >= 400:
        detail = (
            result.get("detail", fallback_error)
            if isinstance(result, dict)
            else fallback_error
        )
        raise NovelApiError(
            str(detail),
            status_code=response.status_code,
        )

    if not isinstance(result, dict):
        raise NovelApiError("后端返回了无法解析的响应。")
    return result


def check_backend(backend_url: Optional[str] = None) -> bool:
    """Return whether the FastAPI health endpoint is available."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(f"{base_url}/health", timeout=1)
        result = response.json()
        return (
            response.status_code == 200
            and isinstance(result, dict)
            and result.get("status") == "ok"
            and result.get("project") == "NovelMind"
        )
    except (requests.RequestException, ValueError):
        return False


def get_system_status(
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return non-secret backend provider configuration status."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(
            f"{base_url}/api/system/status",
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "读取系统状态失败，请稍后重试。")


def upload_novel(
    filename: str,
    content: bytes,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Upload one TXT file and return the parsed novel data."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")

    try:
        response = requests.post(
            f"{base_url}/api/novels/upload",
            params={"filename": filename},
            data=content,
            headers={"Content-Type": "application/octet-stream"},
            timeout=60,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error

    return _parse_response(response, "上传失败，请稍后重试。")


def list_novels(
    offset: int = 0,
    limit: int = 20,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return one page of novels from the saved library."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(
            f"{base_url}/api/novels",
            params={"offset": offset, "limit": limit},
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "读取小说资料库失败，请稍后重试。")


def get_novel(
    novel_id: int,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return metadata for one saved novel."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(
            f"{base_url}/api/novels/{novel_id}",
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "读取小说信息失败，请稍后重试。")


def list_chapters(
    novel_id: int,
    offset: int = 0,
    limit: int = 50,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return one page of chapter metadata."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(
            f"{base_url}/api/novels/{novel_id}/chapters",
            params={"offset": offset, "limit": limit},
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "读取章节列表失败，请稍后重试。")


def get_chapter(
    novel_id: int,
    chapter_index: int,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return one complete chapter."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(
            (
                f"{base_url}/api/novels/{novel_id}"
                f"/chapters/{chapter_index}"
            ),
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "读取章节失败，请稍后重试。")


def delete_novel(
    novel_id: int,
    backend_url: Optional[str] = None,
) -> None:
    """Delete one novel from the saved library."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.delete(
            f"{base_url}/api/novels/{novel_id}",
            timeout=30,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error

    if response.status_code == 204:
        return
    if 200 <= response.status_code < 300:
        raise NovelApiError(
            "后端返回了意外的删除响应。",
            status_code=response.status_code,
        )
    _parse_response(response, "删除小说失败，请稍后重试。")


def start_summaries(
    novel_id: int,
    confirm_external_processing: bool,
    confirm_content_rights: bool,
    force: bool = False,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Start asynchronous chapter summaries for one novel."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.post(
            f"{base_url}/api/novels/{novel_id}/summaries",
            json={
                "confirm_external_processing": confirm_external_processing,
                "confirm_content_rights": confirm_content_rights,
                "force": force,
            },
            timeout=30,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "启动章节摘要任务失败，请稍后重试。")


def list_summaries(
    novel_id: int,
    offset: int = 0,
    limit: int = 50,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return one page of saved chapter summaries for one novel."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(
            f"{base_url}/api/novels/{novel_id}/summaries",
            params={"offset": offset, "limit": limit},
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "读取章节摘要失败，请稍后重试。")


def start_chapter_summary(
    novel_id: int,
    chapter_index: int,
    confirm_external_processing: bool,
    confirm_content_rights: bool,
    force: bool = False,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Start asynchronous summary generation for one chapter."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.post(
            (
                f"{base_url}/api/novels/{novel_id}"
                f"/chapters/{chapter_index}/summary"
            ),
            json={
                "confirm_external_processing": confirm_external_processing,
                "confirm_content_rights": confirm_content_rights,
                "force": force,
            },
            timeout=30,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "启动章节摘要任务失败，请稍后重试。")


def get_chapter_summary(
    novel_id: int,
    chapter_index: int,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the saved summary for one chapter."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(
            (
                f"{base_url}/api/novels/{novel_id}"
                f"/chapters/{chapter_index}/summary"
            ),
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "读取章节摘要失败，请稍后重试。")


def get_analysis_job(
    job_id: str,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the latest state of one asynchronous analysis job."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(
            f"{base_url}/api/analysis-jobs/{job_id}",
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "读取分析任务状态失败，请稍后重试。")


def get_summary_estimate(
    novel_id: int,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return external-processing scope before starting summaries."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(
            f"{base_url}/api/novels/{novel_id}/summaries/estimate",
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "读取摘要任务估算失败，请稍后重试。")


def list_analysis_jobs(
    novel_id: int,
    job_type: Optional[str] = None,
    limit: int = 20,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return recent background jobs for one novel."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    params: Dict[str, Any] = {"limit": limit}
    if job_type is not None:
        params["job_type"] = job_type
    try:
        response = requests.get(
            f"{base_url}/api/novels/{novel_id}/analysis-jobs",
            params=params,
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "读取分析任务列表失败，请稍后重试。")


def get_character_estimate(
    novel_id: int,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return external-processing scope before character analysis."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(
            f"{base_url}/api/novels/{novel_id}/characters/estimate",
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(
        response,
        "读取人物统计估算失败，请稍后重试。",
    )


def start_character_analysis(
    novel_id: int,
    confirm_external_processing: bool,
    confirm_content_rights: bool,
    force: bool = False,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Start asynchronous character extraction for one novel."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.post(
            f"{base_url}/api/novels/{novel_id}/characters",
            json={
                "confirm_external_processing": confirm_external_processing,
                "confirm_content_rights": confirm_content_rights,
                "force": force,
            },
            timeout=30,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(
        response,
        "启动人物统计任务失败，请稍后重试。",
    )


def list_characters(
    novel_id: int,
    offset: int = 0,
    limit: int = 20,
    search: str = "",
    sort: str = "mention_count",
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return one searched and sorted page of saved characters."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(
            f"{base_url}/api/novels/{novel_id}/characters",
            params={
                "offset": offset,
                "limit": limit,
                "search": search,
                "sort": sort,
            },
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(
        response,
        "读取人物列表失败，请稍后重试。",
    )


def get_character(
    novel_id: int,
    character_id: int,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return one saved character profile."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(
            (
                f"{base_url}/api/novels/{novel_id}"
                f"/characters/{character_id}"
            ),
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(
        response,
        "读取人物详情失败，请稍后重试。",
    )


def list_character_appearances(
    novel_id: int,
    character_id: int,
    offset: int = 0,
    limit: int = 100,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return saved chapter appearances for one character."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(
            (
                f"{base_url}/api/novels/{novel_id}"
                f"/characters/{character_id}/appearances"
            ),
            params={"offset": offset, "limit": limit},
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(
        response,
        "读取人物出场记录失败，请稍后重试。",
    )


def list_character_relationships(
    novel_id: int,
    character_id: int,
    offset: int = 0,
    limit: int = 100,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return saved relationships involving one character."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(
            (
                f"{base_url}/api/novels/{novel_id}"
                f"/characters/{character_id}/relationships"
            ),
            params={"offset": offset, "limit": limit},
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(
        response,
        "读取人物关系失败，请稍后重试。",
    )
