"""全局配置：全部支持 .env / 环境变量覆盖。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
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


# ---- 目录 ----
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", BASE_DIR / "uploads"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", BASE_DIR / "outputs"))

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

# OpenAI 兼容接口（DeepSeek / 千问 / OpenAI 等）
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.deepseek.com/v1")
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "deepseek-chat")
AI_TIMEOUT = _env_int("AI_TIMEOUT", 90)
AI_MAX_RETRIES = _env_int("AI_MAX_RETRIES", 2)
AI_TEMPERATURE = _env_float("AI_TEMPERATURE", 0.1)
