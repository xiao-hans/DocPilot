"""ExcelBuilder 单元测试：生成链路的核心逻辑（布局/顺序/空结果/转义/样式）。

约定：
- 零网络、确定性：直接构造 Job 实例（独立对象），不经过 API / pipeline
- 每次 build 输出到各自的 tmp_path，测试间零共享
- 通过 openpyxl 读回 .xlsx 文件断言，验证的是真实落盘产物
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from app.models.job import Job, JobStatus
from app.models.schema import INVOICE_TEMPLATE, validate_fields
from app.adapters.exports.excel import ExcelBuilder

# 发票模板字段（校验后即为 FieldDefinition 列表，与真实流程一致）
INVOICE_FIELDS = validate_fields(INVOICE_TEMPLATE)


# ---------- 工具 ----------


def _fields_from(names: list[str]) -> list:
    """按给定 name 列表构造 FieldDefinition，类型自动推断。"""
    defs = []
    for name in names:
        if name in ("价税合计", "税额"):
            ftype = "number"
        elif name in ("开票日期", "日期"):
            ftype = "date"
        else:
            ftype = "text"
        defs.append({"name": name, "type": ftype, "description": f"{name}说明"})
    return validate_fields(defs)


def _make_job(fields: list | None = None, result: list | None = None) -> Job:
    """构造一个 status=SUCCEEDED、字段指定、结果指定的独立 Job。"""
    return Job(
        id="excel-test-job",
        fields=fields if fields is not None else INVOICE_FIELDS,
        created_at=0.0,
        status=JobStatus.SUCCEEDED,
        result=result,
    )


def _build(job: Job, tmp_path: Path, name: str = "out.xlsx") -> Path:
    """build + 校验返回值，返回输出文件路径。"""
    out = tmp_path / name
    returned = ExcelBuilder().build(job, out)
    assert returned == out, "build 应返回 output_path"
    assert out.is_file() and out.stat().st_size > 0
    return out


def _load(out: Path) -> Worksheet:
    ws = load_workbook(out).active
    assert ws is not None  # 新读出的工作簿必有 active sheet
    return ws


def _headers(ws: Worksheet) -> list:
    return [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]


def test_formula_injection_escaped_in_header_and_filename(tmp_path: Path):
    """表头（字段名）与文件名列也是用户可控输入 → 同样要转义（P1 安全修复验证）。"""
    evil_name = '=HYPERLINK("http://evil","x")'
    fields = _fields_from([evil_name])
    result = [
        {"file": "=1+1.pdf", "values": {f.name: "v" for f in fields}, "warnings": []}
    ]
    job = _make_job(fields=fields, result=result)
    ws = _load(_build(job, tmp_path))

    header_cell = ws.cell(1, 2)
    assert isinstance(header_cell.value, str) and header_cell.value.startswith("'")
    assert header_cell.data_type == "s", "表头不应被落盘为公式"

    file_cell = ws.cell(2, 1)
    assert isinstance(file_cell.value, str) and file_cell.value == "'=1+1.pdf"
    assert file_cell.data_type == "s", "文件名不应被落盘为公式"


def test_normal_header_and_filename_not_escaped(tmp_path: Path):
    """普通表头/文件名不加引号前缀（转义只针对 = + - @ 开头）。"""
    job = _make_job(
        fields=_fields_from(["发票号码"]),
        result=[
            {
                "file": "invoice.pdf",
                "values": {"发票号码": "01100200011100045239"},
                "warnings": [],
            }
        ],
    )
    ws = _load(_build(job, tmp_path))

    assert ws.cell(1, 2).value == "发票号码"
    assert ws.cell(2, 1).value == "invoice.pdf"
    assert ws.cell(2, 2).value == "01100200011100045239"


# ================================================================
# 场景 1：单文件 Excel 生成
# ================================================================


def test_single_file_excel_layout_and_types(tmp_path: Path):
    """1 行真实结果 → 2 行（表头+数据）、表头正确、filename 与 values 正确、数字保真。"""
    fields = _fields_from(["发票号码", "开票日期", "价税合计"])
    result = [
        {
            "file": "invoice_a.pdf",
            "values": {
                "发票号码": "01100200011100045239",
                "开票日期": "2025-01-12",
                "价税合计": 1280.5,
            },
            "warnings": [],
        }
    ]
    job = _make_job(fields=fields, result=result)
    ws = _load(_build(job, tmp_path))

    # 恰好表头 + 1 数据行
    assert ws.max_row == 2, f"期望 2 行，实际 max_row={ws.max_row}"
    # 表头：第一列固定 filename，其余按 fields 顺序
    assert _headers(ws) == ["filename", "发票号码", "开票日期", "价税合计"]
    # 数据行 filename 列
    assert ws["A2"].value == "invoice_a.pdf"
    # values 正确
    assert ws["B2"].value == "01100200011100045239"
    assert ws["C2"].value == "2025-01-12"
    # 数字类型保真：float 非字符串
    total = ws["D2"].value
    assert isinstance(total, float) and total == 1280.5, (
        f"期望 float 1280.5，实际 {total!r}"
    )


# ================================================================
# 场景 2：多文件 Excel 生成
# ================================================================


def test_multi_file_excel_rows_and_order(tmp_path: Path):
    """3 个不同文件 → 3 数据行、顺序与 result 一致、各自 values 正确。"""
    fields = _fields_from(["发票号码", "价税合计"])
    result = [
        {
            "file": "inv_b.pdf",
            "values": {"发票号码": "01100200011100045240", "价税合计": 2500.5},
            "warnings": [],
        },
        {
            "file": "inv_c.pdf",
            "values": {"发票号码": "01100200011100045241", "价税合计": 999.99},
            "warnings": [],
        },
        {
            "file": "inv_d.pdf",
            "values": {"发票号码": "01100200011100045242", "价税合计": 0.01},
            "warnings": [],
        },
    ]
    job = _make_job(fields=fields, result=result)
    ws = _load(_build(job, tmp_path))

    assert ws.max_row == 4, f"期望 4 行（表头+3 数据），实际 {ws.max_row}"
    # 行顺序与 result 一致
    assert [ws.cell(r, 1).value for r in range(2, 5)] == [
        "inv_b.pdf",
        "inv_c.pdf",
        "inv_d.pdf",
    ]
    for i, expect in enumerate(result):
        r = i + 2
        assert ws.cell(r, 2).value == expect["values"]["发票号码"], (
            f"第 {r} 行发票号不匹配"
        )
        assert ws.cell(r, 3).value == expect["values"]["价税合计"], (
            f"第 {r} 行金额不匹配"
        )


# ================================================================
# 场景 3：字段顺序
# ================================================================


def test_field_order_follows_fields_sequence(tmp_path: Path):
    """fields 故意乱序 [B, A, C] → 列头顺序与 fields 一一对应。"""
    fields = _fields_from(["销售方名称", "发票号码", "价税合计"])  # 乱序
    result = [
        {
            "file": "scramble.pdf",
            "values": {
                "销售方名称": "恒信达科技有限公司",
                "发票号码": "99999999999999999999",
                "价税合计": 88.8,
            },
            "warnings": [],
        }
    ]
    job = _make_job(fields=fields, result=result)
    ws = _load(_build(job, tmp_path))

    assert _headers(ws) == ["filename", "销售方名称", "发票号码", "价税合计"]
    # 数据行按同一顺序落位
    assert ws["B2"].value == "恒信达科技有限公司"  # 第一字段列
    assert ws["C2"].value == "99999999999999999999"
    assert ws["D2"].value == 88.8


def test_invoice_template_full_column_count(tmp_path: Path):
    """INVOICE_TEMPLATE 全字段：首列仍是 filename，列数 = 1 + len(fields)。"""
    expect_cols = 1 + len(INVOICE_TEMPLATE)
    assert expect_cols == 7  # 模板本身共 6 个字段

    result = [
        {
            "file": "tpl.pdf",
            "values": {f["name"]: "x" for f in INVOICE_TEMPLATE},
            "warnings": [],
        }
    ]
    job = _make_job(result=result)
    ws = _load(_build(job, tmp_path))

    assert ws.max_column == expect_cols, f"期望 {expect_cols} 列，实际 {ws.max_column}"
    assert ws["A1"].value == "filename"
    assert _headers(ws) == ["filename", *(f["name"] for f in INVOICE_TEMPLATE)]
    assert ws["A2"].value == "tpl.pdf"


# ================================================================
# 场景 4：空结果处理
# ================================================================


def test_empty_result_none_header_only(tmp_path: Path):
    """job.result=None → 仅表头行、不抛异常。"""
    job = _make_job(result=None)
    ws = _load(_build(job, tmp_path))

    assert ws.max_row == 1, f"期望仅表头行，实际 max_row={ws.max_row}"
    assert _headers(ws) == ["filename", *(f["name"] for f in INVOICE_TEMPLATE)]
    assert ws.max_column == 1 + len(INVOICE_TEMPLATE)


def test_empty_result_empty_list_header_only(tmp_path: Path):
    """job.result=[] → 仅表头行、不抛异常。"""
    job = _make_job(result=[])
    ws = _load(_build(job, tmp_path))

    assert ws.max_row == 1, f"期望仅表头行，实际 max_row={ws.max_row}"
    assert _headers(ws) == ["filename", *(f["name"] for f in INVOICE_TEMPLATE)]


# ================================================================
# 场景 5：公式注入转义（安全必有）
# ================================================================


def test_formula_injection_prefix_escaped(tmp_path: Path):
    """以 = + - @ 开头的字符串值 → 写入前加单引号前缀；普通字符串不加。"""
    fields = _fields_from(["字段一", "字段二", "字段三", "字段四", "字段五", "字段六"])
    result = [
        {
            "file": "inj.pdf",
            "values": {
                "字段一": "=SUM(A1:A9)",
                "字段二": "+1+1",
                "字段三": "-2+3",
                "字段四": "@cmd",
                "字段五": "01100200011100045239",  # 数字开头，非注入前缀
                "字段六": "2025-01-12",  # 日期字符串，非注入前缀
            },
            "warnings": [],
        }
    ]
    job = _make_job(fields=fields, result=result)
    ws = _load(_build(job, tmp_path))

    assert ws["B2"].value == "'=SUM(A1:A9)", f"实际 {ws['B2'].value!r}"
    assert ws["C2"].value == "'+1+1", f"实际 {ws['C2'].value!r}"
    assert ws["D2"].value == "'-2+3", f"实际 {ws['D2'].value!r}"
    assert ws["E2"].value == "'@cmd", f"实际 {ws['E2'].value!r}"
    # 普通字符串不加前缀
    assert ws["F2"].value == "01100200011100045239"
    assert ws["G2"].value == "2025-01-12"


def test_formula_injection_each_row_escaped(tmp_path: Path):
    """多行场景：转义逐行生效，不影响其它行。"""
    fields = _fields_from(["备注"])
    result = [
        {"file": "safe.pdf", "values": {"备注": "普通文本"}, "warnings": []},
        {
            "file": "evil.pdf",
            "values": {"备注": '=HYPERLINK("http://evil")'},
            "warnings": [],
        },
    ]
    job = _make_job(fields=fields, result=result)
    ws = _load(_build(job, tmp_path))

    assert ws["B2"].value == "普通文本"
    assert ws["B3"].value == '\'=HYPERLINK("http://evil")'


# ================================================================
# 补充：字段缺失 / 样式 / 列宽
# ================================================================


def test_missing_field_yields_empty_cell(tmp_path: Path):
    """values 缺某字段 key → 该单元格为空（None 或空串），不影响其它字段。"""
    fields = _fields_from(["销售方名称", "税额", "购买方名称"])
    result = [
        {
            "file": "missing.pdf",
            "values": {
                "销售方名称": "恒信达科技有限公司",
                "购买方名称": "某某公司",
            },  # 缺"税额"
            "warnings": [],
        }
    ]
    job = _make_job(fields=fields, result=result)
    ws = _load(_build(job, tmp_path))

    assert ws["B2"].value == "恒信达科技有限公司"
    v = ws["C2"].value
    assert v is None or v == "", f"缺失字段应为空，实际 {v!r}"
    assert ws["D2"].value == "某某公司"


def test_freeze_panes_and_header_style(tmp_path: Path):
    """freeze_panes=A2；表头加粗、藏青填充、白字、细边框；数据行 wrap_text。"""
    fields = _fields_from(["发票号码"])
    result = [
        {
            "file": "style.pdf",
            "values": {"发票号码": "01100200011100045239"},
            "warnings": [],
        }
    ]
    job = _make_job(fields=fields, result=result)
    ws = _load(_build(job, tmp_path))

    assert ws.freeze_panes == "A2"

    h = ws["A1"]
    assert h.font.bold is True, "表头应加粗"
    assert h.fill.fill_type == "solid", "表头应有实色填充"
    assert "1F4E79" in str(h.fill.fgColor.rgb).upper(), f"填充色={h.fill.fgColor.rgb}"
    assert "FFFFFF" in str(h.font.color.rgb).upper(), f"字体色={h.font.color.rgb}"
    for side in ("left", "right", "top", "bottom"):
        assert getattr(h.border, side).style == "thin", f"表头 {side} 边框缺失"

    # 边框覆盖整行表头（最后一列也应带边框）
    last_header = ws.cell(1, ws.max_column)
    assert last_header.border.left.style == "thin"

    # 数据行 wrap_text
    assert ws["B2"].alignment.wrap_text is True


def test_column_width_adaptive_and_bounded(tmp_path: Path):
    """列宽自适应：短内容不低于 min(10)，长内容不超 max(60)。"""
    fields = _fields_from(["A", "B"])  # 表头长 1
    long_file = "f" * 80
    result = [{"file": long_file, "values": {"A": "v" * 20, "B": "y"}, "warnings": []}]
    job = _make_job(fields=fields, result=result)
    ws = _load(_build(job, tmp_path))

    widths = {col: ws.column_dimensions[col].width for col in ("A", "B", "C")}
    # 各列宽度都在 [10, 60] 区间内
    for col, w in widths.items():
        assert w is not None and 10 <= w <= 60, f"{col} 列宽 {w} 越界"
    # 超长文件名列被截到上限 60
    assert widths["A"] == 60
    # 20 字符内容列宽度=20（表头 1 字符，取内容长）
    assert widths["B"] == 20
    # 短内容列回落到最小值 10
    assert widths["C"] == 10
