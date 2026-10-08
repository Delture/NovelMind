"""Parser/upload and read-only repair rehearsal regressions on authored temporary data."""

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("encoding", ["utf-8", "gb18030"])
@pytest.mark.parametrize("ending", ["？", "！"])
def test_upload_persists_punctuated_chapter_boundary_without_losing_text(isolated_config, encoding, ending):
    """Use the actual upload/parser/database path, with synthetic content only."""
    from sqlalchemy.orm import sessionmaker
    from backend.app.api.novels import router
    from backend.app.core.database import create_db_engine, get_db, init_db
    from backend.app.models.novel import Novel, Chapter

    case = isolated_config
    engine = create_db_engine(case.config.get_settings().database_url)
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def database():
        with factory() as db:
            yield db

    case.app.include_router(router, prefix="/api")
    case.app.dependency_overrides[get_db] = database
    title = f"第3章3.来信在哪里{ending}"
    bodies = ["前一章原文。\n\n第十章发生的事情，让他久久不能忘怀。",
              "独立第三章原文。\n\n  中间缩进与标点!?！？必须保留。\n第三章结尾。",
              "后一章原文。"]
    titles = ["第2章 前夜", title, "第4章 晨光"]
    source = "\n\n".join(f"{heading}\n{body}" for heading, body in zip(titles, bodies))
    try:
        response = case.client.post("/api/novels/upload?filename=punctuation-original.txt",
                                    content=source.encode(encoding),
                                    headers={"content-type": "application/octet-stream"})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["chapter_count"] == 3, result
        assert [item["title"] for item in result["chapters"]] == titles
        with factory() as db:
            novel = db.get(Novel, result["novel_id"])
            saved = db.query(Chapter).filter(Chapter.novel_id == novel.id).order_by(Chapter.chapter_index).all()
            assert novel.chapter_count == len(saved) == 3
            assert [(chapter.chapter_index, chapter.title, chapter.content) for chapter in saved] == [
                (index, heading, body) for index, (heading, body) in enumerate(zip(titles, bodies), 1)
            ]
            assert "\n\n".join(f"{chapter.title}\n{chapter.content}" for chapter in saved) == source
    finally:
        case.app.dependency_overrides.pop(get_db, None)
        engine.dispose()


@pytest.fixture
def repair_source(isolated_config):
    """Three old records, two embedded boundaries, and an unrelated second book."""
    from backend.app.core.database import create_db_engine, init_db
    from backend.app.models.novel import Novel, Chapter
    from backend.app.models.workspace import ReadingState

    engine = create_db_engine(isolated_config.config.get_settings().database_url)
    init_db(engine)
    merged = ("第二章原文。\n\n  第3章3.来信在哪里？  \n\n第三章首段📚。\n"
              "  第三章第二段有缩进。 \n\n第4章4．再次出发！\n第四章独立段落。")
    bookmark = lambda name, chapter, paragraph, offset: dict(
        id=name, chapter=chapter, paragraph=paragraph, offset=offset,
        snippet="PRIVATE_BOOKMARK_EXCERPT_" + name)
    reading = dict(prefs={"theme": "dark", "size": 20},
                   position=dict(chapter=2, paragraph=2, offset=7),
                   bookmarks=[bookmark("before", 2, 0, 3), bookmark("heading", 2, 1, 4),
                              bookmark("body", 2, 3, 2), bookmark("fourth", 2, 5, 2),
                              bookmark("following", 3, 0, 6), bookmark("top", 3, -1, 0),
                              bookmark("heading-fourth", 2, 4, 0)])
    with engine.begin() as db:
        for novel_id, count in ((1, 3), (2, 1)):
            db.execute(Novel.__table__.insert().values(id=novel_id, filename=f"synthetic-{novel_id}.txt",
                                                     encoding="utf-8", chapter_count=count))
        for key, index, title, content, novel_id in [
            (11, 1, "第1章 初见", "原首章正文。\n", 1),
            (12, 2, "第2章 旧信", merged, 1),
            (13, 3, "第5章 归来", "末章原文🪶。", 1),
            (21, 1, "第1章 另一部书", "OTHER_BOOK_UNCHANGED", 2),
        ]:
            db.execute(Chapter.__table__.insert().values(id=key, novel_id=novel_id, chapter_index=index,
                        title=title, content=content, character_count=len(content)))
        db.execute(ReadingState.__table__.insert().values(novel_id=1, value=reading))
    engine.dispose()
    return SimpleNamespace(path=isolated_config.root / "review.db", root=isolated_config.root,
                           reading=reading, merged=merged)


def load_repair_source(path):
    from scripts import repair_chapter_boundaries as repair
    with closing(repair.open_readonly(path)) as db:
        return repair.snapshot(db, 1)


def database_dump(path):
    with closing(sqlite3.connect(path)) as db:
        return tuple(db.iterdump())


def prepare_rehearsal(case):
    from scripts import repair_chapter_boundaries as repair
    source = load_repair_source(case.path)
    plan, groups, reading = repair.make_plan(source)
    copy = case.root / "test-rehearsal.sqlite3"
    with closing(repair.open_readonly(case.path)) as db, closing(sqlite3.connect(copy)) as target:
        db.backup(target)
    return repair, source, plan, groups, reading, copy


@pytest.mark.parametrize("with_reading", [False, True])
def test_rehearsal_preserves_ids_text_other_book_anchors_and_is_idempotent(repair_source, with_reading):
    case = repair_source
    if not with_reading:
        with closing(sqlite3.connect(case.path)) as db:
            db.execute("DELETE FROM reading_states WHERE novel_id=1")
            db.commit()
    original_bytes = case.path.read_bytes()
    before_dump = database_dump(case.path)
    repair, source, plan, groups, reading, copy = prepare_rehearsal(case)
    assert (plan["old_count"], plan["new_count"], plan["added_boundaries"]) == (3, 5, 2)
    assert [(item["old_chapter_id"], item["new_indices"]) for item in plan["mapping"]] == [
        (11, [1]), (12, [2, 3, 4]), (13, [5])]
    assert source == load_repair_source(case.path), "Planning must not mutate its input"
    public_plan = json.dumps(plan, ensure_ascii=False)
    assert "PRIVATE_BOOKMARK_EXCERPT" not in public_plan
    assert "第三章首段" not in public_plan
    result = repair.rehearse(copy, source, plan, groups, reading)
    assert result["inserted"] == 2 and result["retained_old_ids"] == 3
    after = load_repair_source(copy)
    assert [row["chapter_index"] for row in after["chapters"]] == [1, 2, 3, 4, 5]
    assert [after["chapters"][index]["id"] for index in (0, 1, 4)] == [11, 12, 13]
    assert len({row["id"] for row in after["chapters"]}) == 5
    assert after["chapters"][2]["title"] == "第3章3.来信在哪里？"
    assert after["chapters"][3]["title"] == "第4章4．再次出发！"
    restored = (after["chapters"][1]["content"] +
                "".join(row["title"] + row["content"] for row in after["chapters"][2:4]))
    assert restored == case.merged  # Includes every original space, blank line and emoji.
    for old, new in ((source["chapters"][0], after["chapters"][0]),
                     (source["chapters"][2], after["chapters"][4])):
        assert (new["id"], new["title"], new["content"]) == (old["id"], old["title"], old["content"])
    assert all(row["character_count"] == len(row["content"]) for row in after["chapters"])
    if with_reading:
        expected = deepcopy(case.reading)
        expected["position"] = dict(chapter=3, paragraph=0, offset=7)
        destinations = [(2, 0, 3), (3, -1, 0), (3, 1, 2), (4, 0, 2), (5, 0, 6), (5, -1, 0), (4, -1, 0)]
        for bookmark, (chapter, paragraph, offset) in zip(expected["bookmarks"], destinations):
            bookmark.update(chapter=chapter, paragraph=paragraph, offset=offset)
        assert after["reading"] == expected
    else:
        assert after["reading"] is None
    with closing(sqlite3.connect(copy)) as db:
        assert db.execute("SELECT chapter_index,content FROM chapters WHERE id=21").fetchone() == (1, "OTHER_BOOK_UNCHANGED")
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert case.path.read_bytes() == original_bytes
    assert database_dump(case.path) == before_dump
    before_second = database_dump(copy)
    plan2, groups2, reading2 = repair.make_plan(after)
    assert plan2["added_boundaries"] == 0
    assert repair.rehearse(copy, after, plan2, groups2, reading2)["inserted"] == 0
    assert database_dump(copy) == before_second


def add_dependency(path, kind):
    """Seed valid dependencies instead of weakening database integrity constraints."""
    from backend.app.core.database import create_db_engine
    from backend.app.models.analysis import AnalysisJob, ChapterSummary
    from backend.app.models.character import CharacterAnalysis, ChapterCharacterExtraction
    from backend.app.models.rag import RagIndex, RagQuestion, RagChunkRecord
    from backend.app.models.report import NovelReport
    from backend.app.models.workspace import Workflow
    engine = create_db_engine("sqlite:///" + path.as_posix())
    try:
        with engine.begin() as db:
            if kind.startswith("analysis-"):
                db.execute(AnalysisJob.__table__.insert().values(id="dependency", novel_id=1,
                    job_type="chapter_summaries", status=kind.split("-", 1)[1]))
                return "analysis_jobs"
            if kind.startswith("workflow-"):
                db.execute(Workflow.__table__.insert().values(id="dependency", novel_id=1, kind="report",
                    status=kind.split("-", 1)[1], created_at=__import__("datetime").datetime(2026, 9, 27)))
                return "workflows"
            if kind == "summary":
                db.execute(ChapterSummary.__table__.insert().values(chapter_id=12, summary="派生摘要", key_events=[],
                    plot_progression="旧进展", model="fake", prompt_version="v1", source_hash="a"*64))
                return "chapter_summaries"
            if kind == "characters":
                db.execute(CharacterAnalysis.__table__.insert().values(novel_id=1, source_hash="a"*64, model="fake",
                    extraction_prompt_version="v1", merge_prompt_version="v1", relationship_prompt_version="v1", chapter_count=3))
                return "character_analyses"
            if kind == "extraction":
                db.execute(ChapterCharacterExtraction.__table__.insert().values(chapter_id=12, source_hash="a"*64,
                    model="fake", prompt_version="v1", extraction_data={"observations": []}))
                return "chapter_character_extractions"
            if kind == "rag-index":
                db.execute(RagIndex.__table__.insert().values(id=10, novel_id=1, source_hash="a"*64,
                    collection_name="fake-collection", embedding_provider="openai_compatible", embedding_model="fake",
                    embedding_dimensions=4, chunk_version="v1", chunk_count=1))
                db.execute(RagChunkRecord.__table__.insert().values(rag_index_id=10, chapter_id=12,
                    chunk_id="chunk_"+"a"*64, chapter_index=2, chunk_index=1, start_char=0, end_char=4, content_hash="a"*64))
                return "rag_indices"
            if kind == "rag-question":
                db.execute(RagQuestion.__table__.insert().values(id="dependency", novel_id=1, question="假问题",
                    index_source_hash="a"*64, top_k=3, status="running", generation_model="fake", prompt_version="v1"))
                return "rag_questions"
            if kind == "report":
                db.execute(NovelReport.__table__.insert().values(novel_id=1, source_hash="a"*64, model="fake",
                    prompt_version="v1", summary_count=3, character_count=0, generation_steps=1, report_data={"title":"旧报告"}))
                return "novel_reports"
            raise AssertionError(kind)
    finally:
        engine.dispose()


@pytest.mark.parametrize("dependency", ["analysis-queued", "analysis-running", "analysis-succeeded",
    "workflow-queued", "workflow-running", "summary", "characters", "extraction", "rag-index", "rag-question", "report"])
def test_rehearsal_refuses_tasks_and_derived_data_without_deleting_them(repair_source, dependency):
    table = add_dependency(repair_source.path, dependency)
    repair, source, plan, groups, reading, copy = prepare_rehearsal(repair_source)
    assert plan["dependencies"][table] == 1
    assert plan["ready_for_rehearsal"] is False
    original = database_dump(copy)
    with pytest.raises(ValueError, match="存在分析或任务依赖"):
        repair.rehearse(copy, source, plan, groups, reading)
    assert database_dump(copy) == original
    assert database_dump(repair_source.path) == original


def test_rehearsal_rolls_back_after_partial_writes(repair_source):
    repair, source, plan, groups, reading, copy = prepare_rehearsal(repair_source)
    # Fail on the second INSERT, after renumbering, rewriting an old row, and inserting a new row.
    with closing(sqlite3.connect(copy)) as db:
        db.executescript("""CREATE TRIGGER injected_failure BEFORE INSERT ON chapters
            WHEN NEW.title='第4章4．再次出发！'
            BEGIN SELECT RAISE(ABORT, 'injected rehearsal failure'); END;""")
    original = database_dump(copy)
    with pytest.raises(sqlite3.IntegrityError, match="injected rehearsal failure"):
        repair.rehearse(copy, source, plan, groups, reading)
    assert database_dump(copy) == original
    assert load_repair_source(repair_source.path) == source


def test_rehearsal_rejects_stale_plan_without_overwriting_new_state(repair_source):
    repair, source, plan, groups, reading, copy = prepare_rehearsal(repair_source)
    with closing(sqlite3.connect(copy)) as db:
        db.execute("UPDATE chapters SET content=content||'new local edit' WHERE id=13")
        db.commit()
    changed = database_dump(copy)
    with pytest.raises(ValueError, match="来源已变化"):
        repair.rehearse(copy, source, plan, groups, reading)
    assert database_dump(copy) == changed


@pytest.mark.parametrize("field,value", [("paragraph", 500), ("offset", 500)])
def test_invalid_reading_anchor_refuses_to_guess(repair_source, field, value):
    from scripts import repair_chapter_boundaries as repair
    source = load_repair_source(repair_source.path)
    source["reading"]["position"][field] = value
    with pytest.raises(ValueError, match="范围"):
        repair.make_plan(source)


def test_cli_dryrun_keeps_source_readonly_and_writes_only_separate_rehearsal(repair_source):
    from scripts import repair_chapter_boundaries as repair
    source = repair_source.path
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
    with closing(repair.open_readonly(source)) as db:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("UPDATE novels SET chapter_count=99 WHERE id=1")
    result = subprocess.run([sys.executable, "-m", "scripts.repair_chapter_boundaries",
                             "--database", str(source), "--novel-id", "1"],
                            cwd=Path(__file__).resolve().parents[2], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    public = json.loads(result.stdout)
    output = Path(public["output_directory"])
    assert output != source.parent and output.name.startswith("novelmind-chapter-repair-")
    assert public["source_opened_readonly"] is True
    assert public["rehearsal"]["inserted"] == 2
    assert database_dump(output / "before.sqlite3") == database_dump(source)
    assert load_repair_source(output / "rehearsal.sqlite3")["novel"]["chapter_count"] == 5
    assert hashlib.sha256(source.read_bytes()).hexdigest() == fingerprint
    assert "PRIVATE_BOOKMARK_EXCERPT" not in (output / "plan.json").read_text(encoding="utf-8")
    assert "第三章首段" not in (output / "plan.json").read_text(encoding="utf-8")
