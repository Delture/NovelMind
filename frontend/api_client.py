"""HTTP client used by the Streamlit frontend."""

import os
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv


load_dotenv()

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")


class NovelApiError(Exception):
    """A user-facing error returned by the NovelMind backend."""


def _parse_response(
    response: requests.Response,
    fallback_error: str,
) -> Dict[str, Any]:
    """Validate a JSON object response and raise readable API errors."""
    try:
        result = response.json()
    except ValueError as error:
        raise NovelApiError("后端返回了无法解析的响应。") from error

    if response.status_code >= 400:
        detail = (
            result.get("detail", fallback_error)
            if isinstance(result, dict)
            else fallback_error
        )
        raise NovelApiError(str(detail))

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
    _parse_response(response, "删除小说失败，请稍后重试。")
