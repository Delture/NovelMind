
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
