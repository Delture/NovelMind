"""RAG 的确定性章节分块、embedding 文本格式和整书来源哈希。

分块边界是索引协议的一部分：同一原文必须每次得到相同 chunk_id、字符范围和
哈希，才能安全复用旧向量。算法优先在自然段或换行附近断开，找不到再硬切；
相邻块保留重叠，减少关键信息恰好跨边界而检索不到的概率。
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable, Protocol


CHUNK_FORMAT_VERSION = "rag-chunk-v2-local-sqlite"
MAX_CHUNK_CHARACTERS = 1_200
CHUNK_OVERLAP_CHARACTERS = 200
_MIN_BREAK_POSITION = 720
_BREAK_MARKERS = ("\n\n", "\n", "。", "！", "？", "；")


class ChapterLike(Protocol):
    """分块所需的最小章节形状，使 ORM 对象和测试假对象都可使用。"""

    id: int
    chapter_index: int
    title: str
    content: str


@dataclass(frozen=True)
class RagChunk:
    """尚未持久化的分块值对象，记录章节内的精确字符范围。"""

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
    """返回文本 UTF-8 SHA-256 十六进制摘要，作为稳定内容标识。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _find_chunk_end(content: str, start: int) -> int:
    """从 start 起选择不超过最大长度且尽量靠近自然换行的结束位置。

    返回值是左闭右开切片的 end。若搜索窗口无合适分隔，退回目标硬边界；始终
    保证 end 大于 start，调用循环因此能够前进。
    """
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
    """返回稳定字符区间；区间采用 Python 左闭右开的切片语义。"""
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


# chunk_id 包含章节与块序号；content_hash 证明这个范围内文字没有变化。
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


# 整书哈希绑定分块版本，算法升级后旧索引自动过期。
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
