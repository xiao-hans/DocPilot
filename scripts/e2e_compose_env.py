"""阶段 G8/G13：在 Compose 依赖栈上为端到端用例准备并回收一次性环境。

用法：
    PYTHONPATH=. .venv/Scripts/python.exe scripts/e2e_compose_env.py prepare  --run <id> [--app-containers]
    PYTHONPATH=. .venv/Scripts/python.exe scripts/e2e_compose_env.py app-up   [--no-build]
    PYTHONPATH=. .venv/Scripts/python.exe scripts/e2e_compose_env.py app-down
    PYTHONPATH=. .venv/Scripts/python.exe scripts/e2e_compose_env.py teardown --run <id>

为什么需要它：G5 的端到端跑在一次性本地进程（独立 SQLite + 本机目录），因此"核心 UI 流程
能在 Compose 形态下跑通"这条一直没被证明（G-DEC-12）。这个脚本把 E2E 换到真实形态上：
PostgreSQL 一次性库 + RabbitMQ 队列 + SeaweedFS 对象存储。

两种形态：
- 默认（G8）：应用、派发器、Worker 仍是主机进程，只依赖 compose 提供的三件套。
- `--app-containers` + `app-up`（G13）：应用侧也进容器（compose 的 app profile），主机只剩 vite。
  这条形态下 prepare 不在主机跑迁移，`app-up` 之后 PostgreSQL 里出现的 alembic_version
  就只能来自 migrate 容器——这是"容器连的是这套库"的判据，不是"端口通了"。

prepare 把本次运行该用的环境变量打成一段 JSON 给 Node 侧消费（含 E2E 专用队列名与对象前缀，
因此不会和开发基线抢消息、也不会把测试对象写进开发前缀）。

teardown 只回收本次自己创建的两样东西：名字精确等于 `docpilot_e2e_test` 的库，以及
`e2e-<run>/` 前缀下的对象。删完必须重新查询确认，确认不了就退出码 1——G7 的教训是"发完
删除命令就声称已回收"会在下一次运行把自己 wedge 住。`app-down` 同理，且只删 app profile
那四个服务：`docker compose down` 会连 PostgreSQL/RabbitMQ/对象存储一起停，那三样是开发
基线和备份演练共用的。

不覆盖：跨机部署，真实提取器，镜像的构建产物审计。
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
from typing import Any

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

# compose 侧只碰 app profile 这四个服务；迁移是一次性的，其余三个常驻。
APP_SERVICES = ("migrate", "api", "dispatcher", "worker", "webhooks")
COMPOSE_DIR = "/mnt/d/workspace/DocPilot"
COMPOSE_ENV_REL = "data/e2e/compose-app.env"
COMPOSE_ENV_PATH = BASE_DIR / COMPOSE_ENV_REL
# `--env-file` 会替换默认的 .env，所以两个都要给：.env 提供 PostgreSQL/RabbitMQ 凭据，
# 容器 env 提供 app 侧地址，后者覆盖同名键。
ENV_FLAGS_HOST_ONLY = "--env-file .env"


def env_flags() -> str:
    both = f"{ENV_FLAGS_HOST_ONLY} --env-file {COMPOSE_ENV_REL}"
    return both if COMPOSE_ENV_PATH.exists() else ENV_FLAGS_HOST_ONLY
API_ORIGIN = "http://127.0.0.1:8000"


def wsl(command: str, timeout: int = 120) -> str:
    result = subprocess.run(
        WSL + [command], capture_output=True, text=True, encoding="utf-8", timeout=timeout
    )
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



def safe_env_value(name: str, value: str) -> str:
    """env_file 的解析很朴素：换行、行首 # 都会改变含义。宁可拒绝生成，也不产出一份
    "看起来成功、实际把密钥截断"的文件。"""
    if "\n" in value or "\r" in value:
        raise SystemExit(f"{name} 含换行，无法写进 compose env_file")
    if value.startswith("#"):
        raise SystemExit(f"{name} 以 # 开头，会被 env_file 当注释吃掉")
    return value


def write_compose_env(run: str, path: Path, bootstrap_token: str) -> dict[str, str]:
    """生成"容器视角"的 env 文件。

    主机侧脚本连的是 127.0.0.1 上发布的端口；容器在 compose 网络里连 127.0.0.1 只会连到自己，
    必须走服务名（postgres / rabbitmq / object-store）。两套地址不能混用，所以分开生成。
    """
    values = {
        # 用 production 而不是 development：development 会在 API 启动时隐式迁移，
        # 那样就测不到"迁移由一次性服务负责、API 只等它成功"这条既定形状（docs/17 §4）。
        "APP_ENV": "production",
        "APP_DATABASE_URL": (
            f"postgresql+psycopg://{require_env('POSTGRES_USER')}:{require_env('POSTGRES_PASSWORD')}"
            f"@postgres:5432/{E2E_DB}"
        ),
        "APP_BROKER_URL": (
            f"amqp://{require_env('RABBITMQ_USER')}:{require_env('RABBITMQ_PASSWORD')}@rabbitmq:5672/"
        ),
        "QUEUE_NAME": f"docpilot-e2e-{run}",
        "STORAGE_BACKEND": "s3",
        "APP_S3_ENDPOINT_URL": "http://object-store:8333",
        "S3_BUCKET": require_env("S3_BUCKET"),
        "S3_REGION": os.getenv("S3_REGION", "us-east-1").strip() or "us-east-1",
        "S3_ACCESS_KEY": require_env("S3_ACCESS_KEY"),
        "S3_SECRET_KEY": require_env("S3_SECRET_KEY"),
        "S3_KEY_PREFIX": f"e2e-{run}",
        "BOOTSTRAP_TOKEN": bootstrap_token,
        # 浏览器走 http 访问本机端口，Secure cookie 会被丢掉：登录看起来成功、下一个请求 401。
        "AUTH_COOKIE_SECURE": "false",
        "EXTRACTOR_MODE": "mock",
        # Webhook 投递进程没有主密钥会拒绝启动；这里给一把只在一次性环境里用的密钥，
        # 让 app-up 的形状核对能真的看到 webhooks 起来（端到端用例不会真发外联）。
        "APP_WEBHOOK_MASTER_KEY": "e2e-webhook-master-" + run,
    }
    lines = [f"{key}={safe_env_value(key, value)}" for key, value in values.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    # 必须用 LF：compose 的 env_file 解析不会剥掉行尾的 \r，`postgres\r` 这种主机会被
    # 原样送进容器，最后在 psycopg 里报一个看不出错在哪的失败。
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return values


def read_compose_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise SystemExit(f"容器 env 文件不存在：{path}（先跑 prepare --app-containers）")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        values[key.strip()] = value
    return values


def check_compose_env(values: dict[str, str]) -> None:
    """容器视角的地址必须是服务名，不能是主机的 127.0.0.1。

    这两套地址混用不会报错在配置上，而是表现为"容器连不上自己的依赖"，排查成本很高，
    所以生成之后立刻按形状回查一遍。
    """
    problems: list[str] = []
    for key in ("APP_DATABASE_URL", "APP_BROKER_URL", "APP_S3_ENDPOINT_URL", "BOOTSTRAP_TOKEN"):
        if not values.get(key, "").strip():
            problems.append(f"{key} 为空")
    for key in ("APP_DATABASE_URL", "APP_BROKER_URL", "APP_S3_ENDPOINT_URL"):
        if "127.0.0.1" in values.get(key, "") or "localhost" in values.get(key, ""):
            problems.append(f"{key} 指向主机地址，容器里连不到：{values[key]}")
    for key, host in (("APP_DATABASE_URL", "@postgres:"), ("APP_BROKER_URL", "@rabbitmq:")):
        if host not in values.get(key, ""):
            problems.append(f"{key} 没走 compose 服务名（应含 {host}）")
    if "e2e-" not in values.get("S3_KEY_PREFIX", ""):
        problems.append(f"S3_KEY_PREFIX 不是 E2E 前缀：{values.get('S3_KEY_PREFIX')!r}")
    if problems:
        raise SystemExit("容器 env 文件不可信：\n  - " + "\n  - ".join(problems))


def compose_app(*args: str, timeout: int = 300) -> str:
    """对 app profile 那四个服务下命令。cwd 必须显式给：`bash -lc` 起来在 root 家目录，
    不在仓库里，compose 会报"找不到配置文件"。"""
    command = (
        f"cd {COMPOSE_DIR} && docker compose {env_flags()} --profile app " + " ".join(args)
    )
    return wsl(command, timeout=timeout)


def app_service_rows() -> dict[str, tuple[str, str]]:
    """app profile 四个服务的 {service: (state, status)}。

    用 `docker compose ps` 而不是 `docker ps`：后者没有 `.Service` 字段，且要自己按 label
    过滤项目；compose 直接给出服务名，`-a` 才看得见已经退出的 migrate。
    """
    out = compose_app(
        "ps", "-a", "--format", "'{{.Service}}|{{.State}}|{{.Status}}'", timeout=120
    )
    rows: dict[str, tuple[str, str]] = {}
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) == 3:
            rows[parts[0]] = (parts[1], parts[2])
    return rows


def e2e_db_revision() -> str | None:
    """直接问 PostgreSQL：这个库里现在登记的 alembic 版本是什么。"""
    engine = sa.create_engine(e2e_url(), isolation_level="AUTOCOMMIT", future=True)
    try:
        with engine.connect() as conn:
            return conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:
        return None
    finally:
        engine.dispose()


def wait_readyz(timeout_seconds: int) -> dict[str, Any]:
    """等 API 承认"队列已启用且有消费者"，而不是"端口通了"。

    消费者数来自 Worker 容器对队列的声明，主机侧看不到容器内部状态，这是唯一能从外部
    证明"容器里的 Worker 真的在消费"的信号。
    """
    deadline = time.time() + timeout_seconds
    last = "未尝试"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(API_ORIGIN + "/readyz", timeout=3) as response:
                body = json.loads(response.read().decode("utf-8"))
                queue = body.get("queue") or {}
                if (
                    response.status == 200
                    and queue.get("enabled") is True
                    and (queue.get("consumers") or 0) >= 1
                ):
                    return body
                last = f"status={response.status} body={json.dumps(body, ensure_ascii=False)[:240]}"
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(2)
    raise SystemExit(f"/readyz 未在 {timeout_seconds}s 内确认队列有消费者：{last}")


def app_shape_problems(rows: dict[str, tuple[str, str]]) -> list[str]:
    """容器形态的期望形状：api/dispatcher/worker 都 running 且 API 健康检查已过，migrate 已退出。"""
    problems: list[str] = []
    for service in ("api", "dispatcher", "worker", "webhooks"):
        state, status = rows.get(service, ("missing", "missing"))
        if state != "running":
            problems.append(f"{service} state={state}")
        elif service == "api" and "(healthy)" not in status:
            # API 是唯一带 HEALTHCHECK 的服务：它说"进程在"不等于"能服务请求"。
            problems.append(f"api health 未就绪：{status}")
    state, status = rows.get("migrate", ("missing", "missing"))
    if state != "exited":
        problems.append(f"migrate state={state}（应该跑完就退出，status={status}）")
    return problems


def app_up(build: bool, ready_timeout: int) -> int:
    values = read_compose_env(COMPOSE_ENV_PATH)
    check_compose_env(values)
    print(f"[app-up] env 校验通过：queue={values['QUEUE_NAME']} prefix={values['S3_KEY_PREFIX']}", flush=True)

    # 先清掉上一轮的 app 容器：留着 exited(0) 的 migrate 容器，compose 认为依赖已满足、不会重跑，
    # "迁移由一次性容器负责"这条就没被走第二遍；跑第二次端到端时会看到假绿。
    compose_app("rm", "-sf", *APP_SERVICES, timeout=180)
    args = ("up", "-d", *(("--build",) if build else ()))
    print(f"[app-up] docker compose --profile app {' '.join(args)}", flush=True)
    compose_app(*args, timeout=1800)

    rows = app_service_rows()
    problems = app_shape_problems(rows)
    # 容器起来不等于健康检查过了：API 的 HEALTHCHECK 有自己的间隔与 start_period，
    # 所以这里按形状轮询，而不是抓一次快照就把"还在 starting"判成失败。
    deadline = time.time() + 180
    while problems and time.time() < deadline:
        time.sleep(5)
        rows = app_service_rows()
        problems = app_shape_problems(rows)
    if problems:
        logs = ""
        try:
            logs = compose_app("logs", "--tail", "40", *APP_SERVICES, timeout=120)
        except Exception as exc:
            logs = f"日志取不到：{type(exc).__name__}"
        hint = ""
        if not build:
            hint = (
                "提示：这次是 --no-build，复用已有 tag 意味着镜像里不会有本次新增的进程入口"
                "（例如新模块的 `python -m ...`）。去掉 --no-build 重建一次再判断。\n"
            )
        raise SystemExit(
            "app 容器形态不符合预期：" + "、".join(problems) + "\n" + hint + logs
        )

    revision = e2e_db_revision()
    if revision is None:
        raise SystemExit(
            "PostgreSQL 的 E2E 库里没有 alembic_version：migrate 容器连的不是这个库。\n"
            "最常见的原因是 APP_DATABASE_URL 没生效，应用回落到镜像里的 SQLite 默认值——"
            "那样栈也能起、用例也可能过，但测的已经不是 PostgreSQL 了。"
        )
    body = wait_readyz(ready_timeout)
    print(
        "[app-up] readyz consumers="
        f"{(body.get('queue') or {}).get('consumers')} revision={revision}",
        flush=True,
    )
    print(
        json.dumps(
            {
                "API_ORIGIN": API_ORIGIN,
                "REVISION": revision,
                "QUEUE": body.get("queue"),
                "CONTAINERS": {s: list(v) for s, v in rows.items()},
            },
            ensure_ascii=False,
        )
    )
    return 0


def app_down() -> int:
    """只回收 app profile 的四个服务，绝不用 `down`：那会连 PostgreSQL/RabbitMQ/对象存储一起停，
    而这三样是开发基线和备份演练共用的。"""
    problems: list[str] = []
    try:
        compose_app("rm", "-sf", *APP_SERVICES, timeout=300)
    except Exception as exc:
        problems.append(f"rm 失败：{type(exc).__name__}: {exc}")
    # 回查：删完不算完，G7/G8 的教训都是"发完删除命令就声称已回收"。
    left = {s: v for s, v in app_service_rows().items() if s in APP_SERVICES}
    if left:
        problems.append(
            "仍有 app 容器残留：" + "、".join(f"{s}({v[0]})" for s, v in left.items())
        )
    if problems:
        print("APP DOWN: RESIDUE")
        for line in problems:
            print(f"  - {line}")
        return 1
    print("APP DOWN: CLEAN")
    return 0


def prepare(run: str, bootstrap_token: str, app_containers: bool) -> int:
    assert_compose_healthy()
    reset_database()
    if not app_containers:
        # app 容器形态下故意不在主机上迁移：把库留在"未迁移"状态，交给 compose 里的一次性
        # migrate 容器。主机先迁移一次的话，后面"PostgreSQL 里出现 alembic_version"这条判据
        # 永远成立，等于什么都没验。
        migrate(e2e_url())
    _client, bucket, prefix = storage(run)
    write_compose_env(run, COMPOSE_ENV_PATH, bootstrap_token)

    payload = {
        "COMPOSE_ENV_FILE": str(COMPOSE_ENV_PATH),
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
        "MIGRATOR": "compose" if app_containers else "host",
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
    parser.add_argument("action", choices=("prepare", "teardown", "app-up", "app-down"))
    parser.add_argument("--run", default="")
    parser.add_argument("--bootstrap-token", default="e2e-platform-bootstrap-token")
    parser.add_argument(
        "--app-containers",
        action="store_true",
        help="容器形态：迁移留给 compose 里的 migrate 容器，不在主机跑 alembic",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help=(
            "app-up 复用已有镜像，不重新构建。"
            "只在「刚构建过镜像、且代码未变」时用：维护轮 M18 实测过误用代价——"
            "用 E2E_SKIP_BUILD=1 跑跨轮回归时，容器里是 5 天前的镜像，"
            "而当时的改动是当天加的，于是「容器形态失败」其实是测了旧代码。"
            "判断镜像是否新鲜：docker images docpilot-app:dev --format '{{.CreatedSince}}'"
        ),
    )
    parser.add_argument("--ready-timeout", type=int, default=180)
    args = parser.parse_args()

    if args.action in ("prepare", "teardown"):
        if not args.run or not args.run.replace("-", "").isalnum():
            raise SystemExit(f"--run 只允许字母数字和短横：{args.run!r}")
        if args.action == "prepare":
            return prepare(args.run, args.bootstrap_token, args.app_containers)
        return teardown(args.run)
    if args.action == "app-up":
        return app_up(build=not args.no_build, ready_timeout=args.ready_timeout)
    return app_down()


if __name__ == "__main__":
    raise SystemExit(main())
