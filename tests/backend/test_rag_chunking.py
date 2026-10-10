
from dataclasses import dataclass


from backend.app.rag.chunking import (
    CHUNK_FORMAT_VERSION,
    CHUNK_OVERLAP_CHARACTERS,
    MAX_CHUNK_CHARACTERS,
    build_rag_chunks,
    rag_source_hash,
    split_chapter_ranges,
)


@dataclass
class FakeChapter:
    id: int
    chapter_index: int
    title: str
    content: str


def test_long_chunk_ranges_are_bounded_ordered_and_cover_every_character() -> None:
    content = ("甲" * 850 + "。\n") * 5
    ranges = split_chapter_ranges(content)

    assert len(ranges) > 1
    assert ranges[0][0] == 0
    assert ranges[-1][1] == len(content)
    assert all(
        0 <= start < end <= len(content)
        and end - start <= MAX_CHUNK_CHARACTERS
        for start, end in ranges
    )
    assert all(
        0 < previous_end - next_start <= CHUNK_OVERLAP_CHARACTERS
        for (_, previous_end), (next_start, _) in zip(
            ranges,
            ranges[1:],
        )
    )
    covered = {
        index
        for start, end in ranges
        for index in range(start, end)
    }
    assert covered == set(range(len(content)))


def test_chunks_preserve_exact_slices_and_are_stable() -> None:
    chapters = [
        FakeChapter(2, 2, "后章", "乙" * 1_500),
        FakeChapter(1, 1, "前章", "甲" * 20),
    ]
    first = build_rag_chunks(
        novel_id=7,
        chapters=chapters,
        embedding_model="Qwen/Qwen3-Embedding-4B",
        dimensions=1_024,
    )
    second = build_rag_chunks(
        novel_id=7,
        chapters=reversed(chapters),
        embedding_model="Qwen/Qwen3-Embedding-4B",
        dimensions=1_024,
    )

    assert first == second
    assert [chunk.chapter_index for chunk in first] == [1, 2, 2]
    assert len({chunk.chunk_id for chunk in first}) == len(first)
    for chunk in first:
        chapter = next(item for item in chapters if item.id == chunk.chapter_id)
        assert chunk.text == chapter.content[
            chunk.start_char:chunk.end_char
        ]
        assert chunk.text in chunk.embedding_text
        assert chapter.title in chunk.embedding_text


def test_source_hash_changes_with_source_or_embedding_contract() -> None:
    chapter = FakeChapter(1, 1, "第一章", "正文")
    chunks = build_rag_chunks(
        novel_id=1,
        chapters=[chapter],
        embedding_model="Qwen/Qwen3-Embedding-4B",
        dimensions=1_024,
    )
    original = rag_source_hash(
        chunks,
        embedding_model="Qwen/Qwen3-Embedding-4B",
        dimensions=1_024,
    )
    changed_chunks = build_rag_chunks(
        novel_id=1,
        chapters=[FakeChapter(1, 1, "第一章", "正文变更")],
        embedding_model="Qwen/Qwen3-Embedding-4B",
        dimensions=1_024,
    )

    assert CHUNK_FORMAT_VERSION == "rag-chunk-v2-local-sqlite"
    assert original != rag_source_hash(
        changed_chunks,
        embedding_model="Qwen/Qwen3-Embedding-4B",
        dimensions=1_024,
    )
    assert original != rag_source_hash(
        chunks,
        embedding_model="Qwen/Qwen3-Embedding-4B",
        dimensions=768,
    )


