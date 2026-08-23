"""普通 PDF：直接提取文本层（无 OCR）。"""

from __future__ import annotations

from pathlib import Path

import pymupdf

from app.config import MAX_PDF_PAGES
from app.services.parsing.base import (
    SOURCE_PDF_TEXT,
    CorruptedFileError,
    FileParser,
    ParseResult,
    validate_readable_file,
)


class PDFTextParser(FileParser):
    """对含文本层的 PDF 逐页抽取文本，页间加分隔标记。"""

    source_type = SOURCE_PDF_TEXT

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
                page_text = doc[i].get_text("text") or ""
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
