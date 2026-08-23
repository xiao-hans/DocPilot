"""字段 schema：用户自定义字段 + 内置示例模板。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

FieldType = Literal["text", "number", "date"]

MAX_FIELDS = 30


class FieldDefinition(BaseModel):
    """一个待提取字段：名称 + 类型 + 描述（描述是提准确度的关键，必填）。"""

    name: str = Field(min_length=1, max_length=50)
    type: FieldType = "text"
    description: str = Field(min_length=1, max_length=500)

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        return v.strip()


# 内置示例模板：发票（一键填入后可自由增删改）
INVOICE_TEMPLATE: list[dict] = [
    {
        "name": "发票号码",
        "type": "text",
        "description": "发票右上角的发票号码（20 位数字）",
    },
    {
        "name": "开票日期",
        "type": "date",
        "description": "发票上的开票日期，格式 YYYY-MM-DD",
    },
    {"name": "销售方名称", "type": "text", "description": "销售方（销货单位）的全称"},
    {"name": "购买方名称", "type": "text", "description": "购买方（购货单位）的全称"},
    {
        "name": "价税合计",
        "type": "number",
        "description": "发票右下角的价税合计（含税金额），数字，单位元",
    },
    {"name": "税额", "type": "number", "description": "发票上的合计税额，数字，单位元"},
]


def validate_fields(fields: list[dict]) -> list[FieldDefinition]:
    """校验用户提交的字段列表，返回规范化后的 FieldDefinition 列表。

    Raises:
        ValueError: 字段列表为空、数量超限、或某个字段定义非法。
    """
    if not fields:
        raise ValueError("字段列表不能为空")
    if len(fields) > MAX_FIELDS:
        raise ValueError(f"字段数量不能超过 {MAX_FIELDS} 个")

    try:
        return [FieldDefinition(**f) for f in fields]
    except (ValidationError, TypeError) as e:
        raise ValueError(f"字段定义非法: {e}") from e
