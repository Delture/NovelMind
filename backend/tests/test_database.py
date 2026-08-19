"""Tests for database engine configuration."""

from importlib import import_module

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core import database
from backend.app.models.novel import Chapter, Novel
from backend.app.models.rag import RagChunkRecord, RagIndex


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
        "chapter_character_extractions",
        "character_analyses",
        "characters",
            "character_appearances",
            "character_relationships",
            "rag_indices",
            "rag_chunks",
                "rag_questions",
                "novel_reports",
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
        (5, "add character analysis snapshots"),
        (6, "add rag index snapshots"),
                (7, "add rag question history"),
                (8, "add novel report snapshots"),
                (9, "allow configurable embedding models"),
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
    assert version_count == 9


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
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9]


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
                CREATE TABLE chapters (
                    id INTEGER PRIMARY KEY,
                    novel_id INTEGER NOT NULL,
                    FOREIGN KEY(novel_id)
                        REFERENCES novels(id) ON DELETE CASCADE
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
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9]


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


def _build_v4_database(engine) -> None:
    """Create the last pre-character schema with migrations 1 through 4."""
    migrations = import_module("backend.app.core.migrations")
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
                    FOREIGN KEY(novel_id)
                        REFERENCES novels(id) ON DELETE CASCADE
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
        applied = []
        for version, name, migrate in migrations.MIGRATIONS[:4]:
            migrate(connection)
            applied.append({"version": version, "name": name})
        connection.execute(
            text(
                """
                INSERT INTO schema_migrations (version, name)
                VALUES (:version, :name)
                """
            ),
            applied,
        )
        connection.execute(
            text(
                """
                INSERT INTO novels (
                    id,
                    filename,
                    encoding,
                    chapter_count,
                    created_at
                )
                VALUES (
                    1,
                    'legacy.txt',
                    'utf-8',
                    1,
                    CURRENT_TIMESTAMP
                )
                """
            )
        )
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
                VALUES (1, 1, 1, '第一章', '旧数据正文', 5)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO chapter_summaries (
                    id,
                    chapter_id,
                    summary,
                    key_events,
                    plot_progression,
                    cliffhanger,
                    model,
                    prompt_version,
                    source_hash,
                    created_at,
                    updated_at
                )
                VALUES (
                    1,
                    1,
                    '旧摘要',
                    '[]',
                    '旧情节',
                    NULL,
                    'legacy-model',
                    'summary-v1',
                    :source_hash,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
                """
            ),
            {"source_hash": "a" * 64},
        )
        connection.execute(
            text(
                """
                INSERT INTO analysis_jobs (
                    id,
                    novel_id,
                    job_type,
                    target_chapter_index,
                    force_refresh,
                    status,
                    progress_current,
                    progress_total,
                    error_message,
                    created_at,
                    updated_at,
                    started_at,
                    completed_at
                )
                VALUES (
                    'legacy-job',
                    1,
                    'chapter_summaries',
                    NULL,
                    0,
                    'succeeded',
                    1,
                    1,
                    NULL,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
                """
            )
        )


def test_v5_upgrades_v4_with_strict_character_snapshot_schema() -> None:
    migrations = import_module("backend.app.core.migrations")
    engine = database.create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    _build_v4_database(engine)

    try:
        migrations.run_migrations(engine)
        migrations.run_migrations(engine)

        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        extraction_uniques = inspector.get_unique_constraints(
            "chapter_character_extractions"
        )
        analysis_uniques = inspector.get_unique_constraints(
            "character_analyses"
        )
        chapter_unique_constraints = inspector.get_unique_constraints(
            "chapters"
        )
        chapter_uniques = (
            chapter_unique_constraints
            + inspector.get_indexes("chapters")
        )
        character_uniques = inspector.get_unique_constraints("characters")
        appearance_uniques = inspector.get_unique_constraints(
            "character_appearances"
        )
        appearance_columns = {
            item["name"]
            for item in inspector.get_columns("character_appearances")
        }
        appearance_foreign_keys = {
            tuple(item["constrained_columns"]): (
                item["referred_table"],
                tuple(item["referred_columns"]),
                str(item.get("options", {}).get("ondelete", "")).upper(),
            )
            for item in inspector.get_foreign_keys(
                "character_appearances"
            )
        }
        relationship_uniques = inspector.get_unique_constraints(
            "character_relationships"
        )
        relationship_checks = {
            item["name"]: item["sqltext"]
            for item in inspector.get_check_constraints(
                "character_relationships"
            )
        }
        relationship_foreign_keys = {
            tuple(item["constrained_columns"]): (
                item["referred_table"],
                tuple(item["referred_columns"]),
                str(item.get("options", {}).get("ondelete", "")).upper(),
            )
            for item in inspector.get_foreign_keys(
                "character_relationships"
            )
        }
        index_definitions = {
            item["name"]: (
                item["column_names"],
                bool(item.get("unique")),
            )
            for table_name in (
                "characters",
                "character_appearances",
                "character_relationships",
            )
            for item in inspector.get_indexes(table_name)
            if item.get("name")
        }
        with engine.connect() as connection:
            versions = connection.execute(
                text(
                    "SELECT version, name FROM schema_migrations "
                    "ORDER BY version"
                )
            ).all()
            legacy_rows = {
                table_name: connection.execute(
                    text(f"SELECT COUNT(*) FROM {table_name}")
                ).scalar_one()
                for table_name in (
                    "novels",
                    "chapters",
                    "chapter_summaries",
                    "analysis_jobs",
                )
            }
            foreign_key_violations = connection.execute(
                text("PRAGMA foreign_key_check")
            ).all()
            ownership_trigger = connection.execute(
                text(
                    """
                    SELECT sql
                    FROM sqlite_master
                    WHERE type = 'trigger'
                      AND name = (
                          'trg_character_appearances_fill_ownership'
                      )
                    """
                )
            ).scalar_one()
    finally:
        engine.dispose()

    assert {
        "chapter_character_extractions",
        "character_analyses",
        "characters",
        "character_appearances",
        "character_relationships",
    } <= table_names
    assert any(
        item["column_names"] == ["chapter_id"]
        for item in extraction_uniques
    )
    assert any(
        item["column_names"] == ["novel_id"]
        for item in analysis_uniques
    )
    assert any(
        item["column_names"] == ["id", "novel_id"]
        for item in analysis_uniques
    )
    assert any(
        item["column_names"] == ["novel_id", "id"]
        and (
            item in chapter_unique_constraints
            or bool(item.get("unique"))
        )
        for item in chapter_uniques
    )
    assert not any(
        item["column_names"] == ["analysis_id", "canonical_name"]
        for item in character_uniques
    )
    assert any(
        item["column_names"] == ["analysis_id", "id"]
        for item in character_uniques
    )
    assert any(
        item["column_names"] == ["character_id", "chapter_id"]
        for item in appearance_uniques
    )
    assert {
        "id",
        "analysis_id",
        "novel_id",
        "character_id",
        "chapter_id",
        "mention_count",
        "summary",
    } == appearance_columns
    assert appearance_foreign_keys[("analysis_id", "novel_id")] == (
        "character_analyses",
        ("id", "novel_id"),
        "CASCADE",
    )
    assert appearance_foreign_keys[
        ("analysis_id", "character_id")
    ] == (
        "characters",
        ("analysis_id", "id"),
        "CASCADE",
    )
    assert appearance_foreign_keys[("novel_id", "chapter_id")] == (
        "chapters",
        ("novel_id", "id"),
        "CASCADE",
    )
    assert any(
        item["column_names"]
        == [
            "analysis_id",
            "source_character_id",
            "target_character_id",
            "directed",
            "relation_type",
        ]
        for item in relationship_uniques
    )
    assert "ck_character_relationships_no_self" in relationship_checks
    assert (
        "ck_character_relationships_undirected_canonical"
        in relationship_checks
    )
    assert relationship_foreign_keys[
        ("analysis_id", "source_character_id")
    ] == (
        "characters",
        ("analysis_id", "id"),
        "CASCADE",
    )
    assert relationship_foreign_keys[
        ("analysis_id", "target_character_id")
    ] == (
        "characters",
        ("analysis_id", "id"),
        "CASCADE",
    )
    assert index_definitions["ix_characters_analysis_chapter_count"] == (
        ["analysis_id", "chapter_count"],
        False,
    )
    assert index_definitions[
        "ix_characters_analysis_canonical_name"
    ] == (
        ["analysis_id", "canonical_name"],
        False,
    )
    assert index_definitions["ix_characters_analysis_mention_count"] == (
        ["analysis_id", "mention_count"],
        False,
    )
    assert index_definitions["ix_character_appearances_chapter_id"] == (
        ["chapter_id"],
        False,
    )
    assert index_definitions[
        "ix_character_appearances_analysis_id"
    ] == (
        ["analysis_id"],
        False,
    )
    assert index_definitions[
        "ix_character_relationships_analysis_target"
    ] == (
        ["analysis_id", "target_character_id"],
        False,
    )
    assert (5, "add character analysis snapshots") in versions
    assert versions[-1] == (
        9,
        "allow configurable embedding models",
    )
    assert len(versions) == 9
    assert legacy_rows == {
        "novels": 1,
        "chapters": 1,
        "chapter_summaries": 1,
        "analysis_jobs": 1,
    }
    assert foreign_key_violations == []
    assert (
        "UPDATE character_appearances"
        in ownership_trigger
    )


def test_embedding_provider_migration_preserves_rag_snapshot_data() -> None:
    engine = database.create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    database.init_db(engine)
    try:
        with Session(engine) as session:
            novel = Novel(
                filename="migration-rag.txt",
                encoding="utf-8",
                chapter_count=1,
                chapters=[
                    Chapter(
                        chapter_index=1,
                        title="第一章",
                        content="迁移测试正文",
                        character_count=6,
                    )
                ],
            )
            session.add(novel)
            session.flush()
            index = RagIndex(
                novel_id=novel.id,
                source_hash="a" * 64,
                collection_name="migration_rag_collection",
                embedding_provider="aliyun_model_studio",
                embedding_model="text-embedding-v4",
                embedding_dimensions=1_024,
                chunk_version="rag-chunk-v2-local-sqlite",
                chunk_count=1,
            )
            session.add(index)
            session.flush()
            session.add(
                RagChunkRecord(
                    rag_index_id=index.id,
                    chapter_id=novel.chapters[0].id,
                    chunk_id="chunk_" + "b" * 64,
                    chapter_index=1,
                    chunk_index=1,
                    start_char=0,
                    end_char=6,
                    content_hash="c" * 64,
                )
            )
            session.commit()

        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM schema_migrations WHERE version = 9")
            )
        import_module("backend.app.core.migrations").run_migrations(engine)

        with engine.begin() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM rag_indices")
            ).scalar_one() == 1
            assert connection.execute(
                text("SELECT COUNT(*) FROM rag_chunks")
            ).scalar_one() == 1
            connection.execute(
                text(
                    """
                    UPDATE rag_indices
                    SET embedding_provider = 'openai_compatible',
                        embedding_model = 'custom-embedding',
                        embedding_dimensions = 1536
                    """
                )
            )
            assert connection.execute(
                text("PRAGMA foreign_key_check")
            ).all() == []
    finally:
        engine.dispose()


def test_v5_rejects_conflicting_named_character_index() -> None:
    migrations = import_module("backend.app.core.migrations")
    engine = database.create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    _build_v4_database(engine)
    migrations.run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM schema_migrations WHERE version = 5")
        )
        connection.execute(
            text(
                "DROP INDEX ix_character_relationships_analysis_target"
            )
        )
        connection.execute(
            text(
                """
                CREATE INDEX ix_character_relationships_analysis_target
                ON character_relationships (
                    analysis_id,
                    source_character_id
                )
                """
            )
        )

    try:
        with pytest.raises(
            RuntimeError,
            match="ix_character_relationships_analysis_target",
        ):
            migrations.run_migrations(engine)
        with engine.connect() as connection:
            recorded = connection.execute(
                text(
                    "SELECT COUNT(*) FROM schema_migrations "
                    "WHERE version = 5"
                )
            ).scalar_one()
    finally:
        engine.dispose()

    assert recorded == 0


def test_v5_rejects_obsolete_character_name_uniqueness() -> None:
    migrations = import_module("backend.app.core.migrations")
    engine = database.create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    _build_v4_database(engine)
    migrations.run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM schema_migrations WHERE version = 5")
        )
        connection.execute(
            text(
                """
                CREATE UNIQUE INDEX
                    obsolete_characters_analysis_canonical_name
                ON characters (analysis_id, canonical_name)
                """
            )
        )

    try:
        with pytest.raises(
            RuntimeError,
            match="不应强制.*人物名称唯一",
        ):
            migrations.run_migrations(engine)
        with engine.connect() as connection:
            recorded = connection.execute(
                text(
                    "SELECT COUNT(*) FROM schema_migrations "
                    "WHERE version = 5"
                )
            ).scalar_one()
    finally:
        engine.dispose()

    assert recorded == 0


def test_v5_rejects_conflicting_chapter_parent_key() -> None:
    migrations = import_module("backend.app.core.migrations")
    engine = database.create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    _build_v4_database(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE UNIQUE INDEX uq_chapters_novel_id_id
                ON chapters (id)
                """
            )
        )

    try:
        with pytest.raises(
            RuntimeError,
            match="uq_chapters_novel_id_id",
        ):
            migrations.run_migrations(engine)
        with engine.connect() as connection:
            recorded = connection.execute(
                text(
                    "SELECT COUNT(*) FROM schema_migrations "
                    "WHERE version = 5"
                )
            ).scalar_one()
    finally:
        engine.dispose()

    assert recorded == 0


def test_v5_rejects_conflicting_appearance_ownership_trigger() -> None:
    migrations = import_module("backend.app.core.migrations")
    engine = database.create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    _build_v4_database(engine)
    migrations.run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM schema_migrations WHERE version = 5")
        )
        connection.execute(
            text(
                "DROP TRIGGER "
                "trg_character_appearances_fill_ownership"
            )
        )
        connection.execute(
            text(
                """
                CREATE TRIGGER
                    trg_character_appearances_fill_ownership
                AFTER INSERT ON character_appearances
                BEGIN
                    SELECT 1;
                END
                """
            )
        )

    try:
        with pytest.raises(
            RuntimeError,
            match="trg_character_appearances_fill_ownership",
        ):
            migrations.run_migrations(engine)
        with engine.connect() as connection:
            recorded = connection.execute(
                text(
                    "SELECT COUNT(*) FROM schema_migrations "
                    "WHERE version = 5"
                )
            ).scalar_one()
    finally:
        engine.dispose()

    assert recorded == 0


def test_v5_rejects_wrong_named_undirected_canonical_check() -> None:
    migrations = import_module("backend.app.core.migrations")
    engine = database.create_db_engine(
        "sqlite://",
        poolclass=StaticPool,
    )
    _build_v4_database(engine)
    migrations.run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM schema_migrations WHERE version = 5")
        )
        connection.execute(text("DROP TABLE character_relationships"))
        connection.execute(
            text(
                """
                CREATE TABLE character_relationships (
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
                        CHECK (
                            source_character_id <> target_character_id
                        ),
                    CONSTRAINT ck_character_relationships_relation_type
                        CHECK (
                            length(trim(relation_type))
                            BETWEEN 1 AND 100
                        ),
                    CONSTRAINT ck_character_relationships_description
                        CHECK (length(trim(description)) > 0),
                    CONSTRAINT ck_character_relationships_evidence_nonempty
                        CHECK (
                            json_valid(evidence_chapter_indices)
                            AND json_type(
                                evidence_chapter_indices
                            ) = 'array'
                            AND json_array_length(
                                evidence_chapter_indices
                            ) > 0
                        ),
                    CONSTRAINT ck_character_relationships_undirected_canonical
                        CHECK (
                            directed = 1
                            OR source_character_id
                                <> target_character_id
                        )
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE INDEX
                    ix_character_relationships_analysis_target
                ON character_relationships (
                    analysis_id,
                    target_character_id
                )
                """
            )
        )

    try:
        with pytest.raises(
            RuntimeError,
            match=(
                "ck_character_relationships_undirected_canonical"
            ),
        ):
            migrations.run_migrations(engine)
        with engine.connect() as connection:
            recorded = connection.execute(
                text(
                    "SELECT COUNT(*) FROM schema_migrations "
                    "WHERE version = 5"
                )
            ).scalar_one()
    finally:
        engine.dispose()

    assert recorded == 0
