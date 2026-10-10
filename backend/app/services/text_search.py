
import re

from sqlalchemy import func

from backend.app.models.novel import Chapter

PER_CHAPTER_LIMIT = 3
MAX_OFFSET = 10000


def utf16_length(value: str) -> int:
    """DOM Range 按 UTF-16 code units 定位，不能直接使用 Python 字符数。"""
    return len(value.encode("utf-16-le")) // 2


def search_text(db, novel_id: int, query: str, offset: int, limit: int) -> dict:
    """SQLite instr 按字面过滤；段落规则与 reader.js 的非空行切分一致。
    空行不占段落序号，缩进保留。
    """
    seen = 0
    hits = []
    chapters = (
        db.query(Chapter.id, Chapter.chapter_index, Chapter.title, Chapter.content)
        .filter(Chapter.novel_id == novel_id, func.instr(Chapter.content, query) > 0)
        .order_by(Chapter.chapter_index)
        .yield_per(8)
    )
    for chapter in chapters:
        count = 0
        paragraphs = (p for p in re.split(r"\r?\n", chapter.content) if p.strip())
        for paragraph_index, paragraph in enumerate(paragraphs):
            cursor = 0
            while count < PER_CHAPTER_LIMIT:
                position = paragraph.find(query, cursor)
                if position < 0:
                    break
                count += 1
                cursor = position + len(query)
                if seen >= offset:
                    start = max(0, position - 48)
                    end = min(len(paragraph), position + len(query) + 64)
                    hits.append(dict(
                        chapter_id=chapter.id, chapter_index=chapter.chapter_index,
                        chapter_title=chapter.title, paragraph=paragraph_index,
                        offset=utf16_length(paragraph[:position]),
                        match_length=utf16_length(query), snippet=paragraph[start:end],
                        snippet_match_start=utf16_length(paragraph[start:position]),
                        snippet_match_length=utf16_length(query),
                    ))
                    if len(hits) > limit:
                        return dict(items=hits[:limit], offset=offset, limit=limit,
                                    has_more=True,
                                    next_offset=offset + limit if offset + limit <= MAX_OFFSET else None,
                                    per_chapter_limit=PER_CHAPTER_LIMIT)
                seen += 1
            if count >= PER_CHAPTER_LIMIT:
                break
    return dict(items=hits, offset=offset, limit=limit, has_more=False,
                next_offset=None, per_chapter_limit=PER_CHAPTER_LIMIT)
