"""Small, ordered SQLite schema migrations for local NovelMind databases."""

from typing import Callable

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine


MigrationFunction = Callable[[Connection], None]


def _has_columns(
    connection: Connection,
    table_name: str,
    expected_columns: set[str],
) -> None:
    """Raise a readable error when an existing table is incomplete."""
    actual_columns = {
        column["name"]
        for column in inspect(connection).get_columns(table_name)
    }
    missing_columns = expected_columns - actual_columns
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise RuntimeError(
            f"数据库表 {table_name} 缺少字段：{missing}。"
            "请先备份并修复数据库结构。"
        )


def _has_cascade_foreign_key(
    connection: Connection,
    table_name: str,
    local_column: str,
    referred_table: str,
) -> bool:
    """Return whether an expected cascading foreign key exists."""
    for foreign_key in inspect(connection).get_foreign_keys(table_name):
        ondelete = str(
            foreign_key.get("options", {}).get("ondelete", "")
        ).upper()
        if (
            foreign_key.get("constrained_columns") == [local_column]
            and foreign_key.get("referred_table") == referred_table
            and ondelete == "CASCADE"
        ):
            return True
    return False


def _has_unique_columns(
    connection: Connection,
    table_name: str,
    expected_columns: list[str],
) -> bool:
    """Return whether a table enforces uniqueness for exact columns."""
    inspector = inspect(connection)
    candidates = (
        inspector.get_unique_constraints(table_name)
        + inspector.get_indexes(table_name)
    )
    return any(
        item.get("column_names") == expected_columns
        and (
            item in inspector.get_unique_constraints(table_name)
            or bool(item.get("unique"))
        )
        for item in candidates
    )


def _validate_index(
    connection: Connection,
    table_name: str,
    index_name: str,
    expected_columns: list[str],
) -> None:
    """Validate a named non-unique lookup index."""
    matching = [
        item
        for item in inspect(connection).get_indexes(table_name)
        if item.get("name") == index_name
    ]
    if (
        len(matching) != 1
        or matching[0].get("column_names") != expected_columns
        or bool(matching[0].get("unique"))
    ):
        raise RuntimeError(
            f"数据库索引 {index_name} 的定义不正确，"
            "请先备份并修复数据库结构。"
        )


def _normalized_sql(sqltext: object) -> str:
    """Normalize a reflected SQL expression for strict comparison."""
    return (
        "".join(str(sqltext).casefold().split())
        .replace('"', "")
        .replace("`", "")
    )


def _validate_active_job_index(
    connection: Connection,
) -> bool:
    """Validate the partial unique index, returning whether it exists."""
    index_name = "ux_analysis_jobs_active_novel_type"
    matching = [
        item
        for item in inspect(connection).get_indexes("analysis_jobs")
        if item.get("name") == index_name
    ]
    if not matching:
        return False

    item = matching[0]
    where_clause = item.get("dialect_options", {}).get("sqlite_where")
    expected_where = "status IN ('queued', 'running')"
    if (
        len(matching) != 1
        or not bool(item.get("unique"))
        or item.get("column_names") != ["novel_id", "job_type"]
        or _normalized_sql(where_clause)
        != _normalized_sql(expected_where)
    ):
        raise RuntimeError(
            f"数据库索引 {index_name} 的定义不正确，"
            "请先备份并修复数据库结构。"
        )
    return True


def _add_unique_chapter_index(connection: Connection) -> None:
    """Prevent duplicate display indexes inside one novel."""
    inspector = inspect(connection)
    if "chapters" not in inspector.get_table_names():
        return

    expected_name = "ux_chapters_novel_index"
    expected_columns = ["novel_id", "chapter_index"]
    indexes = inspector.get_indexes("chapters")
    constraints = inspector.get_unique_constraints("chapters")
    matching_objects = [
        item
        for item in indexes + constraints
        if item.get("name") == expected_name
    ]
    if matching_objects:
        item = matching_objects[0]
        is_unique = item in constraints or bool(item.get("unique"))
        if not (
            is_unique
            and item.get("column_names") == expected_columns
        ):
            raise RuntimeError(
                f"数据库索引 {expected_name} 的定义不正确，"
                "请先备份并修复数据库结构。"
            )
        return

    duplicate = connection.execute(
        text(
            """
            SELECT novel_id, chapter_index
            FROM chapters
            GROUP BY novel_id, chapter_index
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "数据库中存在重复章节序号，无法完成结构升级："
            f"novel_id={duplicate.novel_id}, "
            f"chapter_index={duplicate.chapter_index}"
        )

    connection.execute(
        text(
            """
            CREATE UNIQUE INDEX ux_chapters_novel_index
            ON chapters (novel_id, chapter_index)
            """
        )
    )


def _add_analysis_tables(connection: Connection) -> None:
    """Create and validate chapter-summary and analysis-job tables."""
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS chapter_summaries (
                id INTEGER NOT NULL PRIMARY KEY,
                chapter_id INTEGER NOT NULL,
                summary TEXT NOT NULL,
                key_events JSON NOT NULL,
                plot_progression TEXT NOT NULL,
                cliffhanger TEXT,
                model VARCHAR(100) NOT NULL,
                prompt_version VARCHAR(50) NOT NULL,
                source_hash VARCHAR(64) NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT uq_chapter_summaries_chapter_id
                    UNIQUE (chapter_id),
                FOREIGN KEY(chapter_id) REFERENCES chapters(id) ON DELETE CASCADE
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS analysis_jobs (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                novel_id INTEGER NOT NULL,
                job_type VARCHAR(40) NOT NULL,
                target_chapter_index INTEGER,
                status VARCHAR(20) NOT NULL,
                progress_current INTEGER NOT NULL,
                progress_total INTEGER NOT NULL,
                error_message TEXT,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                started_at DATETIME,
                completed_at DATETIME,
                CONSTRAINT ck_analysis_jobs_job_type CHECK (
                    job_type IN (
                        'chapter_summaries',
                        'characters',
                        'rag_index',
                        'report',
                        'full_analysis'
                    )
                ),
                CONSTRAINT ck_analysis_jobs_status CHECK (
                    status IN ('queued', 'running', 'succeeded', 'failed')
                ),
                CONSTRAINT ck_analysis_jobs_progress_current_nonnegative
                    CHECK (progress_current >= 0),
                CONSTRAINT ck_analysis_jobs_progress_total_nonnegative
                    CHECK (progress_total >= 0),
                CONSTRAINT ck_analysis_jobs_progress_within_total
                    CHECK (progress_current <= progress_total),
                CONSTRAINT ck_analysis_jobs_target_chapter_index_positive
                    CHECK (
                        target_chapter_index IS NULL
                        OR target_chapter_index >= 1
                    ),
                FOREIGN KEY(novel_id) REFERENCES novels(id) ON DELETE CASCADE
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_analysis_jobs_novel_status
            ON analysis_jobs (novel_id, status)
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_analysis_jobs_novel_type_created
            ON analysis_jobs (novel_id, job_type, created_at)
            """
        )
    )

    _has_columns(
        connection,
        "chapter_summaries",
        {
            "id",
            "chapter_id",
            "summary",
            "key_events",
            "plot_progression",
            "cliffhanger",
            "model",
            "prompt_version",
            "source_hash",
            "created_at",
            "updated_at",
        },
    )
    _has_columns(
        connection,
        "analysis_jobs",
        {
            "id",
            "novel_id",
            "job_type",
            "target_chapter_index",
            "status",
            "progress_current",
            "progress_total",
            "error_message",
            "created_at",
            "updated_at",
            "started_at",
            "completed_at",
        },
    )

    if not _has_unique_columns(
        connection,
        "chapter_summaries",
        ["chapter_id"],
    ):
        raise RuntimeError(
            "数据库表 chapter_summaries 缺少 chapter_id 唯一约束，"
            "请先备份并修复数据库结构。"
        )
    if not _has_cascade_foreign_key(
        connection,
        "chapter_summaries",
        "chapter_id",
        "chapters",
    ):
        raise RuntimeError(
            "数据库表 chapter_summaries 的章节外键定义不正确，"
            "请先备份并修复数据库结构。"
        )
    if not _has_cascade_foreign_key(
        connection,
        "analysis_jobs",
        "novel_id",
        "novels",
    ):
        raise RuntimeError(
            "数据库表 analysis_jobs 的小说外键定义不正确，"
            "请先备份并修复数据库结构。"
        )

    actual_checks = {
        item.get("name"): "".join(
            str(item.get("sqltext", "")).casefold().split()
        )
        for item in inspect(connection).get_check_constraints(
            "analysis_jobs"
        )
    }
    expected_checks = {
        "ck_analysis_jobs_job_type": (
            "job_type IN ("
            "'chapter_summaries', 'characters', 'rag_index', "
            "'report', 'full_analysis'"
            ")"
        ),
        "ck_analysis_jobs_status": (
            "status IN ('queued', 'running', 'succeeded', 'failed')"
        ),
        "ck_analysis_jobs_progress_current_nonnegative": (
            "progress_current >= 0"
        ),
        "ck_analysis_jobs_progress_total_nonnegative": (
            "progress_total >= 0"
        ),
        "ck_analysis_jobs_progress_within_total": (
            "progress_current <= progress_total"
        ),
        "ck_analysis_jobs_target_chapter_index_positive": (
            "target_chapter_index IS NULL "
            "OR target_chapter_index >= 1"
        ),
    }
    for check_name, sqltext in expected_checks.items():
        normalized_sql = "".join(sqltext.casefold().split())
        if actual_checks.get(check_name) != normalized_sql:
            raise RuntimeError(
                "数据库表 analysis_jobs 的检查约束定义不正确，"
                "请先备份并修复数据库结构。"
            )

    _validate_index(
        connection,
        "analysis_jobs",
        "ix_analysis_jobs_novel_status",
        ["novel_id", "status"],
    )
    _validate_index(
        connection,
        "analysis_jobs",
        "ix_analysis_jobs_novel_type_created",
        ["novel_id", "job_type", "created_at"],
    )


def _add_unique_active_job_index(connection: Connection) -> None:
    """Add target scope and enforce one active job per novel and type."""
    if "analysis_jobs" not in inspect(connection).get_table_names():
        raise RuntimeError(
            "数据库缺少 analysis_jobs 表，无法添加活动任务唯一索引。"
        )

    columns = {
        column["name"]: column
        for column in inspect(connection).get_columns("analysis_jobs")
    }
    if "target_chapter_index" not in columns:
        connection.execute(
            text(
                """
                ALTER TABLE analysis_jobs
                ADD COLUMN target_chapter_index INTEGER
                CONSTRAINT ck_analysis_jobs_target_chapter_index_positive
                CHECK (
                    target_chapter_index IS NULL
                    OR target_chapter_index >= 1
                )
                """
            )
        )

    columns = {
        column["name"]: column
        for column in inspect(connection).get_columns("analysis_jobs")
    }
    target_column = columns.get("target_chapter_index")
    if target_column is None or not bool(target_column.get("nullable")):
        raise RuntimeError(
            "数据库表 analysis_jobs 的 target_chapter_index "
            "字段定义不正确，请先备份并修复数据库结构。"
        )
    target_checks = {
        item.get("name"): _normalized_sql(item.get("sqltext", ""))
        for item in inspect(connection).get_check_constraints(
            "analysis_jobs"
        )
    }
    expected_target_check = _normalized_sql(
        "target_chapter_index IS NULL "
        "OR target_chapter_index >= 1"
    )
    if (
        target_checks.get(
            "ck_analysis_jobs_target_chapter_index_positive"
        )
        != expected_target_check
    ):
        raise RuntimeError(
            "数据库表 analysis_jobs 的单章目标检查约束定义不正确，"
            "请先备份并修复数据库结构。"
        )

    if _validate_active_job_index(connection):
        return

    duplicate = connection.execute(
        text(
            """
            SELECT novel_id, job_type, COUNT(*) AS duplicate_count
            FROM analysis_jobs
            WHERE status IN ('queued', 'running')
            GROUP BY novel_id, job_type
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "数据库中存在重复活动分析任务，无法完成结构升级："
            f"novel_id={duplicate.novel_id}, "
            f"job_type={duplicate.job_type}, "
            f"count={duplicate.duplicate_count}"
        )

    connection.execute(
        text(
            """
            CREATE UNIQUE INDEX ux_analysis_jobs_active_novel_type
            ON analysis_jobs (novel_id, job_type)
            WHERE status IN ('queued', 'running')
            """
        )
    )
    if not _validate_active_job_index(connection):
        raise RuntimeError(
            "活动分析任务唯一索引创建失败，请检查数据库结构。"
        )


def _add_analysis_force_scope(connection: Connection) -> None:
    """Persist whether an analysis job intentionally refreshes results."""
    if "analysis_jobs" not in inspect(connection).get_table_names():
        raise RuntimeError(
            "数据库缺少 analysis_jobs 表，无法添加强制刷新字段。"
        )

    columns = {
        column["name"]: column
        for column in inspect(connection).get_columns("analysis_jobs")
    }
    if "force_refresh" not in columns:
        connection.execute(
            text(
                """
                ALTER TABLE analysis_jobs
                ADD COLUMN force_refresh BOOLEAN NOT NULL DEFAULT 0
                CONSTRAINT ck_analysis_jobs_force_refresh_boolean
                CHECK (force_refresh IN (0, 1))
                """
            )
        )

    columns = {
        column["name"]: column
        for column in inspect(connection).get_columns("analysis_jobs")
    }
    force_column = columns.get("force_refresh")
    default_value = _normalized_sql(
        force_column.get("default", "")
        if force_column is not None
        else ""
    ).strip("()'")
    if (
        force_column is None
        or bool(force_column.get("nullable"))
        or default_value != "0"
    ):
        raise RuntimeError(
            "数据库表 analysis_jobs 的 force_refresh 字段定义不正确，"
            "请先备份并修复数据库结构。"
        )

    checks = {
        item.get("name"): _normalized_sql(item.get("sqltext", ""))
        for item in inspect(connection).get_check_constraints(
            "analysis_jobs"
        )
    }
    if checks.get("ck_analysis_jobs_force_refresh_boolean") != (
        _normalized_sql("force_refresh IN (0, 1)")
    ):
        raise RuntimeError(
            "数据库表 analysis_jobs 的强制刷新检查约束定义不正确，"
            "请先备份并修复数据库结构。"
        )


def _validate_exact_columns(
    connection: Connection,
    table_name: str,
    expected: dict[str, tuple[str, bool, int, str | None]],
) -> None:
    """Validate ordered names, types, nullability, PKs, and defaults."""
    actual_columns = inspect(connection).get_columns(table_name)
    if [item["name"] for item in actual_columns] != list(expected):
        raise RuntimeError(
            f"数据库表 {table_name} 的字段集合或顺序不正确，"
            "请先备份并修复数据库结构。"
        )

    for column in actual_columns:
        expected_type, nullable, primary_key, default = expected[
            column["name"]
        ]
        actual_default = column.get("default")
        normalized_default = (
            _normalized_sql(actual_default).strip("()'")
            if actual_default is not None
            else None
        )
        if (
            str(column["type"]).upper() != expected_type
            or bool(column["nullable"]) is not nullable
            or int(column["primary_key"]) != primary_key
            or normalized_default != default
        ):
            raise RuntimeError(
                f"数据库表 {table_name} 的字段 {column['name']} "
                "定义不正确，请先备份并修复数据库结构。"
            )


def _validate_named_unique(
    connection: Connection,
    table_name: str,
    constraint_name: str,
    expected_columns: list[str],
) -> None:
    """Validate one named unique constraint with exact column order."""
    inspector = inspect(connection)
    candidates = (
        inspector.get_unique_constraints(table_name)
        + inspector.get_indexes(table_name)
    )
    matching = [
        item
        for item in candidates
        if item.get("name") == constraint_name
    ]
    if (
        len(matching) != 1
        or matching[0].get("column_names") != expected_columns
        or (
            matching[0]
            not in inspector.get_unique_constraints(table_name)
            and not bool(matching[0].get("unique"))
        )
    ):
        raise RuntimeError(
            f"数据库唯一约束 {constraint_name} 的定义不正确，"
            "请先备份并修复数据库结构。"
        )


def _ensure_named_unique_index(
    connection: Connection,
    table_name: str,
    index_name: str,
    expected_columns: list[str],
) -> None:
    """Create a legacy-compatible parent key or validate its name."""
    inspector = inspect(connection)
    if table_name not in inspector.get_table_names():
        raise RuntimeError(
            f"数据库缺少 {table_name} 表，无法创建父级唯一键。"
        )
    named_objects = [
        item
        for item in (
            inspector.get_unique_constraints(table_name)
            + inspector.get_indexes(table_name)
        )
        if item.get("name") == index_name
    ]
    if named_objects:
        _validate_named_unique(
            connection,
            table_name,
            index_name,
            expected_columns,
        )
        return

    column_sql = ", ".join(expected_columns)
    connection.execute(
        text(
            f"CREATE UNIQUE INDEX {index_name} "
            f"ON {table_name} ({column_sql})"
        )
    )
    _validate_named_unique(
        connection,
        table_name,
        index_name,
        expected_columns,
    )


def _validate_exact_cascade_foreign_key(
    connection: Connection,
    table_name: str,
    constrained_columns: list[str],
    referred_table: str,
    referred_columns: list[str],
) -> None:
    """Validate one exact cascading foreign-key path."""
    matching = [
        item
        for item in inspect(connection).get_foreign_keys(table_name)
        if item.get("constrained_columns") == constrained_columns
    ]
    if len(matching) != 1:
        raise RuntimeError(
            f"数据库表 {table_name} 的外键 "
            f"{','.join(constrained_columns)} 定义不正确，"
            "请先备份并修复数据库结构。"
        )

    foreign_key = matching[0]
    ondelete = str(
        foreign_key.get("options", {}).get("ondelete", "")
    ).upper()
    if (
        foreign_key.get("referred_table") != referred_table
        or foreign_key.get("referred_columns") != referred_columns
        or ondelete != "CASCADE"
    ):
        raise RuntimeError(
            f"数据库表 {table_name} 的外键 "
            f"{','.join(constrained_columns)} 定义不正确，"
            "请先备份并修复数据库结构。"
        )


def _validate_exact_checks(
    connection: Connection,
    table_name: str,
    expected: dict[str, str],
) -> None:
    """Validate the complete named CHECK-constraint set."""
    actual = {
        item.get("name"): _normalized_sql(item.get("sqltext", ""))
        for item in inspect(connection).get_check_constraints(table_name)
    }
    normalized_expected = {
        name: _normalized_sql(sqltext)
        for name, sqltext in expected.items()
    }
    if actual != normalized_expected:
        mismatched_names = sorted(
            str(name)
            for name in set(actual) | set(normalized_expected)
            if actual.get(name) != normalized_expected.get(name)
        )
        raise RuntimeError(
            f"数据库表 {table_name} 的检查约束 "
            f"{', '.join(mismatched_names)} 定义不正确，"
            "请先备份并修复数据库结构。"
        )


def _validate_trigger(
    connection: Connection,
    table_name: str,
    trigger_name: str,
    expected_sql: str,
) -> None:
    """Validate one exact SQLite trigger definition."""
    rows = connection.execute(
        text(
            """
            SELECT tbl_name, sql
            FROM sqlite_master
            WHERE type = 'trigger' AND name = :trigger_name
            """
        ),
        {"trigger_name": trigger_name},
    ).all()

    def normalize(value: object) -> str:
        return _normalized_sql(value).replace("ifnotexists", "")

    if (
        len(rows) != 1
        or rows[0].tbl_name != table_name
        or normalize(rows[0].sql) != normalize(expected_sql)
    ):
        raise RuntimeError(
            f"数据库触发器 {trigger_name} 的定义不正确，"
            "请先备份并修复数据库结构。"
        )


def _add_character_analysis_tables(connection: Connection) -> None:
    """Create and strictly validate the character snapshot schema."""
    from backend.app.models.character import (
        CHARACTER_APPEARANCE_OWNERSHIP_TRIGGER_SQL,
    )

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
    connection.execute(
        text(CHARACTER_APPEARANCE_OWNERSHIP_TRIGGER_SQL)
    )

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

    _validate_trigger(
        connection,
        "character_appearances",
        "trg_character_appearances_fill_ownership",
        CHARACTER_APPEARANCE_OWNERSHIP_TRIGGER_SQL,
    )


MIGRATIONS: tuple[tuple[int, str, MigrationFunction], ...] = (
    (1, "add unique chapter index", _add_unique_chapter_index),
    (2, "add analysis tables", _add_analysis_tables),
    (
        3,
        "add analysis target and active job uniqueness",
        _add_unique_active_job_index,
    ),
    (4, "add analysis force scope", _add_analysis_force_scope),
    (
        5,
        "add character analysis snapshots",
        _add_character_analysis_tables,
    ),
)


def run_migrations(engine: Engine) -> None:
    """Apply every pending migration exactly once."""
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        applied_versions = {
            row.version
            for row in connection.execute(
                text("SELECT version FROM schema_migrations")
            )
        }

        for version, name, migrate in MIGRATIONS:
            if version in applied_versions:
                continue
            migrate(connection)
            connection.execute(
                text(
                    """
                    INSERT INTO schema_migrations (version, name)
                    VALUES (:version, :name)
                    """
                ),
                {"version": version, "name": name},
            )
