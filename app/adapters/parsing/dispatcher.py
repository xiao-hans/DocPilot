"""解析调度：按文件类型 + ParsingOptions 选择解析器。"""

from __future__ import annotations

from pathlib import Path

from app.platform.config import ALLOWED_EXTENSIONS, MIN_TEXT_CHARS_PER_PAGE
from app.adapters.parsing.base import (
    FileParser,
    ParseResult,
    ParsingMode,
    ParsingOptions,
    UnsupportedFormatError,
)
from app.adapters.parsing.image_ocr import ImageOCRParser
from app.adapters.parsing.ocr_engine import OCREngine, RapidOCREngine
from app.adapters.parsing.pdf_text import PDFTextParser
from app.adapters.parsing.scanned_pdf import ScannedPDFParser


class ParserDispatcher:
    """按扩展名 + 处理模式选择解析器。

    - auto（默认）：PDF 先走文本层，文本量过低自动回退 OCR；图片直接 OCR
    - force_ocr：PDF 无视文本层直接渲染 OCR
    - text_only：PDF 只用文本层，不做 OCR 回退

    parse() 返回前会在 metadata 里填入 filename / page_count。
    """

    def __init__(self, ocr: OCREngine | None = None) -> None:
        self.ocr = ocr or RapidOCREngine()

    def parse(self, path: Path, options: ParsingOptions | None = None) -> ParseResult:
        opts = options or ParsingOptions()
        ext = path.suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise UnsupportedFormatError(f"不支持的文件格式: {ext or path.name}")

        if ext == ".pdf":
            result = self._parse_pdf(path, opts)
        else:
            result = ImageOCRParser(self.ocr).parse(path)

        result.metadata["filename"] = path.name
        result.metadata["page_count"] = result.pages
        return result

    def _parse_pdf(self, path: Path, opts: ParsingOptions) -> ParseResult:
        if opts.mode is ParsingMode.FORCE_OCR:
            result = ScannedPDFParser(self.ocr).parse(path)
            result.warnings.append(f"{path.name}: 已按强制 OCR 模式处理")
            return result

        result = PDFTextParser().parse(path)

        # auto 模式：文本量过低的 PDF 视为扫描件，走 OCR
        threshold = (
            opts.min_text_chars
            if opts.min_text_chars is not None
            else MIN_TEXT_CHARS_PER_PAGE
        )
        effective_chars = sum(1 for c in result.text if not c.isspace())
        if effective_chars >= threshold:
            return result

        if opts.mode is ParsingMode.TEXT_ONLY:
            result.warnings.append(
                f"{path.name}: 仅文本模式，文本层字符数过少（{effective_chars}）"
            )
            return result

        scan_result = ScannedPDFParser(self.ocr).parse(path)
        scan_result.warnings.append(
            f"{path.name}: 文本层字符数过少（{effective_chars}），已按扫描件 OCR 处理"
        )
        return scan_result


# 便捷入口：单例调度器（默认注入 RapidOCR）
_dispatcher: ParserDispatcher | None = None


def get_dispatcher() -> ParserDispatcher:
    """获取进程级调度器单例（测试中可注入 FakeOCR 后使用）。"""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = ParserDispatcher()
    return _dispatcher


def parse_file(path: Path) -> ParseResult:
    """便捷函数：文件路径 → ParseResult。"""
    return get_dispatcher().parse(path)


# 为类型检查导出（部分解析器实现文件）
__all__ = ["ParserDispatcher", "FileParser", "parse_file", "get_dispatcher"]
