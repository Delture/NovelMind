"""Optional AI review of rule-based chapter boundaries (merge + retitle only)."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator


CHAPTER_REVIEW_PROMPT_VERSION = "chapter-review-v3"
HEAD_CHARS = 120
# Keep one-shot catalog prompts bounded; over this count, keep rule chapters.
MAX_REVIEW_CHAPTERS = 400
_MAX_TITLE = 255


class ReviewedChapter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=_MAX_TITLE)
    from_index: int = Field(ge=1)
    to_index: int = Field(ge=1)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("title must not be blank")
        return text


class ChapterReviewPayload(BaseModel):
    """Sparse model output: only merges/retitles. Empty chapters = keep as-is."""

    model_config = ConfigDict(extra="forbid")

    chapters: list[ReviewedChapter]


@dataclass(frozen=True)
class ChapterSnapshot:
    index: int
    title: str
    content: str
    character_count: int


def review_system_prompt() -> str:
    return (
        "你是 NovelMind 的小说章节目录结构校对助手。输入是由规则切分生成的章节目录快照"
        "（含原索引、标题、字数与开头摘录），你的任务是还原作品真实的章节边界与标题全貌。"
        "【校对判准（The Boundary Test）】"
        "仅在以下两种情况有明确文本依据时才输出修改项，其余正常章节一律不输出（保持原样）："
        "1. 误切合并：相邻条目实为同一章节的拆分（如标题含“（上）（下）”“（一）（二）”的分段残片、"
        "正文中途断行被误判为标题、或字数异常短小且开头摘录直接承接上一章句意）。"
        "2. 标题修正：原切分标题包含乱码、多余站点水印、目录序号错位，或开头摘录中存在更完整准确的真实章节标题。"
        "【操作约束与输出契约】"
        "- 严禁改写小说正文、严禁凭空捏造原文没有的标题、严禁跨非相邻章节合并、严禁跳号或遗漏。"
        "- 仅输出需要合并或修正标题的项；未列出的章节保持原样。全目录无修改时返回 {\"chapters\":[]}。"
        "- 输出严格 JSON，顶层字段为 chapters[{title, from_index, to_index}]；区间为闭区间，"
        "按原目录索引严格升序排列且互不重叠。"
    )


def catalog_for_prompt(chapters: list[ChapterSnapshot]) -> list[dict]:
    rows: list[dict] = []
    for chapter in chapters:
        head = chapter.content[:HEAD_CHARS].replace("\n", " ")
        rows.append(
            {
                "index": chapter.index,
                "title": chapter.title,
                "character_count": chapter.character_count,
                "head": head,
            }
        )
    return rows


def reconstruct_review_plan(
    payload: ChapterReviewPayload,
    chapters: list[ChapterSnapshot],
) -> ChapterReviewPayload:
    """Expand sparse changes into a full coverage plan; reject illegal ranges first."""
    chapter_count = len(chapters)
    if chapter_count < 1:
        raise ValueError("novel has no chapters")
    by_index = {chapter.index: chapter for chapter in chapters}
    if set(by_index) != set(range(1, chapter_count + 1)):
        raise ValueError("chapter indexes must be contiguous from 1")

    cursor = 1
    rebuilt: list[ReviewedChapter] = []
    for item in payload.chapters:
        if item.from_index < cursor:
            raise ValueError("chapter ranges must be in ascending order without overlap")
        if item.to_index < item.from_index:
            raise ValueError("to_index must be >= from_index")
        if item.to_index > chapter_count:
            raise ValueError("to_index exceeds chapter count")
        while cursor < item.from_index:
            original = by_index[cursor]
            rebuilt.append(
                ReviewedChapter(
                    title=original.title,
                    from_index=cursor,
                    to_index=cursor,
                )
            )
            cursor += 1
        rebuilt.append(item)
        cursor = item.to_index + 1
    while cursor <= chapter_count:
        original = by_index[cursor]
        rebuilt.append(
            ReviewedChapter(
                title=original.title,
                from_index=cursor,
                to_index=cursor,
            )
        )
        cursor += 1
    return ChapterReviewPayload(chapters=rebuilt)


def validate_review_plan(
    payload: ChapterReviewPayload, chapter_count: int
) -> list[ReviewedChapter]:
    if chapter_count < 1:
        raise ValueError("novel has no chapters")
    expected = 1
    ordered: list[ReviewedChapter] = []
    for item in payload.chapters:
        if item.from_index != expected:
            raise ValueError("chapter ranges must be contiguous from 1")
        if item.to_index < item.from_index:
            raise ValueError("to_index must be >= from_index")
        if item.to_index > chapter_count:
            raise ValueError("to_index exceeds chapter count")
        ordered.append(item)
        expected = item.to_index + 1
    if expected != chapter_count + 1:
        raise ValueError("chapter ranges must cover every chapter exactly once")
    return ordered


def plan_is_identity(
    plan: list[ReviewedChapter], chapters: list[ChapterSnapshot]
) -> bool:
    if len(plan) != len(chapters):
        return False
    for item, chapter in zip(plan, chapters, strict=True):
        if item.from_index != chapter.index or item.to_index != chapter.index:
            return False
        if item.title != chapter.title:
            return False
    return True


def apply_review_plan(
    chapters: list[ChapterSnapshot], plan: list[ReviewedChapter]
) -> list[ChapterSnapshot]:
    by_index = {chapter.index: chapter for chapter in chapters}
    rebuilt: list[ChapterSnapshot] = []
    for new_index, item in enumerate(plan, start=1):
        parts: list[str] = []
        for source_index in range(item.from_index, item.to_index + 1):
            source = by_index[source_index]
            if source_index == item.from_index:
                parts.append(source.content)
            else:
                # Keep dropped boundary titles visible inside merged body.
                parts.append(f"{source.title}\n{source.content}".strip())
        content = "\n\n".join(part for part in parts if part).strip()
        rebuilt.append(
            ChapterSnapshot(
                index=new_index,
                title=item.title,
                content=content,
                character_count=len(content),
            )
        )
    return rebuilt


def map_chapter_index(old_index: int, plan: list[ReviewedChapter]) -> int:
    for new_index, item in enumerate(plan, start=1):
        if item.from_index <= old_index <= item.to_index:
            return new_index
    return max(1, min(old_index, len(plan)))
