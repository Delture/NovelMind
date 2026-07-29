"""Tests for the Streamlit frontend API client."""

from unittest.mock import Mock, patch

import pytest
import requests

from frontend import api_client


NovelApiError = api_client.NovelApiError
check_backend = api_client.check_backend
upload_novel = api_client.upload_novel


@patch("frontend.api_client.requests.post")
def test_upload_novel_sends_binary_file(mock_post: Mock) -> None:
    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "novel_id": 1,
        "filename": "novel.txt",
        "encoding": "utf-8",
        "chapter_count": 1,
        "chapters": [],
    }
    mock_post.return_value = response

    result = upload_novel(
        "小说.txt",
        b"novel content",
        backend_url="http://testserver/",
    )

    assert result["novel_id"] == 1
    mock_post.assert_called_once_with(
        "http://testserver/api/novels/upload",
        params={"filename": "小说.txt"},
        data=b"novel content",
        headers={"Content-Type": "application/octet-stream"},
        timeout=60,
    )


@patch("frontend.api_client.requests.post")
def test_upload_novel_raises_backend_detail(mock_post: Mock) -> None:
    response = Mock()
    response.status_code = 400
    response.json.return_value = {"detail": "TXT 文件内容为空。"}
    mock_post.return_value = response

    with pytest.raises(
        NovelApiError,
        match="TXT 文件内容为空",
    ) as error_info:
        upload_novel("empty.txt", b"")
    assert error_info.value.status_code == 400


@patch("frontend.api_client.requests.post")
def test_upload_novel_handles_invalid_response(mock_post: Mock) -> None:
    response = Mock()
    response.status_code = 500
    response.json.side_effect = ValueError
    mock_post.return_value = response

    with pytest.raises(NovelApiError, match="无法解析"):
        upload_novel("novel.txt", b"content")


@patch("frontend.api_client.requests.post")
def test_upload_novel_handles_connection_error(mock_post: Mock) -> None:
    mock_post.side_effect = requests.ConnectionError

    with pytest.raises(NovelApiError, match="无法连接后端服务"):
        upload_novel("novel.txt", b"content")


@patch("frontend.api_client.requests.get")
def test_check_backend(mock_get: Mock) -> None:
    response = Mock()
    response.status_code = 200
    response.json.return_value = {"status": "ok", "project": "NovelMind"}
    mock_get.return_value = response

    assert check_backend("http://testserver/") is True
    mock_get.side_effect = requests.ConnectionError
    assert check_backend("http://testserver/") is False


@patch("frontend.api_client.requests.get")
def test_check_backend_rejects_unexpected_payload(mock_get: Mock) -> None:
    response = Mock()
    response.status_code = 200
    response.json.return_value = {"status": "maintenance"}
    mock_get.return_value = response

    assert check_backend("http://testserver/") is False


@patch("frontend.api_client.requests.get")
def test_get_system_status_uses_public_status_endpoint(
    mock_get: Mock,
) -> None:
    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "status": "ok",
        "generation": {
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "configured": True,
        },
        "embedding": {
            "provider": "external_api",
            "model": None,
            "configured": False,
        },
    }
    mock_get.return_value = response

    result = api_client.get_system_status(
        backend_url="http://testserver/",
    )

    assert result["generation"]["configured"] is True
    mock_get.assert_called_once_with(
        "http://testserver/api/system/status",
        timeout=10,
    )


@patch("frontend.api_client.requests.get")
def test_list_novels_sends_pagination(mock_get: Mock) -> None:
    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "items": [],
        "total": 0,
        "offset": 20,
        "limit": 10,
    }
    mock_get.return_value = response

    result = api_client.list_novels(
        offset=20,
        limit=10,
        backend_url="http://testserver/",
    )

    assert result["total"] == 0
    mock_get.assert_called_once_with(
        "http://testserver/api/novels",
        params={"offset": 20, "limit": 10},
        timeout=10,
    )


@patch("frontend.api_client.requests.get")
def test_get_novel_and_chapters_use_library_endpoints(
    mock_get: Mock,
) -> None:
    responses = []
    for payload in (
        {"id": 7, "filename": "story.txt"},
        {"items": [], "total": 0, "offset": 0, "limit": 50},
        {"id": 9, "novel_id": 7, "index": 2, "content": "正文"},
    ):
        response = Mock()
        response.status_code = 200
        response.json.return_value = payload
        responses.append(response)
    mock_get.side_effect = responses

    assert api_client.get_novel(7, backend_url="http://testserver")["id"] == 7
    assert api_client.list_chapters(7, backend_url="http://testserver")["total"] == 0
    assert api_client.get_chapter(7, 2, backend_url="http://testserver")["index"] == 2

    assert [call.args[0] for call in mock_get.call_args_list] == [
        "http://testserver/api/novels/7",
        "http://testserver/api/novels/7/chapters",
        "http://testserver/api/novels/7/chapters/2",
    ]
    assert mock_get.call_args_list[1].kwargs["params"] == {
        "offset": 0,
        "limit": 50,
    }


@patch("frontend.api_client.requests.delete")
def test_delete_novel_accepts_empty_204_response(mock_delete: Mock) -> None:
    response = Mock()
    response.status_code = 204
    response.content = b""
    mock_delete.return_value = response

    api_client.delete_novel(7, backend_url="http://testserver/")

    mock_delete.assert_called_once_with(
        "http://testserver/api/novels/7",
        timeout=30,
    )


@patch("frontend.api_client.requests.delete")
def test_delete_novel_rejects_unexpected_success_response(
    mock_delete: Mock,
) -> None:
    response = Mock()
    response.status_code = 200
    response.json.return_value = {}
    mock_delete.return_value = response

    with pytest.raises(
        NovelApiError,
        match="意外的删除响应",
    ) as error_info:
        api_client.delete_novel(7)

    assert error_info.value.status_code == 200


@patch("frontend.api_client.requests.post")
def test_error_response_with_non_object_json_uses_fallback(
    mock_post: Mock,
) -> None:
    response = Mock()
    response.status_code = 502
    response.json.return_value = ["gateway failure"]
    mock_post.return_value = response

    with pytest.raises(NovelApiError, match="上传失败，请稍后重试"):
        upload_novel("story.txt", b"content")


@patch("frontend.api_client.requests.post")
def test_start_summaries_sends_consents_and_force(mock_post: Mock) -> None:
    response = Mock()
    response.status_code = 202
    response.json.return_value = {"id": "job-12", "status": "queued"}
    mock_post.return_value = response

    result = api_client.start_summaries(
        7,
        confirm_external_processing=True,
        confirm_content_rights=True,
        force=True,
        backend_url="http://testserver/",
    )

    assert result["id"] == "job-12"
    mock_post.assert_called_once_with(
        "http://testserver/api/novels/7/summaries",
        json={
            "confirm_external_processing": True,
            "confirm_content_rights": True,
            "force": True,
        },
        timeout=30,
    )


@patch("frontend.api_client.requests.post")
def test_start_summaries_defaults_force_to_false(mock_post: Mock) -> None:
    response = Mock()
    response.status_code = 202
    response.json.return_value = {"id": "job-13", "status": "queued"}
    mock_post.return_value = response

    api_client.start_summaries(
        7,
        confirm_external_processing=True,
        confirm_content_rights=True,
        backend_url="http://testserver",
    )

    assert mock_post.call_args.kwargs["json"]["force"] is False


@patch("frontend.api_client.requests.get")
def test_list_summaries_uses_novel_endpoint(mock_get: Mock) -> None:
    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "items": [],
        "total": 0,
        "completed": 0,
        "offset": 20,
        "limit": 10,
    }
    mock_get.return_value = response

    result = api_client.list_summaries(
        7,
        offset=20,
        limit=10,
        backend_url="http://testserver/",
    )

    assert result["items"] == []
    mock_get.assert_called_once_with(
        "http://testserver/api/novels/7/summaries",
        params={"offset": 20, "limit": 10},
        timeout=10,
    )


@patch("frontend.api_client.requests.get")
def test_get_chapter_summary_uses_single_chapter_endpoint(
    mock_get: Mock,
) -> None:
    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "chapter_id": 9,
        "chapter_index": 2,
        "summary": "章节摘要",
    }
    mock_get.return_value = response

    result = api_client.get_chapter_summary(
        7,
        2,
        backend_url="http://testserver/",
    )

    assert result["summary"] == "章节摘要"
    mock_get.assert_called_once_with(
        "http://testserver/api/novels/7/chapters/2/summary",
        timeout=10,
    )


@patch("frontend.api_client.requests.post")
def test_start_chapter_summary_sends_consents_and_force(
    mock_post: Mock,
) -> None:
    response = Mock()
    response.status_code = 202
    response.json.return_value = {"id": "job-single", "status": "queued"}
    mock_post.return_value = response

    result = api_client.start_chapter_summary(
        7,
        2,
        confirm_external_processing=True,
        confirm_content_rights=True,
        force=True,
        backend_url="http://testserver/",
    )

    assert result["id"] == "job-single"
    mock_post.assert_called_once_with(
        "http://testserver/api/novels/7/chapters/2/summary",
        json={
            "confirm_external_processing": True,
            "confirm_content_rights": True,
            "force": True,
        },
        timeout=30,
    )


@patch("frontend.api_client.requests.get")
def test_get_analysis_job_uses_job_endpoint(mock_get: Mock) -> None:
    response = Mock()
    response.status_code = 200
    response.json.return_value = {"id": "job-12", "status": "running"}
    mock_get.return_value = response

    result = api_client.get_analysis_job(
        "job-12",
        backend_url="http://testserver/",
    )

    assert result["status"] == "running"
    mock_get.assert_called_once_with(
        "http://testserver/api/analysis-jobs/job-12",
        timeout=10,
    )


@patch("frontend.api_client.requests.get")
def test_get_summary_estimate_uses_disclosure_endpoint(
    mock_get: Mock,
) -> None:
    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "chapter_count": 2,
        "character_count": 16,
        "estimated_generation_steps": 2,
        "maximum_request_attempts": 18,
    }
    mock_get.return_value = response

    result = api_client.get_summary_estimate(
        7,
        backend_url="http://testserver/",
    )

    assert result["estimated_generation_steps"] == 2
    assert result["maximum_request_attempts"] == 18
    mock_get.assert_called_once_with(
        "http://testserver/api/novels/7/summaries/estimate",
        timeout=10,
    )


@patch("frontend.api_client.requests.get")
def test_list_analysis_jobs_sends_optional_filter(mock_get: Mock) -> None:
    response = Mock()
    response.status_code = 200
    response.json.return_value = {"items": [], "total": 0}
    mock_get.return_value = response

    result = api_client.list_analysis_jobs(
        7,
        job_type="chapter_summaries",
        limit=5,
        backend_url="http://testserver/",
    )

    assert result["total"] == 0
    mock_get.assert_called_once_with(
        "http://testserver/api/novels/7/analysis-jobs",
        params={"limit": 5, "job_type": "chapter_summaries"},
        timeout=10,
    )


@pytest.mark.parametrize(
    ("function_name", "arguments", "request_method"),
    [
        ("get_system_status", (), "get"),
        ("start_summaries", (7, True, True), "post"),
        ("list_summaries", (7,), "get"),
        ("start_chapter_summary", (7, 2, True, True), "post"),
        ("get_chapter_summary", (7, 2), "get"),
        ("get_analysis_job", ("job-12",), "get"),
        ("get_summary_estimate", (7,), "get"),
        ("list_analysis_jobs", (7,), "get"),
    ],
)
def test_summary_clients_handle_connection_errors(
    function_name: str,
    arguments: tuple,
    request_method: str,
) -> None:
    with patch(
        f"frontend.api_client.requests.{request_method}",
        side_effect=requests.ConnectionError,
    ):
        client_function = getattr(api_client, function_name)
        with pytest.raises(NovelApiError, match="无法连接后端服务"):
            client_function(*arguments)


@pytest.mark.parametrize(
    ("function_name", "arguments", "request_method"),
    [
        ("get_system_status", (), "get"),
        ("start_summaries", (7, True, True), "post"),
        ("list_summaries", (7,), "get"),
        ("start_chapter_summary", (7, 2, True, True), "post"),
        ("get_chapter_summary", (7, 2), "get"),
        ("get_analysis_job", ("job-12",), "get"),
        ("get_summary_estimate", (7,), "get"),
        ("list_analysis_jobs", (7,), "get"),
    ],
)
def test_summary_clients_reject_non_object_json(
    function_name: str,
    arguments: tuple,
    request_method: str,
) -> None:
    response = Mock()
    response.status_code = 200
    response.json.return_value = []
    with patch(
        f"frontend.api_client.requests.{request_method}",
        return_value=response,
    ):
        client_function = getattr(api_client, function_name)
        with pytest.raises(NovelApiError, match="无法解析"):
            client_function(*arguments)


@pytest.mark.parametrize(
    ("function_name", "arguments", "request_method", "status_code"),
    [
        ("get_system_status", (), "get", 500),
        ("list_summaries", (7,), "get", 404),
        ("start_chapter_summary", (7, 2, True, True), "post", 400),
        ("get_chapter_summary", (7, 2), "get", 404),
    ],
)
def test_new_clients_preserve_http_error_status(
    function_name: str,
    arguments: tuple,
    request_method: str,
    status_code: int,
) -> None:
    response = Mock()
    response.status_code = status_code
    response.json.return_value = {"detail": "请求失败。"}
    with patch(
        f"frontend.api_client.requests.{request_method}",
        return_value=response,
    ):
        client_function = getattr(api_client, function_name)
        with pytest.raises(
            NovelApiError,
            match="请求失败",
        ) as error_info:
            client_function(*arguments)

    assert error_info.value.status_code == status_code


@patch("frontend.api_client.requests.delete")
def test_delete_novel_exposes_not_found_status(mock_delete: Mock) -> None:
    response = Mock()
    response.status_code = 404
    response.json.return_value = {"detail": "未找到指定小说。"}
    mock_delete.return_value = response

    with pytest.raises(
        NovelApiError,
        match="未找到指定小说",
    ) as error_info:
        api_client.delete_novel(999)

    assert error_info.value.status_code == 404


@patch("frontend.api_client.requests.get")
def test_get_character_estimate_uses_disclosure_endpoint(
    mock_get: Mock,
) -> None:
    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "chapter_count": 3,
        "character_count": 1_024,
        "estimated_generation_steps": 5,
        "maximum_request_attempts": 15,
    }
    mock_get.return_value = response

    result = api_client.get_character_estimate(
        7,
        backend_url="http://testserver/",
    )

    assert result["estimated_generation_steps"] == 5
    mock_get.assert_called_once_with(
        "http://testserver/api/novels/7/characters/estimate",
        timeout=10,
    )


@patch("frontend.api_client.requests.post")
def test_start_character_analysis_sends_consents_and_force(
    mock_post: Mock,
) -> None:
    response = Mock()
    response.status_code = 202
    response.json.return_value = {
        "id": "job-characters",
        "job_type": "characters",
        "status": "queued",
    }
    mock_post.return_value = response

    result = api_client.start_character_analysis(
        7,
        confirm_external_processing=True,
        confirm_content_rights=True,
        force=True,
        backend_url="http://testserver/",
    )

    assert result["id"] == "job-characters"
    mock_post.assert_called_once_with(
        "http://testserver/api/novels/7/characters",
        json={
            "confirm_external_processing": True,
            "confirm_content_rights": True,
            "force": True,
        },
        timeout=30,
    )


@patch("frontend.api_client.requests.get")
def test_list_characters_sends_pagination_search_and_sort(
    mock_get: Mock,
) -> None:
    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "items": [],
        "total": 0,
        "offset": 20,
        "limit": 10,
    }
    mock_get.return_value = response

    result = api_client.list_characters(
        7,
        offset=20,
        limit=10,
        search="林",
        sort="first_chapter",
        backend_url="http://testserver/",
    )

    assert result["items"] == []
    mock_get.assert_called_once_with(
        "http://testserver/api/novels/7/characters",
        params={
            "offset": 20,
            "limit": 10,
            "search": "林",
            "sort": "first_chapter",
        },
        timeout=10,
    )


@patch("frontend.api_client.requests.get")
def test_character_detail_appearances_and_relationships_use_endpoints(
    mock_get: Mock,
) -> None:
    responses = []
    for payload in (
        {"id": 11, "canonical_name": "林舟"},
        {"items": [], "total": 0},
        {"items": [], "total": 0},
    ):
        response = Mock()
        response.status_code = 200
        response.json.return_value = payload
        responses.append(response)
    mock_get.side_effect = responses

    assert api_client.get_character(
        7,
        11,
        backend_url="http://testserver/",
    )["canonical_name"] == "林舟"
    assert api_client.list_character_appearances(
        7,
        11,
        backend_url="http://testserver/",
    )["total"] == 0
    assert api_client.list_character_relationships(
        7,
        11,
        backend_url="http://testserver/",
    )["total"] == 0

    assert [call.args[0] for call in mock_get.call_args_list] == [
        "http://testserver/api/novels/7/characters/11",
        "http://testserver/api/novels/7/characters/11/appearances",
        "http://testserver/api/novels/7/characters/11/relationships",
    ]
    assert mock_get.call_args_list[1].kwargs["params"] == {
        "offset": 0,
        "limit": 100,
    }
    assert mock_get.call_args_list[2].kwargs["params"] == {
        "offset": 0,
        "limit": 100,
    }
    assert mock_get.call_args_list[1].kwargs["timeout"] == 10
    assert mock_get.call_args_list[2].kwargs["timeout"] == 10


@pytest.mark.parametrize(
    ("function_name", "arguments", "request_method"),
    [
        ("get_character_estimate", (7,), "get"),
        ("start_character_analysis", (7, True, True), "post"),
        ("list_characters", (7,), "get"),
        ("get_character", (7, 11), "get"),
        ("list_character_appearances", (7, 11), "get"),
        ("list_character_relationships", (7, 11), "get"),
    ],
)
def test_character_clients_handle_connection_errors(
    function_name: str,
    arguments: tuple,
    request_method: str,
) -> None:
    with patch(
        f"frontend.api_client.requests.{request_method}",
        side_effect=requests.ConnectionError,
    ):
        client_function = getattr(api_client, function_name)
        with pytest.raises(NovelApiError, match="无法连接后端服务"):
            client_function(*arguments)


@pytest.mark.parametrize(
    ("function_name", "arguments", "request_method"),
    [
        ("get_character_estimate", (7,), "get"),
        ("start_character_analysis", (7, True, True), "post"),
        ("list_characters", (7,), "get"),
        ("get_character", (7, 11), "get"),
        ("list_character_appearances", (7, 11), "get"),
        ("list_character_relationships", (7, 11), "get"),
    ],
)
def test_character_clients_reject_non_object_json(
    function_name: str,
    arguments: tuple,
    request_method: str,
) -> None:
    response = Mock()
    response.status_code = 200
    response.json.return_value = []
    with patch(
        f"frontend.api_client.requests.{request_method}",
        return_value=response,
    ):
        client_function = getattr(api_client, function_name)
        with pytest.raises(NovelApiError, match="无法解析"):
            client_function(*arguments)


@patch("frontend.api_client.requests.get")
def test_rag_estimate_and_status_use_expected_endpoints(
    mock_get: Mock,
) -> None:
    estimate_response = Mock()
    estimate_response.status_code = 200
    estimate_response.json.return_value = {"chunk_count": 4}
    status_response = Mock()
    status_response.status_code = 200
    status_response.json.return_value = {"current": True}
    mock_get.side_effect = [estimate_response, status_response]

    assert api_client.get_rag_estimate(
        7,
        force=True,
        backend_url="http://testserver/",
    )["chunk_count"] == 4
    assert api_client.get_rag_index_status(
        7,
        backend_url="http://testserver/",
    )["current"] is True
    assert mock_get.call_args_list[0].args[0] == (
        "http://testserver/api/novels/7/rag/estimate"
    )
    assert mock_get.call_args_list[0].kwargs["params"] == {"force": True}
    assert mock_get.call_args_list[1].args[0] == (
        "http://testserver/api/novels/7/rag/index"
    )


@patch("frontend.api_client.requests.post")
def test_start_rag_index_sends_all_confirmations(
    mock_post: Mock,
) -> None:
    response = Mock()
    response.status_code = 202
    response.json.return_value = {"id": "rag-index-job", "status": "queued"}
    mock_post.return_value = response

    result = api_client.start_rag_index(
        7,
        confirm_external_processing=True,
        confirm_content_rights=True,
        force=True,
        confirm_force_refresh=True,
        backend_url="http://testserver/",
    )

    assert result["id"] == "rag-index-job"
    mock_post.assert_called_once_with(
        "http://testserver/api/novels/7/rag/index",
        json={
            "confirm_external_processing": True,
            "confirm_content_rights": True,
            "force": True,
            "confirm_force_refresh": True,
        },
        timeout=30,
    )


@patch("frontend.api_client.requests.post")
def test_start_rag_question_sends_provider_specific_consents(
    mock_post: Mock,
) -> None:
    response = Mock()
    response.status_code = 202
    response.json.return_value = {
        "id": "rag-question",
        "status": "queued",
    }
    mock_post.return_value = response

    result = api_client.start_rag_question(
        7,
        "钥匙在哪里？",
        6,
        confirm_embedding_processing=True,
        confirm_generation_processing=True,
        confirm_content_rights=True,
        backend_url="http://testserver/",
    )

    assert result["id"] == "rag-question"
    mock_post.assert_called_once_with(
        "http://testserver/api/novels/7/rag/questions",
        json={
            "question": "钥匙在哪里？",
            "top_k": 6,
            "confirm_embedding_processing": True,
            "confirm_generation_processing": True,
            "confirm_content_rights": True,
        },
        timeout=30,
    )


@patch("frontend.api_client.requests.get")
def test_rag_history_and_detail_use_persisted_question_endpoints(
    mock_get: Mock,
) -> None:
    list_response = Mock()
    list_response.status_code = 200
    list_response.json.return_value = {
        "items": [],
        "total": 0,
        "offset": 20,
        "limit": 10,
    }
    detail_response = Mock()
    detail_response.status_code = 200
    detail_response.json.return_value = {
        "id": "question-id",
        "status": "succeeded",
    }
    mock_get.side_effect = [list_response, detail_response]

    api_client.list_rag_questions(
        7,
        offset=20,
        limit=10,
        backend_url="http://testserver/",
    )
    api_client.get_rag_question(
        7,
        "question-id",
        backend_url="http://testserver/",
    )

    assert mock_get.call_args_list[0].args[0] == (
        "http://testserver/api/novels/7/rag/questions"
    )
    assert mock_get.call_args_list[0].kwargs["params"] == {
        "offset": 20,
        "limit": 10,
    }
    assert mock_get.call_args_list[1].args[0] == (
        "http://testserver/api/novels/7/rag/questions/question-id"
    )


@pytest.mark.parametrize(
    ("function_name", "arguments", "request_method"),
    [
        ("get_rag_estimate", (7,), "get"),
        ("get_rag_index_status", (7,), "get"),
        ("start_rag_index", (7, True, True), "post"),
        (
            "start_rag_question",
            (7, "问题", 6, True, True, True),
            "post",
        ),
        ("list_rag_questions", (7,), "get"),
        ("get_rag_question", (7, "question-id"), "get"),
    ],
)
def test_rag_clients_handle_connection_errors(
    function_name: str,
    arguments: tuple,
    request_method: str,
) -> None:
    with patch(
        f"frontend.api_client.requests.{request_method}",
        side_effect=requests.ConnectionError,
    ):
        with pytest.raises(NovelApiError, match="无法连接后端服务"):
            getattr(api_client, function_name)(*arguments)
