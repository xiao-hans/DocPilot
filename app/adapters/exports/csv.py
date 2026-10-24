"""CSV 导出：与 XLSX 共用同一列顺序与公式注入防护（DOC-DD-005 §6.4）。"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from app.models.job import Job
from app.adapters.exports.excel import cell_text

BOM = "﻿"


def _safe(value: object) -> str:
    """与 XLSX 走同一个单元格归一函数（换行统一、控制字符、公式前缀、长度上限）。

    两种格式各写一套规则时，同一份"已核对"数据会导出不同字节，用户无法判断哪一份
    才是复核过的内容；制表符等可安全承载的字符仍然原样保留。
    """
    return cell_text(value)


def build(job: Job, destination: Path) -> Path:
    """生成带 BOM 的全引号 CSV，列顺序与 XLSX 完全一致。"""
    header = ["filename", *[field.name for field in job.fields]]
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow([_safe(name) for name in header])
    for row in job.result or []:
        values = row.get("values") or {}
        writer.writerow(
            [_safe(row.get("file", ""))]
            + [_safe(values.get(field.name, "")) for field in job.fields]
        )
    destination.write_text(BOM + buffer.getvalue(), encoding="utf-8", newline="")
    return destination
