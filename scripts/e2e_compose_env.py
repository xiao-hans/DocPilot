"""阶段 G8：在 Compose 依赖栈上为端到端用例准备并回收一次性环境。

用法：
    PYTHONPATH=. .venv/Scripts/python.exe scripts/e2e_compose_env.py prepare  --run <id>
    PYTHONPATH=. .venv/Scripts/python.exe scripts/e2e_compose_env.py teardown --run <id>

为什么需要它：G5 的端到端跑在一次性本地进程（独立 SQLite + 本机目录），因此"核心 UI 流程
能在 Compose 形态下跑通"这条一直没被证明（G-DEC-12）。这个脚本把 E2E 换到真实形态上：
PostgreSQL 一次性库 + RabbitMQ 队列 + SeaweedFS 对象存储，应用与 Worker 仍是主机进程。

prepare 把本次运行该用的环境变量打成一段 JSON 给 Node 侧消费（含 E2E 专用队列名与对象前缀，
因此不会和开发基线抢消息、也不会把测试对象写进开发前缀）。

teardown 只回收本次自己创建的两样东西：名字精确等于 `docpilot_e2e_test` 的库，以及
`e2e-<run>/` 前缀下的对象。删完必须重新查询确认，确认不了就退出码 1——G7 的教训是"发完
删除命令就声称已回收"会在下一次运行把自己 wedge 住。

不覆盖：应用与 Worker 的容器化（G-DEC-08 仍是待决），跨机部署，真实提取器。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

import boto3  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE_DIR / ".env")

E2E_DB = "docpilot_e2e_test"
HEALTHY_CONTAINERS = ("docpilot-postgres", "docpilot-rabbitmq", "docpilot-object-store")
WSL = ["wsl.exe", "-d", "Ubuntu-24.04", "-u", "root", "--", "bash", "-lc"]
VENV_PY = str(BASE_DIR / ".venv" / "Scripts" / "python.exe")


def wsl(command: str) -> str:
    result = subprocess.run(WSL + [command], capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(f"命令失败（{result.returncode}）：{command}\n{result.stderr.strip()}")
    return (result.stdout or "").strip()


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"缺少 {name}：端到端 Compose 形态需要它，见 .env.example")
    return value


def assert_compose_healthy() -> None:
    """依赖必须显式 healthy，而不是"端口能连上就算"。

    容器还在起的时候 SeaweedFS 会出现 head_bucket 通过、GET 大对象 IncompleteRead 的中间态
    （G7 实测），把它当成就绪会让用例随机失败且看不出原因。
    """
    unhealthy: list[str] = []
    for name in HEALTHY_CONTAINERS:
        try:
            state = wsl(f"docker inspect {name} --format '{{{{.State.Status}}}} {{{{if .State.Health}}}}{{{{.State.Health.Status}}}}{{{{else}}}}none{{{{end}}}}'")
        except RuntimeError:
            state = "missing"
        if "running healthy" not in state:
            unhealthy.append(f"{name}={state or 'unknown'}")
    if unhealthy:
        raise SystemExit(
            "Compose 依赖未全部 healthy，拒绝开始：" + "、".join(unhealthy)
            + "\n先跑 `wsl -d Ubuntu-24.04 -- bash -lc 'cd /mnt/d/workspace/DocPilot && docker compose up -d'`"
        )


def maintenance_url() -> str:
    return (
        f"postgresql+psycopg://{require_env('POSTGRES_USER')}:{require_env('POSTGRES_PASSWORD')}"
        f"@127.0.0.1:5432/{require_env('POSTGRES_DB')}"
    )


def e2e_url() -> str:
    return (
        f"postgresql+psycopg://{require_env('POSTGRES_USER')}:{require_env('POSTGRES_PASSWORD')}"
        f"@127.0.0.1:5432/{E2E_DB}"
    )


def database_names() -> list[str]:
    engine = sa.create_engine(maintenance_url(), isolation_level="AUTOCOMMIT", future=True)
    try:
        with engine.connect() as conn:
            return [row[0] for row in conn.execute(sa.text("SELECT datname FROM pg_database"))]
    finally:
        engine.dispose()


def reset_database() -> None:
    """只允许重建那一个精确名字的 E2E 库；名字不符就中止，绝不去碰开发库。"""
    if E2E_DB != "docpilot_e2e_test" or E2E_DB == require_env("POSTGRES_DB"):
        raise SystemExit(f"E2E 库名守卫失败：{E2E_DB!r}")
    engine = sa.create_engine(maintenance_url(), isolation_level="AUTOCOMMIT", future=True)
    try:
        with engine.connect() as conn:
            # WITH (FORCE) 是必需的：用例超时或进程没停干净时，残留连接会让 DROP 一直失败，
            # 下一次端到端就卡在这里而不是报出真实原因。
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{E2E_DB}" WITH (FORCE)'))
            conn.execute(sa.text(f'CREATE DATABASE "{E2E_DB}"'))
    finally:
        engine.dispose()
    if E2E_DB not in database_names():
        raise SystemExit(f"建库后查不到 {E2E_DB}，环境不可信")


def migrate(url: str) -> None:
    """迁移显式跑一次，不依赖 API lifespan 的隐式自动迁移（docs/17 §4）。"""
    env = {**os.environ, "DATABASE_URL": url, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [VENV_PY, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=BASE_DIR, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        raise SystemExit(f"迁移失败：\n{result.stdout}\n{result.stderr}")


def storage(run: str):
    bucket = require_env("S3_BUCKET")
    client = boto3.client(
        "s3",
        endpoint_url=require_env("S3_ENDPOINT_URL"),
        aws_access_key_id=require_env("S3_ACCESS_KEY"),
        aws_secret_access_key=require_env("S3_SECRET_KEY"),
        region_name=os.getenv("S3_REGION", "us-east-1").strip() or "us-east-1",
    )
    try:
        client.head_bucket(Bucket=bucket)
    except Exception as exc:
        raise SystemExit(f"对象桶 {bucket} 不可用（端到端不代为建桶）：{exc}")
    return client, bucket, f"e2e-{run}"


def delete_queue(run: str) -> str | None:
    """删掉本次的 E2E 队列。队列是 durable 的，不删就在开发 broker 上越积越多。

    走 management API 而不是再起一条 AMQP 连接；失败只报不拦，因为它不影响数据正确性。
    """
    name = f"docpilot-e2e-{run}"
    url = f"http://127.0.0.1:15672/api/queues/%2F/{name}"
    auth = "Basic " + base64.b64encode(
        f"{require_env('RABBITMQ_USER')}:{require_env('RABBITMQ_PASSWORD')}".encode()
    ).decode()

    def request(method: str) -> urllib.request.Request:
        # 回查也要带凭据：不带的话 management API 返 401，会被读成"队列没删掉"。
        return urllib.request.Request(url, method=method, headers={"Authorization": auth})

    try:
        with urllib.request.urlopen(request("DELETE"), timeout=5) as response:
            if response.status not in (200, 204):
                return f"队列 {name} 删除返回 {response.status}"
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None  # 没建过就算干净
        return f"队列 {name} 删除失败：HTTP {exc.code}"
    except Exception as exc:
        return f"队列 {name} 删除失败：{type(exc).__name__}（management API 可达吗？）"
    # 删完回查：G7 的教训是"发完删除命令就声称已回收"。
    try:
        with urllib.request.urlopen(request("GET"), timeout=5) as response:
            if response.status == 200:
                return f"队列 {name} 删除后仍存在"
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            return f"队列 {name} 回查失败：HTTP {exc.code}"
    except Exception as exc:
        return f"队列 {name} 回查失败：{type(exc).__name__}"
    return None


def prepare(run: str) -> int:
    assert_compose_healthy()
    reset_database()
    migrate(e2e_url())
    _client, bucket, prefix = storage(run)

    payload = {
        "RUN": run,
        "DATABASE_URL": e2e_url(),
        "E2E_DB": E2E_DB,
        "BROKER_URL": (
            f"amqp://{require_env('RABBITMQ_USER')}:{require_env('RABBITMQ_PASSWORD')}@127.0.0.1:5672/"
        ),
        "QUEUE_NAME": f"docpilot-e2e-{run}",
        "STORAGE_BACKEND": "s3",
        "S3_BUCKET": bucket,
        "S3_KEY_PREFIX": prefix,
        "E2E_OBJECT_PREFIX": f"{prefix}/",
    }
    print(json.dumps(payload))
    return 0


def teardown(run: str) -> int:
    problems: list[str] = []

    # 1) 库：只删这一个精确名字，删完回查。
    if E2E_DB != "docpilot_e2e_test":
        raise SystemExit(f"E2E 库名守卫失败：{E2E_DB!r}")
    try:
        engine = sa.create_engine(maintenance_url(), isolation_level="AUTOCOMMIT", future=True)
        try:
            with engine.connect() as conn:
                conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{E2E_DB}" WITH (FORCE)'))
        finally:
            engine.dispose()
        if E2E_DB in database_names():
            problems.append(f"库仍存在：{E2E_DB}")
    except Exception as exc:
        problems.append(f"库回收失败：{type(exc).__name__}: {exc}")

    # 2) 对象：只删本次前缀下的键，删完回查。
    try:
        client, bucket, prefix = storage(run)
        for _attempt in range(3):
            keys = [
                item["Key"]
                for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket)
                for item in page.get("Contents", [])
                if item["Key"].startswith(f"{prefix}/")
            ]
            if not keys:
                break
            for key in keys:
                client.delete_object(Bucket=bucket, Key=key)
            time.sleep(0.5)
        else:
            problems.append(f"对象前缀 {prefix}/ 仍未清空")
    except SystemExit as exc:
        problems.append(f"对象存储不可达：{exc}")
    except Exception as exc:
        problems.append(f"对象回收失败：{type(exc).__name__}: {exc}")

    queue_note = delete_queue(run)
    if queue_note:
        problems.append(queue_note)

    if problems:
        print("TEARDOWN RESULT: RESIDUE")
        for line in problems:
            print(f"  - {line}")
        return 1
    print("TEARDOWN RESULT: CLEAN")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "teardown"))
    parser.add_argument("--run", required=True)
    args = parser.parse_args()
    if not args.run.replace("-", "").isalnum():
        raise SystemExit(f"--run 只允许字母数字和短横：{args.run!r}")
    return prepare(args.run) if args.action == "prepare" else teardown(args.run)


if __name__ == "__main__":
    raise SystemExit(main())
