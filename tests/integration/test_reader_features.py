
from copy import deepcopy
import sqlite3

import pytest


@pytest.fixture
def reader_case(isolated_config):
    from sqlalchemy.orm import sessionmaker
    from backend.app.api import novels, workspace
    from backend.app.core.database import create_db_engine, get_db, init_db
    from backend.app.models.novel import Novel, Chapter
    from backend.app.models.workspace import ReadingState

    case = isolated_config
    engine = create_db_engine(case.config.get_settings().database_url)
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    contents = {
        1: "首段。\n\n   \n  📚😀 target target target target。\n<b>unsafe</b> % _ \\ '\n包含🧪目标两次🧪目标。",
        2: "target second\n\n  这里还有 target。\n目标在另一章出现。",
        3: "不匹配的普通正文。\nTARGET is case-sensitive.",
        4: "前" * 90 + "🚀定位值" + "后" * 90 + "\n" + "x" * 200,
        5: "aaaa",
    }
    old_state = dict(prefs={"theme": "dark", "size": 20},
                     position=dict(chapter=2, paragraph=1, offset=2),
                     bookmarks=[dict(id="legacy-bookmark", chapter=1, paragraph=1, offset=7,
                                     snippet="OLD_SNIPPET")])
    with factory() as db:
        book = Novel(id=1, filename="reader-features-authored.txt", encoding="utf-8", chapter_count=5)
        other = Novel(id=2, filename="other-authored.txt", encoding="utf-8", chapter_count=1)
        db.add_all([book, other]); db.flush()
        for index, content in contents.items():
            db.add(Chapter(novel_id=1, chapter_index=index, title=f"第{index}章 测试", content=content,
                           character_count=len(content)))
        db.add(Chapter(novel_id=2, chapter_index=1, title="另一部书", content="target OTHER_BOOK_ONLY",
                       character_count=22))
        db.add(ReadingState(novel_id=1, value=deepcopy(old_state)))
        db.commit()

    def database():
        with factory() as db:
            yield db

    case.app.include_router(novels.router, prefix="/api")
    case.app.include_router(workspace.router, prefix="/api")
    case.app.dependency_overrides[get_db] = database
    case.contents, case.old_state, case.factory = contents, old_state, factory
    try:
        yield case
    finally:
        case.app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def dump(case):
    db = sqlite3.connect((case.root / "review.db").as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        return tuple(db.iterdump())
    finally:
        db.close()


def search(case, query, **params):
    response = case.client.get("/api/novels/1/search", params={"q": query, **params})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    return response.json()


def utf16_slice(text, start, length):
    return text.encode("utf-16-le")[start * 2:(start + length) * 2].decode("utf-16-le")


def test_search_paginates_literal_hits_in_chapter_order_without_writing(reader_case):
    case = reader_case
    original = dump(case)
    pages = [search(case, "target", offset=offset, limit=2) for offset in (0, 2, 4)]
    hits = [hit for page in pages for hit in page["items"]]
    assert [hit["chapter_index"] for hit in hits] == [1, 1, 1, 2, 2]
    assert [page["has_more"] for page in pages] == [True, True, False]
    assert [page["next_offset"] for page in pages] == [2, 4, None]
    assert all(page["per_chapter_limit"] == 3 for page in pages)
    assert len({(hit["chapter_id"], hit["paragraph"], hit["offset"]) for hit in hits}) == 5
    for hit in hits:
        paragraph = [line for line in case.contents[hit["chapter_index"]].splitlines() if line.strip()][hit["paragraph"]]
        assert utf16_slice(paragraph, hit["offset"], hit["match_length"]) == "target"
        assert utf16_slice(hit["snippet"], hit["snippet_match_start"], hit["snippet_match_length"]) == "target"
    assert [hit["offset"] for hit in hits[:3]] == [7, 14, 21]  # Two emoji each occupy two UTF-16 units.
    assert dump(case) == original
    assert case.client.get("/api/workspace/tasks").json() == []


@pytest.mark.parametrize("query", ["%", "_", "\\", "'", "<b>unsafe</b>"])
def test_search_treats_like_symbols_and_markup_as_plain_text(reader_case, query):
    result = search(reader_case, query)
    assert len(result["items"]) == 1
    hit = result["items"][0]
    assert hit["chapter_index"] == 1
    assert query in hit["snippet"]
    assert utf16_slice(hit["snippet"], hit["snippet_match_start"], hit["snippet_match_length"]) == query
    assert search(reader_case, "' OR 1=1 --")["items"] == []
    assert search(reader_case, "OTHER_BOOK_ONLY")["items"] == []


def test_search_handles_emoji_indentation_empty_lines_and_bounded_snippets(reader_case):
    result = search(reader_case, "🧪目标")
    assert [hit["paragraph"] for hit in result["items"]] == [3, 3]
    assert [hit["offset"] for hit in result["items"]] == [2, 8]
    assert all(hit["match_length"] == 4 for hit in result["items"])
    hit = search(reader_case, "定位值")["items"][0]
    assert hit["offset"] == 92
    assert len(hit["snippet"]) <= 48 + len("定位值") + 64
    assert hit["snippet"] != reader_case.contents[4]
    assert utf16_slice(hit["snippet"], hit["snippet_match_start"], hit["snippet_match_length"]) == "定位值"


def test_search_nonoverlap_case_query_trimming_and_no_match_contract(reader_case):
    assert [hit["offset"] for hit in search(reader_case, "aa")["items"]] == [0, 2]
    assert [hit["chapter_index"] for hit in search(reader_case, "TARGET")["items"]] == [3]
    assert search(reader_case, "  target  ") == search(reader_case, "target")
    assert len(search(reader_case, "x" * 200)["items"]) == 1
    for result in (search(reader_case, "不存在的词"), search(reader_case, "target", offset=10000, limit=50)):
        assert result["items"] == [] and result["has_more"] is False and result["next_offset"] is None


@pytest.mark.parametrize("params", [{}, {"q": ""}, {"q": "   "}, {"q": "x" * 201},
    {"q": "target\n"}, {"q": "target\r"}, {"q": "target\x00"},
    {"q": "target", "offset": -1}, {"q": "target", "offset": 10001},
    {"q": "target", "limit": 0}, {"q": "target", "limit": 51}])
def test_search_rejects_invalid_bounds_without_changing_reader_state(reader_case, params):
    original = dump(reader_case)
    response = reader_case.client.get("/api/novels/1/search", params=params)
    assert response.status_code == 422, response.text
    assert dump(reader_case) == original


def test_search_missing_book_is_not_an_empty_success(reader_case):
    assert reader_case.client.get("/api/novels/999/search", params={"q": "target"}).status_code == 404


def test_old_bookmarks_default_label_note_without_a_get_write(reader_case):
    case = reader_case
    original = dump(case)
    response = case.client.get("/api/novels/1/reading")
    assert response.status_code == 200
    state = response.json()
    bookmark = state["bookmarks"][0]
    assert bookmark == {**case.old_state["bookmarks"][0], "label": "", "note": ""}
    assert state["position"] == case.old_state["position"]
    assert dump(case) == original
    assert case.client.put("/api/novels/1/reading", json=case.old_state).status_code == 200
    assert case.client.get("/api/novels/1/reading").json() == state


def test_bookmark_rename_note_and_delete_preserve_position_and_original_text(reader_case):
    case = reader_case
    state = case.client.get("/api/novels/1/reading").json()
    old = deepcopy(state)
    state["bookmarks"][0].update(label='<script>自编书签</script>', note="  备注📚\n第二行  ")
    assert case.client.put("/api/novels/1/reading", json=state).status_code == 200
    assert case.client.get("/api/novels/1/reading").json() == state
    assert state["position"] == old["position"] and state["prefs"] == old["prefs"]
    for name in ("id", "chapter", "paragraph", "offset", "snippet"):
        assert state["bookmarks"][0][name] == old["bookmarks"][0][name]
    state["bookmarks"] = []
    assert case.client.put("/api/novels/1/reading", json=state).status_code == 200
    assert case.client.get("/api/novels/1/reading").json() == state
    for index, original in case.contents.items():
        assert case.client.get(f"/api/novels/1/chapters/{index}").json()["content"] == original


@pytest.mark.parametrize("field,length", [("label", 80), ("note", 500)])
def test_bookmark_field_limits_accept_boundary_and_reject_atomic_overflow(reader_case, field, length):
    case = reader_case
    state = case.client.get("/api/novels/1/reading").json()
    state["bookmarks"][0][field] = "x" * length
    assert case.client.put("/api/novels/1/reading", json=state).status_code == 200
    original = dump(case)
    state["bookmarks"][0][field] += "x"
    assert case.client.put("/api/novels/1/reading", json=state).status_code == 422
    assert dump(case) == original
