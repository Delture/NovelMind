"""进程可能在新集合写完、主库指针切换前退出而留下孤儿集合；
以主库当前 collection_name 与有效构建检查点为保留依据。
维护失败只记日志，不阻止后端启动。
"""

import logging
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session

from backend.app.models.novel import Chapter
from backend.app.models.rag import RagBuildCheckpoint, RagIndex
from backend.app.rag.chunking import (
    CHUNK_FORMAT_VERSION,
    build_rag_chunks,
    rag_source_hash,
)
from backend.app.rag.vector_store import LocalVectorStore


logger = logging.getLogger(__name__)
_MANAGED_COLLECTION_PREFIX = "novelmind_n"


def _checkpoint_still_valid(
    db: Session,
    checkpoint: RagBuildCheckpoint,
) -> bool:
    if checkpoint.chunk_version != CHUNK_FORMAT_VERSION:
        return False
    chapters = (
        db.query(Chapter)
        .filter(Chapter.novel_id == checkpoint.novel_id)
        .order_by(Chapter.chapter_index)
        .all()
    )
    if not chapters:
        return False
    chunks = build_rag_chunks(
        novel_id=checkpoint.novel_id,
        chapters=chapters,
        embedding_model=checkpoint.embedding_model,
        dimensions=checkpoint.embedding_dimensions,
    )
    if not chunks:
        return False
    return (
        rag_source_hash(
            chunks,
            embedding_model=checkpoint.embedding_model,
            dimensions=checkpoint.embedding_dimensions,
        )
        == checkpoint.source_hash
    )


# 主数据库是当前快照与可恢复构建的唯一事实来源，不能按文件修改时间猜测。
def cleanup_orphan_rag_collections(
    session_factory: Callable[[], Session],
    vector_store_path: str,
) -> int:
    if not Path(vector_store_path).expanduser().exists():
        return 0
    store = LocalVectorStore(vector_store_path)
    keep: set[str] = set()
    with session_factory() as db:
        keep.update(
            name
            for (name,) in db.query(RagIndex.collection_name).all()
        )
        stale: list[RagBuildCheckpoint] = []
        for checkpoint in db.query(RagBuildCheckpoint).all():
            if _checkpoint_still_valid(db, checkpoint):
                keep.add(checkpoint.collection_name)
            else:
                stale.append(checkpoint)
        for checkpoint in stale:
            db.delete(checkpoint)
        if stale:
            db.commit()
    removed = 0
    for name in store.list_collection_names():
        if (
            name.startswith(_MANAGED_COLLECTION_PREFIX)
            and name not in keep
        ):
            try:
                store.delete_collection(name)
            except Exception:
                logger.exception(
                    "Could not remove orphan RAG collection %s.",
                    name,
                )
            else:
                removed += 1
    return removed
