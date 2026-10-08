"""只读清点已合并章节，并在系统临时 SQLite 副本预演拆分。

此工具没有真实库 apply 命令：源始终以 URI mode=ro 打开。只对带相同重复数字的
独占章标题提出拆分；保存来源指纹、完整旧→新映射和阅读锚点映射，供人复核。
存在任何分析/任务依赖便拒绝预演写入，绝不删除派生结果来让修复通过。
"""

import argparse
from copy import deepcopy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile

from backend.app.parser.chapter_splitter import _is_chapter_title

NUMBERED_TITLE = re.compile(r"^第(?P<chapter>[0-9]+)章\s*(?P<repeat>[0-9０-９]+)\s*[.．、:：]\s*\S")
NOVEL_TABLES = ("analysis_jobs", "character_analyses", "character_appearances", "rag_indices",
                "rag_questions", "novel_reports", "workflows")
CHAPTER_TABLES = ("chapter_summaries", "chapter_character_extractions", "rag_chunks")
ANALYSIS_TABLES = ("characters", "character_relationships")


def digest(value):
    """只在计划中保存内容指纹，不输出小说正文或书签摘录。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def open_readonly(path):
    """只读连接不导入应用配置，不读取 .env，也不创建开发数据库。"""
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def snapshot(db, novel_id):
    """读取一部小说的源记录、阅读状态及所有已知章节派生依赖数量。"""
    novel = db.execute("SELECT * FROM novels WHERE id=?", (novel_id,)).fetchone()
    if novel is None:
        raise ValueError("未找到指定小说。")
    chapters = [dict(row) for row in db.execute(
        "SELECT * FROM chapters WHERE novel_id=? ORDER BY chapter_index", (novel_id,))]
    if [row["chapter_index"] for row in chapters] != list(range(1, len(chapters) + 1)):
        raise ValueError("现有章节序号不连续，拒绝自动预演。")
    if novel["chapter_count"] != len(chapters):
        raise ValueError("小说章节数与实际记录不一致，须先人工复核。")
    dependencies = {}
    for table in NOVEL_TABLES:
        dependencies[table] = db.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE novel_id=?', (novel_id,)).fetchone()[0]
    for table in CHAPTER_TABLES:
        dependencies[table] = db.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE chapter_id IN '
            '(SELECT id FROM chapters WHERE novel_id=?)', (novel_id,)).fetchone()[0]
    for table in ANALYSIS_TABLES:
        dependencies[table] = db.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE analysis_id IN '
            '(SELECT id FROM character_analyses WHERE novel_id=?)', (novel_id,)).fetchone()[0]
    row = db.execute("SELECT value FROM reading_states WHERE novel_id=?", (novel_id,)).fetchone()
    reading = json.loads(row[0]) if row else None
    return {"novel": dict(novel), "chapters": chapters, "dependencies": dependencies, "reading": reading}


def paragraphs(content):
    """与 reader.js 的非空行段落规则一致，保留每段在原字符串中的范围。"""
    result = []
    for match in re.finditer(r"([^\n]*)(?:\n|$)", content):
        value = match.group(1)
        if value.endswith("\r"):
            value = value[:-1]
        if value.strip():
            result.append((match.start(), match.start() + len(value), value))
    return result


def split_existing(row):
    """保留全部正文字符；新标题仅从正文移入 title 字段，不去除空行或缩进。"""
    content = row["content"]
    boundaries = []
    for start, end, line in paragraphs(content):
        title = line.strip()
        match = NUMBERED_TITLE.match(title)
        if match and int(match["chapter"]) == int(match["repeat"]) and _is_chapter_title(title):
            heading_start = start + len(line) - len(line.lstrip())
            boundaries.append((heading_start, heading_start + len(title), title))
        elif _is_chapter_title(title):
            raise ValueError("正文中存在本工具未支持的标题格式，须人工指定边界。")
    sections = []
    cursor = 0
    title = row["title"]
    previous_heading = None
    for start, end, next_title in boundaries:
        sections.append({"title": title, "content": content[cursor:start], "start": cursor,
                         "end": start, "heading": None if cursor == 0 else previous_heading})
        title = next_title
        cursor = end
        previous_heading = (start, end)
    sections.append({"title": title, "content": content[cursor:], "start": cursor,
                     "end": len(content), "heading": None if cursor == 0 else previous_heading})
    restored = sections[0]["content"] + "".join(item["title"] + item["content"] for item in sections[1:])
    if restored != content:
        raise ValueError("拆分前后字符校验不一致，拒绝继续。")
    return sections


def relocate_anchor(anchor, old, sections):
    """用段落原文和范围迁移锚点；正文段落的 UTF-16 字符偏移保持原值。

    若锚点恰好落在提升为标题的那一行，转到新章顶部。其余情况逐段比对，无法
    精确匹配时拒绝预演，避免静默把阅读位置移到另一段。
    """
    result = deepcopy(anchor)
    paragraph = anchor.get("paragraph", -1)
    if paragraph == -1:
        result.update(chapter=sections[0]["new_index"], paragraph=-1, offset=0)
        return result
    original_paragraphs = paragraphs(old["content"])
    if not 0 <= paragraph < len(original_paragraphs):
        raise ValueError("已存阅读锚点超出原文章节范围，须人工复核。")
    start, end, value = original_paragraphs[paragraph]
    offset = anchor.get("offset", 0)
    if not 0 <= offset <= len(value.encode("utf-16-le")) // 2:
        raise ValueError("已存阅读偏移超出段落范围，须人工复核。")
    for section in sections:
        heading = section["heading"]
        if heading and start <= heading[0] and end >= heading[1] and value.strip() == section["title"]:
            result.update(chapter=section["new_index"], paragraph=-1, offset=0)
            return result
        if section["start"] <= start and end <= section["end"]:
            for index, (new_start, _, new_value) in enumerate(paragraphs(section["content"])):
                if new_start == start - section["start"] and new_value == value:
                    result.update(chapter=section["new_index"], paragraph=index)
                    return result
    raise ValueError("阅读锚点无法无损映射，拒绝继续。")


def make_plan(source):
    """生成仅含标题、范围、指纹和计数的计划，以及驻内存的完整拆分记录。"""
    groups = {}
    new_index = 0
    mapping = []
    for row in source["chapters"]:
        sections = split_existing(row)
        for index, section in enumerate(sections):
            new_index += 1
            section.update(new_index=new_index, retained_id=row["id"] if index == 0 else None)
        groups[row["chapter_index"]] = sections
        mapping.append({"old_chapter_id": row["id"], "old_index": row["chapter_index"],
                        "new_indices": [s["new_index"] for s in sections],
                        "new_titles": [s["title"] for s in sections]})
    if new_index > 10000:
        raise ValueError("修复后的章节数超过当前阅读状态支持范围。")
    reading = deepcopy(source["reading"])
    anchor_changes = []
    by_index = {row["chapter_index"]: row for row in source["chapters"]}
    if reading:
        anchors = [("position", reading["position"])] + [("bookmark", a) for a in reading.get("bookmarks", [])]
        for kind, anchor in anchors:
            old_index = anchor["chapter"]
            if old_index not in groups:
                raise ValueError("阅读状态引用未知章节，拒绝继续。")
            mapped = relocate_anchor(anchor, by_index[old_index], groups[old_index])
            anchor_changes.append({"kind": kind,
                "old": {k: anchor[k] for k in ("chapter", "paragraph", "offset")},
                "new": {k: mapped[k] for k in ("chapter", "paragraph", "offset")}})
            anchor.clear()
            anchor.update(mapped)
    plan = {"novel_id": source["novel"]["id"], "filename": source["novel"]["filename"],
            "source_fingerprint": digest(source), "old_count": len(source["chapters"]),
            "new_count": new_index, "added_boundaries": new_index - len(source["chapters"]),
            "affected_old_chapters": sum(len(s) > 1 for s in groups.values()),
            "dependencies": source["dependencies"], "reading_states": int(reading is not None),
            "bookmarks": len(reading.get("bookmarks", [])) if reading else 0,
            "anchor_changes": anchor_changes, "mapping": mapping,
            "body_and_promoted_titles_preserved_exactly": True,
            "ready_for_rehearsal": not any(source["dependencies"].values())}
    plan["plan_fingerprint"] = digest(plan)
    return plan, groups, reading


def rehearse(copy_path, source, plan, groups, reading):
    """仅对调用方新建的临时副本执行事务；保留旧 ID，不删除任何记录。"""
    with closing(sqlite3.connect(copy_path)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        if digest(snapshot(db, plan["novel_id"])) != plan["source_fingerprint"]:
            raise ValueError("来源已变化，必须重新生成计划。")
        if not plan["ready_for_rehearsal"]:
            raise ValueError("存在分析或任务依赖，拒绝自动拆分或清除结果。")
        offset = plan["old_count"] + plan["new_count"] + 1
        db.execute("UPDATE chapters SET chapter_index=chapter_index+? WHERE novel_id=?",
                   (offset, plan["novel_id"]))
        inserted = 0
        for sections in groups.values():
            for item in sections:
                if item["retained_id"] is not None:
                    db.execute("UPDATE chapters SET chapter_index=?,content=?,character_count=? WHERE id=? AND novel_id=?",
                               (item["new_index"], item["content"], len(item["content"]),
                                item["retained_id"], plan["novel_id"]))
                else:
                    db.execute("INSERT INTO chapters(novel_id,chapter_index,title,content,character_count) VALUES(?,?,?,?,?)",
                               (plan["novel_id"], item["new_index"], item["title"], item["content"], len(item["content"])))
                    inserted += 1
        db.execute("UPDATE novels SET chapter_count=? WHERE id=?", (plan["new_count"], plan["novel_id"]))
        if reading is not None:
            db.execute("UPDATE reading_states SET value=? WHERE novel_id=?",
                       (json.dumps(reading, ensure_ascii=False), plan["novel_id"]))
        after = snapshot(db, plan["novel_id"])
        saved = {row["chapter_index"]: row for row in after["chapters"]}
        for old in source["chapters"]:
            sections = groups[old["chapter_index"]]
            first = saved[sections[0]["new_index"]]
            restored = first["content"] + "".join(saved[s["new_index"]]["title"] + saved[s["new_index"]]["content"] for s in sections[1:])
            if restored != old["content"] or first["id"] != old["id"] or first["title"] != old["title"]:
                raise ValueError("写入后的正文、旧 ID 或原标题校验失败，事务回滚。")
        if after["reading"] != reading or after["dependencies"] != source["dependencies"]:
            raise ValueError("阅读状态或依赖数据校验失败，事务回滚。")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("外键检查失败，事务回滚。")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("数据库完整性检查失败，事务回滚。")
        db.commit()
    return {"inserted": inserted, "retained_old_ids": len(source["chapters"]),
            "content_exact": True, "reading_exact": True, "foreign_keys": "ok", "quick_check": "ok"}


def main():
    """生成只读 SQLite 备份、可复核计划和预演副本；所有产物限系统临时目录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--novel-id", type=int, required=True)
    args = parser.parse_args()
    directory = Path(tempfile.mkdtemp(prefix="novelmind-chapter-repair-"))
    backup = directory / "before.sqlite3"
    copy = directory / "rehearsal.sqlite3"
    with closing(open_readonly(args.database)) as origin:
        with closing(sqlite3.connect(backup)) as target:
            origin.backup(target)
    with closing(open_readonly(backup)) as saved:
        saved.execute("BEGIN")
        source = snapshot(saved, args.novel_id)
        plan, groups, reading = make_plan(source)
        saved.rollback()
        with closing(sqlite3.connect(copy)) as target:
            saved.backup(target)
    (directory / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    result = rehearse(copy, source, plan, groups, reading) if plan["ready_for_rehearsal"] else {"blocked": "存在派生数据或任务，仅输出计划"}
    (directory / "rehearsal.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output_directory": str(directory), "novel_id": plan["novel_id"],
                      "old_count": plan["old_count"], "new_count": plan["new_count"],
                      "added_boundaries": plan["added_boundaries"], "affected_old_chapters": plan["affected_old_chapters"],
                      "dependencies": plan["dependencies"], "reading_states": plan["reading_states"],
                      "bookmarks": plan["bookmarks"], "anchor_changes": plan["anchor_changes"],
                      "rehearsal": result, "source_opened_readonly": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
