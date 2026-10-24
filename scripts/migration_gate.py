"""阶段 G1 迁移门禁（docs/17 §6、TC-REC-008）。

对一次性 PostgreSQL 库执行：清空 schema → upgrade head → downgrade -1 → upgrade head → alembic check。
只允许指向名称含 test 的库，避免误清开发数据。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

BASE_DIR = Path(__file__).resolve().parent.parent
# 以 `python scripts/migration_gate.py` 启动时 sys.path[0] 是 scripts/，需要仓库根目录才能 import app。
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
load_dotenv(BASE_DIR / ".env")

DATABASE_URL = (
    os.getenv("MIGRATION_GATE_DATABASE_URL") or os.getenv("TEST_DATABASE_URL") or ""
).strip()
if not DATABASE_URL:
    sys.exit("缺少 MIGRATION_GATE_DATABASE_URL 或 TEST_DATABASE_URL（见 .env.example）")

# migrations/env.py 用 app.platform.config.DATABASE_URL 覆盖连接串，因此必须在导入应用配置前落定环境变量。
os.environ["DATABASE_URL"] = DATABASE_URL

from app.platform.db import assert_disposable_database  # noqa: E402

# 门禁会 DROP SCHEMA public 并执行 downgrade，库名不合规则直接退出。
GATE_DB = assert_disposable_database(DATABASE_URL)
print(f"gate: 目标库 {GATE_DB} 通过一次性库命名校验")

config = AlembicConfig(str(BASE_DIR / "alembic.ini"))
engine = create_engine(DATABASE_URL)

with engine.begin() as connection:
    connection.execute(text("DROP SCHEMA public CASCADE"))
    connection.execute(text("CREATE SCHEMA public"))
print("gate: schema 已重置为空库")

command.upgrade(config, "head")
print("gate: 空库 upgrade head 通过")

command.downgrade(config, "-1")
command.upgrade(config, "head")
print("gate: upgrade/downgrade/upgrade 往返通过")

command.check(config)
print("gate: alembic check 无模型漂移")
print("G1 迁移门禁通过")
