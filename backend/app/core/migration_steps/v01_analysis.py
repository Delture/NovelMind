from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from .validation import (
    _has_columns,
    _has_cascade_foreign_key,
    _has_unique_columns,
    _validate_index,
    _normalized_sql,
    _validate_active_job_index,
)

def _add_unique_chapter_index(connection: Connection) -> None:
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
