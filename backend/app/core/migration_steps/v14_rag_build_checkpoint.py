"""Add thin RAG build checkpoint table for resume-after-transient-failure."""

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .validation import (
    _validate_exact_cascade_foreign_key,
    _validate_exact_checks,
    _validate_exact_columns,
    _validate_named_unique,
)


def add_rag_build_checkpoint_table(connection: Connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS rag_build_checkpoints (
                id INTEGER NOT NULL PRIMARY KEY,
                novel_id INTEGER NOT NULL,
                source_hash VARCHAR(64) NOT NULL,
                collection_name VARCHAR(128) NOT NULL,
                embedding_provider VARCHAR(50) NOT NULL,
                embedding_model VARCHAR(100) NOT NULL,
                embedding_dimensions INTEGER NOT NULL,
                chunk_version VARCHAR(50) NOT NULL,
                CONSTRAINT uq_rag_build_checkpoints_novel_id UNIQUE (novel_id),
                CONSTRAINT ck_rag_build_checkpoints_source_hash
                    CHECK (length(source_hash) = 64),
                CONSTRAINT ck_rag_build_checkpoints_collection_name
                    CHECK (length(trim(collection_name)) > 0),
                CONSTRAINT ck_rag_build_checkpoints_embedding_provider
                    CHECK (
                        length(trim(embedding_provider)) BETWEEN 1 AND 50
                    ),
                CONSTRAINT ck_rag_build_checkpoints_embedding_model
                    CHECK (
                        length(trim(embedding_model)) BETWEEN 1 AND 100
                    ),
                CONSTRAINT ck_rag_build_checkpoints_embedding_dimensions
                    CHECK (embedding_dimensions BETWEEN 1 AND 8192),
                CONSTRAINT ck_rag_build_checkpoints_chunk_version
                    CHECK (length(trim(chunk_version)) > 0),
                FOREIGN KEY(novel_id) REFERENCES novels(id) ON DELETE CASCADE
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
            uq_rag_build_checkpoints_collection_name
            ON rag_build_checkpoints (collection_name)
            """
        )
    )
    _validate_exact_columns(
        connection,
        "rag_build_checkpoints",
        {
            "id": ("INTEGER", False, 1, None),
            "novel_id": ("INTEGER", False, 0, None),
            "source_hash": ("VARCHAR(64)", False, 0, None),
            "collection_name": ("VARCHAR(128)", False, 0, None),
            "embedding_provider": ("VARCHAR(50)", False, 0, None),
            "embedding_model": ("VARCHAR(100)", False, 0, None),
            "embedding_dimensions": ("INTEGER", False, 0, None),
            "chunk_version": ("VARCHAR(50)", False, 0, None),
        },
    )
    _validate_named_unique(
        connection,
        "rag_build_checkpoints",
        "uq_rag_build_checkpoints_novel_id",
        ["novel_id"],
    )
    _validate_named_unique(
        connection,
        "rag_build_checkpoints",
        "uq_rag_build_checkpoints_collection_name",
        ["collection_name"],
    )
    _validate_exact_cascade_foreign_key(
        connection,
        "rag_build_checkpoints",
        ["novel_id"],
        "novels",
        ["id"],
    )
    _validate_exact_checks(
        connection,
        "rag_build_checkpoints",
        {
            "ck_rag_build_checkpoints_source_hash": (
                "length(source_hash) = 64"
            ),
            "ck_rag_build_checkpoints_collection_name": (
                "length(trim(collection_name)) > 0"
            ),
            "ck_rag_build_checkpoints_embedding_provider": (
                "length(trim(embedding_provider)) BETWEEN 1 AND 50"
            ),
            "ck_rag_build_checkpoints_embedding_model": (
                "length(trim(embedding_model)) BETWEEN 1 AND 100"
            ),
            "ck_rag_build_checkpoints_embedding_dimensions": (
                "embedding_dimensions BETWEEN 1 AND 8192"
            ),
            "ck_rag_build_checkpoints_chunk_version": (
                "length(trim(chunk_version)) > 0"
            ),
        },
    )
