"""提取层公共 API。"""

from __future__ import annotations

from app.models.schema import FieldDefinition

from .base import (
    ExtractionError,
    ExtractorBase,
    ExtractResult,
    apply_type_coercion,
    coerce_value,
)
from .label import LabelExtractor
from .llm import LLMExtractor
from .mock import MockExtractor
from .prompt import build_extraction_prompt


def get_extractor(mode: str) -> ExtractorBase:
    """提取器工厂。

    mode: "mock" → MockExtractor；"llm" → LLMExtractor；"label" → LabelExtractor；
    其他抛 ValueError。
    """
    if mode == "mock":
        return MockExtractor()
    if mode == "llm":
        return LLMExtractor()
    if mode == "label":
        return LabelExtractor()
    raise ValueError(f"不支持的提取模式: {mode}（可选：mock / llm / label）")


__all__ = [
    "ExtractResult",
    "ExtractionError",
    "ExtractorBase",
    "coerce_value",
    "apply_type_coercion",
    "MockExtractor",
    "LLMExtractor",
    "LabelExtractor",
    "build_extraction_prompt",
    "get_extractor",
    "FieldDefinition",
]
