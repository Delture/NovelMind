"""v05_characters：保留历史数据升级和约束检查。"""
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from .validation import (
    _has_unique_columns,
    _validate_index,
    _validate_exact_columns,
    _validate_named_unique,
    _ensure_named_unique_index,
    _validate_exact_cascade_foreign_key,
    _validate_exact_checks,
)

# 仅为旧版本升级保留的历史结构；v11 移除触发器。
CHARACTER_APPEARANCE_OWNERSHIP_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_character_appearances_fill_ownership
AFTER INSERT ON character_appearances
WHEN NEW.analysis_id = 0 OR NEW.novel_id = 0
BEGIN
    UPDATE character_appearances
    SET
        analysis_id = CASE
            WHEN NEW.analysis_id = 0 THEN (
                SELECT characters.analysis_id
                FROM characters
                WHERE characters.id = NEW.character_id
            )
            ELSE NEW.analysis_id
        END,
        novel_id = CASE
            WHEN NEW.novel_id = 0 THEN (
                SELECT character_analyses.novel_id
                FROM characters
                JOIN character_analyses
                  ON character_analyses.id = characters.analysis_id
                WHERE characters.id = NEW.character_id
            )
            ELSE NEW.novel_id
        END
    WHERE id = NEW.id;
END
"""


def _add_character_analysis_tables(connection: Connection) -> None:
    """Create and strictly validate the character snapshot schema."""

    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS chapter_character_extractions (
                id INTEGER NOT NULL PRIMARY KEY,
                chapter_id INTEGER NOT NULL,
                extraction_data JSON NOT NULL,
                source_hash VARCHAR(64) NOT NULL,
                model VARCHAR(100) NOT NULL,
                prompt_version VARCHAR(50) NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT uq_chapter_character_extractions_chapter_id UNIQUE (chapter_id),
                CONSTRAINT ck_chapter_character_extractions_data_object
                    CHECK (
                        json_valid(extraction_data)
                        AND json_type(extraction_data) = 'object'
                    ),
                CONSTRAINT ck_chapter_character_extractions_source_hash
                    CHECK (
                        length(source_hash) = 64
                        AND source_hash NOT GLOB '*[^0-9a-f]*'
                    ),
                CONSTRAINT ck_chapter_character_extractions_model
                    CHECK (length(trim(model)) BETWEEN 1 AND 100),
                CONSTRAINT ck_chapter_character_extractions_prompt_version
                    CHECK (
                        length(trim(prompt_version)) BETWEEN 1 AND 50
                    ),
                FOREIGN KEY(chapter_id) REFERENCES chapters(id) ON DELETE CASCADE
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS character_analyses (
                id INTEGER NOT NULL PRIMARY KEY,
                novel_id INTEGER NOT NULL,
                source_hash VARCHAR(64) NOT NULL,
                model VARCHAR(100) NOT NULL,
                extraction_prompt_version VARCHAR(50) NOT NULL,
                merge_prompt_version VARCHAR(50) NOT NULL,
                relationship_prompt_version VARCHAR(50) NOT NULL,
                chapter_count INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT uq_character_analyses_novel_id UNIQUE (novel_id),
                CONSTRAINT uq_character_analyses_id_novel_id UNIQUE (id, novel_id),
                CONSTRAINT ck_character_analyses_source_hash
                    CHECK (
                        length(source_hash) = 64
                        AND source_hash NOT GLOB '*[^0-9a-f]*'
                    ),
                CONSTRAINT ck_character_analyses_model
                    CHECK (length(trim(model)) BETWEEN 1 AND 100),
                CONSTRAINT ck_character_analyses_extraction_prompt_version
                    CHECK (
                        length(trim(extraction_prompt_version))
                        BETWEEN 1 AND 50
                    ),
                CONSTRAINT ck_character_analyses_merge_prompt_version
                    CHECK (
                        length(trim(merge_prompt_version))
                        BETWEEN 1 AND 50
                    ),
                CONSTRAINT ck_character_analyses_relationship_prompt_version
                    CHECK (
                        length(trim(relationship_prompt_version))
                        BETWEEN 1 AND 50
                    ),
                CONSTRAINT ck_character_analyses_chapter_count_nonnegative
                    CHECK (chapter_count >= 0),
                FOREIGN KEY(novel_id) REFERENCES novels(id) ON DELETE CASCADE
            )
            """
        )
    )
    _ensure_named_unique_index(
        connection,
        "chapters",
        "uq_chapters_novel_id_id",
        ["novel_id", "id"],
    )
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS characters (
                id INTEGER NOT NULL PRIMARY KEY,
                analysis_id INTEGER NOT NULL,
                canonical_name VARCHAR(255) NOT NULL,
                aliases JSON NOT NULL,
                short_description TEXT NOT NULL,
                first_chapter_index INTEGER NOT NULL,
                last_chapter_index INTEGER NOT NULL,
                chapter_count INTEGER NOT NULL,
                mention_count INTEGER NOT NULL,
                CONSTRAINT uq_characters_analysis_id_id UNIQUE (analysis_id, id),
                CONSTRAINT ck_characters_canonical_name
                    CHECK (
                        length(trim(canonical_name)) BETWEEN 1 AND 255
                    ),
                CONSTRAINT ck_characters_aliases_array
                    CHECK (
                        json_valid(aliases)
                        AND json_type(aliases) = 'array'
                    ),
                CONSTRAINT ck_characters_short_description
                    CHECK (length(trim(short_description)) > 0),
                CONSTRAINT ck_characters_chapter_range
                    CHECK (
                        first_chapter_index >= 1
                        AND last_chapter_index >= first_chapter_index
                    ),
                CONSTRAINT ck_characters_chapter_count_positive
                    CHECK (chapter_count > 0),
                CONSTRAINT ck_characters_chapter_count_within_range
                    CHECK (
                        chapter_count <= (
                            last_chapter_index
                            - first_chapter_index
                            + 1
                        )
                    ),
                CONSTRAINT ck_characters_mention_count_valid
                    CHECK (mention_count >= chapter_count),
                FOREIGN KEY(analysis_id) REFERENCES character_analyses(id) ON DELETE CASCADE
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS character_appearances (
                id INTEGER NOT NULL PRIMARY KEY,
                analysis_id INTEGER NOT NULL DEFAULT 0,
                novel_id INTEGER NOT NULL DEFAULT 0,
                character_id INTEGER NOT NULL,
                chapter_id INTEGER NOT NULL,
                mention_count INTEGER NOT NULL,
                summary TEXT NOT NULL,
                CONSTRAINT uq_character_appearances_character_chapter UNIQUE (character_id, chapter_id),
                CONSTRAINT ck_character_appearances_mention_count_positive
                    CHECK (mention_count > 0),
                CONSTRAINT ck_character_appearances_summary
                    CHECK (length(trim(summary)) > 0),
                CONSTRAINT fk_character_appearances_analysis_novel FOREIGN KEY(analysis_id, novel_id) REFERENCES character_analyses(id, novel_id) ON DELETE CASCADE,
                CONSTRAINT fk_character_appearances_character FOREIGN KEY(analysis_id, character_id) REFERENCES characters(analysis_id, id) ON DELETE CASCADE,
                CONSTRAINT fk_character_appearances_chapter FOREIGN KEY(novel_id, chapter_id) REFERENCES chapters(novel_id, id) ON DELETE CASCADE
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS character_relationships (
                id INTEGER NOT NULL PRIMARY KEY,
                analysis_id INTEGER NOT NULL,
                source_character_id INTEGER NOT NULL,
                target_character_id INTEGER NOT NULL,
                directed BOOLEAN NOT NULL DEFAULT 0,
                relation_type VARCHAR(100) NOT NULL,
                description TEXT NOT NULL,
                evidence_chapter_indices JSON NOT NULL,
                CONSTRAINT fk_character_relationships_analysis FOREIGN KEY(analysis_id) REFERENCES character_analyses(id) ON DELETE CASCADE,
                CONSTRAINT fk_character_relationships_source FOREIGN KEY(analysis_id, source_character_id) REFERENCES characters(analysis_id, id) ON DELETE CASCADE,
                CONSTRAINT fk_character_relationships_target FOREIGN KEY(analysis_id, target_character_id) REFERENCES characters(analysis_id, id) ON DELETE CASCADE,
                CONSTRAINT uq_character_relationships_snapshot_edge UNIQUE (analysis_id, source_character_id, target_character_id, directed, relation_type),
                CONSTRAINT ck_character_relationships_directed_boolean
                    CHECK (directed IN (0, 1)),
                CONSTRAINT ck_character_relationships_no_self
                    CHECK (source_character_id <> target_character_id),
                CONSTRAINT ck_character_relationships_undirected_canonical
                    CHECK (
                        directed = 1
                        OR source_character_id < target_character_id
                    ),
                CONSTRAINT ck_character_relationships_relation_type
                    CHECK (
                        length(trim(relation_type)) BETWEEN 1 AND 100
                    ),
                CONSTRAINT ck_character_relationships_description
                    CHECK (length(trim(description)) > 0),
                CONSTRAINT ck_character_relationships_evidence_nonempty
                    CHECK (
                        json_valid(evidence_chapter_indices)
                        AND json_type(evidence_chapter_indices) = 'array'
                        AND json_array_length(
                            evidence_chapter_indices
                        ) > 0
                    )
            )
            """
        )
    )

    for sqltext in (
        """
        CREATE INDEX IF NOT EXISTS ix_characters_analysis_chapter_count
        ON characters (analysis_id, chapter_count)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_characters_analysis_canonical_name
        ON characters (analysis_id, canonical_name)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_characters_analysis_mention_count
        ON characters (analysis_id, mention_count)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_character_appearances_chapter_id
        ON character_appearances (chapter_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_character_appearances_analysis_id
        ON character_appearances (analysis_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS
            ix_character_relationships_analysis_target
        ON character_relationships (analysis_id, target_character_id)
        """,
    ):
        connection.execute(text(sqltext))
    upgraded = ("schema_migrations" in inspect(connection).get_table_names()
                and connection.execute(text("SELECT 1 FROM schema_migrations WHERE version = 11")).first())
    if not upgraded:
        connection.execute(text(CHARACTER_APPEARANCE_OWNERSHIP_TRIGGER_SQL))

    _validate_exact_columns(
        connection,
        "chapter_character_extractions",
        {
            "id": ("INTEGER", False, 1, None),
            "chapter_id": ("INTEGER", False, 0, None),
            "extraction_data": ("JSON", False, 0, None),
            "source_hash": ("VARCHAR(64)", False, 0, None),
            "model": ("VARCHAR(100)", False, 0, None),
            "prompt_version": ("VARCHAR(50)", False, 0, None),
            "created_at": ("DATETIME", False, 0, None),
            "updated_at": ("DATETIME", False, 0, None),
        },
    )
    _validate_exact_columns(
        connection,
        "character_analyses",
        {
            "id": ("INTEGER", False, 1, None),
            "novel_id": ("INTEGER", False, 0, None),
            "source_hash": ("VARCHAR(64)", False, 0, None),
            "model": ("VARCHAR(100)", False, 0, None),
            "extraction_prompt_version": (
                "VARCHAR(50)",
                False,
                0,
                None,
            ),
            "merge_prompt_version": ("VARCHAR(50)", False, 0, None),
            "relationship_prompt_version": (
                "VARCHAR(50)",
                False,
                0,
                None,
            ),
            "chapter_count": ("INTEGER", False, 0, None),
            "created_at": ("DATETIME", False, 0, None),
            "updated_at": ("DATETIME", False, 0, None),
        },
    )
    _validate_exact_columns(
        connection,
        "characters",
        {
            "id": ("INTEGER", False, 1, None),
            "analysis_id": ("INTEGER", False, 0, None),
            "canonical_name": ("VARCHAR(255)", False, 0, None),
            "aliases": ("JSON", False, 0, None),
            "short_description": ("TEXT", False, 0, None),
            "first_chapter_index": ("INTEGER", False, 0, None),
            "last_chapter_index": ("INTEGER", False, 0, None),
            "chapter_count": ("INTEGER", False, 0, None),
            "mention_count": ("INTEGER", False, 0, None),
        },
    )
    _validate_exact_columns(
        connection,
        "character_appearances",
        {
            "id": ("INTEGER", False, 1, None),
            "analysis_id": ("INTEGER", False, 0, "0"),
            "novel_id": ("INTEGER", False, 0, "0"),
            "character_id": ("INTEGER", False, 0, None),
            "chapter_id": ("INTEGER", False, 0, None),
            "mention_count": ("INTEGER", False, 0, None),
            "summary": ("TEXT", False, 0, None),
        },
    )
    _validate_exact_columns(
        connection,
        "character_relationships",
        {
            "id": ("INTEGER", False, 1, None),
            "analysis_id": ("INTEGER", False, 0, None),
            "source_character_id": ("INTEGER", False, 0, None),
            "target_character_id": ("INTEGER", False, 0, None),
            "directed": ("BOOLEAN", False, 0, "0"),
            "relation_type": ("VARCHAR(100)", False, 0, None),
            "description": ("TEXT", False, 0, None),
            "evidence_chapter_indices": ("JSON", False, 0, None),
        },
    )

    for table_name, name, columns in (
        (
            "chapter_character_extractions",
            "uq_chapter_character_extractions_chapter_id",
            ["chapter_id"],
        ),
        (
            "character_analyses",
            "uq_character_analyses_novel_id",
            ["novel_id"],
        ),
        (
            "character_analyses",
            "uq_character_analyses_id_novel_id",
            ["id", "novel_id"],
        ),
        (
            "characters",
            "uq_characters_analysis_id_id",
            ["analysis_id", "id"],
        ),
        (
            "character_appearances",
            "uq_character_appearances_character_chapter",
            ["character_id", "chapter_id"],
        ),
        (
            "character_relationships",
            "uq_character_relationships_snapshot_edge",
            [
                "analysis_id",
                "source_character_id",
                "target_character_id",
                "directed",
                "relation_type",
            ],
        ),
    ):
        _validate_named_unique(connection, table_name, name, columns)

    if _has_unique_columns(
        connection,
        "characters",
        ["analysis_id", "canonical_name"],
    ):
        raise RuntimeError(
            "数据库表 characters 不应强制同一分析快照内人物名称唯一，"
            "请先备份并修复数据库结构。"
        )

    for table_name, local, remote_table, remote in (
        (
            "chapter_character_extractions",
            ["chapter_id"],
            "chapters",
            ["id"],
        ),
        (
            "character_analyses",
            ["novel_id"],
            "novels",
            ["id"],
        ),
        (
            "characters",
            ["analysis_id"],
            "character_analyses",
            ["id"],
        ),
        (
            "character_appearances",
            ["analysis_id", "novel_id"],
            "character_analyses",
            ["id", "novel_id"],
        ),
        (
            "character_appearances",
            ["analysis_id", "character_id"],
            "characters",
            ["analysis_id", "id"],
        ),
        (
            "character_appearances",
            ["novel_id", "chapter_id"],
            "chapters",
            ["novel_id", "id"],
        ),
        (
            "character_relationships",
            ["analysis_id"],
            "character_analyses",
            ["id"],
        ),
        (
            "character_relationships",
            ["analysis_id", "source_character_id"],
            "characters",
            ["analysis_id", "id"],
        ),
        (
            "character_relationships",
            ["analysis_id", "target_character_id"],
            "characters",
            ["analysis_id", "id"],
        ),
    ):
        _validate_exact_cascade_foreign_key(
            connection,
            table_name,
            local,
            remote_table,
            remote,
        )

    _validate_exact_checks(
        connection,
        "chapter_character_extractions",
        {
            "ck_chapter_character_extractions_data_object": (
                "json_valid(extraction_data) "
                "AND json_type(extraction_data) = 'object'"
            ),
            "ck_chapter_character_extractions_source_hash": (
                "length(source_hash) = 64 "
                "AND source_hash NOT GLOB '*[^0-9a-f]*'"
            ),
            "ck_chapter_character_extractions_model": (
                "length(trim(model)) BETWEEN 1 AND 100"
            ),
            "ck_chapter_character_extractions_prompt_version": (
                "length(trim(prompt_version)) BETWEEN 1 AND 50"
            ),
        },
    )
    _validate_exact_checks(
        connection,
        "character_analyses",
        {
            "ck_character_analyses_source_hash": (
                "length(source_hash) = 64 "
                "AND source_hash NOT GLOB '*[^0-9a-f]*'"
            ),
            "ck_character_analyses_model": (
                "length(trim(model)) BETWEEN 1 AND 100"
            ),
            "ck_character_analyses_extraction_prompt_version": (
                "length(trim(extraction_prompt_version)) BETWEEN 1 AND 50"
            ),
            "ck_character_analyses_merge_prompt_version": (
                "length(trim(merge_prompt_version)) BETWEEN 1 AND 50"
            ),
            "ck_character_analyses_relationship_prompt_version": (
                "length(trim(relationship_prompt_version)) "
                "BETWEEN 1 AND 50"
            ),
            "ck_character_analyses_chapter_count_nonnegative": (
                "chapter_count >= 0"
            ),
        },
    )
    _validate_exact_checks(
        connection,
        "characters",
        {
            "ck_characters_canonical_name": (
                "length(trim(canonical_name)) BETWEEN 1 AND 255"
            ),
            "ck_characters_aliases_array": (
                "json_valid(aliases) AND json_type(aliases) = 'array'"
            ),
            "ck_characters_short_description": (
                "length(trim(short_description)) > 0"
            ),
            "ck_characters_chapter_range": (
                "first_chapter_index >= 1 "
                "AND last_chapter_index >= first_chapter_index"
            ),
            "ck_characters_chapter_count_positive": "chapter_count > 0",
            "ck_characters_chapter_count_within_range": (
                "chapter_count <= "
                "(last_chapter_index - first_chapter_index + 1)"
            ),
            "ck_characters_mention_count_valid": (
                "mention_count >= chapter_count"
            ),
        },
    )
    _validate_exact_checks(
        connection,
        "character_appearances",
        {
            "ck_character_appearances_mention_count_positive": (
                "mention_count > 0"
            ),
            "ck_character_appearances_summary": (
                "length(trim(summary)) > 0"
            ),
        },
    )
    _validate_exact_checks(
        connection,
        "character_relationships",
        {
            "ck_character_relationships_directed_boolean": (
                "directed IN (0, 1)"
            ),
            "ck_character_relationships_no_self": (
                "source_character_id <> target_character_id"
            ),
            "ck_character_relationships_undirected_canonical": (
                "directed = 1 "
                "OR source_character_id < target_character_id"
            ),
            "ck_character_relationships_relation_type": (
                "length(trim(relation_type)) BETWEEN 1 AND 100"
            ),
            "ck_character_relationships_description": (
                "length(trim(description)) > 0"
            ),
            "ck_character_relationships_evidence_nonempty": (
                "json_valid(evidence_chapter_indices) "
                "AND json_type(evidence_chapter_indices) = 'array' "
                "AND json_array_length(evidence_chapter_indices) > 0"
            ),
        },
    )

    for table_name, index_name, columns in (
        (
            "characters",
            "ix_characters_analysis_chapter_count",
            ["analysis_id", "chapter_count"],
        ),
        (
            "characters",
            "ix_characters_analysis_canonical_name",
            ["analysis_id", "canonical_name"],
        ),
        (
            "characters",
            "ix_characters_analysis_mention_count",
            ["analysis_id", "mention_count"],
        ),
        (
            "character_appearances",
            "ix_character_appearances_chapter_id",
            ["chapter_id"],
        ),
        (
            "character_appearances",
            "ix_character_appearances_analysis_id",
            ["analysis_id"],
        ),
        (
            "character_relationships",
            "ix_character_relationships_analysis_target",
            ["analysis_id", "target_character_id"],
        ),
    ):
        _validate_index(
            connection,
            table_name,
            index_name,
            columns,
        )
