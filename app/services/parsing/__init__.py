"""文件解析层包。"""

from app.services.parsing.base import (
    SOURCE_IMAGE,
    SOURCE_PDF_SCAN,
    SOURCE_PDF_TEXT,
    CorruptedFileError,
    EmptyFileError,
    FileParser,
    OCRError,
    ParseError,
    ParseResult,
    ParsingMode,
    ParsingOptions,
    UnsupportedFormatError,
)
from app.services.parsing.dispatcher import ParserDispatcher, get_dispatcher, parse_file
from app.services.parsing.image_ocr import ImageOCRParser
from app.services.parsing.ocr_engine import OCREngine, RapidOCREngine
from app.services.parsing.pdf_text import PDFTextParser
from app.services.parsing.scanned_pdf import ScannedPDFParser

__all__ = [
    "SOURCE_IMAGE",
    "SOURCE_PDF_SCAN",
    "SOURCE_PDF_TEXT",
    "CorruptedFileError",
    "EmptyFileError",
    "FileParser",
    "ImageOCRParser",
    "OCREngine",
    "OCRError",
    "PDFTextParser",
    "ParserDispatcher",
    "ParseError",
    "ParsingMode",
    "ParsingOptions",
    "ParseResult",
    "RapidOCREngine",
    "ScannedPDFParser",
    "UnsupportedFormatError",
    "get_dispatcher",
    "parse_file",
]
