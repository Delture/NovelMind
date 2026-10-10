
import asyncio
import json
from typing import Generator, Optional
from unittest.mock import Mock
from urllib.parse import urlencode

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api import novels as novels_api
from backend.app.api.novels import MAX_UPLOAD_BYTES
from backend.app.core.database import Base, create_db_engine, get_db
from backend.app.main import app
from backend.app.models.novel import Novel


@pytest.fixture(autouse=True)
def db_session() -> Generator[Session, None, None]:
    test_engine = create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    testing_session = sessionmaker(
        bind=test_engine,
        autocommit=False,
        autoflush=False,
    )
    Base.metadata.create_all(bind=test_engine)
    session = testing_session()

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield session
    finally:
        app.dependency_overrides.pop(get_db, None)
        session.close()
        Base.metadata.drop_all(bind=test_engine)


def send_request(
    content: bytes,
    filename: Optional[str],
    *,
    content_type: bytes = b"application/octet-stream",
) -> tuple[int, dict]:
    query_string = urlencode({"filename": filename}).encode() if filename else b""
    messages: list[dict] = []
    request_sent = False

    async def receive() -> dict:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": content, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/novels/upload",
        "raw_path": b"/api/novels/upload",
        "query_string": query_string,
        "headers": [(b"content-type", content_type)],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 80),
    }

    asyncio.run(app(scope, receive, send))

    start_message = next(
        message
        for message in messages
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return start_message["status"], json.loads(body)


def test_upload_utf8_txt_and_save_chapters(db_session: Session) -> None:
    text = "第一章 开始\n第一章正文。\n\n第二章 相遇\n第二章正文。"

    status_code, result = send_request(text.encode("utf-8"), "novel.txt")

    assert status_code == 200
    assert result["novel_id"] > 0
    assert result["filename"] == "novel.txt"
    assert result["encoding"] == "utf-8"
    assert result["chapter_count"] == 2
    assert [chapter["title"] for chapter in result["chapters"]] == [
        "第一章 开始",
        "第二章 相遇",
    ]
    assert "content" not in result["chapters"][0]
    assert result["chapters"][0]["character_count"] == 6

    saved_novel = db_session.get(Novel, result["novel_id"])
    assert saved_novel is not None
    assert saved_novel.filename == "novel.txt"
    assert saved_novel.encoding == "utf-8"
    assert saved_novel.chapter_count == 2
    assert [
        (chapter.chapter_index, chapter.title, chapter.content)
        for chapter in saved_novel.chapters
    ] == [
        (1, "第一章 开始", "第一章正文。"),
        (2, "第二章 相遇", "第二章正文。"),
    ]


def test_upload_gb18030_txt(db_session: Session) -> None:
    text = "第1章 开始\n正文内容。"

    status_code, result = send_request(text.encode("gb18030"), "novel.txt")

    assert status_code == 200
    assert result["encoding"] == "gb18030"
    assert result["chapter_count"] == 1
    saved_novel = db_session.get(Novel, result["novel_id"])
    assert saved_novel is not None
    assert saved_novel.encoding == "gb18030"


def test_reject_non_txt_file(db_session: Session) -> None:
    status_code, result = send_request(b"content", "novel.pdf")

    assert status_code == 400
    assert result["detail"] == "只支持上传 TXT 文件。"
    assert db_session.query(Novel).count() == 0


def test_reject_empty_txt_file() -> None:
    status_code, result = send_request(b"", "empty.txt")

    assert status_code == 400
    assert result["detail"] == "TXT 文件内容为空。"


def test_reject_oversized_txt_file() -> None:
    status_code, result = send_request(
        b"a" * (MAX_UPLOAD_BYTES + 1),
        "large.txt",
    )

    assert status_code == 413
    assert result["detail"] == "TXT 文件不能超过 20 MB。"


def test_database_error_rolls_back() -> None:
    failing_session = Mock(spec=Session)
    failing_session.commit.side_effect = SQLAlchemyError("commit failed")

    def override_get_db():
        yield failing_session

    app.dependency_overrides[get_db] = override_get_db
    status_code, result = send_request(
        "第一章 开始\n正文。".encode("utf-8"),
        "novel.txt",
    )

    assert status_code == 500
    assert result["detail"] == "小说保存失败，请稍后重试。"
    failing_session.rollback.assert_called_once()


def test_reject_text_with_null_bytes(db_session: Session) -> None:
    status_code, result = send_request(
        "第一章 开始\x00\n正文。".encode("utf-8"),
        "novel.txt",
    )

    assert status_code == 400
    assert result["detail"] == "TXT 文件包含无效的二进制控制字符。"
    assert db_session.query(Novel).count() == 0


def test_reject_excessive_chapter_count(db_session: Session) -> None:
    text = "\n".join(
        f"第{index}章\n正文"
        for index in range(1, novels_api.MAX_CHAPTERS + 2)
    )

    status_code, result = send_request(text.encode("utf-8"), "novel.txt")

    assert status_code == 400
    assert result["detail"] == (
        f"单本小说不能超过 {novels_api.MAX_CHAPTERS} 个章节。"
    )
    assert db_session.query(Novel).count() == 0


