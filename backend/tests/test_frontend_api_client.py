"""Tests for the Streamlit frontend API client."""

from unittest.mock import Mock, patch

import pytest
import requests

from frontend.api_client import (
    NovelApiError,
    check_backend,
    upload_novel,
)


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
    mock_get.return_value = response

    assert check_backend("http://testserver/") is True
    mock_get.side_effect = requests.ConnectionError
    assert check_backend("http://testserver/") is False
