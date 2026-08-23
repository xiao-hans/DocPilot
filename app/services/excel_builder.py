"""把任务结果（job.result）导出为 .xlsx 文件。"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.models.job import Job

_INJECTION_PREFIXES = ("=", "+", "-", "@")  # Excel 公式注入前缀
_MIN_COL_WIDTH = 10
_MAX_COL_WIDTH = 60
_HEADER_BG = "1F4E79"  # 藏青


class ExcelBuilder:
    """Job 结果 → xlsx 文件。返回 output_path（写入后返回）。

    布局：第一列固定 "filename"，后续列按 job.fields 顺序，列头 = 字段 name。
    字段缺失（values 无该 key）→ 空单元格；空结果（result 为 None/[]）只写表头行。
    输出目录由调用方创建，本类只负责写文件。
    """

    def build(self, job: Job, output_path: Path) -> Path:
        # 表头（字段名）与文件名均为用户可控输入，统一过公式注入转义（P1 安全修复）
        headers = ["filename", *(f.name for f in job.fields)]
        headers = [_escape_formula(h) for h in headers]

        wb = Workbook()
        ws = wb.active
        assert ws is not None  # 新工作簿必有 active sheet
        ws.append(headers)

        rows: list[list[object]] = []
        col_max_len = [len(h) for h in headers]
        for row in job.result or []:
            values: list[object] = [_escape_formula(_as_text(row["file"]))]
            for f in job.fields:
                value = row["values"].get(f.name, "")
                if isinstance(value, str):
                    value = _escape_formula(value)
                values.append(value)
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
