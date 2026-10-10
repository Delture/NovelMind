
import pytest

from backend.app.parser.chapter_splitter import Chapter, split_chapters


def test_split_arabic_number_chapters() -> None:
    text = "第1章 开始\n第一章正文。\n\n第002章 相遇\n第二章正文。"

    chapters = split_chapters(text)

    assert chapters == [
        Chapter(index=1, title="第1章 开始", content="第一章正文。"),
        Chapter(index=2, title="第002章 相遇", content="第二章正文。"),
    ]


def test_split_chinese_number_chapters() -> None:
    text = "第一章 初见\n内容一。\n第十七章 再会\n内容二。"

    chapters = split_chapters(text)

    assert [chapter.title for chapter in chapters] == ["第一章 初见", "第十七章 再会"]
    assert [chapter.content for chapter in chapters] == ["内容一。", "内容二。"]


def test_split_english_chapters_case_insensitively() -> None:
    text = "Chapter 1 Start\nFirst.\nCHAPTER 001 Return\nSecond."

    chapters = split_chapters(text)

    assert [chapter.title for chapter in chapters] == [
        "Chapter 1 Start",
        "CHAPTER 001 Return",
    ]


def test_text_without_chapter_title_becomes_one_chapter() -> None:
    chapters = split_chapters("开头。\n\n结尾。")

    assert chapters == [Chapter(index=1, title="第1章", content="开头。\n\n结尾。")]


def test_empty_text_returns_empty_list() -> None:
    assert split_chapters("") == []
    assert split_chapters(" \n\t ") == []


def test_inline_chapter_words_are_not_treated_as_titles() -> None:
    text = "序言\n他读到第3章时停了下来。\n故事仍在继续。"

    chapters = split_chapters(text)

    assert len(chapters) == 1
    assert chapters[0].content == text


def test_preface_is_kept_in_first_chapter() -> None:
    text = "作品简介\n这是序言。\n\n第一章 正文\n章节内容。"

    chapters = split_chapters(text)

    assert chapters[0].content == "作品简介\n这是序言。\n\n章节内容。"


def test_split_common_titles_without_spaces() -> None:
    text = "第一章初见\n内容一。\n第2章：重逢\n内容二。\nChapter 3: End\n内容三。"

    chapters = split_chapters(text)

    assert [chapter.title for chapter in chapters] == [
        "第一章初见",
        "第2章：重逢",
        "Chapter 3: End",
    ]


def test_very_long_line_is_not_treated_as_a_chapter_title() -> None:
    long_sentence = "第1章" + "这是一段正文" * 50

    chapters = split_chapters(f"{long_sentence}\n后续正文。")

    assert chapters == [
        Chapter(
            index=1,
            title="第1章",
            content=f"{long_sentence}\n后续正文。",
        )
    ]


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
