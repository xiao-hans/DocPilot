"""标签定位提取器：在真实文本里找「字段名: 值」，未命中就留空并告警，绝不造值。

与 MockExtractor 的本质区别：mock 未命中规则时返回编造的示例值（面向演示），本实现
的输出是**文档内容与模板的纯函数**——文本里有什么就提取什么，没有就空着并说明原因。
它是提取接缝上的第三个可替换实现（NFR-004），也是第一个不编造任何值的本地引擎。

定位规则（刻意保持最小，均为确定性、无网络依赖）：

- 标签就是字段名本身；命中处必须是「标签 + 分隔符（冒号/空白）+ 行内值」的紧邻结构，
  "价税合计(小写):" 这类带限定词的行不会被当成 "价税合计" 的命中；
- 逐字段自左向右扫描，每个字段从上一处命中之后开始找，后面的行不会被前面的字段抢走；
- number 字段剥掉首尾货币记号（¥￥/元）再交类型强转，逗号千分位由强转层处理；
- 未命中：值置空 + 一条告警，空值不参与类型强转（空串对 date/number 必然失败，
  会把"没找到"误报成"格式不对"）；强转失败：保留原值 + 一条告警（与 mock / llm 同口径）。
"""

from __future__ import annotations

import re
from typing import Any

from app.models.schema import FieldDefinition

from .base import ExtractorBase, ExtractResult, coerce_value

# 标签之后必须紧跟分隔符（冒号或空白）才开始取值；值取到行尾为止。
_VALUE_RE = re.compile(r"[:：\s]\s*(\S.*)")


class LabelExtractor(ExtractorBase):
    """按字段名作标签在文档文本中定位「标签: 值」；未命中留空并告警。"""

    def extract(
        self,
        text: str,
        fields: list[FieldDefinition],
        metadata: dict | None = None,
    ) -> ExtractResult:
        if not text or not text.strip():
            return ExtractResult(
                values={f.name: "" for f in fields},
                warnings=["输入文本为空，所有字段值置空"],
            )

        raw: dict[str, str] = {}
        warnings: list[str] = []
        pos = 0
        for f in fields:
            found = self._find_value(text, f.name, pos)
            if found is None:
                raw[f.name] = ""
                warnings.append(f"未在文本中找到标签「{f.name}」，值置空")
                continue
            value, pos = found
            if f.type == "number":
                value = value.lstrip("¥￥ ").rstrip("元 ").strip() or value
            raw[f.name] = value

        # 未命中留空的字段不再过类型强转：空串对 date/number 必然失败，
        # 那会把"没找到"误报成"格式不对"。强转只作用于真实提取到的值。
        values: dict[str, Any] = {}
        for f in fields:
            if raw[f.name] == "":
                values[f.name] = ""
                continue
            coerced, ok = coerce_value(raw[f.name], f.type)
            if ok:
                values[f.name] = coerced
            else:
                values[f.name] = raw[f.name]
                warnings.append(
                    f"字段「{f.name}」值「{raw[f.name]}」类型强转失败，已保留原值"
                )
        return ExtractResult(values=values, warnings=warnings)

    @staticmethod
    def _find_value(text: str, label: str, start: int) -> tuple[str, int] | None:
        """从 start 起找「label + 分隔符 + 行内值」；返回 (值, 行尾偏移) 或 None。"""
        idx = text.find(label, start)
        while idx != -1:
            line_end = text.find("\n", idx)
            if line_end == -1:
                line_end = len(text)
            rest = text[idx + len(label):line_end]
            m = _VALUE_RE.match(rest)
            if m:
                return m.group(1).strip(), line_end
            idx = text.find(label, idx + 1)
        return None
