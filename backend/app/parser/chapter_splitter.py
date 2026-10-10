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
    rf"^第(?P<number>[{_CHINESE_NUMBER}]+)[章回节]",
)
_REPEATED_NUMBER_SUFFIX_PATTERN = re.compile(
    r"^\s*(?P<number>[0-9０-９]+)\s*[.．、:：]\s*\S",
)
_ENGLISH_CHAPTER_TITLE_PATTERN = re.compile(
    r"^chapter\s+\d+.*$",
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
