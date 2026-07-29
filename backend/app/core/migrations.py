"""Small, ordered SQLite schema migrations for local NovelMind databases."""

from typing import Callable

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine


MigrationFunction = Callable[[Connection], None]


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


MIGRATIONS: tuple[tuple[int, str, MigrationFunction], ...] = (
    (1, "add unique chapter index", _add_unique_chapter_index),
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
