"""SQLAlchemy 数据库连接与请求级 Session。"""

from __future__ import annotations

import re
from collections.abc import Generator
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.platform.config import BASE_DIR, DATABASE_URL

# 一次性库的名字必须是 `<前缀>_test` 或 `test_<后缀>`：子串判断会把 `latest`
# 这类正常库名误判为可丢弃，而清库与降级操作不可逆。
_DISPOSABLE_DB_NAME = re.compile(r"^(?:[\w.-]*_test|test_[\w.-]+)$")


def assert_disposable_database(database_url: str) -> str:
    """校验目标数据库可被清空，返回**库名**；不合规时抛出异常。

    SQLite 的 `database` 是文件路径而不是库名，直接拿整条路径去匹配名字形状会让守卫
    **对所有 SQLite 目标一律拒绝**（维护轮 M11 实测：`sqlite:///…/test_replay.sqlite3`
    被判不合规）。那是个假阴性、方向安全但让"在 SQLite 上演练"变得不可能，所以这里
    按方言取不同的名字来源：SQLite 取文件名去掉最后一个后缀，其余方言取 `database`。
    """
    url = make_url(database_url)
    raw = url.database or ""
    if url.drivername.startswith("sqlite"):
        # `:memory:` 与空串没有文件名可判：内存库不可能装着真实数据，但也没有
        # "名字形状"可核，按最小惊讶拒绝（fail-closed），由调用方显式换用文件库。
        raw = Path(raw).name
        if raw.endswith(".sqlite3"):
            raw = raw[: -len(".sqlite3")]
        elif raw.endswith(".db"):
            raw = raw[: -len(".db")]
    if not _DISPOSABLE_DB_NAME.match(raw):
        raise RuntimeError(
            f"数据库名 {raw!r} 不符合一次性库命名（<名称>_test 或 test_<名称>），拒绝清空。"
        )
    return raw


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
