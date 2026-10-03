"""XLSX 与 CSV 导出一致性与注入防护（TC-FUNC-028、TC-SEC-015）。"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from openpyxl import load_workbook

from app.db import SessionLocal
from app.models.access import AuditEvent
from app.models.persistent_job import PersistentJob
from app.models.schema import INVOICE_TEMPLATE

FIELDS = json.dumps(INVOICE_TEMPLATE)


def _upload(workspace: dict, pdf: Path) -> str:
    response = workspace["client"].post(
        f"/api/v1/tenants/{workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": workspace["csrf"]},
        files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))],
        data={"fields": FIELDS},
    )
    assert response.status_code == 201, response.text
    return response.json()["job_id"]


def _result_id(workspace: dict, job_id: str) -> str:
    listed = workspace["client"].get(
        f"/api/v1/tenants/{workspace['tenant_id']}/results?job_id={job_id}"
    ).json()["results"]
    assert len(listed) == 1
    return listed[0]["id"]


def _csv_rows(content: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))


def test_csv_and_xlsx_share_columns_and_escape_formulas(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-FUNC-028：两种格式列顺序一致，注入前缀按同一规则转义。"""
    base = f"/api/v1/tenants/{owner_workspace['tenant_id']}"
    job_id = _upload(owner_workspace, make_invoice_pdf())
    result_id = _result_id(owner_workspace, job_id)

    hostile = {
        "发票号码": "=1+1",
        "开票日期": "2025-01-12",
        "销售方名称": "+8800123456",
        "购买方名称": "@SUM(A1:A9)",
        "金额合计": "-2+3",
        "税额": "普通\t制表符",
    }
    detail = owner_workspace["client"].get(f"{base}/results/{result_id}").json()
    # 注入样本按模板实际字段生成，避免测试与模板字段名耦合
    names = [field["name"] for field in detail["fields"]]
    injections = ["=1+1", "+8800123456", "@SUM(A1:A9)", "-2+3", "普通	z", "2025-01-12"]
    hostile = {name: injections[index % len(injections)] for index, name in enumerate(names)}
    revised = owner_workspace["client"].patch(
        f"{base}/results/{result_id}",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        json={"values": hostile, "expected_revision": detail["revision_no"]},
    )
    assert revised.status_code == 200, revised.text

    unknown = owner_workspace["client"].patch(
        f"{base}/results/{result_id}",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        json={"values": {**hostile, "不存在的字段": "x"}, "expected_revision": 2},
    )
    assert unknown.status_code == 422

    csv_response = owner_workspace["client"].get(f"{base}/jobs/{job_id}/export?format=csv")
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert csv_response.content.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM

    rows = _csv_rows(csv_response.content)
    assert rows[0] == ["filename", *[field["name"] for field in INVOICE_TEMPLATE]]
    assert len(rows) == 2
    values = rows[1][1:]
    assert all(not value.startswith(("=", "+", "@")) for value in values), values
    assert all(not value.startswith("-") or value.startswith("'") for value in values), values
    assert "\t" not in rows[1][0]

    xlsx_response = owner_workspace["client"].get(f"{base}/jobs/{job_id}/export")
    assert xlsx_response.status_code == 200
    assert xlsx_response.content[:2] == b"PK"

    sheet = load_workbook(io.BytesIO(xlsx_response.content)).active
    header = [cell.value for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
    body = [cell.value for cell in next(sheet.iter_rows(min_row=2, max_row=2))]
    assert header == rows[0]
    # 两种格式必须给出同样的转义结果，否则复制 CSV 用户会拿到可执行单元格。
    assert body[1:] == values, (body, values)


def test_export_is_audited_and_tenant_scoped(
    owner_workspace: dict, identity, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-SEC-015：导出动作写审计；他租户无法按 ID 导出。"""
    job_id = _upload(owner_workspace, make_invoice_pdf())
    base = f"/api/v1/tenants/{owner_workspace['tenant_id']}"
    assert owner_workspace["client"].get(f"{base}/jobs/{job_id}/export?format=csv").status_code == 200
    assert owner_workspace["client"].get(f"{base}/jobs/{job_id}/export").status_code == 200

    with SessionLocal() as db:
        # 不能依赖数据库返回顺序：SQLite 恰好按插入顺序返回，PostgreSQL 对无 ORDER BY 的
        # 查询不保证任何顺序，同一用例在两种方言下会得到不同排列。
        events = (
            db.query(AuditEvent)
            .filter(AuditEvent.action == "job.exported")
            .order_by(AuditEvent.created_at, AuditEvent.id)
            .all()
        )
    assert sorted(event.metadata_.get("format") for event in events) == ["csv", "xlsx"]
    assert all(event.object_id == job_id for event in events)
    assert all(str(sandbox) not in json.dumps(event.metadata_) for event in events)

    outsider = identity("outsider-export@example.com", "外部空间", "owner")
    assert (
        outsider["client"]
        .get(f"/api/v1/tenants/{outsider['tenant_id']}/jobs/{job_id}/export?format=csv")
        .status_code
        == 404
    )
    assert outsider["client"].get(f"{base}/jobs/{job_id}/export?format=csv").status_code == 403


def test_export_rejects_unfinished_jobs_and_bad_formats(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    job_id = _upload(owner_workspace, make_invoice_pdf())
    base = f"/api/v1/tenants/{owner_workspace['tenant_id']}"
    with SessionLocal() as db:
        db.get(PersistentJob, job_id).status = "failed"
        db.commit()

    assert owner_workspace["client"].get(f"{base}/jobs/{job_id}").json()["status"] == "failed"
    assert owner_workspace["client"].get(f"{base}/jobs/{job_id}/excel").status_code == 404
    assert owner_workspace["client"].get(f"{base}/jobs/{job_id}/export?format=csv").status_code == 404
    assert owner_workspace["client"].get(f"{base}/jobs/{job_id}/export?format=xml").status_code == 422


def test_csv_and_xlsx_share_control_chars_whitespace_and_overlong_cells(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-FUNC-028 强化：XLSX 拒绝的字符与超长值必须与 CSV 同一归一结果。"""
    base = f"/api/v1/tenants/{owner_workspace['tenant_id']}"
    job_id = _upload(owner_workspace, make_invoice_pdf())
    result_id = _result_id(owner_workspace, job_id)
    detail = owner_workspace["client"].get(f"{base}/results/{result_id}").json()
    names = [field["name"] for field in detail["fields"]]

    awkward = ["\x00控制\x07字符", "  =1+1", "x" * 40000, "换行\r\n结尾", 1234.5, -7]
    values = {**detail["values"]}
    for name, value in zip(names, awkward, strict=False):
        values[name] = value
    revised = owner_workspace["client"].patch(
        f"{base}/results/{result_id}",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        json={"values": values, "expected_revision": detail["revision_no"]},
    )
    assert revised.status_code == 200, revised.text

    csv_rows = _csv_rows(
        owner_workspace["client"].get(f"{base}/jobs/{job_id}/export?format=csv").content
    )[1][1:]
    sheet = load_workbook(
        io.BytesIO(owner_workspace["client"].get(f"{base}/jobs/{job_id}/export").content)
    ).active
    xlsx_cells = [cell.value for cell in next(sheet.iter_rows(min_row=2, max_row=2))][1:]

    assert [str(cell) for cell in xlsx_cells] == csv_rows, (xlsx_cells, csv_rows)
    assert "\x00" not in "".join(csv_rows) and "\x07" not in "".join(csv_rows)
    assert "  =1+1" in csv_rows  # 前导空白的等号不会被 Excel 当公式，两侧都原样保留
    assert any(len(row) == 32767 for row in csv_rows)  # 超长值在同一个上限处截断
    assert 1234.5 in xlsx_cells  # 数字仍是数字，可在表格里继续计算
