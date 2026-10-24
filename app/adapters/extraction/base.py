"""提取层：异常、结果结构、基类与类型强转辅助。

设计原则：
- 提取层只依赖字段 schema（FieldDefinition）与全局配置，不依赖解析层；
- 数据以 text 字符串在层间传递，解析层与提取层完全解耦。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.models.schema import FieldDefinition


class ExtractionError(Exception):
    """提取层错误基类。"""


@dataclass
class ExtractResult:
    """统一提取结果。

    values: 键=字段名，顺序遵循传入的 fields（dict 保留插入顺序）；
    warnings: 提取过程中的告警（如类型强转失败、文本为空等）。
    """

    values: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def coerce_number(raw: Any) -> tuple[Any, bool]:
    """number → float；整数值转 int。失败返回 (原值, False)。"""
    s = str(raw).strip().replace(",", "")
    try:
        f = float(s)
    except (ValueError, TypeError):
        return (raw, False)
    if f.is_integer():
        try:
            return (int(f), True)
        except (ValueError, TypeError):
            return (raw, False)
    return (f, True)


def coerce_date(raw: Any) -> tuple[Any, bool]:
    """date → ISO 字符串 YYYY-MM-DD。

    兼容常见分隔形式（YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD / YYYY年M月D日）。
    失败返回 (原值, False)。
    """
    s = (
        str(raw)
        .strip()
        .replace("年", "-")
        .replace("月", "-")
        .replace("日", "")
        .replace(".", "-")
        .replace("/", "-")
    )
    parts = [p for p in s.split("-") if p != ""]
    if len(parts) != 3:
        return (raw, False)
    try:
        y, m, d = (int(p) for p in parts)
        return (date(y, m, d).isoformat(), True)
    except (ValueError, TypeError):
        return (raw, False)


def coerce_value(raw: Any, field_type: str) -> tuple[Any, bool]:
    """按字段类型强转。返回 (强转后的值, 是否成功)。

    成功：number→float（整数转 int）、date→ISO 字符串、text→去空白原值；
    失败：返回 (原值, False)，由调用方决定是否追加 warning。
    """
    if field_type == "number":
        return coerce_number(raw)
    if field_type == "date":
        return coerce_date(raw)
    return (str(raw).strip(), True)


def apply_type_coercion(
    values: dict[str, Any], fields: list[FieldDefinition]
) -> tuple[dict[str, Any], list[str]]:
    """对 values 按 fields 类型逐字段强转。

    返回 (强转后的 values, warnings)。强转失败时保留原值并追加一条 warning。
    values 的键顺序遵循传入 fields 顺序。
    """
    coerced: dict[str, Any] = {}
    warnings: list[str] = []
    for f in fields:
        raw = values.get(f.name, "")
        value, ok = coerce_value(raw, f.type)
        if not ok:
            warnings.append(f"字段「{f.name}」值「{raw}」类型强转失败，已保留原值")
        coerced[f.name] = value
    return coerced, warnings


class ExtractorBase(ABC):
    """所有提取器的统一接口。"""

    @abstractmethod
    def extract(
        self,
        text: str,
        fields: list[FieldDefinition],
        metadata: dict | None = None,
    ) -> ExtractResult:
        """从文本中提取指定字段。失败抛 ExtractionError 子类。"""
