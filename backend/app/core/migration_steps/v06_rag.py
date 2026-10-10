from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from .validation import (
    _validate_index,
    _normalized_sql,
    _validate_exact_columns,
    _validate_named_unique,
    _validate_exact_cascade_foreign_key,
    _validate_exact_checks,
)

def _add_rag_index_tables(connection: Connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS rag_indices (
                id INTEGER NOT NULL PRIMARY KEY,
                novel_id INTEGER NOT NULL,
                source_hash VARCHAR(64) NOT NULL,
                collection_name VARCHAR(128) NOT NULL,
                embedding_provider VARCHAR(50) NOT NULL,
                embedding_model VARCHAR(100) NOT NULL,
                embedding_dimensions INTEGER NOT NULL,
                chunk_version VARCHAR(50) NOT NULL,
                chunk_count INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT uq_rag_indices_novel_id UNIQUE (novel_id),
                CONSTRAINT uq_rag_indices_collection_name UNIQUE (collection_name),
                CONSTRAINT ck_rag_indices_source_hash
                    CHECK (length(source_hash) = 64),
                CONSTRAINT ck_rag_indices_collection_name
                    CHECK (length(trim(collection_name)) > 0),
                CONSTRAINT ck_rag_indices_embedding_provider
                    CHECK (
                        length(trim(embedding_provider)) BETWEEN 1 AND 50
                    ),
                CONSTRAINT ck_rag_indices_embedding_model
                    CHECK (
                        length(trim(embedding_model)) BETWEEN 1 AND 100
                    ),
                CONSTRAINT ck_rag_indices_embedding_dimensions
                    CHECK (embedding_dimensions BETWEEN 1 AND 8192),
                CONSTRAINT ck_rag_indices_chunk_version
                    CHECK (length(trim(chunk_version)) > 0),
                CONSTRAINT ck_rag_indices_chunk_count_positive
                    CHECK (chunk_count > 0),
                FOREIGN KEY(novel_id) REFERENCES novels(id) ON DELETE CASCADE
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS rag_chunks (
                id INTEGER NOT NULL PRIMARY KEY,
                rag_index_id INTEGER NOT NULL,
                chapter_id INTEGER NOT NULL,
                chunk_id VARCHAR(70) NOT NULL,
                chapter_index INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                start_char INTEGER NOT NULL,
                end_char INTEGER NOT NULL,
                content_hash VARCHAR(64) NOT NULL,
                CONSTRAINT uq_rag_chunks_index_chunk_id UNIQUE (rag_index_id, chunk_id),
                CONSTRAINT uq_rag_chunks_index_chapter_chunk UNIQUE (rag_index_id, chapter_id, chunk_index),
                CONSTRAINT ck_rag_chunks_chunk_id
                    CHECK (length(chunk_id) = 70),
                CONSTRAINT ck_rag_chunks_indexes_positive
                    CHECK (
                        chapter_index >= 1
                        AND chunk_index >= 1
                    ),
                CONSTRAINT ck_rag_chunks_character_range
                    CHECK (
                        start_char >= 0
                        AND end_char > start_char
                    ),
                CONSTRAINT ck_rag_chunks_content_hash
                    CHECK (length(content_hash) = 64),
                FOREIGN KEY(rag_index_id) REFERENCES rag_indices(id) ON DELETE CASCADE,
                FOREIGN KEY(chapter_id) REFERENCES chapters(id) ON DELETE CASCADE
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_rag_chunks_index_chapter
            ON rag_chunks (
                rag_index_id,
                chapter_index,
                chunk_index
            )
            """
        )
    )

    _validate_exact_columns(
        connection,
        "rag_indices",
        {
            "id": ("INTEGER", False, 1, None),
            "novel_id": ("INTEGER", False, 0, None),
            "source_hash": ("VARCHAR(64)", False, 0, None),
            "collection_name": ("VARCHAR(128)", False, 0, None),
            "embedding_provider": ("VARCHAR(50)", False, 0, None),
            "embedding_model": ("VARCHAR(100)", False, 0, None),
            "embedding_dimensions": ("INTEGER", False, 0, None),
            "chunk_version": ("VARCHAR(50)", False, 0, None),
            "chunk_count": ("INTEGER", False, 0, None),
            "created_at": ("DATETIME", False, 0, None),
            "updated_at": ("DATETIME", False, 0, None),
        },
    )
    _validate_exact_columns(
        connection,
        "rag_chunks",
        {
            "id": ("INTEGER", False, 1, None),
            "rag_index_id": ("INTEGER", False, 0, None),
            "chapter_id": ("INTEGER", False, 0, None),
            "chunk_id": ("VARCHAR(70)", False, 0, None),
            "chapter_index": ("INTEGER", False, 0, None),
            "chunk_index": ("INTEGER", False, 0, None),
            "start_char": ("INTEGER", False, 0, None),
            "end_char": ("INTEGER", False, 0, None),
            "content_hash": ("VARCHAR(64)", False, 0, None),
        },
    )
    for table_name, constraint_name, columns in (
        (
            "rag_indices",
            "uq_rag_indices_novel_id",
            ["novel_id"],
        ),
        (
            "rag_indices",
            "uq_rag_indices_collection_name",
            ["collection_name"],
        ),
        (
            "rag_chunks",
            "uq_rag_chunks_index_chunk_id",
            ["rag_index_id", "chunk_id"],
        ),
        (
            "rag_chunks",
            "uq_rag_chunks_index_chapter_chunk",
            ["rag_index_id", "chapter_id", "chunk_index"],
        ),
    ):
        _validate_named_unique(
            connection,
            table_name,
            constraint_name,
            columns,
        )
    _validate_exact_cascade_foreign_key(
        connection,
        "rag_indices",
        ["novel_id"],
        "novels",
        ["id"],
    )
    _validate_exact_cascade_foreign_key(
        connection,
        "rag_chunks",
        ["rag_index_id"],
        "rag_indices",
        ["id"],
    )
    _validate_exact_cascade_foreign_key(
        connection,
        "rag_chunks",
        ["chapter_id"],
        "chapters",
        ["id"],
    )
    _validate_exact_checks(
        connection,
        "rag_indices",
        {
            "ck_rag_indices_source_hash": "length(source_hash) = 64",
            "ck_rag_indices_collection_name": (
                "length(trim(collection_name)) > 0"
            ),
            "ck_rag_indices_embedding_provider": (
                "length(trim(embedding_provider)) BETWEEN 1 AND 50"
            ),
            "ck_rag_indices_embedding_model": (
                "length(trim(embedding_model)) BETWEEN 1 AND 100"
            ),
            "ck_rag_indices_embedding_dimensions": (
                "embedding_dimensions BETWEEN 1 AND 8192"
            ),
            "ck_rag_indices_chunk_version": (
                "length(trim(chunk_version)) > 0"
            ),
            "ck_rag_indices_chunk_count_positive": "chunk_count > 0",
        },
    )
    _validate_exact_checks(
        connection,
        "rag_chunks",
        {
            "ck_rag_chunks_chunk_id": "length(chunk_id) = 70",
            "ck_rag_chunks_indexes_positive": (
                "chapter_index >= 1 AND chunk_index >= 1"
            ),
            "ck_rag_chunks_character_range": (
                "start_char >= 0 AND end_char > start_char"
            ),
            "ck_rag_chunks_content_hash": (
                "length(content_hash) = 64"
            ),
        },
    )
    _validate_index(
        connection,
        "rag_chunks",
        "ix_rag_chunks_index_chapter",
        ["rag_index_id", "chapter_index", "chunk_index"],
    )


def _add_rag_question_table(connection: Connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS rag_questions (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                novel_id INTEGER NOT NULL,
                index_source_hash VARCHAR(64) NOT NULL,
                question VARCHAR(2000) NOT NULL,
                top_k INTEGER NOT NULL,
                status VARCHAR(20) NOT NULL,
                answer VARCHAR(12000),
                insufficient_evidence BOOLEAN,
                citation_ids JSON NOT NULL,
                evidence JSON NOT NULL,
                generation_model VARCHAR(100) NOT NULL,
                prompt_version VARCHAR(50) NOT NULL,
                error_message VARCHAR(1000),
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                started_at DATETIME,
                completed_at DATETIME,
                CONSTRAINT ck_rag_questions_question_length
                    CHECK (
                        length(trim(question)) BETWEEN 1 AND 2000
                    ),
                CONSTRAINT ck_rag_questions_source_hash
                    CHECK (length(index_source_hash) = 64),
                CONSTRAINT ck_rag_questions_top_k
                    CHECK (top_k BETWEEN 3 AND 10),
                CONSTRAINT ck_rag_questions_status
                    CHECK (
                        status IN (
                            'queued',
                            'running',
                            'succeeded',
                            'failed'
                        )
                    ),
                CONSTRAINT ck_rag_questions_insufficient_boolean
                    CHECK (
                        insufficient_evidence IS NULL
                        OR insufficient_evidence IN (0, 1)
                    ),
                CONSTRAINT ck_rag_questions_citation_ids_array
                    CHECK (
                        json_valid(citation_ids)
                        AND json_type(citation_ids) = 'array'
                    ),
                CONSTRAINT ck_rag_questions_evidence_array
                    CHECK (
                        json_valid(evidence)
                        AND json_type(evidence) = 'array'
                    ),
                CONSTRAINT ck_rag_questions_generation_model
                    CHECK (length(trim(generation_model)) > 0),
                CONSTRAINT ck_rag_questions_prompt_version
                    CHECK (length(trim(prompt_version)) > 0),
                FOREIGN KEY(novel_id) REFERENCES novels(id) ON DELETE CASCADE
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_rag_questions_novel_created
            ON rag_questions (novel_id, created_at)
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                ux_rag_questions_active_novel
            ON rag_questions (novel_id)
            WHERE status IN ('queued', 'running')
            """
        )
    )
    _validate_exact_columns(
        connection,
        "rag_questions",
        {
            "id": ("VARCHAR(36)", False, 1, None),
            "novel_id": ("INTEGER", False, 0, None),
            "index_source_hash": ("VARCHAR(64)", False, 0, None),
            "question": ("VARCHAR(2000)", False, 0, None),
            "top_k": ("INTEGER", False, 0, None),
            "status": ("VARCHAR(20)", False, 0, None),
            "answer": ("VARCHAR(12000)", True, 0, None),
            "insufficient_evidence": ("BOOLEAN", True, 0, None),
            "citation_ids": ("JSON", False, 0, None),
            "evidence": ("JSON", False, 0, None),
            "generation_model": ("VARCHAR(100)", False, 0, None),
            "prompt_version": ("VARCHAR(50)", False, 0, None),
            "error_message": ("VARCHAR(1000)", True, 0, None),
            "created_at": ("DATETIME", False, 0, None),
            "updated_at": ("DATETIME", False, 0, None),
            "started_at": ("DATETIME", True, 0, None),
            "completed_at": ("DATETIME", True, 0, None),
        },
    )
    _validate_exact_cascade_foreign_key(
        connection,
        "rag_questions",
        ["novel_id"],
        "novels",
        ["id"],
    )
    _validate_exact_checks(
        connection,
        "rag_questions",
        {
            "ck_rag_questions_question_length": (
                "length(trim(question)) BETWEEN 1 AND 2000"
            ),
            "ck_rag_questions_source_hash": (
                "length(index_source_hash) = 64"
            ),
            "ck_rag_questions_top_k": "top_k BETWEEN 3 AND 10",
            "ck_rag_questions_status": (
                "status IN ('queued', 'running', 'succeeded', 'failed')"
            ),
            "ck_rag_questions_insufficient_boolean": (
                "insufficient_evidence IS NULL "
                "OR insufficient_evidence IN (0, 1)"
            ),
            "ck_rag_questions_citation_ids_array": (
                "json_valid(citation_ids) "
                "AND json_type(citation_ids) = 'array'"
            ),
            "ck_rag_questions_evidence_array": (
                "json_valid(evidence) "
                "AND json_type(evidence) = 'array'"
            ),
            "ck_rag_questions_generation_model": (
                "length(trim(generation_model)) > 0"
            ),
            "ck_rag_questions_prompt_version": (
                "length(trim(prompt_version)) > 0"
            ),
        },
    )
    _validate_index(
        connection,
        "rag_questions",
        "ix_rag_questions_novel_created",
        ["novel_id", "created_at"],
    )
    active_indexes = [
        item
        for item in inspect(connection).get_indexes("rag_questions")
        if item.get("name") == "ux_rag_questions_active_novel"
    ]
    if (
        len(active_indexes) != 1
        or not bool(active_indexes[0].get("unique"))
        or active_indexes[0].get("column_names") != ["novel_id"]
        or _normalized_sql(
            active_indexes[0]
            .get("dialect_options", {})
            .get("sqlite_where")
        )
        != _normalized_sql("status IN ('queued', 'running')")
    ):
        raise RuntimeError(
            "数据库索引 ux_rag_questions_active_novel 的定义不正确，"
            "请先备份并修复数据库结构。"
        )


def _allow_configurable_embedding_models(connection: Connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE rag_indices_v9 (
                id INTEGER NOT NULL PRIMARY KEY,
                novel_id INTEGER NOT NULL,
                source_hash VARCHAR(64) NOT NULL,
                collection_name VARCHAR(128) NOT NULL,
                embedding_provider VARCHAR(50) NOT NULL,
                embedding_model VARCHAR(100) NOT NULL,
                embedding_dimensions INTEGER NOT NULL,
                chunk_version VARCHAR(50) NOT NULL,
                chunk_count INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT ck_rag_indices_source_hash
                    CHECK (length(source_hash) = 64),
                CONSTRAINT ck_rag_indices_collection_name
                    CHECK (length(trim(collection_name)) > 0),
                CONSTRAINT ck_rag_indices_embedding_provider
                    CHECK (
                        length(trim(embedding_provider)) BETWEEN 1 AND 50
                    ),
                CONSTRAINT ck_rag_indices_embedding_model
                    CHECK (
                        length(trim(embedding_model)) BETWEEN 1 AND 100
                    ),
                CONSTRAINT ck_rag_indices_embedding_dimensions
                    CHECK (embedding_dimensions BETWEEN 1 AND 8192),
                CONSTRAINT ck_rag_indices_chunk_version
                    CHECK (length(trim(chunk_version)) > 0),
                CONSTRAINT ck_rag_indices_chunk_count_positive
                    CHECK (chunk_count > 0),
                FOREIGN KEY(novel_id) REFERENCES novels(id) ON DELETE CASCADE
            )
            """
        )
    )
    connection.execute(
        text(
            """
            INSERT INTO rag_indices_v9 (
                id, novel_id, source_hash, collection_name,
                embedding_provider, embedding_model,
                embedding_dimensions, chunk_version, chunk_count,
                created_at, updated_at
            )
            SELECT
                id, novel_id, source_hash, collection_name,
                embedding_provider, embedding_model,
                embedding_dimensions, chunk_version, chunk_count,
                created_at, updated_at
            FROM rag_indices
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE rag_chunks_v9 (
                id INTEGER NOT NULL PRIMARY KEY,
                rag_index_id INTEGER NOT NULL,
                chapter_id INTEGER NOT NULL,
                chunk_id VARCHAR(70) NOT NULL,
                chapter_index INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                start_char INTEGER NOT NULL,
                end_char INTEGER NOT NULL,
                content_hash VARCHAR(64) NOT NULL,
                CONSTRAINT ck_rag_chunks_chunk_id
                    CHECK (length(chunk_id) = 70),
                CONSTRAINT ck_rag_chunks_indexes_positive
                    CHECK (chapter_index >= 1 AND chunk_index >= 1),
                CONSTRAINT ck_rag_chunks_character_range
                    CHECK (start_char >= 0 AND end_char > start_char),
                CONSTRAINT ck_rag_chunks_content_hash
                    CHECK (length(content_hash) = 64),
                FOREIGN KEY(rag_index_id)
                    REFERENCES rag_indices_v9(id) ON DELETE CASCADE,
                FOREIGN KEY(chapter_id)
                    REFERENCES chapters(id) ON DELETE CASCADE
            )
            """
        )
    )
    connection.execute(
        text(
            """
            INSERT INTO rag_chunks_v9 (
                id, rag_index_id, chapter_id, chunk_id,
                chapter_index, chunk_index, start_char, end_char,
                content_hash
            )
            SELECT
                id, rag_index_id, chapter_id, chunk_id,
                chapter_index, chunk_index, start_char, end_char,
                content_hash
            FROM rag_chunks
            """
        )
    )
    connection.execute(text("DROP TABLE rag_chunks"))
    connection.execute(text("DROP TABLE rag_indices"))
    connection.execute(
        text("ALTER TABLE rag_indices_v9 RENAME TO rag_indices")
    )
    connection.execute(
        text(
            """
            CREATE TABLE rag_chunks (
                id INTEGER NOT NULL PRIMARY KEY,
                rag_index_id INTEGER NOT NULL,
                chapter_id INTEGER NOT NULL,
                chunk_id VARCHAR(70) NOT NULL,
                chapter_index INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                start_char INTEGER NOT NULL,
                end_char INTEGER NOT NULL,
                content_hash VARCHAR(64) NOT NULL,
                CONSTRAINT ck_rag_chunks_chunk_id
                    CHECK (length(chunk_id) = 70),
                CONSTRAINT ck_rag_chunks_indexes_positive
                    CHECK (chapter_index >= 1 AND chunk_index >= 1),
                CONSTRAINT ck_rag_chunks_character_range
                    CHECK (start_char >= 0 AND end_char > start_char),
                CONSTRAINT ck_rag_chunks_content_hash
                    CHECK (length(content_hash) = 64),
                FOREIGN KEY(rag_index_id) REFERENCES rag_indices(id) ON DELETE CASCADE,
                FOREIGN KEY(chapter_id) REFERENCES chapters(id) ON DELETE CASCADE
            )
            """
        )
    )
    connection.execute(
        text(
            """
            INSERT INTO rag_chunks (
                id, rag_index_id, chapter_id, chunk_id,
                chapter_index, chunk_index, start_char, end_char,
                content_hash
            )
            SELECT
                id, rag_index_id, chapter_id, chunk_id,
                chapter_index, chunk_index, start_char, end_char,
                content_hash
            FROM rag_chunks_v9
            """
        )
    )
    connection.execute(text("DROP TABLE rag_chunks_v9"))
    connection.execute(
        text(
            """
            CREATE UNIQUE INDEX uq_rag_indices_novel_id
            ON rag_indices (novel_id)
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE UNIQUE INDEX uq_rag_indices_collection_name
            ON rag_indices (collection_name)
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE UNIQUE INDEX uq_rag_chunks_index_chunk_id
            ON rag_chunks (rag_index_id, chunk_id)
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE UNIQUE INDEX uq_rag_chunks_index_chapter_chunk
            ON rag_chunks (rag_index_id, chapter_id, chunk_index)
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE INDEX ix_rag_chunks_index_chapter
            ON rag_chunks (
                rag_index_id,
                chapter_index,
                chunk_index
            )
            """
        )
    )
    _add_rag_index_tables(connection)
