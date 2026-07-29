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

    with pytest.raises(NovelApiError, match="TXT 文件内容为空"):
        upload_novel("empty.txt", b"")


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
