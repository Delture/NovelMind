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

    assert {"novels", "chapters", "schema_migrations"} <= set(table_names)
    assert any(
        item.get("name") == "ux_chapters_novel_index"
        for item in indexes + constraints
    )
    assert versions == [(1, "add unique chapter index")]


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
                    FOREIGN KEY(novel_id) REFERENCES novels(id)
                        ON DELETE CASCADE
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
    assert version_count == 1


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
