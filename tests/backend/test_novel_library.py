"""Tests for browsing and deleting saved novels."""

import asyncio
import json
from datetime import datetime, timedelta
from typing import Generator, Optional
from urllib.parse import urlencode

import pytest
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base, create_db_engine, get_db
from backend.app.core.time import utc_now
from backend.app.main import app
from backend.app.models.novel import Chapter, Novel


@pytest.fixture(autouse=True)
def db_session() -> Generator[Session, None, None]:
    """Use a fresh in-memory SQLite database for every test."""
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
        test_engine.dispose()


def send_request(
    method: str,
    path: str,
    query: Optional[dict[str, object]] = None,
) -> tuple[int, object]:
    """Send one HTTP request directly to the FastAPI ASGI application."""
    messages: list[dict] = []
    request_sent = False

    async def receive() -> dict:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": urlencode(query or {}).encode(),
        "headers": [],
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
    return start_message["status"], json.loads(body) if body else None


def add_novel(
    db_session: Session,
    filename: str,
    chapter_contents: list[str],
    created_at: Optional[datetime] = None,
) -> Novel:
    """Create one saved novel for an API test."""
    novel = Novel(
        filename=filename,
        encoding="utf-8",
        chapter_count=len(chapter_contents),
        created_at=created_at or utc_now(),
        chapters=[
            Chapter(
                chapter_index=index,
                title=f"第{index}章",
                content=content,
                character_count=len(content),
            )
            for index, content in enumerate(chapter_contents, start=1)
        ],
    )
    db_session.add(novel)
    db_session.commit()
    db_session.refresh(novel)
    return novel


def test_list_novels_returns_newest_first_and_total(
    db_session: Session,
) -> None:
    older = utc_now() - timedelta(days=1)
    add_novel(db_session, "older.txt", ["旧内容"], created_at=older)
    latest = add_novel(db_session, "latest.txt", ["第一章", "第二章"])

    status_code, result = send_request(
        "GET",
        "/api/novels",
        {"offset": 0, "limit": 1},
    )

    assert status_code == 200
    assert result["total"] == 2
    assert result["offset"] == 0
    assert result["limit"] == 1
    assert result["items"] == [
        {
            "id": latest.id,
            "filename": "latest.txt",
            "encoding": "utf-8",
            "chapter_count": 2,
            "character_count": 6,
            "created_at": latest.created_at.isoformat(),
        }
    ]


def test_list_novels_returns_empty_page_for_empty_library() -> None:
    status_code, result = send_request("GET", "/api/novels")

    assert status_code == 200
    assert result == {
        "items": [],
        "total": 0,
        "offset": 0,
        "limit": 20,
    }


def test_list_novels_has_stable_id_order_and_allows_large_offset(
    db_session: Session,
) -> None:
    created_at = utc_now()
    older_id = add_novel(
        db_session,
        "first.txt",
        ["正文"],
        created_at=created_at,
    ).id
    newer_id = add_novel(
        db_session,
        "second.txt",
        ["正文"],
        created_at=created_at,
    ).id

    status_code, result = send_request(
        "GET",
        "/api/novels",
        {"offset": 1, "limit": 1},
    )
    empty_status, empty_result = send_request(
        "GET",
        "/api/novels",
        {"offset": 99, "limit": 1},
    )

    assert status_code == 200
    assert result["items"][0]["id"] == older_id
    assert newer_id > older_id
    assert empty_status == 200
    assert empty_result["items"] == []
    assert empty_result["total"] == 2


def test_get_novel_detail_returns_aggregate_metadata(
    db_session: Session,
) -> None:
    novel = add_novel(db_session, "story.txt", ["甲乙", "丙丁戊"])

    status_code, result = send_request("GET", f"/api/novels/{novel.id}")

    assert status_code == 200
    assert result["id"] == novel.id
    assert result["filename"] == "story.txt"
    assert result["chapter_count"] == 2
    assert result["character_count"] == 5


def test_list_chapters_is_paginated_without_full_content(
    db_session: Session,
) -> None:
    novel = add_novel(db_session, "story.txt", ["第一章正文", "第二章正文"])

    status_code, result = send_request(
        "GET",
        f"/api/novels/{novel.id}/chapters",
        {"offset": 1, "limit": 1},
    )

    assert status_code == 200
    assert result == {
        "total": 2,
        "offset": 1,
        "limit": 1,
        "items": [
            {
                "id": novel.chapters[1].id,
                "index": 2,
                "title": "第2章",
                "character_count": 5,
            }
        ],
    }
    assert "content" not in result["items"][0]


def test_list_chapters_does_not_select_full_content(
    db_session: Session,
) -> None:
    novel = add_novel(db_session, "story.txt", ["不应读取的正文"])
    statements: list[str] = []
    engine = db_session.get_bind()

    def capture_statement(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        status_code, _ = send_request(
            "GET",
            f"/api/novels/{novel.id}/chapters",
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    chapter_selects = [
        statement
        for statement in statements
        if statement.lstrip().startswith("select")
        and "from chapters" in statement
    ]
    assert status_code == 200
    assert chapter_selects
    assert all("chapters.content" not in statement for statement in chapter_selects)


def test_get_chapter_returns_full_content(db_session: Session) -> None:
    novel = add_novel(db_session, "story.txt", ["需要按需读取的正文"])

    status_code, result = send_request(
        "GET",
        f"/api/novels/{novel.id}/chapters/1",
    )

    assert status_code == 200
    assert result == {
        "id": novel.chapters[0].id,
        "novel_id": novel.id,
        "index": 1,
        "title": "第1章",
        "content": "需要按需读取的正文",
        "character_count": 9,
    }


@pytest.mark.parametrize(
    "path",
    [
        "/api/novels/999",
        "/api/novels/999/chapters",
        "/api/novels/999/chapters/1",
    ],
)
def test_library_endpoints_return_404_for_missing_novel(path: str) -> None:
    status_code, result = send_request("GET", path)

    assert status_code == 404
    assert result["detail"] == "未找到指定小说。"


def test_get_chapter_returns_404_for_missing_index(
    db_session: Session,
) -> None:
    novel = add_novel(db_session, "story.txt", ["正文"])

    status_code, result = send_request(
        "GET",
        f"/api/novels/{novel.id}/chapters/9",
    )

    assert status_code == 404
    assert result["detail"] == "未找到指定章节。"


def test_delete_novel_removes_its_chapters(db_session: Session) -> None:
    novel = add_novel(db_session, "story.txt", ["第一章", "第二章"])
    novel_id = novel.id
    statements: list[str] = []
    engine = db_session.get_bind()

    def capture_statement(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        status_code, result = send_request(
            "DELETE",
            f"/api/novels/{novel_id}",
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    assert status_code == 204
    assert result is None
    assert not any(
        statement.lstrip().startswith("select")
        and "from chapters" in statement
        for statement in statements
    )
    assert db_session.get(Novel, novel_id) is None
    assert db_session.query(Chapter).filter_by(novel_id=novel_id).count() == 0


def test_delete_missing_novel_returns_404() -> None:
    status_code, result = send_request("DELETE", "/api/novels/999")

    assert status_code == 404
    assert result["detail"] == "未找到指定小说。"


def test_delete_rolls_back_when_commit_fails(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    novel = add_novel(db_session, "story.txt", ["正文"])
    novel_id = novel.id

    def fail_commit() -> None:
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(db_session, "commit", fail_commit)

    status_code, result = send_request("DELETE", f"/api/novels/{novel_id}")

    assert status_code == 500
    assert result["detail"] == "小说删除失败，请稍后重试。"
    assert db_session.get(Novel, novel_id) is not None


@pytest.mark.parametrize(
    ("path", "query"),
    [
        ("/api/novels", {"offset": -1}),
        ("/api/novels", {"limit": 0}),
        ("/api/novels", {"limit": 101}),
        ("/api/novels/1/chapters", {"offset": -1}),
        ("/api/novels/1/chapters", {"limit": 0}),
        ("/api/novels/1/chapters", {"limit": 201}),
        ("/api/novels/0", None),
        ("/api/novels/0/chapters", None),
        ("/api/novels/1/chapters/0", None),
    ],
)
def test_library_rejects_invalid_pagination_and_identifiers(
    path: str,
    query: Optional[dict[str, object]],
) -> None:
    status_code, _ = send_request("GET", path, query)

    assert status_code == 422
