"""Mock 提取器：纯规则启发式，确定性、无网络依赖。

用于本地开发、测试与离线演示。返回符合业务形态的合法值：
命中规则用真实正则提取；未命中用合法示例值（非 "mock/test" 之类测试字符串）。
要求：同输入两次调用结果一致（纯规则，无随机）。
"""

from __future__ import annotations

import re

from app.models.schema import FieldDefinition

from .base import ExtractorBase, ExtractResult, apply_type_coercion

_INVOICE_RE = re.compile(r"\b\d{20}\b")
_DATE_RE = re.compile(r"\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?")
# 金额必须含千分位逗号或小数点，避免误匹配发票号等纯数字串（如 "011..."）
_MONEY_RE = re.compile(r"[¥￥]?\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+)\s*元?")
_NAME_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9（）()·&]{1,30}?(?:公司|厂|店|集团|中心))"
)


class MockExtractor(ExtractorBase):
    """基于字段名关键词的启发式提取器（确定性）。"""

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

        # 按字段顺序自左向右逐段扫描：每个字段只从上一处匹配之后开始查找，
        # 避免前面的内容（如发票号）被后续字段的错误正则命中。
        raw: dict[str, str] = {}
        pos = 0
        for f in fields:
            found = self._extract_one(text, f.name, pos)
            if found is not None:
                raw[f.name] = found
                idx = text.find(found, pos)
                pos = idx + len(found) if idx >= 0 else pos
            else:
                raw[f.name] = self._example_for(f)

        values, warnings = apply_type_coercion(raw, fields)
        return ExtractResult(values=values, warnings=warnings)

    def _extract_one(self, text: str, name: str, start: int = 0) -> str | None:
        if any(k in name for k in ("发票", "号码", "编号", "单号")):
            m = _INVOICE_RE.search(text, start)
            return m.group(0) if m else None
        if "日期" in name or "时间" in name:
            m = _DATE_RE.search(text, start)
            return m.group(0) if m else None
        if any(k in name for k in ("金额", "税额", "合计", "价税", "费用", "小写")):
            m = _MONEY_RE.search(text, start)
            return m.group(1) if m else None
        if any(k in name for k in ("名称", "方", "单位", "公司", "厂", "店")):
            m = _NAME_RE.search(text, start)
            return m.group(0) if m else None
        return None

    def _example_for(self, f: FieldDefinition) -> str:
        """未命中时返回符合业务形态的合法示例值（非测试字符串）。"""
        if any(k in f.name for k in ("发票", "号码", "编号", "单号")):
            return "01100200011100045239"
        if f.type == "date":
            return "2025-01-12"
        if f.type == "number":
            return "1280.50"
        return "恒信达科技有限公司"
