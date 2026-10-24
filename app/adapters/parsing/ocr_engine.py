"""OCR 引擎抽象：解析层通过接口注入，便于测试和切换实现。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.adapters.parsing.base import OCRError


class OCREngine(ABC):
    """OCR 引擎接口。输入为图片（PIL.Image / numpy 数组），输出识别文本。"""

    @abstractmethod
    def recognize(self, image: Any) -> str:
        """识别单张图片，返回拼接后的文本。失败抛 OCRError。"""


class RapidOCREngine(OCREngine):
    """RapidOCR 实现：懒加载模型（首次调用才初始化，降低启动开销）。"""

    def __init__(self) -> None:
        self._engine = None  # RapidOCR 实例，延迟创建

    def _get_engine(self) -> Any:
        if self._engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as e:
                raise OCRError(
                    "RapidOCR 未安装，请执行 pip install rapidocr-onnxruntime"
                ) from e
            self._engine = RapidOCR()
        return self._engine

    def recognize(self, image: Any) -> str:
        try:
            result, _elapse = self._get_engine()(image)
        except OCRError:
            raise
        except Exception as e:
            raise OCRError(f"OCR 识别失败: {e}") from e
        if not result:
            return ""
        lines = [item[1] for item in result if len(item) > 1]
        return "\n".join(lines)
