"""数据库引擎、会话和建表入口。

理解 SQLAlchemy 时先区分三个概念：``Engine`` 管理数据库连接；``Session``
表示一段可提交或回滚的工作单元；``Base`` 收集 Python 类到数据表的映射。
Web 请求不应共用同一个 Session。一次请求拿一个会话并在结束时关闭，才能避免
并发请求互相污染事务状态。
"""

from typing import Any, Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from backend.app.core.config import settings


def create_db_engine(database_url: str, **kwargs: Any) -> Engine:
    """创建引擎，并补齐 SQLite 在 Web 服务中的必要选项。

    ``check_same_thread=False`` 允许不同工作线程使用连接池；它不等于任意并发
    写入都安全，业务层仍需缩短事务并处理 ``database is locked``。
    """
    if database_url.startswith("sqlite"):
        # SQLite 默认不执行外键约束。忘记打开会让模型中的级联删除形同虚设。
        connect_args = dict(kwargs.pop("connect_args", {}))
        connect_args.setdefault("check_same_thread", False)
        kwargs["connect_args"] = connect_args

    db_engine = create_engine(database_url, **kwargs)

    if database_url.startswith("sqlite"):
        @event.listens_for(db_engine, "connect")
        def enable_sqlite_foreign_keys(
            dbapi_connection: Any,
            _: Any,
        ) -> None:
            """每条 DB-API 连接建立后开启 SQLite 外键约束。

            ``dbapi_connection`` 是底层 sqlite3 连接；第二个连接记录参数未使用，
            因此命名为 ``_``。事件回调没有业务返回值。
            """
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

    return db_engine


# 这三个对象在模块导入时只创建一次。业务代码不要为每个请求重建 Engine。
engine = create_db_engine(settings.database_url)
SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
)
Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    """通过依赖注入为一次请求提供会话，并保证最终关闭。

    ``yield`` 把会话交给路由；无论路由返回还是抛错，``finally`` 都会执行。
    提交/回滚由业务函数明确决定，避免依赖层暗中提交。
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(db_engine: Engine = engine) -> None:
    """加载全部模型、创建缺失表，再执行可重复运行的版本迁移。

    下列模型导入不能删除：导入会向 ``Base.metadata`` 注册表定义。默认参数允许
    测试传入隔离引擎而不触碰真实数据库。
    """
    from backend.app.models import analysis  # noqa: F401
    from backend.app.models import character  # noqa: F401
    from backend.app.models import novel  # noqa: F401
    from backend.app.models import rag  # noqa: F401
    from backend.app.models import report  # noqa: F401
    from backend.app.core.migrations import run_migrations

    Base.metadata.create_all(bind=db_engine)
    run_migrations(db_engine)
