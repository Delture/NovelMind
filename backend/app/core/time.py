"""集中提供数据库使用的 UTC 当前时间。

SQLite 的传统 ``DateTime`` 列在本项目中保存无时区的 UTC 值。集中转换可以避免
各处混用本地时间和带时区时间；界面展示时再决定是否本地化。
"""

from datetime import datetime, timezone


def utc_now() -> datetime:
    """返回适合当前 SQLite 列的无时区 UTC 值，并避开已弃用的 utcnow。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)
