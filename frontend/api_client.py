"""Streamlit 前端访问 FastAPI 的唯一 HTTP 客户端层。

界面函数不直接拼 URL 或处理 requests 异常，而统一调用本模块。这样后端路径、
超时、错误解析和测试替身都有单一位置。每个函数只做一次 HTTP 操作并返回字典；
响应的业务字段由 UI 层继续严格验证，不能因为来自“自己的后端”就盲目信任。
"""

import os
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv


load_dotenv()

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")


class NovelApiError(Exception):
    """可安全展示给用户的 API 错误，并保留状态码供界面分支判断。"""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
    ) -> None:
        """保存可展示错误文字和可选 HTTP 状态码。

        ``message`` 交给 Exception 基类；``status_code`` 让 UI 针对 404、409 等
        分支显示提示。连接失败没有 HTTP 响应时该值为 None。
        """
        super().__init__(message)
        self.status_code = status_code


def _parse_response(
    response: requests.Response,
    fallback_error: str,
) -> Dict[str, Any]:
    """统一解析 JSON 对象，并把 HTTP/协议故障转换成稳定前端异常。

    成功状态也必须检查顶层是否为对象。若反向代理或崩溃页返回 HTML，直接调用
    ``response.json()`` 的底层异常不适合作为用户提示。
    """
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
    """快速探测后端是否为正确的 NovelMind 服务；离线属于正常 UI 状态。"""
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


def get_model_settings(
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return editable model settings without either stored API key."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(
            f"{base_url}/api/system/model-settings",
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "读取模型配置失败，请稍后重试。")


def update_model_settings(
    payload: Dict[str, Any],
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Save provider, model, endpoint, and optional replacement keys."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.put(
            f"{base_url}/api/system/model-settings",
            json=payload,
            timeout=30,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "保存模型配置失败，请检查填写内容。")


def reset_model_settings(
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Remove frontend overrides and restore the local environment config."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.delete(
            f"{base_url}/api/system/model-settings",
            timeout=30,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "恢复环境变量配置失败。")


def upload_novel(
    filename: str,
    content: bytes,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """以原始字节上传 TXT；后端而非浏览器负责最终编码与内容校验。"""
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


def get_rag_estimate(
    novel_id: int,
    force: bool = False,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return RAG cost bounds and external-processing disclosures."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(
            f"{base_url}/api/novels/{novel_id}/rag/estimate",
            params={"force": force},
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "读取 RAG 估算失败，请稍后重试。")


def get_rag_index_status(
    novel_id: int,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return current local RAG index readiness."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(
            f"{base_url}/api/novels/{novel_id}/rag/index",
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "读取 RAG 索引状态失败，请稍后重试。")


def start_rag_index(
    novel_id: int,
    confirm_external_processing: bool,
    confirm_content_rights: bool,
    force: bool = False,
    confirm_force_refresh: bool = False,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """请求后端启动一部小说的 RAG 建索引任务。

    ``novel_id`` 进入路径；三个 confirm/force 布尔值组成 JSON 授权范围；可选
    backend_url 仅供测试或自定义本机地址。返回任务字典；HTTP/网络错误统一抛
    NovelApiError，本函数自身不调用 embedding 供应商。
    """
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.post(
            f"{base_url}/api/novels/{novel_id}/rag/index",
            json={
                "confirm_external_processing": (
                    confirm_external_processing
                ),
                "confirm_content_rights": confirm_content_rights,
                "force": force,
                "confirm_force_refresh": confirm_force_refresh,
            },
            timeout=30,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "启动 RAG 索引失败，请稍后重试。")


def list_rag_index_jobs(
    novel_id: int,
    limit: int = 10,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return recent RAG index jobs as an object for UI consistency."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(
            f"{base_url}/api/novels/{novel_id}/rag/index/jobs",
            params={"limit": limit},
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    try:
        payload = response.json()
    except ValueError as error:
        raise NovelApiError(
            "后端返回了无法解析的响应。",
            status_code=(
                response.status_code
                if response.status_code >= 400
                else None
            ),
        ) from error
    if response.status_code >= 400:
        detail = (
            payload.get("detail", "读取 RAG 索引任务失败，请稍后重试。")
            if isinstance(payload, dict)
            else "读取 RAG 索引任务失败，请稍后重试。"
        )
        raise NovelApiError(str(detail), status_code=response.status_code)
    if not isinstance(payload, list):
        raise NovelApiError("后端返回了无法解析的响应。")
    return {"items": payload, "total": len(payload)}


def start_rag_question(
    novel_id: int,
    question: str,
    top_k: int,
    confirm_embedding_processing: bool,
    confirm_generation_processing: bool,
    confirm_content_rights: bool,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Start one external-embedding and DeepSeek RAG question."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.post(
            f"{base_url}/api/novels/{novel_id}/rag/questions",
            json={
                "question": question,
                "top_k": top_k,
                "confirm_embedding_processing": (
                    confirm_embedding_processing
                ),
                "confirm_generation_processing": (
                    confirm_generation_processing
                ),
                "confirm_content_rights": confirm_content_rights,
            },
            timeout=30,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "启动 RAG 问答失败，请稍后重试。")


def list_rag_questions(
    novel_id: int,
    offset: int = 0,
    limit: int = 20,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return one page of persisted RAG questions and answers."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(
            f"{base_url}/api/novels/{novel_id}/rag/questions",
            params={"offset": offset, "limit": limit},
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "读取 RAG 问答历史失败，请稍后重试。")


def get_rag_question(
    novel_id: int,
    question_id: str,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return one recoverable RAG question task."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(
            (
                f"{base_url}/api/novels/{novel_id}"
                f"/rag/questions/{question_id}"
            ),
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "读取 RAG 问答结果失败，请稍后重试。")


def get_report_estimate(
    novel_id: int,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return report prerequisites and external-processing bounds."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(
            f"{base_url}/api/novels/{novel_id}/report/estimate",
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "读取分析报告估算失败，请稍后重试。")


def start_report(
    novel_id: int,
    confirm_external_processing: bool,
    confirm_content_rights: bool,
    force: bool = False,
    confirm_force_refresh: bool = False,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """请求后端启动全书报告任务。

    ``novel_id`` 进入路径；confirm_external_processing、confirm_content_rights 和
    force/confirm_force_refresh 进入 JSON。返回持久任务字典；真正模型调用在后端
    BackgroundTasks 中执行。
    """
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.post(
            f"{base_url}/api/novels/{novel_id}/report",
            json={
                "confirm_external_processing": (
                    confirm_external_processing
                ),
                "confirm_content_rights": confirm_content_rights,
                "force": force,
                "confirm_force_refresh": confirm_force_refresh,
            },
            timeout=30,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "启动分析报告任务失败，请稍后重试。")


def get_report(
    novel_id: int,
    backend_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the current saved whole-novel report snapshot."""
    base_url = (backend_url or BACKEND_URL).rstrip("/")
    try:
        response = requests.get(
            f"{base_url}/api/novels/{novel_id}/report",
            timeout=10,
        )
    except requests.RequestException as error:
        raise NovelApiError(
            "无法连接后端服务，请确认 FastAPI 已启动。"
        ) from error
    return _parse_response(response, "读取分析报告失败，请稍后重试。")
