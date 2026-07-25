"""HTTP client used by the Streamlit frontend."""

import os
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv


load_dotenv()

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")


class NovelApiError(Exception):
    """A user-facing error returned by the NovelMind backend."""


def check_backend(backend_url: Optional[str] = None) -> bool:
    """Return whether the FastAPI health endpoint is available."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(f"{base_url}/health", timeout=1)
        return response.status_code == 200
    except requests.RequestException:
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

    try:
        result = response.json()
    except ValueError as error:
        raise NovelApiError("后端返回了无法解析的响应。") from error

    if response.status_code >= 400:
        detail = result.get("detail", "上传失败，请稍后重试。")
        raise NovelApiError(str(detail))

    return result
