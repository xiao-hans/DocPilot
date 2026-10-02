"""把任务结果（job.result）导出为 .xlsx 文件。"""

from __future__ import annotations

import re
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.models.job import Job

_INJECTION_PREFIXES = ("=", "+", "-", "@")  # Excel 公式注入前缀
_MIN_COL_WIDTH = 10
_MAX_COL_WIDTH = 60
_HEADER_BG = "1F4E79"  # 藏青
_MAX_CELL_CHARS = 32767  # Excel 单元格上限，超出后 CSV 与 XLSX 会在同一处截断
# openpyxl 拒绝写入 XML 非法控制字符，CSV 却会原样写出：不先归一就会出现
# "XLSX 报错、CSV 带毒" 的分叉，两种格式必须走同一条归一路径。
_ILLEGAL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitize_text(value: object) -> str:
    """文本归一：换行统一 → 控制字符 → 公式前缀 → 单元格长度上限。

    XLSX 的 XML 解析会把 CRLF 折成 LF，这是格式本身的限制；CSV 若原样保留 CRLF，
    同一单元格在两种格式里就会有不同的换行，所以两侧统一到 LF。
    """
    text = _as_text(value).replace("\r\n", "\n").replace("\r", "\n")
    text = _ILLEGAL_CHARS.sub(" ", text)
    text = _escape_formula(text)
    if len(text) > _MAX_CELL_CHARS:
        return text[: _MAX_CELL_CHARS - 3] + "..."
    return text


def cell_value(value: object) -> object:
    """XLSX 单元格值：数字保持数字（可继续参与表格计算），其余按文本归一。

    布尔值按文本写，否则 XLSX 显示 TRUE 而 CSV 显示 True，同一份数据两种读法。
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return _sanitize_text(value)


def cell_text(value: object) -> str:
    """CSV 单元格文本：与 XLSX 同一归一路径，保证可见内容逐字符一致。

    两种格式各写一套规则时，XLSX 会拒绝 XML 非法控制字符而 CSV 照写，
    同一任务于是出现"一份能下载、一份报错"的分叉。
    """
    return _as_text(cell_value(value))


class ExcelBuilder:
    """Job 结果 → xlsx 文件。返回 output_path（写入后返回）。

    布局：第一列固定 "filename"，后续列按 job.fields 顺序，列头 = 字段 name。
    字段缺失（values 无该 key）→ 空单元格；空结果（result 为 None/[]）只写表头行。
    输出目录由调用方创建，本类只负责写文件。
    """

    def build(self, job: Job, output_path: Path) -> Path:
        # 表头（字段名）与文件名均为用户可控输入，统一过公式注入转义（P1 安全修复）
        headers = ["filename", *(f.name for f in job.fields)]
        headers = [cell_text(h) for h in headers]

        wb = Workbook()
        ws = wb.active
        assert ws is not None  # 新工作簿必有 active sheet
        ws.append(headers)

        rows: list[list[object]] = []
        col_max_len = [len(h) for h in headers]
        for row in job.result or []:
            values: list[object] = [cell_value(row["file"])]
            for f in job.fields:
                values.append(cell_value(row["values"].get(f.name, "")))
            rows.append(values)
            for i, value in enumerate(values):
                col_max_len[i] = max(col_max_len[i], len(str(value)))

        for values in rows:
            ws.append(values)

        _style_header(ws, len(headers))
        for i, width in enumerate(col_max_len, start=1):
            ws.column_dimensions[get_column_letter(i)].width = min(
                max(width, _MIN_COL_WIDTH), _MAX_COL_WIDTH
            )
        if ws.max_row > 1:
            for row_cells in ws.iter_rows(min_row=2):
                for cell in row_cells:
                    cell.alignment = Alignment(wrap_text=True)

        ws.freeze_panes = "A2"
        wb.save(output_path)
        return output_path


def _escape_formula(value: str) -> str:
    """防 Excel 公式注入：以 = + - @ 开头的字符串值前缀单引号。"""
    if value.startswith(_INJECTION_PREFIXES):
        return "'" + value
    return value


def _as_text(value: object) -> str:
    return "" if value is None else str(value)


def _style_header(ws, ncols: int) -> None:
    fill = PatternFill(fill_type="solid", fgColor=_HEADER_BG)
    font = Font(bold=True, color="FFFFFF")
    thin = Side(style="thin")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for cell in ws[1][:ncols]:
        cell.fill = fill
        cell.font = font
        cell.border = border
