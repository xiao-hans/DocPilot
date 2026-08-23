"""扫描版 PDF：渲染页面为图片后经 OCR 引擎识别。"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pymupdf
from PIL import Image

from app.config import MAX_PDF_PAGES, OCR_DPI
from app.services.parsing.base import (
    SOURCE_PDF_SCAN,
    CorruptedFileError,
    FileParser,
    ParseResult,
    validate_readable_file,
)
from app.services.parsing.ocr_engine import OCREngine


class ScannedPDFParser(FileParser):
    """对无文本层的 PDF（扫描件）逐页渲染 + OCR。"""

    source_type = SOURCE_PDF_SCAN

    def __init__(self, ocr: OCREngine) -> None:
        self.ocr = ocr

    def parse(self, path: Path) -> ParseResult:
        validate_readable_file(path)
        try:
            doc = pymupdf.open(path)
        except Exception as e:
            raise CorruptedFileError(f"无法打开 PDF: {path.name}: {e}") from e

        with doc:
            total_pages = doc.page_count
            pages_to_read = min(total_pages, MAX_PDF_PAGES)
            parts: list[str] = []
            warnings: list[str] = []

            for i in range(pages_to_read):
                pix = doc[i].get_pixmap(dpi=OCR_DPI)
                with Image.open(BytesIO(pix.tobytes("png"))) as img:
                    page_text = self.ocr.recognize(img)
                parts.append(f"--- 第 {i + 1} 页 ---\n{page_text}")

            if total_pages > MAX_PDF_PAGES:
                warnings.append(
                    f"PDF 共 {total_pages} 页，超过上限仅处理前 {MAX_PDF_PAGES} 页"
                )

        return ParseResult(
            text="\n\n".join(parts),
            pages=total_pages,
            source_type=self.source_type,
            warnings=warnings,
        )
