"""不让大模型猜章节：章标题须独占一行并用确定性正则，结果可复现。
识别第N章/回/节；宁可少切，也不把正文中偶然出现的“第十章……”误判为新章节。
"""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Chapter:
    index: int
    title: str
    content: str


_CHINESE_NUMBER = "0-9零〇一二三四五六七八九十百千万两"
_MAX_CHAPTER_TITLE_CHARACTERS = 255
_CHINESE_CHAPTER_PREFIX_PATTERN = re.compile(
    rf"^第(?P<number>[{_CHINESE_NUMBER}]+)(?P<unit>[章回节])",
)
_REPEATED_NUMBER_SUFFIX_PATTERN = re.compile(
    r"^\s*(?P<number>[0-9０-９]+)\s*[.．、:：]\s*\S",
)
_ENGLISH_CHAPTER_TITLE_PATTERN = re.compile(
    r"^chapter\s+(?P<number>\d+).*$",
    re.IGNORECASE,
)


def _is_chapter_title(line: str) -> bool:
    title = line.strip()
    if not title or len(title) > _MAX_CHAPTER_TITLE_CHARACTERS:
        return False

    # 问号/叹号常见于章标题，不能仅因结尾标点拒绝。对句号/分号仍保留正文
    # 防误切；“第3章3.标题。”具有重复章号与分隔符，是明确的编号副标题。
    chinese_match = _CHINESE_CHAPTER_PREFIX_PATTERN.match(title)
    if chinese_match:
        suffix = title[chinese_match.end():]
        direct_suffix = (
            suffix
            and not suffix[0].isspace()
            and suffix[0] not in "：:、.．-—"
        )
        numbered_suffix = _REPEATED_NUMBER_SUFFIX_PATTERN.match(suffix)
        number = chinese_match.group("number")
        repeated_number = (
            numbered_suffix is not None
            and number.isdecimal()
            and int(numbered_suffix.group("number")) == int(number)
        )
        if (
            direct_suffix
            and not repeated_number
            and title.endswith(("。", ".", "；", ";"))
        ):
            return False
        return True

    return bool(_ENGLISH_CHAPTER_TITLE_PATTERN.fullmatch(title))


def _chapter_number_key(title: str) -> tuple[str, int | str] | int | None:
    """Comparable chapter number for adjacent-boundary fold; None = no evidence."""
    stripped = title.strip()
    chinese_match = _CHINESE_CHAPTER_PREFIX_PATTERN.match(stripped)
    if chinese_match:
        number = chinese_match.group("number")
        unit = chinese_match.group("unit")
        value: int | str = int(number) if number.isdecimal() else number
        return (unit, value)
    english_match = _ENGLISH_CHAPTER_TITLE_PATTERN.fullmatch(stripped)
    if english_match:
        number = english_match.group("number")
        # Digits continuing into another numbering component (1.1 / 1A / 1．2).
        suffix = stripped[english_match.start("number") + len(number) :]
        if suffix and (suffix[0].isalnum() or suffix[0] in ".．"):
            return None
        return int(number)
    return None


def _lines_are_blank(lines: list[str]) -> bool:
    return not any(line.strip() for line in lines)


@dataclass(frozen=True)
class _Boundary:
    # Line index where the previous chapter's content must stop (group-first title).
    boundary_line: int
    title_line: int
    early_titles: tuple[str, ...]


def _fold_title_boundaries(lines: list[str], title_positions: list[int]) -> list[_Boundary]:
    boundaries: list[_Boundary] = []
    index = 0
    while index < len(title_positions):
        group_start = index
        while index + 1 < len(title_positions):
            current_line = title_positions[index]
            next_line = title_positions[index + 1]
            current_key = _chapter_number_key(lines[current_line])
            next_key = _chapter_number_key(lines[next_line])
            if (
                current_key is None
                or next_key is None
                or current_key != next_key
                or not _lines_are_blank(lines[current_line + 1 : next_line])
            ):
                break
            index += 1
        group_end = index

        last_line = title_positions[group_end]
        following_end = (
            title_positions[group_end + 1]
            if group_end + 1 < len(title_positions)
            else len(lines)
        )
        has_body = not _lines_are_blank(lines[last_line + 1 : following_end])

        if has_body and group_end > group_start:
            early_titles = tuple(
                lines[title_positions[item]].strip()
                for item in range(group_start, group_end)
            )
            boundaries.append(
                _Boundary(
                    boundary_line=title_positions[group_start],
                    title_line=last_line,
                    early_titles=early_titles,
                )
            )
        else:
            for item in range(group_start, group_end + 1):
                line = title_positions[item]
                boundaries.append(
                    _Boundary(boundary_line=line, title_line=line, early_titles=())
                )
        index = group_end + 1
    return boundaries


def split_chapters(raw_text: str) -> list[Chapter]:
    """短篇没有标题时整体作为第 1 章；首个标题前的简介或序言归入首章，保证原文不会被解析规则静默丢失。"""
    text = raw_text.strip()
    if not text:
        return []

    lines = text.splitlines()
    title_positions = [
        index
        for index, line in enumerate(lines)
        if _is_chapter_title(line)
    ]

    # 格式识别失败时保留全文，比返回空列表更安全，用户仍可阅读和排查。
    if not title_positions:
        return [Chapter(index=1, title="第1章", content=text)]

    boundaries = _fold_title_boundaries(lines, title_positions)
    chapters: list[Chapter] = []
    preface = "\n".join(lines[: title_positions[0]]).strip()

    for position, boundary in enumerate(boundaries):
        next_boundary_line = (
            boundaries[position + 1].boundary_line
            if position + 1 < len(boundaries)
            else len(lines)
        )
        body = "\n".join(lines[boundary.title_line + 1 : next_boundary_line]).strip()
        if boundary.early_titles:
            early = "\n".join(boundary.early_titles)
            content = f"{early}\n\n{body}".strip() if body else early
        else:
            content = body

        if position == 0 and preface:
            content = f"{preface}\n\n{content}".strip()

        chapters.append(
            Chapter(
                index=position + 1,
                title=lines[boundary.title_line].strip(),
                content=content,
            )
        )

    return chapters
