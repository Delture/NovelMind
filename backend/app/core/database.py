
from typing import Any, Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, DeclarativeBase, sessionmaker

from backend.app.core.config import settings


def create_db_engine(database_url: str, **kwargs: Any) -> Engine:
    """check_same_thread=False 只允许工作线程使用连接池，并不代表并发写入安全；
    业务层仍需缩短事务并处理 database is locked。
    """
    if database_url.startswith("sqlite"):
        # SQLite 默认不执行外键约束。忘记打开会让模型中的级联删除形同虚设。
        connect_args = dict(kwargs.pop("connect_args", {}))
        connect_args.setdefault("check_same_thread", False)
        kwargs["connect_args"] = connect_args
        busy_timeout_ms = round(connect_args.get("timeout", 5.0) * 1000)

    db_engine = create_engine(database_url, **kwargs)

    if database_url.startswith("sqlite"):
        @event.listens_for(db_engine, "connect")
        def enable_sqlite_foreign_keys(
            dbapi_connection: Any,
            _: Any,
        ) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
                cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

    return db_engine


engine = create_db_engine(settings.database_url)
SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
)
class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    """提交/回滚由业务函数明确决定，依赖层不暗中提交。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(db_engine: Engine = engine) -> None:
    """下列模型导入不能删除：导入会向 Base.metadata 注册表定义。"""
    from backend.app.models import analysis  # noqa: F401
    from backend.app.models import character  # noqa: F401
    from backend.app.models import novel  # noqa: F401
    from backend.app.models import rag  # noqa: F401
    from backend.app.models import report  # noqa: F401
    from backend.app.models import workspace  # noqa: F401
    from backend.app.core.migrations import run_migrations

    Base.metadata.create_all(bind=db_engine, tables=[Base.metadata.tables["novels"], Base.metadata.tables["chapters"]])
    run_migrations(db_engine)
