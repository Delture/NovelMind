"""迁移账本与原子执行入口。历史步骤按版本保留，新增版本只在末尾追加。"""
from typing import Callable, cast
import sqlite3
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from .migration_steps.v01_analysis import _add_unique_chapter_index, _add_analysis_tables, _add_unique_active_job_index, _add_analysis_force_scope
from .migration_steps.v05_characters import _add_character_analysis_tables
from .migration_steps.v06_rag import _add_rag_index_tables, _add_rag_question_table, _allow_configurable_embedding_models
from .migration_steps.v08_reports import _add_novel_report_table
from .migration_steps.v10_workspace import add_workspace_tables, explicit_appearance_ownership
from .migration_steps.v12_workflow_time import workflow_datetime
from .migration_steps.v13_chapter_review_job import allow_chapter_review_job_type
MigrationFunction = Callable[[Connection], None]

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
    (
        6,
        "add rag index snapshots",
        _add_rag_index_tables,
    ),
    (
        7,
        "add rag question history",
        _add_rag_question_table,
    ),
    (
        8,
        "add novel report snapshots",
        _add_novel_report_table,
    ),
    (
        9,
        "allow configurable embedding models",
        _allow_configurable_embedding_models,
    ),
    (10, "add workspace reading and workflows", add_workspace_tables),
    (11, "explicit character appearance ownership", explicit_appearance_ownership),
    (12, "normalize workflow timestamps", workflow_datetime),
    (13, "allow chapter review job type", allow_chapter_review_job_type),
)


def run_migrations(engine: Engine) -> None:
    """迁移函数本身仍需可重复运行：进程崩溃、手工修复或 SQLite DDL 行为都可能留下边界状态。"""
    with engine.begin() as connection:
        # sqlite3 legacy transaction control does not BEGIN for DDL by itself.
        if connection.dialect.name == "sqlite" and not cast(sqlite3.Connection, connection.connection.driver_connection).in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
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
