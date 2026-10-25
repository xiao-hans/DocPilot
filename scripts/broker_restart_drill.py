"""阶段 G2 broker 重启演练（TC-REC-011 的容器重启部分）。

用法：`PYTHONPATH=. .venv/Scripts/python.exe scripts/broker_restart_drill.py`
（`.env` 需含 RABBITMQ_USER/RABBITMQ_PASSWORD；`docker` CLI 只存在于本机 WSL 内，
脚本通过 `wsl.exe` 调用 `docker compose restart rabbitmq`。）

证明的命题：持久队列 + 持久消息 + publisher confirm 组合下，broker 进程整体重启
不会丢掉已确认投递但尚未消费的消息，也不会让数据库里的任务与派发状态漂移；重启后
租约仍可正常领取。它不证明异机灾备、不证明 broker 集群故障转移，也不给出 RPO/RTO。

判定：`DRILL RESULT: PASS` 需要同时满足"重启后队列深度=1""任务仍为 queued"
"重启后能领到租约""最终队列深度=0"。退出码 0 仅代表脚本跑完，结论看这一行。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

env_text = (BASE_DIR / ".env").read_text(encoding="utf-8") if (BASE_DIR / ".env").exists() else ""


def env_value(key: str) -> str:
    for line in env_text.splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f".env 缺少 {key}，参考 .env.example")


tmp = Path(tempfile.mkdtemp(prefix="docpilot-drill-"))
QUEUE = f"docpilot.drill.{uuid.uuid4().hex[:8]}"
os.environ.update(
    {
        "APP_ENV": "development",
        "DATABASE_URL": f"sqlite:///{(tmp / 'drill.sqlite3').as_posix()}",
        "DATA_DIR": str(tmp),
        "UPLOAD_DIR": str(tmp / "up"),
        "OUTPUT_DIR": str(tmp / "out"),
        "BOOTSTRAP_TOKEN": "drill",
        "EXTRACTOR_MODE": "mock",
        "TEST_DATABASE_URL": "",
        "BROKER_URL": f"amqp://{env_value('RABBITMQ_USER')}:{env_value('RABBITMQ_PASSWORD')}@127.0.0.1:5672/",
        "QUEUE_NAME": QUEUE,
    }
)

import pika  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.platform.config import BROKER_URL  # noqa: E402
from app.contexts.processing import dispatch as dispatch_store  # noqa: E402
from app.adapters.broker.amqp import AmqpTransport  # noqa: E402
from app.contexts.processing.dispatcher import Dispatcher  # noqa: E402
from app.platform.db import SessionLocal, initialize_development_schema  # noqa: E402
from app.models.account import Tenant  # noqa: E402
from app.models.dispatch import JobDispatch  # noqa: E402
from app.models.persistent_job import PersistentJob  # noqa: E402

initialize_development_schema()
now = datetime.now(timezone.utc)
tenant_id, job_id = uuid.uuid4().hex, uuid.uuid4().hex
with SessionLocal() as db:
    db.add(Tenant(id=tenant_id, name="演练空间", status="active", created_at=now))
    db.add(
        PersistentJob(
            id=job_id,
            tenant_id=tenant_id,
            status="queued",
            created_at=now,
            attempt_count=0,
        )
    )
    row = dispatch_store.create_pending(db, tenant_id=tenant_id, job_id=job_id, attempt_no=1)
    dispatch_id = row.id
    db.commit()


def queue_depth() -> int:
    connection = pika.BlockingConnection(pika.URLParameters(BROKER_URL))
    try:
        return int(connection.channel().queue_declare(queue=QUEUE, passive=True).method.message_count)
    finally:
        connection.close()


def db_state() -> tuple[str, str]:
    with SessionLocal() as db:
        return (
            db.get(PersistentJob, job_id).status,
            db.get(JobDispatch, dispatch_id).status,
        )


transport = AmqpTransport(queue=QUEUE)
counters = Dispatcher(transport).run_once()
transport.close()
before_depth, (before_job, before_dispatch) = queue_depth(), db_state()
print(f"1) 派发 {counters}；重启前：队列={before_depth}，任务={before_job}，派发行={before_dispatch}")

restart = subprocess.run(
    [
        "wsl.exe", "-d", "Ubuntu-24.04", "--", "bash", "-lc",
        "cd /mnt/d/workspace/DocPilot && docker compose restart rabbitmq",
    ],
    capture_output=True,
    text=True,
)
print(f"2) docker compose restart rabbitmq 退出码={restart.returncode}")

depth = None
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    try:
        depth = queue_depth()
        break
    except Exception:
        time.sleep(2)
if depth is None:
    raise SystemExit("broker 重启后 60 秒内仍不可用")

after_job, after_dispatch = db_state()
print(f"3) 重启后：队列={depth}，任务={after_job}，派发行={after_dispatch}")

token = dispatch_store.claim_lease(dispatch_id, tenant_id=tenant_id, job_id=job_id, attempt_no=1)
print(f"4) 重启后领取租约：{'成功' if token else '失败'}")

connection = pika.BlockingConnection(pika.URLParameters(BROKER_URL))
channel = connection.channel()
method, _properties, body = channel.basic_get(queue=QUEUE, auto_ack=False)
print(f"5) 取回在途消息：{body!r}")
channel.basic_ack(delivery_tag=method.delivery_tag)
connection.close()

with SessionLocal() as db:
    db.execute(
        JobDispatch.__table__.update()
        .where(JobDispatch.id == dispatch_id)
        .values(status="settled", lease_token=None, lease_expires_at=None)
    )
    db.commit()
final_depth = queue_depth()
print(f"6) 收口后：队列={final_depth}")

passed = depth == 1 and bool(token) and after_job == "queued" and after_dispatch == "published" and final_depth == 0
print("DRILL RESULT:", "PASS" if passed else "CHECK")
