"""文件解析层：统一接口与结果结构。

设计原则：
- 解析层只依赖 OCR 引擎 *接口*（注入式），不绑定具体实现；
- 不依赖 AI 提取、任务管理等上层模块。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# 来源类型常量
SOURCE_PDF_TEXT = "pdf_text"
SOURCE_PDF_SCAN = "pdf_scan"
SOURCE_IMAGE = "image"


class ParsingMode(str, Enum):
    """处理模式：由调用方/UI 决定，而非仅靠规则。"""

    AUTO = "auto"  # 自动：先文本层，文本量过低回退 OCR
    FORCE_OCR = "force_ocr"  # 强制 OCR：不走文本层（如文本层失真但图片清晰）
    TEXT_ONLY = "text_only"  # 仅文本层：不做 OCR


@dataclass
class ParsingOptions:
    """解析选项：调度器通过它决定路由策略，未来 UI 可直接映射。"""

    mode: ParsingMode = ParsingMode.AUTO
    # 自动模式下判定“文本层过低”的阈值（按总非空白字符数）
    min_text_chars: int | None = None  # None = 用 config.MIN_TEXT_CHARS_PER_PAGE


class ParseError(Exception):
    """解析层错误基类。"""


class UnsupportedFormatError(ParseError):
    """不支持的文件格式。"""


class EmptyFileError(ParseError):
    """空文件（0 字节）。"""


class CorruptedFileError(ParseError):
    """文件损坏或无法解码。"""


class OCRError(ParseError):
    """OCR 引擎处理失败。"""


@dataclass
class ParseResult:
    """统一解析结果。

    metadata: 附着在文件上的信息，随解析一路传到 AI 提取与 Excel。
    约定至少包含: filename / page_count；由调度器填充，解析器自身不负责。
    """

    text: str = ""
    pages: int = 0  # 实际参与解析的页数（图片为 1）
    source_type: str = SOURCE_PDF_TEXT  # pdf_text | pdf_scan | image
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class FileParser(ABC):
    """所有解析器的统一接口。"""

    source_type: str = ""

    @abstractmethod
    def parse(self, path: Path) -> ParseResult:
        """解析文件并返回统一结构。失败抛 ParseError 子类。"""


def validate_readable_file(path: Path) -> None:
    """公共前置检查：存在、是文件、非空。"""
    if not path.exists():
        raise ParseError(f"文件不存在: {path}")
    if not path.is_file():
        raise ParseError(f"不是文件: {path}")
    if path.stat().st_size == 0:
        raise EmptyFileError(f"文件为空: {path.name}")
