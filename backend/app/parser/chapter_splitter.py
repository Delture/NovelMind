"""使用可预测的本地规则把 TXT 小说拆成章节。

项目没有让大模型猜章节，而是要求章标题独占一行并使用确定性正则：同一文件
每次处理都得到相同结果，测试也无需联网。规则宁可保守少切，也不要把正文中
偶然出现的“第十章……”误判为新章节。
"""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Chapter:
    """解析阶段的轻量、不可变章节值对象，此时尚未写入数据库。"""

    index: int
    title: str
    content: str


_CHINESE_NUMBER = "0-9零〇一二三四五六七八九十百千万两"
_MAX_CHAPTER_TITLE_CHARACTERS = 255
_CHINESE_CHAPTER_PREFIX_PATTERN = re.compile(
    rf"^第(?P<number>[{_CHINESE_NUMBER}]+)章",
)
_REPEATED_NUMBER_SUFFIX_PATTERN = re.compile(
    r"^\s*(?P<number>[0-9０-９]+)\s*[.．、:：]\s*\S",
)
_ENGLISH_CHAPTER_TITLE_PATTERN = re.compile(
    r"^chapter\s+\d+.*$",
    re.IGNORECASE,
)


def _is_chapter_title(line: str) -> bool:
    """判断一整行是否像章节标题，同时过滤过长正文和明显句子。"""
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
    """按独占一行的章标题切分全文，返回从 1 开始的连续章节。

    没找到标题不代表失败：短篇会整体作为第 1 章。首个标题前的简介或序言归入
    首章，保证输入原文不会因为解析规则而静默丢失。
    """
    text = raw_text.strip()
    if not text:
        return []

    lines = text.splitlines()
    # 先收集全部边界，再按“当前标题到下一标题”切片，逻辑更易验证。
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
