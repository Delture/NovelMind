
import pytest

from backend.app.parser.chapter_splitter import Chapter, split_chapters


def test_split_arabic_number_chapters() -> None:
    text = "第1章 开始\n第一章正文。\n\n第002章 相遇\n第二章正文。"

    chapters = split_chapters(text)

    assert chapters == [
        Chapter(index=1, title="第1章 开始", content="第一章正文。"),
        Chapter(index=2, title="第002章 相遇", content="第二章正文。"),
    ]


def test_text_without_chapter_title_becomes_one_chapter() -> None:
    chapters = split_chapters("开头。\n\n结尾。")

    assert chapters == [Chapter(index=1, title="第1章", content="开头。\n\n结尾。")]


def test_inline_chapter_words_are_not_treated_as_titles() -> None:
    text = "序言\n他读到第3章时停了下来。\n故事仍在继续。"

    chapters = split_chapters(text)

    assert len(chapters) == 1
    assert chapters[0].content == text


def test_preface_is_kept_in_first_chapter() -> None:
    text = "作品简介\n这是序言。\n\n第一章 正文\n章节内容。"

    chapters = split_chapters(text)

    assert chapters[0].content == "作品简介\n这是序言。\n\n章节内容。"


@pytest.mark.parametrize(
    "raw_title",
    [
        "第3章3.来信在哪里?",
        "第3章3.来信在哪里？",
        "第3章3.快回来!",
        "第3章3.快回来！",
        "第3章来信在哪里?",
        "第3章来信在哪里？",
        "第3章快回来!",
        "第3章快回来！",
        "第3章 来信在哪里？",
        "第3章 快回来！",
        "第3章\t来信在哪里？",
        "第3章\u3000快回来！",
        "  第3章3.来信在哪里？  ",
        "\t第3章3.快回来！\t",
        "第3章3．来信在哪里？",
        "第3章3．快回来！",
    ],
)
def test_question_and_exclamation_titles_keep_every_chapter_and_paragraph(raw_title):
    """Authored variants cover punctuation explicitly, without assuming user-book codepoints."""
    text = (
        "第2章 来信前夜\n第二章首段。\n\n第二章末段。\n"
        f"{raw_title}\n第三章首段。\n\n  第三章保留缩进的一段。\n第三章末段。\n"
        "第4章 第二封信\n第四章正文。"
    )
    assert split_chapters(text) == [
        Chapter(index=1, title="第2章 来信前夜", content="第二章首段。\n\n第二章末段。"),
        Chapter(index=2, title=raw_title.strip(),
                content="第三章首段。\n\n  第三章保留缩进的一段。\n第三章末段。"),
        Chapter(index=3, title="第4章 第二封信", content="第四章正文。"),
    ]


@pytest.mark.parametrize("inside_chapter", [False, True])
def test_chapter_reference_sentence_is_preserved_without_a_false_boundary(inside_chapter):
    """Relaxing title-ending punctuation must preserve the existing narrative guard."""
    paragraph = "第十章发生的事情，让他久久不能忘怀。"
    body = f"{paragraph}\n\n他合上书，走向窗边。"
    if inside_chapter:
        text = f"第1章 夜读\n开头一段。\n{body}\n第2章 天亮\n结尾一段。"
        assert split_chapters(text) == [
            Chapter(index=1, title="第1章 夜读", content=f"开头一段。\n{body}"),
            Chapter(index=2, title="第2章 天亮", content="结尾一段。"),
        ]
    else:
        assert split_chapters(body) == [Chapter(index=1, title="第1章", content=body)]

@pytest.mark.parametrize(
    ("title_a", "title_b"),
    [
        ("第一回　甄士隐梦幻识通灵", "第二回　贾夫人仙逝扬州城"),
        ("第1节 开端", "第2节 转折"),
        ("第一章 来信", "第二章 启程"),
    ],
)
def test_chinese_heading_units_split(title_a, title_b) -> None:
    text = f"{title_a}\n甲段。\n\n{title_b}\n乙段。"
    chapters = split_chapters(text)
    assert [c.title for c in chapters] == [title_a, title_b]
    assert chapters[0].content == "甲段。"
    assert chapters[1].content == "乙段。"


def test_hui_period_sentence_is_not_a_title() -> None:
    """Body lines that start with 第N回 and end with 。 stay in the chapter."""
    text = (
        "第五回　开生面梦演红楼梦\n"
        "开头。\n"
        "第四回中既将薛家母子在荣府内寄居等事略已表明，此回则暂时不提。\n"
        "继续。\n"
        "第六回　贾宝玉初试云雨情\n"
        "下章。"
    )
    chapters = split_chapters(text)
    assert len(chapters) == 2
    assert "第四回中既将" in chapters[0].content
    assert chapters[1].title.startswith("第六回")


def test_hongloumeng_first_twelve_hui_without_rewriting() -> None:
    from pathlib import Path

    path = Path("/workspace/nm-live/红楼梦-前十二回-raw.txt")
    if not path.is_file():
        pytest.skip("local 红楼梦 fixture unavailable")
    chapters = split_chapters(path.read_text(encoding="utf-8"))
    assert len(chapters) == 12
    assert chapters[0].title.startswith("第一回")
    assert chapters[11].title.startswith("第十二回")
    assert all(len(c.content) > 1000 for c in chapters)
    assert "第四回中既将" in chapters[4].content
