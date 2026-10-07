"""网页状态入版本账本，并用显式外键替代人物出场的占位归属触发器。"""
from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateTable, CreateIndex
from .validation import (
    _has_columns,
    _has_cascade_foreign_key,
)


def add_workspace_tables(connection):
    """兼容已经 create_all 的网页预览库；缺表创建，已有记录原样保留。"""
    from backend.app.models.workspace import WorkspaceSetting, ReadingState, Workflow
    for model in (WorkspaceSetting, ReadingState, Workflow):
        model.__table__.create(connection, checkfirst=True)
        _has_columns(connection, model.__tablename__, set(model.__table__.columns.keys()))
    for table in ("reading_states", "workflows"):
        if not _has_cascade_foreign_key(connection, table, "novel_id", "novels"):
            raise RuntimeError(f"{table} 缺少小说级联外键，请先修复结构。")
    index = next(iter(Workflow.__table__.indexes))
    index.create(connection, checkfirst=True)
    indexes = inspect(connection).get_indexes("workflows")
    existing = next((i for i in indexes if i["name"] == index.name), None)
    if not existing or not existing.get("unique") or existing.get("column_names") != ["novel_id"]:
        raise RuntimeError("workflows 活动任务唯一索引无效。")


def explicit_appearance_ownership(connection):
    """验证/补齐历史占位归属，原子重建表以移除 0 默认值和填充触发器。

    子表没有下游引用；复制完整字段后替换，保持所有记录 ID、外键、索引和检查。
    任一已有非零归属不匹配便拒绝升级，事务回滚，不猜测更正用户历史数据。
    """
    from backend.app.models.character import CharacterAppearance
    invalid = connection.execute(text("""
        SELECT a.id FROM character_appearances a
        LEFT JOIN characters c ON c.id = a.character_id
        LEFT JOIN character_analyses n ON n.id = c.analysis_id
        LEFT JOIN chapters h ON h.id = a.chapter_id
        WHERE c.id IS NULL OR n.id IS NULL OR h.id IS NULL
           OR h.novel_id != n.novel_id
           OR (a.analysis_id != 0 AND a.analysis_id != c.analysis_id)
           OR (a.novel_id != 0 AND a.novel_id != n.novel_id)
        LIMIT 1
    """)).first()
    if invalid:
        raise RuntimeError("人物出场存在不一致归属，请先修复数据。")
    table = CharacterAppearance.__table__
    ddl = str(CreateTable(table).compile(dialect=connection.dialect))
    ddl = ddl.replace("CREATE TABLE character_appearances", "CREATE TABLE character_appearances_upgrade", 1)
    connection.exec_driver_sql(ddl)
    connection.execute(text("""
        INSERT INTO character_appearances_upgrade
        (id, analysis_id, novel_id, character_id, chapter_id, mention_count, summary)
        SELECT a.id, c.analysis_id, n.novel_id, a.character_id, a.chapter_id, a.mention_count, a.summary
        FROM character_appearances a
        JOIN characters c ON c.id = a.character_id
        JOIN character_analyses n ON n.id = c.analysis_id
    """))
    connection.exec_driver_sql("DROP TRIGGER IF EXISTS trg_character_appearances_fill_ownership")
    connection.exec_driver_sql("DROP TABLE character_appearances")
    connection.exec_driver_sql("ALTER TABLE character_appearances_upgrade RENAME TO character_appearances")
    for index in table.indexes:
        connection.execute(CreateIndex(index))
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first():
        raise RuntimeError("迁移后外键检查失败，已回滚。")
