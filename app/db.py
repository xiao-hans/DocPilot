"""SQLAlchemy 数据库连接与请求级 Session。"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import BASE_DIR, DATABASE_URL


class Base(DeclarativeBase):
    """ORM 模型基类。"""


_url = make_url(DATABASE_URL)
if _url.drivername.startswith("sqlite") and _url.database not in (None, ":memory:"):
    Path(_url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

_engine_options: dict = {"pool_pre_ping": True}
if _url.drivername.startswith("sqlite"):
    _engine_options["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **_engine_options)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

if _url.drivername.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_db() -> Generator[Session, None, None]:
    """为一次 HTTP 请求提供并关闭数据库会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def initialize_development_schema() -> None:
    """开发环境启动时应用待执行迁移；其他环境由发布流程显式迁移。"""
    config = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    command.upgrade(config, "head")
