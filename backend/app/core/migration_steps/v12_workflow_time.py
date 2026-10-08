"""将历史任务链时间规范为与其他表一致的 UTC DateTime。"""

from datetime import datetime, timezone
from sqlalchemy import DateTime, inspect, text
from sqlalchemy.schema import CreateTable, CreateIndex


def workflow_datetime(connection):
    """保留任务记录与唯一索引；历史时间无法解析时回滚而不猜测。"""
    from backend.app.models.workspace import Workflow

    column = next(
        c
        for c in inspect(connection).get_columns("workflows")
        if c["name"] == "created_at"
    )
    if isinstance(column["type"], DateTime):
        return
    rows = list(connection.execute(text("SELECT * FROM workflows")).mappings())
    table = Workflow.__table__
    ddl = str(CreateTable(table).compile(dialect=connection.dialect)).replace(
        "CREATE TABLE workflows", "CREATE TABLE workflows_upgrade", 1
    )
    connection.exec_driver_sql(ddl)
    for row in rows:
        value = dict(row)
        timestamp = datetime.fromisoformat(value["created_at"])
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
        value["created_at"] = timestamp.isoformat(sep=" ", timespec="microseconds")
        connection.execute(
            text("""INSERT INTO workflows_upgrade
            (id, novel_id, kind, status, phase, question, error_message, created_at)
            VALUES (:id, :novel_id, :kind, :status, :phase, :question, :error_message, :created_at)"""),
            value,
        )
    connection.exec_driver_sql("DROP TABLE workflows")
    connection.exec_driver_sql("ALTER TABLE workflows_upgrade RENAME TO workflows")
    for index in table.indexes:
        connection.execute(CreateIndex(index))
