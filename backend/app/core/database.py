"""Database connection and session helpers."""

from typing import Any, Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from backend.app.core.config import settings


def create_db_engine(database_url: str, **kwargs: Any) -> Engine:
    """Create an engine with safe SQLite defaults."""
    if database_url.startswith("sqlite"):
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
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

    return db_engine


engine = create_db_engine(settings.database_url)
SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
)
Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    """Provide one database session for a request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(db_engine: Engine = engine) -> None:
    """Upgrade the schema and create tables that do not exist yet."""
    from backend.app.models import novel  # noqa: F401
    from backend.app.core.migrations import run_migrations

    run_migrations(db_engine)
    Base.metadata.create_all(bind=db_engine)
