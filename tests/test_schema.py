"""字段 schema 校验测试。"""

import pytest

from app.models.schema import INVOICE_TEMPLATE, validate_fields


def test_invoice_template_valid():
    fields = validate_fields(INVOICE_TEMPLATE)
    assert len(fields) == 6
    assert fields[0].name == "发票号码"
    assert fields[0].type == "text"
    assert fields[3].type == "number" or fields[3].name == "购买方名称"


def test_custom_fields_roundtrip():
    custom = [
        {"name": "合同编号", "type": "text", "description": "合同右上角的编号"},
        {"name": "总金额", "type": "number", "description": "合同总金额，单位元"},
        {"name": "签订日期", "type": "date", "description": "合同签订日期"},
    ]
    fields = validate_fields(custom)
    assert [f.name for f in fields] == ["合同编号", "总金额", "签订日期"]
    assert all(f.description for f in fields)


def test_empty_fields_rejected():
    with pytest.raises(ValueError, match="不能为空"):
        validate_fields([])


def test_too_many_fields_rejected():
    many = [{"name": f"字段{i}", "type": "text", "description": "x"} for i in range(31)]
    with pytest.raises(ValueError, match="不能超过"):
        validate_fields(many)


def test_missing_description_rejected():
    with pytest.raises(ValueError, match="字段定义非法"):
        validate_fields([{"name": "发票号", "type": "text"}])


def test_invalid_type_rejected():
    with pytest.raises(ValueError, match="字段定义非法"):
        validate_fields([{"name": "x", "type": "email", "description": "y"}])


def test_name_whitespace_stripped():
    fields = validate_fields(
        [{"name": "  金额  ", "type": "number", "description": "金额"}]
    )
    assert fields[0].name == "金额"
