"""Best-effort cleanup for local derived RAG vector collections."""

import logging
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session

from backend.app.models.rag import RagIndex
from backend.app.rag.vector_store import ChromaVectorStore


logger = logging.getLogger(__name__)
_MANAGED_COLLECTION_PREFIX = "novelmind_n"


def cleanup_orphan_rag_collections(
    session_factory: Callable[[], Session],
    chroma_path: str,
) -> int:
    """Delete managed collections not referenced by current SQLite state."""
    if not Path(chroma_path).expanduser().exists():
        return 0
    store = ChromaVectorStore(chroma_path)
    with session_factory() as db:
        active = {
            name
            for (name,) in db.query(RagIndex.collection_name).all()
        }
    removed = 0
    for name in store.list_collection_names():
        if (
            name.startswith(_MANAGED_COLLECTION_PREFIX)
            and name not in active
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
