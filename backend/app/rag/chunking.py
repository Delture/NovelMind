"""Deterministic character-based chunking for external embeddings."""

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable, Protocol


CHUNK_FORMAT_VERSION = "rag-chunk-v1"
MAX_CHUNK_CHARACTERS = 1_200
CHUNK_OVERLAP_CHARACTERS = 200
_MIN_BREAK_POSITION = 720
_BREAK_MARKERS = ("\n\n", "\n", "。", "！", "？", "；")


class ChapterLike(Protocol):
    """Minimal chapter fields required by the deterministic chunker."""

    id: int
    chapter_index: int
    title: str
    content: str


@dataclass(frozen=True)
class RagChunk:
    """One exact chapter slice with stable retrieval provenance."""

    chunk_id: str
    novel_id: int
    chapter_id: int
    chapter_index: int
    chapter_title: str
    chunk_index: int
    start_char: int
    end_char: int
    text: str
    content_hash: str
    embedding_text: str


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _find_chunk_end(content: str, start: int) -> int:
    target = min(start + MAX_CHUNK_CHARACTERS, len(content))
    if target == len(content):
        return target

    minimum = min(start + _MIN_BREAK_POSITION, target)
    for marker in _BREAK_MARKERS:
        position = content.rfind(marker, minimum, target)
        if position >= minimum:
            return position + len(marker)
    return target


def split_chapter_ranges(content: str) -> list[tuple[int, int]]:
    """Return overlapping ranges that preserve every original character."""
    if not content:
        return []

    ranges: list[tuple[int, int]] = []
    start = 0
    while start < len(content):
        end = _find_chunk_end(content, start)
        if end <= start:
            end = min(start + MAX_CHUNK_CHARACTERS, len(content))
        ranges.append((start, end))
        if end == len(content):
            break
        start = max(start + 1, end - CHUNK_OVERLAP_CHARACTERS)
    return ranges


def document_embedding_text(
    *,
    chapter_index: int,
    chapter_title: str,
    text: str,
) -> str:
    """Format a retrieval document without local semantic processing."""
    return (
        "用途：检索小说原文\n"
        f"章节：第{chapter_index}章 {chapter_title}\n"
        f"正文片段：\n{text}"
    )

def query_embedding_text(question: str) -> str:
    """Format one user question for the same retrieval task."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question cannot be blank")
    return f"用途：检索小说原文\n问题：{question.strip()}"


def build_rag_chunks(
    *,
    novel_id: int,
    chapters: Iterable[ChapterLike],
    embedding_model: str,
    dimensions: int,
) -> list[RagChunk]:
    """Build stable, ordered chunks from stored chapters."""
    result: list[RagChunk] = []
    for chapter in sorted(chapters, key=lambda item: item.chapter_index):
        for chunk_index, (start, end) in enumerate(
            split_chapter_ranges(chapter.content),
            start=1,
        ):
            text = chapter.content[start:end]
            content_hash = _sha256_text(text)
            embedding_text = document_embedding_text(
                chapter_index=chapter.chapter_index,
                chapter_title=chapter.title,
                text=text,
            )
            identity = {
                "version": CHUNK_FORMAT_VERSION,
                "novel_id": novel_id,
                "chapter_id": chapter.id,
                "chapter_index": chapter.chapter_index,
                "chunk_index": chunk_index,
                "start": start,
                "end": end,
                "content_hash": content_hash,
                "embedding_model": embedding_model,
                "dimensions": dimensions,
            }
            chunk_id = "chunk_" + _sha256_text(
                json.dumps(
                    identity,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            result.append(
                RagChunk(
                    chunk_id=chunk_id,
                    novel_id=novel_id,
                    chapter_id=chapter.id,
                    chapter_index=chapter.chapter_index,
                    chapter_title=chapter.title,
                    chunk_index=chunk_index,
                    start_char=start,
                    end_char=end,
                    text=text,
                    content_hash=content_hash,
                    embedding_text=embedding_text,
                )
            )
    return result


def rag_source_hash(
    chunks: Iterable[RagChunk],
    *,
    embedding_model: str,
    dimensions: int,
) -> str:
    """Hash the complete ordered source and retrieval configuration."""
    payload = {
        "version": CHUNK_FORMAT_VERSION,
        "embedding_model": embedding_model,
        "dimensions": dimensions,
        "chunks": [
            {
                "chunk_id": chunk.chunk_id,
                "content_hash": chunk.content_hash,
            }
            for chunk in chunks
        ],
    }
    return _sha256_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
