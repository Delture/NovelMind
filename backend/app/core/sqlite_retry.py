"""SQLite 写锁的唯一退避策略；事务回滚由传入操作负责。"""

import time
from collections.abc import Callable
from typing import TypeVar

from sqlalchemy.exc import OperationalError

T = TypeVar("T")
MAX_ATTEMPTS = 4
BASE_SECONDS = 0.05


def is_sqlite_lock_error(error: OperationalError) -> bool:
    message = str(getattr(error, "orig", error)).lower()
    return "database is locked" in message or "database table is locked" in message


def sleep_before_retry(attempt: int) -> None:
    time.sleep(BASE_SECONDS * 2 ** (attempt - 1))


def retry_sqlite(operation: Callable[[], T], *, sleep=sleep_before_retry) -> T:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return operation()
        except OperationalError as error:
            if not is_sqlite_lock_error(error) or attempt == MAX_ATTEMPTS:
                raise
            sleep(attempt)
    raise AssertionError("unreachable")
