"""全局配置：全部支持 .env / 环境变量覆盖。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 本文件在 `app/platform/`，比原先的 `app/` 深一层，所以仓库根要往上数三级。
# 用 `parents[2]` 而不是链式 `parent.parent`，避免再搬一层时又要数一遍。
BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")


def _env_int(name: str, default: int) -> int:
    """安全的整型环境变量读取：非法值回落默认值。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """安全的浮点环境变量读取：非法值回落默认值。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    """布尔环境变量：未设置用默认值，设置了就按真字符串判定（写错就当 false）。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ---- 目录 ----
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", BASE_DIR / "uploads"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", BASE_DIR / "outputs"))
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))

# ---- 持久化与运行环境 ----
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{(DATA_DIR / 'docpilot.sqlite3').as_posix()}",
)
BOOTSTRAP_TOKEN = os.getenv("BOOTSTRAP_TOKEN", "")
AUTH_COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "docpilot_session")
AUTH_COOKIE_SECURE = os.getenv(
    "AUTH_COOKIE_SECURE", "true" if APP_ENV == "production" else "false"
).strip().lower() in {"1", "true", "yes", "on"}
AUTH_SESSION_DAYS = _env_int("AUTH_SESSION_DAYS", 7)
# 邀请令牌有效期（天）：DOC-DD-003 §4.1，取值限制在 1-30，非法值回落默认。
INVITE_TTL_DAYS = min(max(_env_int("INVITE_TTL_DAYS", 7), 1), 30)

# ---- 处理可靠性：DOC-DD-004 §5 ----
JOB_MAX_ATTEMPTS = min(max(_env_int("JOB_MAX_ATTEMPTS", 3), 1), 10)
JOB_ATTEMPT_TIMEOUT_SECONDS = min(
    max(_env_int("JOB_ATTEMPT_TIMEOUT_SECONDS", 600), 30), 3600
)

# ---- 队列派发与执行租约：DOC-DD-008 §4（阶段 G2）----
# BROKER_URL 为空表示不启用队列：任务仍由 API 进程内执行，派发行只作为权威记录。
BROKER_URL = os.getenv("BROKER_URL", "").strip()
QUEUE_ENABLED = bool(BROKER_URL)
QUEUE_NAME = os.getenv("QUEUE_NAME", "docpilot.processing").strip() or "docpilot.processing"
# Worker 一次最多握有的未 ack 消息数；本地默认远低于 100 并发目标（docs/17 §4.2）。
WORKER_PREFETCH = min(max(_env_int("WORKER_PREFETCH", 4), 1), 100)
# 执行租约时长：到期未续租即视为 Worker 已失联，可由恢复器回收。
JOB_LEASE_SECONDS = min(max(_env_int("JOB_LEASE_SECONDS", 120), 10), 3600)
# 派发器每轮领取的 ready 记录数与空闲轮询间隔。
DISPATCH_BATCH_SIZE = min(max(_env_int("DISPATCH_BATCH_SIZE", 20), 1), 200)
DISPATCH_POLL_SECONDS = min(max(_env_int("DISPATCH_POLL_SECONDS", 2), 1), 60)
# 已发布但迟迟没人执行（消息丢失、broker 数据卷损毁）的派发行，超过该窗口退回 ready 重投。
DISPATCH_REPUBLISH_SECONDS = min(max(_env_int("DISPATCH_REPUBLISH_SECONDS", 300), 60), 86400)

# ---- 对象存储：DOC-DD-008 §5（阶段 G3）----
# local = 本机目录（现有开发基线）；s3 = S3 兼容端点，本机开发指向 SeaweedFS。
STORAGE_BACKEND = (os.getenv("STORAGE_BACKEND", "local").strip().lower() or "local")
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "").strip()
S3_BUCKET = os.getenv("S3_BUCKET", "").strip()
S3_REGION = os.getenv("S3_REGION", "us-east-1").strip() or "us-east-1"
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "")
S3_KEY_PREFIX = os.getenv("S3_KEY_PREFIX", "docpilot").strip("/")

# ---- 上传限额 ----
MAX_UPLOAD_MB = _env_int("MAX_UPLOAD_MB", 20)
MAX_FILES_PER_JOB = _env_int("MAX_FILES_PER_JOB", 10)
MAX_PDF_PAGES = _env_int("MAX_PDF_PAGES", 20)  # 超出的页截断并告警

# ---- 文件解析 ----
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}
# 单页抽取文本低于该字符数视为扫描页 → 转 OCR
MIN_TEXT_CHARS_PER_PAGE = _env_int("MIN_TEXT_CHARS_PER_PAGE", 30)
OCR_DPI = _env_int("OCR_DPI", 200)

# ---- 提取器 ----
# mock | llm
EXTRACTOR_MODE = os.getenv("EXTRACTOR_MODE", "mock")

# ---- 检索后端（NFR-004，docs/22）----
# 当前只有 sql（search_entries 表 + lower(content) LIKE）。未知值在
# `get_search_backend()` 里显式抛错，不静默退回默认实现。
SEARCH_BACKEND = (os.getenv("SEARCH_BACKEND", "sql").strip().lower() or "sql")

# OpenAI 兼容接口（DeepSeek / 千问 / OpenAI 等）
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.deepseek.com/v1")
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "deepseek-chat")
AI_TIMEOUT = _env_int("AI_TIMEOUT", 90)
AI_MAX_RETRIES = _env_int("AI_MAX_RETRIES", 2)
AI_TEMPERATURE = _env_float("AI_TEMPERATURE", 0.1)

# ---- 出站 Webhook（FR-041，DOC-DD-021 §9）----
# 主密钥为空即禁用出站投递：宁可"这件事没做"，也不退化成"用一个内置默认密钥签"，
# 那等于所有部署共用同一个可预测的签名密钥。
WEBHOOK_MASTER_KEY = os.getenv("WEBHOOK_MASTER_KEY", "").strip()
WEBHOOK_ENABLED = bool(WEBHOOK_MASTER_KEY)
WEBHOOK_TIMEOUT_SECONDS = min(max(_env_int("WEBHOOK_TIMEOUT_SECONDS", 5), 1), 60)
WEBHOOK_MAX_ATTEMPTS = min(max(_env_int("WEBHOOK_MAX_ATTEMPTS", 6), 1), 20)
WEBHOOK_BACKOFF_SECONDS = min(max(_env_int("WEBHOOK_BACKOFF_SECONDS", 15), 1), 3600)
WEBHOOK_BACKOFF_MAX_SECONDS = min(max(_env_int("WEBHOOK_BACKOFF_MAX_SECONDS", 1800), 1), 86400)
WEBHOOK_LEASE_SECONDS = min(max(_env_int("WEBHOOK_LEASE_SECONDS", 120), 10), 3600)
WEBHOOK_ROTATION_GRACE_SECONDS = min(max(_env_int("WEBHOOK_ROTATION_GRACE_SECONDS", 3600), 0), 86400)
WEBHOOK_POLL_SECONDS = min(max(_env_int("WEBHOOK_POLL_SECONDS", 3), 1), 300)
# 本机测试要打到 127.0.0.1，默认关：服务端主动外联的目标默认不能落在内网。
WEBHOOK_ALLOW_PRIVATE_TARGETS = _env_bool("WEBHOOK_ALLOW_PRIVATE_TARGETS", False)
WEBHOOK_ALLOW_INSECURE_TARGETS = _env_bool("WEBHOOK_ALLOW_INSECURE_TARGETS", False)
