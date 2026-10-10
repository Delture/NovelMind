"""SQLite DateTime 列保存无时区 UTC 值；集中转换避免混用本地时间。"""

from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
