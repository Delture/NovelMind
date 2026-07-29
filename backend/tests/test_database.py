"""Tests for database engine configuration."""

from importlib import import_module

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core import database
from backend.app.models.novel import Chapter, Novel


def test_sqlite_engine_enables_foreign_keys() -> None:
    engine = database.create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )

    try:
        with engine.connect() as connection:
            enabled = connection.execute(text("PRAGMA foreign_keys")).scalar_one()
    finally:
        engine.dispose()

    assert enabled == 1


def test_database_cascade_deletes_chapters_without_orm() -> None:
    engine = database.create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    database.Base.metadata.create_all(bind=engine)

    try:
        with Session(engine) as session:
            novel = Novel(
                filename="story.txt",
                encoding="utf-8",
                chapter_count=1,
                chapters=[
                    Chapter(
                        chapter_index=1,
                        title="第一章",
                        content="正文",
                        character_count=2,
                    )
                ],
            )
            session.add(novel)
            session.commit()
            novel_id = novel.id

        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM novels WHERE id = :novel_id"),
                {"novel_id": novel_id},
            )
            chapter_count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM chapters "
                    "WHERE novel_id = :novel_id"
                ),
                {"novel_id": novel_id},
            ).scalar_one()
    finally:
        engine.dispose()

    assert chapter_count == 0


def test_fresh_schema_rejects_duplicate_chapter_indexes() -> None:
    engine = database.create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    database.Base.metadata.create_all(bind=engine)

    try:
        with Session(engine) as session:
            novel = Novel(
                filename="story.txt",
                encoding="utf-8",
                chapter_count=2,
                chapters=[
                    Chapter(
                        chapter_index=1,
                        title="第一章",
                        content="内容一",
                        character_count=3,
                    ),
                    Chapter(
                        chapter_index=1,
                        title="重复章节",
                        content="内容二",
                        character_count=3,
                    ),
                ],
            )
            session.add(novel)
            with pytest.raises(IntegrityError):
                session.commit()
    finally:
        engine.dispose()


def test_init_db_builds_and_versions_fresh_schema_idempotently() -> None:
    engine = database.create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )

    try:
        database.init_db(engine)
        database.init_db(engine)

        inspector = inspect(engine)
        table_names = inspector.get_table_names()
        indexes = inspector.get_indexes("chapters")
        constraints = inspector.get_unique_constraints("chapters")
        with engine.connect() as connection:
            versions = connection.execute(
                text(
                    "SELECT version, name FROM schema_migrations "
                    "ORDER BY version"
                )
            ).all()
    finally:
        engine.dispose()

    assert {
        "novels",
        "chapters",
        "chapter_summaries",
        "analysis_jobs",
        "schema_migrations",
    } <= set(table_names)
    assert any(
        item.get("name") == "ux_chapters_novel_index"
        for item in indexes + constraints
    )
    assert versions == [
        (1, "add unique chapter index"),
        (2, "add analysis tables"),
        (3, "add analysis target and active job uniqueness"),
        (4, "add analysis force scope"),
    ]


def test_migration_adds_unique_index_to_legacy_database() -> None:
    engine = database.create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE novels (
                    id INTEGER PRIMARY KEY,
                    filename VARCHAR(255) NOT NULL,
                    encoding VARCHAR(20) NOT NULL,
                    chapter_count INTEGER NOT NULL,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE chapters (
                    id INTEGER PRIMARY KEY,
                    novel_id INTEGER NOT NULL,
                    chapter_index INTEGER NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    content TEXT NOT NULL,
                    character_count INTEGER NOT NULL,
                    FOREIGN KEY(novel_id) REFERENCES novels(id) ON DELETE CASCADE
                )
                """
            )
        )

    try:
        database.init_db(engine)
        database.init_db(engine)

        indexes = inspect(engine).get_indexes("chapters")
        version_count = 0
        with engine.connect() as connection:
            version_count = connection.execute(
                text("SELECT COUNT(*) FROM schema_migrations")
            ).scalar_one()
    finally:
        engine.dispose()

    assert any(
        index["name"] == "ux_chapters_novel_index"
        and index["unique"]
        for index in indexes
    )
    assert version_count == 4


def test_analysis_migration_creates_tables_for_legacy_schema() -> None:
    migrations = import_module("backend.app.core.migrations")
    engine = database.create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE novels (
                    id INTEGER PRIMARY KEY,
                    filename VARCHAR(255) NOT NULL,
                    encoding VARCHAR(20) NOT NULL,
                    chapter_count INTEGER NOT NULL,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE chapters (
                    id INTEGER PRIMARY KEY,
                    novel_id INTEGER NOT NULL,
                    chapter_index INTEGER NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    content TEXT NOT NULL,
                    character_count INTEGER NOT NULL,
                    FOREIGN KEY(novel_id) REFERENCES novels(id)
                        ON DELETE CASCADE
                )
                """
            )
        )

    try:
        migrations.run_migrations(engine)
        migrations.run_migrations(engine)

        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        job_indexes = {
            index["name"]: index
            for index in inspector.get_indexes("analysis_jobs")
        }
        with engine.connect() as connection:
            versions = connection.execute(
                text(
                    "SELECT version FROM schema_migrations "
                    "ORDER BY version"
                )
            ).scalars().all()
    finally:
        engine.dispose()

    assert {"chapter_summaries", "analysis_jobs"} <= table_names
    assert job_indexes["ix_analysis_jobs_novel_status"]["column_names"] == [
        "novel_id",
        "status",
    ]
    assert job_indexes[
        "ix_analysis_jobs_novel_type_created"
    ]["column_names"] == ["novel_id", "job_type", "created_at"]
    assert versions == [1, 2, 3, 4]


def test_v3_upgrades_an_existing_v2_analysis_job_table() -> None:
    migrations = import_module("backend.app.core.migrations")
    engine = database.create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE novels (id INTEGER PRIMARY KEY)"))
        connection.execute(
            text(
                """
                CREATE TABLE analysis_jobs (
                    id VARCHAR(36) NOT NULL PRIMARY KEY,
                    novel_id INTEGER NOT NULL,
                    job_type VARCHAR(40) NOT NULL,
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
                    FOREIGN KEY(novel_id) REFERENCES novels(id) ON DELETE CASCADE
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO schema_migrations (version, name)
                VALUES
                    (1, 'add unique chapter index'),
                    (2, 'add analysis tables')
                """
            )
        )

    try:
        migrations.run_migrations(engine)
        inspector = inspect(engine)
        columns = {
            column["name"]: column
            for column in inspector.get_columns("analysis_jobs")
        }
        checks = {
            item["name"]: item["sqltext"]
            for item in inspector.get_check_constraints("analysis_jobs")
        }
        indexes = {
            item["name"]: item
            for item in inspector.get_indexes("analysis_jobs")
        }
        with engine.connect() as connection:
            versions = connection.execute(
                text(
                    "SELECT version FROM schema_migrations "
                    "ORDER BY version"
                )
            ).scalars().all()
    finally:
        engine.dispose()

    assert columns["target_chapter_index"]["nullable"] is True
    assert (
        "target_chapter_index"
        in checks["ck_analysis_jobs_target_chapter_index_positive"]
    )
    assert indexes["ux_analysis_jobs_active_novel_type"]["unique"]
    assert columns["force_refresh"]["nullable"] is False
    assert str(columns["force_refresh"]["default"]).strip("()'") == "0"
    assert versions == [1, 2, 3, 4]


def test_active_job_migration_rejects_duplicate_active_jobs() -> None:
    migrations = import_module("backend.app.core.migrations")
    engine = database.create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    database.init_db(engine)
    with engine.begin() as connection:
        connection.execute(
            text("DROP INDEX ux_analysis_jobs_active_novel_type")
        )
        connection.execute(
            text("DELETE FROM schema_migrations WHERE version >= 3")
        )
        connection.execute(
            text(
                """
                INSERT INTO novels (
                    id, filename, encoding, chapter_count, created_at
                )
                VALUES (1, 'story.txt', 'utf-8', 0, CURRENT_TIMESTAMP)
                """
            )
        )
        for job_id, job_status in (
            ("queued-job", "queued"),
            ("running-job", "running"),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO analysis_jobs (
                        id,
                        novel_id,
                        job_type,
                        status,
                        progress_current,
                        progress_total,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        :id,
                        1,
                        'chapter_summaries',
                        :status,
                        0,
                        0,
                        CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP
                    )
                    """
                ),
                {"id": job_id, "status": job_status},
            )

    try:
        with pytest.raises(
            RuntimeError,
            match="重复活动分析任务.*novel_id=1.*chapter_summaries",
        ):
            migrations.run_migrations(engine)
        with engine.connect() as connection:
            versions = connection.execute(
                text(
                    "SELECT version FROM schema_migrations "
                    "ORDER BY version"
                )
            ).scalars().all()
    finally:
        engine.dispose()

    assert versions == [1, 2]


def test_active_job_migration_rejects_wrong_named_index() -> None:
    migrations = import_module("backend.app.core.migrations")
    engine = database.create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    database.init_db(engine)
    with engine.begin() as connection:
        connection.execute(
            text("DROP INDEX ux_analysis_jobs_active_novel_type")
        )
        connection.execute(
            text("DELETE FROM schema_migrations WHERE version >= 3")
        )
        connection.execute(
            text(
                """
                CREATE UNIQUE INDEX ux_analysis_jobs_active_novel_type
                ON analysis_jobs (novel_id, job_type)
                WHERE status = 'queued'
                """
            )
        )

    try:
        with pytest.raises(
            RuntimeError,
            match="ux_analysis_jobs_active_novel_type",
        ):
            migrations.run_migrations(engine)
        with engine.connect() as connection:
            versions = connection.execute(
                text(
                    "SELECT version FROM schema_migrations "
                    "ORDER BY version"
                )
            ).scalars().all()
    finally:
        engine.dispose()

    assert versions == [1, 2]


def test_analysis_migration_rejects_wrong_named_check_definitions() -> None:
    migrations = import_module("backend.app.core.migrations")
    engine = database.create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE novels (id INTEGER PRIMARY KEY)"))
        connection.execute(
            text(
                """
                CREATE TABLE chapters (
                    id INTEGER PRIMARY KEY,
                    novel_id INTEGER NOT NULL,
                    chapter_index INTEGER NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    content TEXT NOT NULL,
                    character_count INTEGER NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE analysis_jobs (
                    id VARCHAR(36) NOT NULL PRIMARY KEY,
                    novel_id INTEGER NOT NULL,
                    job_type VARCHAR(40) NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    progress_current INTEGER NOT NULL,
                    progress_total INTEGER NOT NULL,
                    error_message TEXT,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    started_at DATETIME,
                    completed_at DATETIME,
                    target_chapter_index INTEGER,
                    CONSTRAINT ck_analysis_jobs_job_type CHECK (1),
                    CONSTRAINT ck_analysis_jobs_status CHECK (1),
                    CONSTRAINT ck_analysis_jobs_progress_current_nonnegative
                        CHECK (1),
                    CONSTRAINT ck_analysis_jobs_progress_total_nonnegative
                        CHECK (1),
                    CONSTRAINT ck_analysis_jobs_progress_within_total CHECK (1),
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

    try:
        with pytest.raises(RuntimeError, match="检查约束"):
            migrations.run_migrations(engine)
        with engine.connect() as connection:
            versions = connection.execute(
                text(
                    "SELECT version FROM schema_migrations "
                    "ORDER BY version"
                )
            ).scalars().all()
    finally:
        engine.dispose()

    assert versions == []


def test_migration_rejects_conflicting_named_index() -> None:
    migrations = import_module("backend.app.core.migrations")
    engine = database.create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE chapters (
                    id INTEGER PRIMARY KEY,
                    novel_id INTEGER NOT NULL,
                    chapter_index INTEGER NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    content TEXT NOT NULL,
                    character_count INTEGER NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE INDEX ux_chapters_novel_index
                ON chapters (chapter_index)
                """
            )
        )

    try:
        with pytest.raises(RuntimeError, match="ux_chapters_novel_index"):
            migrations.run_migrations(engine)
        with engine.connect() as connection:
            version_count = connection.execute(
                text("SELECT COUNT(*) FROM schema_migrations")
            ).scalar_one()
    finally:
        engine.dispose()

    assert version_count == 0


def test_migration_with_duplicate_data_does_not_record_version() -> None:
    migrations = import_module("backend.app.core.migrations")
    engine = database.create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE chapters (
                    id INTEGER PRIMARY KEY,
                    novel_id INTEGER NOT NULL,
                    chapter_index INTEGER NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    content TEXT NOT NULL,
                    character_count INTEGER NOT NULL
                )
                """
            )
        )
        for row_id in (1, 2):
            connection.execute(
                text(
                    """
                    INSERT INTO chapters (
                        id,
                        novel_id,
                        chapter_index,
                        title,
                        content,
                        character_count
                    )
                    VALUES (:id, 1, 1, '第一章', '正文', 2)
                    """
                ),
                {"id": row_id},
            )

    try:
        with pytest.raises(RuntimeError, match="重复章节序号"):
            migrations.run_migrations(engine)
        with engine.connect() as connection:
            version_count = connection.execute(
                text("SELECT COUNT(*) FROM schema_migrations")
            ).scalar_one()
    finally:
        engine.dispose()

    assert version_count == 0


def test_v4_adds_and_enforces_force_refresh_scope() -> None:
    migrations = import_module("backend.app.core.migrations")
    engine = database.create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE analysis_jobs (
                    id VARCHAR(36) NOT NULL PRIMARY KEY
                )
                """
            )
        )

    try:
        with engine.begin() as connection:
            migrations._add_analysis_force_scope(connection)
            connection.execute(
                text("INSERT INTO analysis_jobs (id) VALUES ('default')")
            )

        inspector = inspect(engine)
        columns = {
            column["name"]: column
            for column in inspector.get_columns("analysis_jobs")
        }
        checks = {
            item["name"]: item["sqltext"]
            for item in inspector.get_check_constraints("analysis_jobs")
        }
        with engine.connect() as connection:
            default_value = connection.execute(
                text(
                    "SELECT force_refresh FROM analysis_jobs "
                    "WHERE id = 'default'"
                )
            ).scalar_one()
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO analysis_jobs (id, force_refresh) "
                        "VALUES ('invalid', 2)"
                    )
                )
    finally:
        engine.dispose()

    assert columns["force_refresh"]["nullable"] is False
    assert str(columns["force_refresh"]["default"]).strip("()'") == "0"
    assert "force_refresh" in checks[
        "ck_analysis_jobs_force_refresh_boolean"
    ]
    assert default_value == 0


def test_v4_rejects_wrong_force_refresh_check() -> None:
    migrations = import_module("backend.app.core.migrations")
    engine = database.create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE analysis_jobs (
                    id VARCHAR(36) NOT NULL PRIMARY KEY,
                    force_refresh BOOLEAN NOT NULL DEFAULT 0,
                    CONSTRAINT ck_analysis_jobs_force_refresh_boolean
                        CHECK (force_refresh IN (0, 2))
                )
                """
            )
        )

    try:
        with pytest.raises(RuntimeError, match="强制刷新检查约束"):
            with engine.begin() as connection:
                migrations._add_analysis_force_scope(connection)
    finally:
        engine.dispose()
