"""图片文件：直接经 OCR 引擎识别为文本。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.adapters.parsing.base import (
    SOURCE_IMAGE,
    CorruptedFileError,
    FileParser,
    ParseResult,
    validate_readable_file,
)
from app.adapters.parsing.ocr_engine import OCREngine


class ImageOCRParser(FileParser):
    """对 jpg/png/webp/bmp 等图片执行 OCR 识别。"""

    source_type = SOURCE_IMAGE

    def __init__(self, ocr: OCREngine) -> None:
        self.ocr = ocr

    def parse(self, path: Path) -> ParseResult:
        validate_readable_file(path)
        try:
            with Image.open(path) as img:
                text = self.ocr.recognize(img)
        except UnidentifiedImageError as e:
            raise CorruptedFileError(f"无法识别的图片: {path.name}: {e}") from e
        except OSError as e:
            raise CorruptedFileError(f"图片损坏: {path.name}: {e}") from e

        return ParseResult(
            text=text,
            pages=1,
            source_type=self.source_type,
            warnings=[],
        )
