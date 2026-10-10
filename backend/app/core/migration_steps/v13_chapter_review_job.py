"""Allow analysis_jobs.job_type = chapter_review for optional AI directory review."""

from sqlalchemy import inspect, text


def allow_chapter_review_job_type(connection) -> None:
    if "analysis_jobs" not in inspect(connection).get_table_names():
        raise RuntimeError("数据库缺少 analysis_jobs 表，无法扩展任务类型。")

    checks = {
        item.get("name"): "".join(str(item.get("sqltext", "")).casefold().split())
        for item in inspect(connection).get_check_constraints("analysis_jobs")
    }
    current = checks.get("ck_analysis_jobs_job_type", "")
    if "chapter_review" in current:
        return

    rows = list(connection.execute(text("SELECT * FROM analysis_jobs")).mappings())
    connection.exec_driver_sql(
        """
        CREATE TABLE analysis_jobs_upgrade (
            id VARCHAR(36) NOT NULL,
            novel_id INTEGER NOT NULL,
            job_type VARCHAR(40) NOT NULL,
            target_chapter_index INTEGER,
            force_refresh BOOLEAN NOT NULL DEFAULT 0,
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
                    'full_analysis',
                    'chapter_review'
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
            CONSTRAINT ck_analysis_jobs_force_refresh_boolean
                CHECK (force_refresh IN (0, 1)),
            PRIMARY KEY (id),
            FOREIGN KEY(novel_id) REFERENCES novels(id) ON DELETE CASCADE
        )
        """
    )
    for row in rows:
        value = dict(row)
        value.setdefault("force_refresh", 0)
        connection.execute(
            text(
                """
                INSERT INTO analysis_jobs_upgrade (
                    id, novel_id, job_type, target_chapter_index, force_refresh,
                    status, progress_current, progress_total, error_message,
                    created_at, updated_at, started_at, completed_at
                ) VALUES (
                    :id, :novel_id, :job_type, :target_chapter_index, :force_refresh,
                    :status, :progress_current, :progress_total, :error_message,
                    :created_at, :updated_at, :started_at, :completed_at
                )
                """
            ),
            value,
        )
    connection.exec_driver_sql("DROP TABLE analysis_jobs")
    connection.exec_driver_sql(
        "ALTER TABLE analysis_jobs_upgrade RENAME TO analysis_jobs"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_analysis_jobs_novel_status "
        "ON analysis_jobs (novel_id, status)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_analysis_jobs_novel_type_created "
        "ON analysis_jobs (novel_id, job_type, created_at)"
    )
    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_analysis_jobs_active_novel_type "
        "ON analysis_jobs (novel_id, job_type) "
        "WHERE status IN ('queued', 'running')"
    )
