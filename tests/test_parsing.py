"""文件解析层测试：PDF 文本、扫描 PDF、图片 OCR、异常场景。

约定：OCR 全部用 FakeOCREngine 注入，不加载真实 RapidOCR 模型。
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from app.platform.config import MAX_PDF_PAGES
from app.adapters.parsing.base import (
    SOURCE_IMAGE,
    SOURCE_PDF_SCAN,
    SOURCE_PDF_TEXT,
    CorruptedFileError,
    EmptyFileError,
    ParseResult,
    ParsingMode,
    ParsingOptions,
    UnsupportedFormatError,
)
from app.adapters.parsing.dispatcher import ParserDispatcher
from app.adapters.parsing.image_ocr import ImageOCRParser
from app.adapters.parsing.ocr_engine import OCREngine
from app.adapters.parsing.pdf_text import PDFTextParser
from app.adapters.parsing.scanned_pdf import ScannedPDFParser


class FakeOCREngine(OCREngine):
    """测试用 OCR：返回固定文本并记录调用次数。"""

    def __init__(self, text: str = "FAKE-OCR-CONTENT") -> None:
        self.text = text
        self.calls: list[object] = []

    def recognize(self, image: object) -> str:
        self.calls.append(image)
        return self.text


# ---------- 测试夹具 ----------


def make_text_pdf(path: Path, pages: int = 3) -> None:
    """生成含文本层的 PDF。"""
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Invoice Line {i + 1} - Amount {100 + i}.50")
    doc.save(path)
    doc.close()


def make_scanned_pdf(path: Path, pages: int = 2) -> None:
    """生成无文本层的 PDF（模拟扫描件：页面上只有图形）。"""
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page()
        page.draw_rect(pymupdf.Rect(72, 72, 300, 200), color=(0, 0, 0))
    doc.save(path)
    doc.close()


def make_image(path: Path) -> None:
    """生成纯色 PNG。"""
    Image.new("RGB", (60, 40), (255, 255, 255)).save(path)


# ---------- 1. 普通 PDF 文本提取 ----------


def test_pdf_text_parser_reads_text(tmp_path: Path):
    pdf = tmp_path / "text.pdf"
    make_text_pdf(pdf, pages=3)

    result = PDFTextParser().parse(pdf)

    assert isinstance(result, ParseResult)
    assert result.source_type == SOURCE_PDF_TEXT
    assert result.pages == 3
    assert "Invoice Line 1" in result.text
    assert "Invoice Line 3" in result.text
    assert "--- 第 2 页 ---" in result.text  # 页间分隔标记
    assert result.warnings == []


def test_pdf_text_parser_pages_when_extra(tmp_path: Path):
    """超长 PDF 应截断并给出警告。"""
    pdf = tmp_path / "long.pdf"
    make_text_pdf(pdf, pages=MAX_PDF_PAGES + 3)

    result = PDFTextParser().parse(pdf)

    assert result.pages == MAX_PDF_PAGES + 3
    assert any("超过上限" in w for w in result.warnings)


# ---------- 2. 扫描 PDF（渲染 + OCR）----------


def test_scanned_pdf_parser_uses_ocr(tmp_path: Path):
    pdf = tmp_path / "scan.pdf"
    make_scanned_pdf(pdf, pages=2)
    fake = FakeOCREngine(text="SCANNED-TEXT")

    result = ScannedPDFParser(fake).parse(pdf)

    assert result.source_type == SOURCE_PDF_SCAN
    assert result.pages == 2
    assert fake.calls, "OCR 引擎应被调用"
    assert len(fake.calls) == 2  # 每页一次
    assert "SCANNED-TEXT" in result.text


# ---------- 3. 图片 OCR ----------


def test_image_ocr_parser(tmp_path: Path):
    img = tmp_path / "page.png"
    make_image(img)
    fake = FakeOCREngine(text="IMAGE-TEXT-123")

    result = ImageOCRParser(fake).parse(img)

    assert result.source_type == SOURCE_IMAGE
    assert result.pages == 1
    assert len(fake.calls) == 1
    assert result.text == "IMAGE-TEXT-123"


# ---------- 4. 调度器：自动选路 ----------


def test_dispatcher_routes_text_pdf(tmp_path: Path):
    pdf = tmp_path / "text.pdf"
    make_text_pdf(pdf)

    result = ParserDispatcher(FakeOCREngine()).parse(pdf)

    assert result.source_type == SOURCE_PDF_TEXT


def test_dispatcher_falls_back_to_ocr_for_scanned_pdf(tmp_path: Path):
    pdf = tmp_path / "scan.pdf"
    make_scanned_pdf(pdf)
    fake = FakeOCREngine(text="SCANNED-FALLBACK")

    result = ParserDispatcher(fake).parse(pdf)

    assert result.source_type == SOURCE_PDF_SCAN
    assert "SCANNED-FALLBACK" in result.text
    assert any("文本层字符数过少" in w for w in result.warnings)


def test_dispatcher_routes_image(tmp_path: Path):
    img = tmp_path / "page.jpg"
    make_image(img)

    result = ParserDispatcher(FakeOCREngine()).parse(img)

    assert result.source_type == SOURCE_IMAGE


# ---------- 4b. ParsingOptions 处理模式 ----------


def test_force_ocr_on_text_pdf(tmp_path: Path):
    """force_ocr：即使有文本层也强制走 OCR（用户显式选择）。"""
    pdf = tmp_path / "contract.pdf"
    make_text_pdf(pdf)
    fake = FakeOCREngine(text="FORCED-OCR")

    result = ParserDispatcher(fake).parse(
        pdf, ParsingOptions(mode=ParsingMode.FORCE_OCR)
    )

    assert result.source_type == SOURCE_PDF_SCAN
    assert "FORCED-OCR" in result.text
    assert fake.calls, "强制 OCR 必须调用 OCR 引擎"
    assert any("强制 OCR" in w for w in result.warnings)


def test_text_only_never_falls_back(tmp_path: Path):
    """text_only：扫描件不回退 OCR，只返回文本层（可能为空）+ 警告。"""
    pdf = tmp_path / "scan.pdf"
    make_scanned_pdf(pdf)
    fake = FakeOCREngine()

    result = ParserDispatcher(fake).parse(
        pdf, ParsingOptions(mode=ParsingMode.TEXT_ONLY)
    )

    assert result.source_type == SOURCE_PDF_TEXT
    # 文本层为空，但页分隔标记仍在；去掉标记后应为空
    content = "".join(
        line for line in result.text.splitlines() if not line.startswith("---")
    )
    assert content.strip() == ""
    assert fake.calls == [], "仅文本模式不得调用 OCR"
    assert any("仅文本模式" in w for w in result.warnings)


def test_auto_mode_is_default(tmp_path: Path):
    pdf = tmp_path / "scan.pdf"
    make_scanned_pdf(pdf)
    fake = FakeOCREngine(text="AUTO-OCR")

    result = ParserDispatcher(fake).parse(pdf)  # 不传 options，默认 auto

    assert result.source_type == SOURCE_PDF_SCAN
    assert "AUTO-OCR" in result.text


def test_custom_min_text_chars_threshold(tmp_path: Path):
    """auto 模式下阈值可调：调高后普通文本 PDF 也会被判为扫描件。"""
    pdf = tmp_path / "short.pdf"
    make_text_pdf(pdf)  # 每页约几十字符
    fake = FakeOCREngine(text="HIGH-THRESHOLD-OCR")

    result = ParserDispatcher(fake).parse(
        pdf, ParsingOptions(mode=ParsingMode.AUTO, min_text_chars=100_000)
    )

    assert result.source_type == SOURCE_PDF_SCAN


# ---------- 4c. ParseResult.metadata ----------


def test_metadata_attached_by_dispatcher(tmp_path: Path):
    pdf = tmp_path / "invoice01.pdf"
    make_text_pdf(pdf, pages=3)

    result = ParserDispatcher(FakeOCREngine()).parse(pdf)

    assert result.metadata["filename"] == "invoice01.pdf"
    assert result.metadata["page_count"] == 3


def test_metadata_attached_for_image(tmp_path: Path):
    img = tmp_path / "page_1.png"
    make_image(img)

    result = ImageOCRParser(FakeOCREngine()).parse(img)  # 直接解析器不含 metadata

    assert "filename" not in result.metadata  # 解析器自身不填充

    result2 = ParserDispatcher(FakeOCREngine()).parse(img)  # 经调度器后填充

    assert result2.metadata["filename"] == "page_1.png"
    assert result2.metadata["page_count"] == 1


# ---------- 5. 异常场景 ----------


def test_empty_file_rejected(tmp_path: Path):
    pdf = tmp_path / "empty.pdf"
    pdf.write_bytes(b"")

    with pytest.raises(EmptyFileError):
        PDFTextParser().parse(pdf)


def test_empty_image_rejected(tmp_path: Path):
    img = tmp_path / "empty.png"
    img.write_bytes(b"")
    fake = FakeOCREngine()

    with pytest.raises(EmptyFileError):
        ImageOCRParser(fake).parse(img)


def test_unsupported_format_rejected(tmp_path: Path):
    txt = tmp_path / "notes.docx"
    txt.write_bytes(b"whatever")

    with pytest.raises(UnsupportedFormatError):
        ParserDispatcher(FakeOCREngine()).parse(txt)


def test_corrupted_pdf_rejected(tmp_path: Path):
    pdf = tmp_path / "broken.pdf"
    pdf.write_bytes(b"this is not a pdf at all")

    with pytest.raises(CorruptedFileError):
        PDFTextParser().parse(pdf)


def test_corrupted_image_rejected(tmp_path: Path):
    img = tmp_path / "broken.png"
    img.write_bytes(b"not a real image")
    fake = FakeOCREngine()

    with pytest.raises(CorruptedFileError):
        ImageOCRParser(fake).parse(img)


def test_file_not_found(tmp_path: Path):
    with pytest.raises(Exception, match="文件不存在"):
        PDFTextParser().parse(tmp_path / "missing.pdf")
