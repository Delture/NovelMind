"""Split TXT novel text into chapters."""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Chapter:
    """A chapter parsed from the original novel text."""

    index: int
    title: str
    content: str


_CHINESE_NUMBER = "0-9零〇一二三四五六七八九十百千万两"
_CHAPTER_TITLE_PATTERN = re.compile(
    rf"^\s*(?:第[{_CHINESE_NUMBER}]+章(?:\s+.*)?|chapter\s+\d+(?:\s+.*)?)\s*$",
    re.IGNORECASE,
)


def split_chapters(raw_text: str) -> list[Chapter]:
    """Split novel text by chapter titles that occupy a complete line."""
    text = raw_text.strip()
    if not text:
        return []

    lines = text.splitlines()
    title_positions = [
        index
        for index, line in enumerate(lines)
        if _CHAPTER_TITLE_PATTERN.fullmatch(line)
    ]

    if not title_positions:
        return [Chapter(index=1, title="第1章", content=text)]

    chapters: list[Chapter] = []
    preface = "\n".join(lines[: title_positions[0]]).strip()

    for position, title_line_index in enumerate(title_positions):
        next_title_index = (
            title_positions[position + 1]
            if position + 1 < len(title_positions)
            else len(lines)
        )
        content = "\n".join(lines[title_line_index + 1 : next_title_index]).strip()

        if position == 0 and preface:
            content = f"{preface}\n\n{content}".strip()

        chapters.append(
            Chapter(
                index=position + 1,
                title=lines[title_line_index].strip(),
                content=content,
            )
        )

    return chapters
