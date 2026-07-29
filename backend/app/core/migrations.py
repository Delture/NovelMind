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


MIGRATIONS: tuple[tuple[int, str, MigrationFunction], ...] = (
    (1, "add unique chapter index", _add_unique_chapter_index),
    (2, "add analysis tables", _add_analysis_tables),
    (
        3,
        "add analysis target and active job uniqueness",
        _add_unique_active_job_index,
    ),
    (4, "add analysis force scope", _add_analysis_force_scope),
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
