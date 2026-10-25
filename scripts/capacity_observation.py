"""阶段 G6 合成数据容量观测（TC-PERF-001 / TC-PERF-002 / TC-PERF-003）。

用法：
    PYTHONPATH=. .venv/Scripts/python.exe scripts/capacity_observation.py \
        --tenants 100 --docs-per-tenant 10 --jobs 100 --workers 8 --large-mb 18

三段测量：
  A 读路径时延与租户隔离：写入合成租户与文档后，逐租户跑任务列表 / 文档库分页 /
    复核队列 / 关键词检索，记录每种查询的 P50/P95/max 与错误率，并检查响应里是否
    出现别的租户的唯一标记。
  B 并发调度：把 N 个任务留在"待派发"状态，由真实派发器投递到 RabbitMQ，用 W 个独立
    Worker 进程消费，测量队列等待、吞吐、失败率与"结果是否与数据库权威状态一致"。
  C 大对象 / 分页 / 导出：写入一个大对象，分别按流式分块读与整对象读比对内存增长，
    深翻页与宽任务导出同样记录 RSS 变化。

证明的命题：在**合成数据 + mock 提取器**下，这些读路径与调度链路的时延分布、内存形状
和一致性结果是多少——形成可重复的测量基线。
不证明：真实 OCR/LLM 的处理耗时与成本、生产容量、SLA。性能阈值尚未评审（`docs/17 §9`），
因此报告只给数字，不写"支持 100 并发 / 1TB"。本地不生成真实 1TB 数据。

数据库受命名守卫保护：目标库名必须匹配 `<名称>_test` / `test_<名称>`（`app/db.assert_disposable_database`），
脚本会清空并重建其 `public` schema。上传对象、导出文件与 Worker 日志都落在 `data/capacity/<run>/`。
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

VENV_PY = BASE_DIR / ".venv" / "Scripts" / "python.exe"
_ENV_TEXT = (BASE_DIR / ".env").read_text(encoding="utf-8") if (BASE_DIR / ".env").exists() else ""


def env_value(key: str) -> str:
    for line in _ENV_TEXT.splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f".env 缺少 {key}，参考 .env.example")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DocPilot 阶段 G6 合成数据容量观测")
    parser.add_argument("--dialect", choices=("postgres", "sqlite"), default="postgres")
    parser.add_argument(
        "--expect-database",
        default="docpilot_test",
        help="PG 模式下允许被清空重建的库名，必须精确相等（通用 _test 形状守卫太宽）",
    )
    parser.add_argument("--tenants", type=int, default=100, help="合成租户数（TC-PERF-001）")
    parser.add_argument("--docs-per-tenant", type=int, default=10, help="每租户文档数")
    parser.add_argument("--query-rounds", type=int, default=3, help="每租户每种读路径重复轮数")
    parser.add_argument("--jobs", type=int, default=100, help="并发调度任务数（TC-PERF-002）")
    parser.add_argument("--workers", type=int, default=8, help="独立 Worker 进程数")
    parser.add_argument("--drain-timeout", type=int, default=900, help="等待队列排空的最长秒数")
    parser.add_argument("--large-mb", type=int, default=18, help="大对象体积（TC-PERF-003）")
    parser.add_argument("--wide-docs", type=int, default=1000, help="宽任务的文档数（同时充当深翻页的语料）")
    parser.add_argument("--keep-data", action="store_true", help="保留 data/capacity/<run> 目录")
    parser.add_argument(
        "--report",
        default=str(BASE_DIR / "docs" / "18-合成数据容量观测报告.md"),
        help="Markdown 报告输出路径",
    )
    return parser.parse_args()


ARGS = parse_args()
RUN_ID = uuid.uuid4().hex[:8]
RUN_DIR = BASE_DIR / "data" / "capacity" / RUN_ID
QUEUE_NAME = f"docpilot.capacity.{RUN_ID}"
RUN_STARTED_AT = datetime.now(timezone.utc)

if ARGS.dialect == "postgres":
    DATABASE_URL = env_value("TEST_DATABASE_URL")
else:
    DATABASE_URL = f"sqlite:///{(RUN_DIR / 'capacity_test.sqlite3').as_posix()}"

os.environ.update(
    {
        "APP_ENV": "development",
        "AUTH_COOKIE_SECURE": "false",
        "BOOTSTRAP_TOKEN": f"capacity-{RUN_ID}",
        "BROKER_URL": f"amqp://{env_value('RABBITMQ_USER')}:{env_value('RABBITMQ_PASSWORD')}@127.0.0.1:5672/",
        "DATA_DIR": str(RUN_DIR),
        "DATABASE_URL": DATABASE_URL,
        "EXTRACTOR_MODE": "mock",
        "MAX_UPLOAD_MB": str(max(ARGS.large_mb + 8, 32)),
        "OUTPUT_DIR": str(RUN_DIR / "outputs"),
        "QUEUE_NAME": QUEUE_NAME,
        "STORAGE_BACKEND": "local",
        "TEST_DATABASE_URL": "",
        "UPLOAD_DIR": str(RUN_DIR / "uploads"),
        "WORKER_PREFETCH": "2",
    }
)

import pymupdf  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from sqlalchemy import func, make_url, select  # noqa: E402

from app.platform.config import DISPATCH_POLL_SECONDS, JOB_MAX_ATTEMPTS, JOB_LEASE_SECONDS, MAX_UPLOAD_MB  # noqa: E402
from app.contexts.documents import service as documents  # noqa: E402
from app.contexts.processing import dispatch as dispatch_store  # noqa: E402
from app.contexts.processing import service as processing  # noqa: E402
from app.adapters.broker.amqp import AmqpTransport, consumer_count  # noqa: E402
from app.contexts.processing.dispatcher import Dispatcher  # noqa: E402
from app.contexts.processing.domain import JobAttempt  # noqa: E402
from app.contexts.review import service as review  # noqa: E402
from app.contexts.review.domain import ExtractionResult  # noqa: E402
from app.contexts.search import service as search  # noqa: E402
from app.platform.db import SessionLocal, assert_disposable_database, engine, initialize_development_schema  # noqa: E402
from app.models.account import Membership, Tenant, User  # noqa: E402
from app.models.dispatch import JobDispatch  # noqa: E402
from app.models.document import Document, DocumentVersion  # noqa: E402
from app.models.job import JobStatus, Progress  # noqa: E402
from app.models.persistent_job import PersistentJob  # noqa: E402
from app.models.schema import INVOICE_TEMPLATE, FieldDefinition  # noqa: E402
from app.adapters.exports.csv import build as csv_build  # noqa: E402
from app.adapters.exports.excel import ExcelBuilder  # noqa: E402
from app.contexts.processing.manager import get_job_manager  # noqa: E402
from app.adapters.storage.object_storage import build_object_storage  # noqa: E402
from scripts.capacity_leak_probe import find_leaks  # noqa: E402
from scripts.capacity_thresholds import evaluate as evaluate_thresholds  # noqa: E402
from scripts.capacity_thresholds import render_section as render_threshold_section  # noqa: E402

FIELDS = [FieldDefinition(**item) for item in INVOICE_TEMPLATE]
TERMINAL = ("succeeded", "partial", "failed")


def log(message: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {message}", flush=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def percentile(values: list[float], point: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = min(len(ordered) - 1, max(0, int(round((point / 100.0) * (len(ordered) - 1)))))
    return ordered[position]


class ProcessMemoryCounters(ctypes.Structure):
    """PROCESS_MEMORY_COUNTERS。类必须只定义一次：ctypes 按类对象身份校验指针类型。"""

    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("page_fault_count", ctypes.c_uint32),
        ("peak_working_set", ctypes.c_size_t),
        ("working_set", ctypes.c_size_t),
        ("quota_peak_paged", ctypes.c_size_t),
        ("quota_paged", ctypes.c_size_t),
        ("quota_peak_nonpaged", ctypes.c_size_t),
        ("quota_nonpaged", ctypes.c_size_t),
        ("pagefile_usage", ctypes.c_size_t),
        ("peak_pagefile_usage", ctypes.c_size_t),
    ]


_RSS: object | None = None  # 解析一次：(函数, 进程句柄)，False 表示这台机器取不到


def process_rss_bytes() -> int | None:
    """当前进程工作集（字节）。Windows 走 psapi/kernel32，POSIX 走 getrusage；拿不到就返回 None。"""
    global _RSS
    if sys.platform == "win32":
        if _RSS is None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            found = False
            # K32GetProcessMemoryInfo 在 Vista+ 由 kernel32 导出；psapi.dll 只留了旧名。
            for dll_name in ("kernel32", "psapi"):
                dll = ctypes.WinDLL(dll_name, use_last_error=True)
                for symbol in ("K32GetProcessMemoryInfo", "GetProcessMemoryInfo"):
                    try:
                        function = getattr(dll, symbol)
                    except AttributeError:
                        continue
                    # argtypes 必须显式声明：进程句柄是 -1 这样的伪句柄，
                    # 不声明会被 ctypes 当 32 位 int 截断而抛 OverflowError。
                    function.argtypes = [
                        ctypes.c_void_p,
                        ctypes.POINTER(ProcessMemoryCounters),
                        ctypes.c_size_t,
                    ]
                    function.restype = ctypes.c_int
                    found = (function, kernel32.GetCurrentProcess())
                    break
                if found:
                    break
            _RSS = found
        if not _RSS:
            return None
        function, process_handle = _RSS
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not function(process_handle, ctypes.byref(counters), counters.cb):
            return None
        return int(counters.working_set)
    import resource

    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024


def mb(value: float | None) -> str:
    return "—" if value is None else f"{value / 1024 / 1024:.1f} MB"


def make_pdf(path: Path, marker: str, extra_pages: int = 0) -> int:
    """生成含可检索文本的小 PDF（mock 提取器能命中默认字段）。"""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), invoice_text(marker))
    doc.save(path)
    doc.close()
    return path.stat().st_size


def noise_bmp(width: int, height: int) -> bytes:
    """未压缩 BMP + 随机像素：PDF 里的"扫描件页面"就该是不可压缩的图像数据。"""
    import struct

    rows = height * width * 3
    header = struct.pack("<2sIHHI", b"BM", 54 + rows, 0, 0, 54)
    dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, rows, 2835, 2835, 0, 0)
    return header + dib + os.urandom(rows)


def invoice_text(marker: str) -> str:
    return "\n".join(
        [
            "增值税普通发票",
            f"发票号码: {marker}",
            "开票日期: 2025-01-12",
            "销售方: 恒信达科技有限公司",
            "购买方: 测试购买方有限公司",
            "价税合计(大写): 壹仟贰佰捌拾元伍角整",
            "价税合计(小写): ¥1280.50元",
            "税额: ¥76.83元",
        ]
    )


def write_scanned_pdf(path: Path, marker: str, image_pages: int) -> int:
    """首页是可检索的发票文本，其余页是随机噪声图像——大对象的体积由图像页决定。"""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), invoice_text(marker))
    for _ in range(image_pages):
        target = doc.new_page()
        target.insert_image(pymupdf.Rect(0, 0, 300, 300), stream=noise_bmp(400, 400))
    doc.save(path)
    doc.close()
    return path.stat().st_size


def make_large_pdf(path: Path, marker: str, target_bytes: int) -> int:
    """按目标体积生成扫描式 PDF。

    填充必须是**不可压缩**内容：用重复文本时 deflate 会把 21 页压到 9.5 KB，
    于是 18 MB 目标要写四万多个页面，观测脚本反而先被自己的夹具拖死。
    """
    base = write_scanned_pdf(path, marker, 0)
    per_page = max(1, write_scanned_pdf(path, marker, 1) - base)
    pages = max(1, int((target_bytes - base) / per_page) + 1)
    size = write_scanned_pdf(path, marker, pages)
    for _ in range(3):
        if size >= target_bytes:
            break
        pages += max(1, int((target_bytes - size) / per_page) + 1)
        size = write_scanned_pdf(path, marker, pages)
    return size


def reset_schema() -> str:
    if DATABASE_URL.startswith("sqlite"):
        # SQLite 的"库名"在 URL 里是完整路径，命名守卫的正则不适用于它。这里改用更强的
        # 构造性保证：文件必须位于本次运行新建的 data/capacity/<run>/ 下、且之前不存在。
        database = make_url(DATABASE_URL).database or ""
        path = Path(database).resolve()
        if RUN_DIR.resolve() not in path.parents or path.exists():
            raise RuntimeError(f"拒绝在非本次运行新建的 SQLite 文件上跑观测：{path}")
        initialize_development_schema()
        return path.name
    database = assert_disposable_database(DATABASE_URL)
    # 通用守卫只要求名字形如 *_test / test_*，`staging_test` 这类库也能通过；本脚本会
    # DROP SCHEMA public CASCADE，因此这里再加一道精确白名单，宁可让人显式改参数也不能误清。
    if database != ARGS.expect_database:
        raise RuntimeError(
            f"观测脚本只会清空 {ARGS.expect_database!r}，当前目标库是 {database!r}。"
            "确实要换库请显式传 --expect-database。"
        )
    with engine.begin() as connection:
        connection.execute(sa.text("DROP SCHEMA public CASCADE"))
        connection.execute(sa.text("CREATE SCHEMA public"))
    initialize_development_schema()
    return database


def seed_tenants(count: int) -> list[dict[str, str]]:
    tenants: list[dict[str, str]] = []
    now = utc_now()
    with SessionLocal() as db:
        for index in range(count):
            tenant_id = uuid.uuid4().hex
            user_id = uuid.uuid4().hex
            # 20 位且全局唯一的"发票号码"：既是合成数据的业务字段，也是跨租户泄漏的探针。
            marker = f"9{index:019d}"
            db.add(Tenant(id=tenant_id, name=f"容量租户 {index:03d}", status="active", created_at=now))
            db.add(
                User(
                    id=user_id,
                    email=f"capacity-{index:03d}-{user_id[:8]}@example.com",
                    password_hash="capacity-observation-not-a-password",
                    is_active=True,
                    is_platform_admin=False,
                    created_at=now,
                )
            )
            db.add(
                Membership(
                    id=uuid.uuid4().hex,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    role="owner",
                    status="active",
                    created_at=now,
                )
            )
            tenants.append({"tenant_id": tenant_id, "user_id": user_id, "marker": marker})
        db.commit()
    return tenants


def upload_job(
    tenant: dict[str, str],
    *,
    status: JobStatus,
    files: list[tuple[str, Path]],
) -> str:
    """按上传接口的同一条链路落库：对象写入 → Job → Document/DocumentVersion → 同事务派发意图。"""
    manager = get_job_manager()
    job = manager.create_job(
        files=[(name, "") for name, _path in files],
        fields=FIELDS,
        tenant_id=tenant["tenant_id"],
        owner_id=tenant["user_id"],
        persist=False,
    )
    job.status = status
    # 快照里的进度要与状态一致：预置为"已完成"的合成任务不该在界面上显示成排队中。
    job.progress = Progress.DONE if status is JobStatus.SUCCEEDED else Progress.QUEUED
    storage = build_object_storage()
    documents_: list[Document] = []
    versions: list[DocumentVersion] = []
    events: list[dict[str, object]] = []
    for (filename, path), finfo in zip(files, job.files, strict=True):
        content = path.read_bytes()
        suffix = Path(filename).suffix.lower()
        object_key = f"{tenant['tenant_id']}/{job.id}/{uuid.uuid4().hex}{suffix}"
        with path.open("rb") as handle:
            storage.put(object_key, handle, content_type="application/pdf")
        finfo.storage_key = object_key
        now = utc_now()
        document = Document(
            id=uuid.uuid4().hex,
            tenant_id=tenant["tenant_id"],
            owner_id=tenant["user_id"],
            status="active",
            created_at=now,
        )
        version = DocumentVersion(
            id=uuid.uuid4().hex,
            document_id=document.id,
            tenant_id=tenant["tenant_id"],
            job_id=job.id,
            revision=1,
            original_filename=filename,
            storage_key=object_key,
            checksum=hashlib.sha256(content).hexdigest(),
            media_type="application/pdf",
            size_bytes=len(content),
            created_at=now,
        )
        documents_.append(document)
        versions.append(version)
        events.append(
            {
                "tenant_id": tenant["tenant_id"],
                "event_type": "document.uploaded",
                "aggregate_type": "document_version",
                "aggregate_id": version.id,
                "payload": {"document_id": document.id, "job_id": job.id, "size_bytes": version.size_bytes},
            }
        )
    events.append(
        {
            "tenant_id": tenant["tenant_id"],
            "event_type": "job.queued",
            "aggregate_type": "job",
            "aggregate_id": job.id,
            "payload": {"status": job.status.value, "file_count": len(versions)},
        }
    )
    manager.save_with_documents(job, documents_, versions, events=events)
    return job.id


def record_results(job_id: str, tenant: dict[str, str], files: list[tuple[str, Path]]) -> None:
    """为已完成任务补写规范化结果（解析原文 + 提取值 + 检索索引），与 Worker 的落库形状一致。"""
    with SessionLocal() as db:
        for filename, _path in files:
            review.record_machine_result(
                db,
                tenant_id=tenant["tenant_id"],
                job_id=job_id,
                filename=filename,
                text=(
                    "增值税普通发票\n"
                    f"发票号码: {tenant['marker']}\n"
                    "开票日期: 2025-01-12\n"
                    "销售方: 恒信达科技有限公司\n"
                    "购买方: 测试购买方有限公司\n"
                    "价税合计(小写): ¥1280.50元\n"
                ),
                fields=[field.model_dump() for field in FIELDS],
                values={
                    "发票号码": tenant["marker"],
                    "开票日期": "2025-01-12",
                    "销售方名称": "恒信达科技有限公司",
                    "购买方名称": "测试购买方有限公司",
                    "价税合计": 1280.5,
                    "税额": 76.83,
                },
                warnings=[],
            )
        # record_machine_result 自己不提交（处理链路在同一事务里收口），脚本必须自己提交，
        # 否则 Session 退出即回滚，"已播种结果"会变成一句假话。
        db.commit()


def phase_reads(tenants: list[dict[str, str]], rounds: int) -> dict[str, object]:
    samples: dict[str, list[float]] = {
        "list_jobs": [],
        "list_documents": [],
        "list_documents_deep_page": [],
        "review_queue": [],
        "search_hit": [],
        "search_miss": [],
    }
    errors = 0
    empty_hits = 0
    leaks: list[str] = []
    owner_of = {tenant["marker"]: tenant["tenant_id"] for tenant in tenants}
    with SessionLocal() as db:
        for round_no in range(rounds):
            for index, tenant in enumerate(tenants):
                tenant_id = tenant["tenant_id"]
                probes = [
                    ("list_jobs", lambda: processing.list_jobs(db, tenant_id=tenant_id, limit=50)),
                    ("list_documents", lambda: documents.list_documents(db, tenant_id=tenant_id, limit=50)),
                    (
                        "list_documents_deep_page",
                        lambda: documents.list_documents(
                            db, tenant_id=tenant_id, limit=50, offset=max(0, ARGS.docs_per_tenant - 50)
                        ),
                    ),
                    ("review_queue", lambda: review.list_results(db, tenant_id=tenant_id, limit=50)),
                    ("search_hit", lambda: search.search(db, tenant_id=tenant_id, q=tenant["marker"], limit=20)),
                    ("search_miss", lambda: search.search(db, tenant_id=tenant_id, q=f"zz{index}nope", limit=20)),
                ]
                for name, probe in probes:
                    started = time.perf_counter()
                    try:
                        payload = probe()
                    except Exception as exc:  # 观测脚本必须把失败也记成数据
                        errors += 1
                        leaks.append(f"{name}:{type(exc).__name__}")
                        continue
                    samples[name].append((time.perf_counter() - started) * 1000.0)
                    if name == "search_hit" and not payload.get("total"):
                        # 检索命中 0 条时，时延数字测的是空索引，不能当作读路径观测。
                        empty_hits += 1
                    text = json.dumps(payload, ensure_ascii=False, default=str)
                    # 泄漏探针：响应里出现任何别的租户的唯一标记都算失败。
                    # 判定在 scripts/capacity_leak_probe.py：只认已播种的标记，
                    # 否则校验和里的一段数字会被读成越权。
                    leaks += find_leaks(text, owner_of, tenant_id, name)
            log(f"读路径轮 {round_no + 1}/{rounds} 完成（{len(samples['search_hit'])} 次检索）")
    latency = {
        name: {
            "count": len(values),
            "p50": round(percentile(values, 50), 1),
            "p95": round(percentile(values, 95), 1),
            "max": round(max(values), 1) if values else 0.0,
        }
        for name, values in samples.items()
        if values
    }
    total = sum(len(values) for values in samples.values())
    return {
        "latency_ms": latency,
        "queries": total,
        "errors": errors,
        "empty_search_hits": empty_hits,
        "leaks": leaks[:10],
    }


def dispatcher_loop(stop: threading.Event, counters: dict[str, int]) -> None:
    transport = AmqpTransport()
    dispatcher = Dispatcher(transport)
    try:
        while not stop.is_set():
            result = dispatcher.run_once()
            for key, value in result.items():
                counters[key] = counters.get(key, 0) + int(value)
            if not any(result.values()):
                time.sleep(min(1.0, DISPATCH_POLL_SECONDS))
    finally:
        transport.close()


def phase_queue(tenants: list[dict[str, str]], job_count: int, worker_count: int, timeout: int) -> dict[str, object]:
    """并发调度观测：先起消费者、再造任务、最后才派发。

    顺序反过来时"队列等待"会被 Worker 进程导入应用的冷启动时间吃掉，量到的不是
    稳态调度延迟；冷启动本身单独测一项。
    """
    scratch = RUN_DIR / "queue-fixtures"
    scratch.mkdir(parents=True, exist_ok=True)
    logs_dir = RUN_DIR / "workers"
    logs_dir.mkdir(parents=True, exist_ok=True)

    workers: list[subprocess.Popen[bytes]] = []
    log_handles: list[object] = []
    stop = threading.Event()
    dispatch_counters: dict[str, int] = {}
    dispatcher_thread: threading.Thread | None = None

    def teardown() -> None:
        """无论正常收尾还是中途抛错都要走到这里。

        泄漏的 Worker 会继续从观测队列取消息并往观测库里写结果，下一个用例读到的
        就是被污染的状态；日志句柄不关则在 Windows 上锁住文件，跑完也删不掉。
        """
        stop.set()
        if dispatcher_thread is not None:
            dispatcher_thread.join(timeout=15)
        for worker in workers:
            worker.terminate()
        for worker in workers:
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
        for handle in log_handles:
            handle.close()

    spawn_started = time.monotonic()
    for index in range(worker_count):
        handle = (logs_dir / f"worker-{index:02d}.log").open("wb")
        log_handles.append(handle)
        workers.append(
            subprocess.Popen(
                [str(VENV_PY), "-m", "app.worker"],
                cwd=str(BASE_DIR),
                env=dict(os.environ, PYTHONPATH=str(BASE_DIR)),
                stdout=handle,
                stderr=subprocess.STDOUT,
            )
        )
    log(f"启动 {worker_count} 个 Worker 进程")
    try:
        consumers = 0
        while time.monotonic() < spawn_started + 120:
            consumers = consumer_count(QUEUE_NAME) or 0
            if consumers >= worker_count:
                break
            time.sleep(0.5)
        consumer_startup_seconds = round(time.monotonic() - spawn_started, 1)
        log(f"消费者到位：{consumers}/{worker_count}（{consumer_startup_seconds} 秒）")

        created: list[tuple[str, str]] = []
        submit_started = time.monotonic()
        for index in range(job_count):
            tenant = tenants[index % len(tenants)]
            path = scratch / f"job-{index:04d}.pdf"
            make_pdf(path, tenant["marker"])
            job_id = upload_job(tenant, status=JobStatus.QUEUED, files=[(path.name, path)])
            created.append((job_id, tenant["tenant_id"]))
        submit_seconds = round(time.monotonic() - submit_started, 1)
        log(f"已创建 {len(created)} 个待派发任务（{submit_seconds} 秒）")

        dispatcher_thread = threading.Thread(
            target=dispatcher_loop, args=(stop, dispatch_counters), daemon=True
        )
        dispatcher_thread.start()

        started_at = utc_now()
        job_ids = [job_id for job_id, _tenant in created]
        timeline: list[dict[str, object]] = []
        terminal_count = 0
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with SessionLocal() as db:
                rows = dict(
                    db.execute(
                        select(PersistentJob.status, func.count())
                        .where(PersistentJob.id.in_(job_ids))
                        .group_by(PersistentJob.status)
                    ).all()
                )
            terminal_count = sum(value for status, value in rows.items() if status in TERMINAL)
            backlog = dispatch_store.queue_backlog()
            timeline.append(
                {
                    "seconds": round((utc_now() - started_at).total_seconds(), 1),
                    "terminal": terminal_count,
                    "pending": backlog["pending"],
                    "oldest_pending_seconds": backlog["oldest_pending_seconds"],
                    "consumers": consumer_count(QUEUE_NAME),
                }
            )
            if terminal_count == len(job_ids):
                break
            time.sleep(1.0)
        wall = (utc_now() - started_at).total_seconds()
    finally:
        teardown()

    # 到这里子进程已全部回收，再读库统计才不会把"还在消费"算进结果
    with SessionLocal() as db:
        statuses = dict(
            db.execute(
                select(PersistentJob.status, func.count())
                .where(PersistentJob.id.in_(job_ids))
                .group_by(PersistentJob.status)
            ).all()
        )
        waits = db.execute(
            select(JobDispatch.created_at, JobDispatch.publish_confirmed_at, JobAttempt.started_at)
            .join(
                JobAttempt,
                sa.and_(
                    JobAttempt.job_id == JobDispatch.job_id,
                    # 按 attempt_no 配对：只按 job_id 连接时，重试过的任务会把第 2 次派发
                    # 的创建时间和第 1 次的开始时间拼在一起，队列等待被算成负数。
                    JobAttempt.attempt_no == JobDispatch.attempt_no,
                ),
            )
            .where(JobDispatch.job_id.in_(job_ids))
        ).all()
        durations = db.execute(
            select(JobAttempt.started_at, JobAttempt.finished_at).where(JobAttempt.job_id.in_(job_ids))
        ).all()
        attempts = int(db.scalar(select(func.count()).select_from(JobAttempt).where(JobAttempt.job_id.in_(job_ids))) or 0)
        result_jobs = set(
            db.scalars(
                select(ExtractionResult.job_id).where(ExtractionResult.job_id.in_(job_ids)).distinct()
            ).all()
        )
        unresolved = int(
            db.scalar(
                select(func.count())
                .select_from(JobDispatch)
                .where(JobDispatch.job_id.in_(job_ids), JobDispatch.status.notin_(("settled",)))
            )
            or 0
        )
    # 两个口径必须分开：从"派发行创建"起算的等待包含任务创建循环本身（本轮 7.3 秒），
    # 从"发布确认"起算的才是调度延迟。只报前者会把建任务的耗时读成队列积压。
    queue_waits = [(as_utc(last) - as_utc(c)).total_seconds() * 1000.0 for c, _p, last in waits if last]
    schedule_delays = [
        (as_utc(last) - as_utc(published)).total_seconds() * 1000.0
        for _c, published, last in waits
        if last and published
    ]
    run_times = [(as_utc(f) - as_utc(s)).total_seconds() for s, f in durations if f]
    missing = [job_id for job_id, _tenant in created if job_id not in result_jobs]
    return {
        "jobs": len(job_ids),
        "workers": worker_count,
        "consumers_ready": consumers,
        "consumer_startup_seconds": consumer_startup_seconds,
        "creation_seconds": submit_seconds,
        "wall_seconds": round(wall, 1),
        "throughput_per_minute": round(len(job_ids) / wall * 60.0, 1) if wall else 0.0,
        "statuses": statuses,
        "failed": sum(value for status, value in statuses.items() if status == "failed"),
        "unfinished": len(job_ids) - terminal_count,
        "dispatch_counters": dispatch_counters,
        "queue_wait_ms": {
            "p50": round(percentile(queue_waits, 50), 1),
            "p95": round(percentile(queue_waits, 95), 1),
            "max": round(max(queue_waits), 1) if queue_waits else 0.0,
        },
        "schedule_delay_ms": {
            "p50": round(percentile(schedule_delays, 50), 1),
            "p95": round(percentile(schedule_delays, 95), 1),
            "max": round(max(schedule_delays), 1) if schedule_delays else 0.0,
        },
        "attempt_seconds": {
            "p50": round(percentile(run_times, 50), 3),
            "p95": round(percentile(run_times, 95), 3),
            "max": round(max(run_times), 3) if run_times else 0.0,
        },
        "attempts": attempts,
        "unresolved_dispatch_rows": unresolved,
        "jobs_without_results": missing[:10],
        "timeline": timeline[:: max(1, len(timeline) // 20)],
    }


def phase_large(tenant: dict[str, str]) -> dict[str, object]:
    storage = build_object_storage()
    scratch = RUN_DIR / "large"
    scratch.mkdir(parents=True, exist_ok=True)
    path = scratch / "large.pdf"
    size = make_large_pdf(path, tenant["marker"], ARGS.large_mb * 1024 * 1024)
    log(f"大对象已生成：{size / 1024 / 1024:.1f} MB")
    key = f"{tenant['tenant_id']}/capacity-large/{uuid.uuid4().hex}.pdf"

    baseline = process_rss_bytes()
    started = time.perf_counter()
    with path.open("rb") as handle:
        storage.put(key, handle, content_type="application/pdf")
    put_seconds = time.perf_counter() - started
    after_put = process_rss_bytes()

    started = time.perf_counter()
    with storage.local_path(key) as local:
        materialized = local.stat().st_size
    local_path_seconds = time.perf_counter() - started
    after_local_path = process_rss_bytes()

    started = time.perf_counter()
    streamed = 0
    with storage.open_read(key) as handle:
        while chunk := handle.read(1024 * 1024):
            streamed += len(chunk)
    stream_seconds = time.perf_counter() - started
    after_stream = process_rss_bytes()

    started = time.perf_counter()
    whole_object = storage.read_bytes(key)
    # 必须在还持有整对象时采样：先算长度再释放，测到的永远是回收之后的水位。
    after_read_bytes = process_rss_bytes()
    whole = len(whole_object)
    del whole_object
    read_bytes_seconds = time.perf_counter() - started

    # 深翻页要有"单租户大量文档"这个前提：先造出宽任务，再在它之上翻页与导出。
    wide_files: list[tuple[str, Path]] = []
    for index in range(ARGS.wide_docs):
        wide_path = scratch / f"wide-{index:04d}.pdf"
        make_pdf(wide_path, tenant["marker"])
        wide_files.append((wide_path.name, wide_path))
    export_baseline = process_rss_bytes()
    started = time.perf_counter()
    wide_job = upload_job(tenant, status=JobStatus.SUCCEEDED, files=wide_files)
    record_results(wide_job, tenant, wide_files)
    upload_wall = time.perf_counter() - started
    # 写入侧与读取侧的内存必须分开归因：混在一起时同一个指标能在两次运行里报出
    # 7.5 MB 和 86.8 MB，读者会以为导出路径变了，其实变的是前面那 1000 次写入。
    after_prepare = process_rss_bytes()

    page_latencies: dict[str, float] = {}
    with SessionLocal() as db:
        total_docs = int(
            db.scalar(select(func.count()).select_from(Document).where(Document.tenant_id == tenant["tenant_id"])) or 0
        )
        for label, offset in (("first", 0), ("middle", total_docs // 2), ("last", max(0, total_docs - 50))):
            started = time.perf_counter()
            documents.list_documents(db, tenant_id=tenant["tenant_id"], limit=50, offset=offset)
            page_latencies[label] = round((time.perf_counter() - started) * 1000.0, 1)

    # 与 /export 完全同一条路径：先取快照重建的 Job，再用规范化结果替换 result。
    with SessionLocal() as db:
        rows = review.export_rows(db, tenant_id=tenant["tenant_id"], job_id=wide_job)
    snapshot_job = get_job_manager().get_job(wide_job, tenant["tenant_id"])
    view = replace(snapshot_job, result=rows)
    after_rows = process_rss_bytes()
    started = time.perf_counter()
    xlsx = RUN_DIR / "wide.xlsx"
    ExcelBuilder().build(view, xlsx)
    xlsx_seconds = time.perf_counter() - started
    after_xlsx = process_rss_bytes()
    started = time.perf_counter()
    csv_path = RUN_DIR / "wide.csv"
    csv_build(view, csv_path)
    csv_seconds = time.perf_counter() - started
    after_csv = process_rss_bytes()

    storage.delete(key)
    storage.delete_prefix(f"{tenant['tenant_id']}/capacity-large")

    def delta(before: int | None, after: int | None) -> float | None:
        if before is None or after is None:
            return None
        return float(after - before)

    measured = {
        "large_object_mb": round(size / 1024 / 1024, 1),
        "max_upload_mb_limit": MAX_UPLOAD_MB,
        "put_seconds": round(put_seconds, 2),
        "put_rss_delta_mb": round((delta(baseline, after_put) or 0) / 1024 / 1024, 1),
        "local_path_seconds": round(local_path_seconds, 2),
        "local_path_rss_delta_mb": round((delta(after_put, after_local_path) or 0) / 1024 / 1024, 1),
        "local_path_materialized_mb": round(materialized / 1024 / 1024, 1),
        "stream_read_seconds": round(stream_seconds, 2),
        "stream_read_mb": round(streamed / 1024 / 1024, 1),
        "stream_rss_delta_mb": round((delta(after_local_path, after_stream) or 0) / 1024 / 1024, 1),
        "read_bytes_seconds": round(read_bytes_seconds, 2),
        "read_bytes_mb": round(whole / 1024 / 1024, 1),
        "read_bytes_rss_delta_mb": round((delta(after_stream, after_read_bytes) or 0) / 1024 / 1024, 1),
        "rss_peak_mb": round((process_rss_bytes() or 0) / 1024 / 1024, 1),
        "deep_pages_ms": page_latencies,
        "documents_in_tenant": total_docs,
        "wide_job": {
            "documents": len(wide_files),
            "upload_seconds": round(upload_wall, 2),
            "prepare_rss_delta_mb": round((delta(export_baseline, after_prepare) or 0) / 1024 / 1024, 1),
            "export_rows_rss_delta_mb": round((delta(after_prepare, after_rows) or 0) / 1024 / 1024, 1),
            "xlsx_seconds": round(xlsx_seconds, 2),
            "xlsx_rss_delta_mb": round((delta(after_rows, after_xlsx) or 0) / 1024 / 1024, 1),
            "xlsx_bytes": xlsx.stat().st_size,
            "csv_seconds": round(csv_seconds, 2),
            "csv_rss_delta_mb": round((delta(after_xlsx, after_csv) or 0) / 1024 / 1024, 1),
        },
    }
    # 只有差值真能分辨时才报"证实"，否则如实写成"本轮未能从 RSS 分辨"。
    measured["read_bytes_verdict"] = (
        f"- 本轮实测：整对象读使 RSS 增加 {measured['read_bytes_rss_delta_mb']} MB"
        f"（对象 {measured['read_bytes_mb']} MB），与上面的代码事实一致。"
        if abs(measured["read_bytes_rss_delta_mb"]) >= 2.0
        else f"- 本轮实测：整对象读 {measured['read_bytes_mb']} MB 后 RSS 变化仅 "
        f"{measured['read_bytes_rss_delta_mb']} MB，**没能从 RSS 上分辨出线性增长**"
        "（差值受分配器回收与采样粒度影响），这条只能算未证实的观察。"
    )
    return measured


def disk_usage() -> dict[str, object]:
    """本次运行留下的字节数，以及数据库自身报告的大小。"""
    run_bytes = sum(path.stat().st_size for path in RUN_DIR.rglob("*") if path.is_file())
    with SessionLocal() as db:
        if DATABASE_URL.startswith("sqlite"):
            database_bytes = Path(make_url(DATABASE_URL).database or "").stat().st_size
            note = "SQLite 文件大小"
        else:
            database_bytes = int(db.scalar(sa.text("SELECT pg_database_size(current_database())")) or 0)
            note = "pg_database_size(current_database())"
    return {
        "run_bytes": run_bytes,
        "database_bytes": database_bytes,
        "database_size_note": note,
    }


def write_report(payload: dict[str, object]) -> Path:
    reads = payload["reads"]
    queue = payload["queue"]
    large = payload["large"]
    counts = payload["counts"]
    seed_seconds = payload["seed_seconds"]
    disk = payload["disk"]
    lines = [
        "# 阶段 G6 合成数据容量观测报告",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 生成时间（UTC） | {RUN_STARTED_AT.strftime('%Y-%m-%d %H:%M:%S')} |",
        f"| 运行 ID | `{RUN_ID}` |",
        f"| 数据库 | {payload['database_dialect']}（一次性库 `{payload['database_name']}`，脚本已清空重建） |",
        f"| 队列 | RabbitMQ 队列 `{QUEUE_NAME}`，{queue['workers']} 个独立 Worker 进程 |",
        f"| 提取器 | `EXTRACTOR_MODE=mock`（**不含 OCR/LLM 真实耗时**） |",
        f"| 对象存储 | `STORAGE_BACKEND=local`（本轮不测 S3 端点） |",
        f"| 运行参数 | 租户 {ARGS.tenants} × 文档 {ARGS.docs_per_tenant}、调度任务 {queue['jobs']}、大对象 {large['large_object_mb']} MB、宽任务 {large['wide_job']['documents']} 文件 |",
        f"| 主机 | {payload['host']} |",
        f"| 磁盘占用 | 观测数据目录 {mb(disk['run_bytes'])}；数据库 {mb(disk['database_bytes'])}（{disk['database_size_note']}） |",
        "",
        "> 这是一份**测量基线**，不是容量结论。`docs/19 §1.2` 的链路阈值已由负责人批准（2026-10-07，`docs/19 §6`），"
        "判定见下面的「链路阈值判定」一节；**处理阈值（真实 OCR/LLM 的单任务耗时、吞吐上限、持续时间、文档构成、"
        "CPU/内存限额）仍未设定**，因此本报告仍不得被引用为\u201c支持 100 并发 / 1TB\u201d；本地也没有写入真实 1TB 数据。",
        "",
        "## A. 读路径时延与租户隔离（TC-PERF-001）",
        "",
        f"合成规模：{ARGS.tenants} 个租户、{counts['documents']} 份文档、{counts['results']} 条规范化结果"
        f"（播种耗时 {seed_seconds} 秒）；"
        f"每租户每种查询 {ARGS.query_rounds} 轮，共 {reads['queries']} 次查询，失败 {reads['errors']} 次。",
        "",
        "| 查询 | 次数 | P50 (ms) | P95 (ms) | max (ms) |",
        "|---|---|---|---|---|",
    ]
    for name, values in reads["latency_ms"].items():
        lines.append(
            f"| `{name}` | {values['count']} | {values['p50']} | {values['p95']} | {values['max']} |"
        )
    lines += [
        "",
        f"跨租户泄漏探针：每个租户持有全局唯一的 20 位\u201c发票号码\u201d，检查所有响应体里是否出现别的租户的标记——"
        f"结果 {'未发现泄漏' if not reads['leaks'] else '发现 ' + str(len(reads['leaks'])) + ' 处：' + str(reads['leaks'])}。",
        "",
        "## B. 并发调度与恢复（TC-PERF-002）",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 任务数 / Worker 数 | {queue['jobs']} / {queue['workers']} |",
        f"| Worker 冷启动（进程 spawn → {queue['workers']} 个消费者到位） | {queue['consumer_startup_seconds']} 秒（实测到位 {queue['consumers_ready']}） |",
        f"| 任务创建（含对象写入与同事务派发意图） | {queue['creation_seconds']} 秒 |",
        f"| 排空总耗时 | {queue['wall_seconds']} 秒 |",
        f"| 吞吐 | {queue['throughput_per_minute']} 任务/分钟 |",
        f"| 排队等待 P50 / P95 / max（派发行创建 → 开始执行，**含任务创建循环**） | {queue['queue_wait_ms']['p50']} / {queue['queue_wait_ms']['p95']} / {queue['queue_wait_ms']['max']} 毫秒 |",
        f"| 调度延迟 P50 / P95 / max（发布确认 → 开始执行，稳态口径） | {queue['schedule_delay_ms']['p50']} / {queue['schedule_delay_ms']['p95']} / {queue['schedule_delay_ms']['max']} 毫秒 |",
        f"| 单次执行 P50 / P95 / max | {queue['attempt_seconds']['p50']} / {queue['attempt_seconds']['p95']} / {queue['attempt_seconds']['max']} 秒 |",
        f"| 尝试次数合计 | {queue['attempts']}（任务数 {queue['jobs']}，重试上限 {JOB_MAX_ATTEMPTS}，租约 {JOB_LEASE_SECONDS} 秒） |",
        f"| 终态分布 | {json.dumps(queue['statuses'], ensure_ascii=False)} |",
        f"| 失败 / 未收敛 | {queue['failed']} / {queue['unfinished']} |",
        f"| 未收口的派发行 | {queue['unresolved_dispatch_rows']} |",
        f"| 有终态但没有规范化结果的任务 | {len(queue['jobs_without_results'])} {queue['jobs_without_results']} |",
        f"| 派发器计数 | {json.dumps(queue['dispatch_counters'], ensure_ascii=False)} |",
        "",
        "一致性判定：任务终态与 `extraction_results` 必须同源——上表\u201c有终态但没有规范化结果\u201d为 0 才算通过。",
        "",
        "队列深度随时间变化（每秒采样，抽稀展示）：",
        "",
        "| 秒 | 已终态 | 待派发 | 最老等待(秒) | 消费者 |",
        "|---|---|---|---|---|",
    ]
    for row in queue["timeline"]:
        lines.append(
            f"| {row['seconds']} | {row['terminal']} | {row['pending']} | "
            f"{row['oldest_pending_seconds'] if row['oldest_pending_seconds'] is not None else '—'} | {row['consumers']} |"
        )
    lines += [
        "",
        "读这两组数字时必须带着两条口径说明，否则会把测量读成结论：",
        "",
        "- 时间线里的「最老等待」取自 `queue_backlog()`，它按派发行**最后更新时间**算年龄；"
        "派发器投递成功会刷新这一列，所以它量的是「最久没有任何动作」，不是「最久的排队时间」——"
        "随后掉到亚秒级正是这个原因。真正的排队等待与调度延迟以上表两行为准"
        "（`job_dispatches.created_at / publish_confirmed_at → job_attempts.started_at`）。这个区别对 `/readyz` 的判据没有影响"
        "（它要的就是「有没有卡住不动」），但把它当排队指标用会低估积压。",
        f"- Worker 冷启动单独列了一行（{queue['consumer_startup_seconds']} 秒）：脚本先等消费者到位再造任务，"
        "所以上表的「调度延迟」不含进程导入应用的时间；「排队等待」那一行则包含任务创建循环，"
        "两者相差一个数量级，引用时必须说清是哪一个。真实部署里冷启动成本发生在扩容时，"
        "不该混进调度指标，也不该被忘掉。",
        "",
        "## C. 大对象、分页与导出（TC-PERF-003）",
        "",
        f"大对象 {large['large_object_mb']} MB（上传上限配置 `MAX_UPLOAD_MB={large['max_upload_mb_limit']}`）。"
        "RSS 变化按操作前后差值测量，负值表示期间发生回收。",
        "",
        "| 操作 | 耗时 (秒) | RSS 变化 |",
        "|---|---|---|",
        f"| 写入对象（流式 `put`） | {large['put_seconds']} | {large['put_rss_delta_mb']} MB |",
        f"| 解析前的临时落盘（`local_path`，1 MB 分块） | {large['local_path_seconds']} | {large['local_path_rss_delta_mb']} MB（临时文件 {large['local_path_materialized_mb']} MB 在磁盘，不驻留内存） |",
        f"| 分块流式读（1 MB） | {large['stream_read_seconds']} | {large['stream_rss_delta_mb']} MB / 读了 {large['stream_read_mb']} MB |",
        f"| 整对象读（`read_bytes`） | {large['read_bytes_seconds']} | {large['read_bytes_rss_delta_mb']} MB / 读了 {large['read_bytes_mb']} MB |",
        "",
        f"深翻页（该租户 {large['documents_in_tenant']} 份文档，`limit=50`）："
        f"首页 {large['deep_pages_ms']['first']} ms、中间 {large['deep_pages_ms']['middle']} ms、"
        f"尾页 {large['deep_pages_ms']['last']} ms。",
        "",
        f"宽任务导出（{large['wide_job']['documents']} 份文档、单任务）：上传+规范化结果 "
        f"{large['wide_job']['upload_seconds']} 秒、写入侧 RSS 变化 {large['wide_job']['prepare_rss_delta_mb']} MB；"
        f"读导出行并重建任务快照，RSS 变化 {large['wide_job']['export_rows_rss_delta_mb']} MB；"
        f"XLSX {large['wide_job']['xlsx_seconds']} 秒 / {large['wide_job']['xlsx_rss_delta_mb']} MB（{large['wide_job']['xlsx_bytes']} 字节）；"
        f"CSV {large['wide_job']['csv_seconds']} 秒 / {large['wide_job']['csv_rss_delta_mb']} MB。",
        "",
        "- 代码事实（不依赖本轮数字）：`read_bytes` 一次性返回整个对象，而解析链路用的 `local_path` 按 1 MB 分块落盘。"
        "因此**面向用户的预览/下载接口必须走流式读**，否则单个大对象就能把进程内存吃满。",
        large["read_bytes_verdict"],
        f"- 观测进程结束时 RSS 为 {large['rss_peak_mb']} MB。",
        "",
    ]
    lines += render_threshold_section(payload)
    lines += [
        "## 未覆盖（不得当作已验证）",
        "",
        "- 真实提取器（OCR/LLM）的耗时、失败率与外部服务配额；本轮 mock 提取器的执行时间不代表生产处理时间。",
        "- S3 后端下的同路径观测（本轮为 local 目录）。对象卷的备份恢复演练已由 G7 交付（`scripts/object_store_backup_drill.py`），但那验证的是「副本能否恢复并核对一致」，不等于 S3 后端下的应用读写路径已被观测。",
        "- 多机部署下的网络分区、broker 集群故障转移、跨机 Worker 时钟偏移。",
        "- 真实 1TB 数据量与 100 并发下的服务端资源竞争（本轮 Worker 与 API 同机、数据库同机）。",
        "- Worker 子进程自身的内存曲线（脚本只测观测进程；Worker 日志留在 `data/capacity/<run>/workers/`）。",
        "- CPU 占用与每秒查询数：观测进程、Worker 进程与数据库同机混跑，采样无法归因，因此本轮不报 CPU 数字。",
        "- 处理阈值判定：真实提取器（OCR/LLM）未接入，本轮 mock 数字不进入 `docs/19 §1.2` 的任何判定；"
        "\u201c链路阈值满足\u201d说的是调度与读路径这几条，不是\u201c处理性能已达标\u201d。",
        "",
    ]
    path = Path(ARGS.report)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    database_name = reset_schema()
    log(f"数据库 {database_name} 已重置（命名守卫通过）")

    tenants = seed_tenants(ARGS.tenants)
    log(f"已写入 {len(tenants)} 个合成租户")

    seeding_started = time.perf_counter()
    scratch = RUN_DIR / "fixtures"
    scratch.mkdir(parents=True, exist_ok=True)
    for index, tenant in enumerate(tenants):
        files: list[tuple[str, Path]] = []
        for doc_index in range(ARGS.docs_per_tenant):
            path = scratch / f"t{index:03d}-d{doc_index:02d}.pdf"
            make_pdf(path, tenant["marker"])
            files.append((path.name, path))
        job_id = upload_job(tenant, status=JobStatus.SUCCEEDED, files=files)
        record_results(job_id, tenant, files)
        if (index + 1) % 20 == 0:
            log(f"合成数据 {index + 1}/{len(tenants)} 租户（{time.perf_counter() - seeding_started:.0f} 秒）")
    seed_seconds = round(time.perf_counter() - seeding_started, 1)

    with SessionLocal() as db:
        counts = {
            "tenants": int(db.scalar(select(func.count()).select_from(Tenant)) or 0),
            "documents": int(db.scalar(select(func.count()).select_from(Document)) or 0),
            "versions": int(db.scalar(select(func.count()).select_from(DocumentVersion)) or 0),
            "results": int(db.scalar(select(func.count()).select_from(ExtractionResult)) or 0),
            "jobs": int(db.scalar(select(func.count()).select_from(PersistentJob)) or 0),
        }
    log(f"合成数据完成：{counts}，用时 {seed_seconds} 秒")

    reads = phase_reads(tenants, ARGS.query_rounds)
    log(
        f"读路径完成：{reads['queries']} 次查询、{reads['errors']} 次失败、"
        f"检索空命中 {reads['empty_search_hits']}、泄漏 {len(reads['leaks'])}"
    )

    queue = phase_queue(tenants, ARGS.jobs, ARGS.workers, ARGS.drain_timeout)
    log(
        f"并发调度完成：{queue['jobs']} 任务 / {queue['wall_seconds']} 秒 / "
        f"{queue['throughput_per_minute']} 任务每分钟，未收敛 {queue['unfinished']}"
    )

    large = phase_large(tenants[0])
    log(f"大对象与导出完成：{large['large_object_mb']} MB，宽任务 {large['wide_job']['documents']} 文件")

    disk = disk_usage()
    log(f"磁盘占用：观测目录 {mb(disk['run_bytes'])}、数据库 {mb(disk['database_bytes'])}")

    payload = {
        "run_id": RUN_ID,
        "started_at": RUN_STARTED_AT.isoformat(),
        "seed_seconds": seed_seconds,
        "counts": counts,
        "reads": reads,
        "queue": queue,
        "large": large,
        "disk": disk,
        "database_dialect": ARGS.dialect,
        "database_name": database_name,
        "host": " · ".join(
            part.strip()
            for part in subprocess.run(
                ["cmd.exe", "/c", "ver"] if os.name == "nt" else ["uname", "-srmo"],
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            if part.strip()
        ),
        "python": sys.version.split()[0],
        "args": vars(ARGS),
    }
    (RUN_DIR / "observation.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report = write_report(payload)
    log(f"报告写入 {report}")
    log(f"原始数据 {RUN_DIR / 'observation.json'}")

    passed = (
        reads["errors"] == 0
        and not reads["leaks"]
        and reads["empty_search_hits"] == 0
        and counts["results"] == counts["documents"]
        and queue["unfinished"] == 0
        and queue["failed"] == 0
        and not queue["jobs_without_results"]
        and queue["unresolved_dispatch_rows"] == 0
    )

    # 阈值判定进入退出码：一份"全绿但超阈值"的报告不能被当成通过。
    verdict = evaluate_thresholds(payload)
    for row in verdict["rows"]:
        log(f"阈值判定 | {row['阈值']} | {row['判定']} | {row['实测']}")
    passed = passed and not verdict["breaches"]
    if verdict["breaches"]:
        threshold_note = f"已批链路阈值不满足 {len(verdict['breaches'])} 项：" + "；".join(verdict["breaches"])
    else:
        threshold_note = "已批链路阈值全部满足"
    log(f"OBSERVATION RESULT: {'PASS' if passed else 'CHECK'}（{threshold_note}；处理阈值未设定，不参与判定）")

    import pika

    from app.platform.config import BROKER_URL

    try:
        connection = pika.BlockingConnection(pika.URLParameters(BROKER_URL))
        connection.channel().queue_delete(queue=QUEUE_NAME)
        connection.close()
    except Exception as exc:
        log(f"清理观测队列失败（不影响结论）：{type(exc).__name__}")
    if not ARGS.keep_data:
        import shutil

        shutil.rmtree(RUN_DIR / "fixtures", ignore_errors=True)
        shutil.rmtree(RUN_DIR / "queue-fixtures", ignore_errors=True)
        shutil.rmtree(RUN_DIR / "large", ignore_errors=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
