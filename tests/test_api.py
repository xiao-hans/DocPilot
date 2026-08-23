"""API 层端到端测试：上传、状态查询、错误校验、任务失败、边界场景。

约定：
- TestClient 同步执行 BackgroundTasks → POST 返回后任务已完成，直接断言终态
- 每个测试通过 monkeypatch 将 UPLOAD_DIR / OUTPUT_DIR 指向 tmp_path，实现隔离
- get_job_manager() 是模块级单例，测试间任务会累积，禁止断言"列表为空"类假设
"""

from __future__ import annotations

import json
import uuid
from io import BytesIO
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.main import app
from app.models.schema import INVOICE_TEMPLATE
from app.services.job_manager import get_job_manager


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """提供隔离 UPLOAD_DIR / OUTPUT_DIR 的 TestClient（上传与 Excel 落盘都进 tmp_path）。"""
    monkeypatch.setattr("app.api.routes.UPLOAD_DIR", tmp_path)
    monkeypatch.setattr("app.api.routes.OUTPUT_DIR", tmp_path)
    with TestClient(app) as c:
        yield c


# ---------- 工具函数 ----------


def _make_invoice_pdf(
    path: Path,
    invoice_no: str = "01100200011100045239",
    seller: str = "恒信达科技有限公司",
    buyer: str = "测试购买方有限公司",
    amount: str = "1280.50",
    tax: str = "76.83",
    date: str = "2025-01-12",
) -> None:
    """生成含发票文本的 PDF，确保 mock 提取器能命中所有字段。"""
    doc = pymupdf.open()
    page = doc.new_page()
    text = (
        f"增值税普通发票\n"
        f"发票号码: {invoice_no}\n"
        f"开票日期: {date}\n"
        f"销售方: {seller}\n"
        f"购买方: {buyer}\n"
        f"价税合计(大写): 壹仟贰佰捌拾元伍角整\n"
        f"价税合计(小写): ¥{amount}元\n"
        f"税额: ¥{tax}元\n"
    )
    page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def _fields_json(fields: list[dict] | None = None) -> str:
    """将字段列表序列化为 API 需要的 JSON 字符串。"""
    return json.dumps(
        fields if fields is not None else INVOICE_TEMPLATE, ensure_ascii=False
    )


# ================================================================
# 1. 上传成功
# ================================================================


def test_upload_success(client: TestClient, tmp_path: Path):
    """上传含发票文本的 PDF → 201 → status=succeeded → result 含正确字段值。"""
    pdf = tmp_path / "invoice_success.pdf"
    _make_invoice_pdf(pdf, invoice_no="20250112003040001234")

    resp = client.post(
        "/api/jobs",
        files=[("files", ("invoice_success.pdf", pdf.read_bytes(), "application/pdf"))],
        data={"fields": _fields_json()},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "job_id" in body
    assert body["status"] == "queued"

    job_id = body["job_id"]

    # GET /api/jobs/{id} — TestClient 已同步执行 BackgroundTasks，直接查终态
    detail = client.get(f"/api/jobs/{job_id}")
    assert detail.status_code == 200
    data = detail.json()
    assert data["status"] == "succeeded"
    assert data["progress"] == "done"

    # result 恰 1 行
    assert isinstance(data["result"], list)
    assert len(data["result"]) == 1
    row = data["result"][0]
    assert "file" in row
    assert "values" in row

    # 发票号码为 20 位数字
    inv_val = row["values"].get("发票号码", "")
    assert inv_val == "20250112003040001234", f"发票号码不匹配: {inv_val}"
    assert inv_val.isdigit() and len(inv_val) == 20

    # 文件落盘检查 — 注意：UPLOAD_DIR 已 monkeypatch 到 tmp_path
    job_dir = tmp_path / job_id
    assert job_dir.is_dir(), f"任务目录不存在: {job_dir}"
    saved = list(job_dir.iterdir())
    assert len(saved) == 1
    assert saved[0].name == "invoice_success.pdf"
    assert saved[0].stat().st_size > 0


# ================================================================
# 2. 状态查询
# ================================================================


def test_get_job_status(client: TestClient, tmp_path: Path):
    """GET /api/jobs/{id} → 200 + 包含 id/status/progress 键。"""
    pdf = tmp_path / "q.pdf"
    _make_invoice_pdf(pdf)

    resp = client.post(
        "/api/jobs",
        files=[("files", ("q.pdf", pdf.read_bytes(), "application/pdf"))],
        data={"fields": _fields_json()},
    )
    job_id = resp.json()["job_id"]

    detail = client.get(f"/api/jobs/{job_id}")
    assert detail.status_code == 200
    data = detail.json()
    assert "id" in data
    assert data["id"] == job_id
    assert "status" in data
    assert "progress" in data
    # to_dict 不含磁盘 path
    assert "path" not in data


def test_get_job_not_found(client: TestClient):
    """GET /api/jobs/{不存在} → 404。"""
    fake_id = uuid.uuid4().hex[:12]
    resp = client.get(f"/api/jobs/{fake_id}")
    assert resp.status_code == 404


# ================================================================
# 3. 不支持格式
# ================================================================


def test_unsupported_format_txt(client: TestClient, tmp_path: Path):
    """上传 .txt → 400 + detail 含"不支持"。"""
    txt = tmp_path / "notes.txt"
    txt.write_text("hello")

    resp = client.post(
        "/api/jobs",
        files=[("files", ("notes.txt", txt.read_bytes(), "text/plain"))],
        data={"fields": _fields_json()},
    )
    assert resp.status_code == 400
    detail = resp.json().get("detail", "")
    assert "不支持" in detail


def test_unsupported_format_docx(client: TestClient, tmp_path: Path):
    """上传 .docx → 400 + detail 含"不支持"。"""
    docx = tmp_path / "report.docx"
    docx.write_bytes(b"PK\x03\x04fake-docx")

    resp = client.post(
        "/api/jobs",
        files=[
            (
                "files",
                (
                    "report.docx",
                    docx.read_bytes(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ),
            )
        ],
        data={"fields": _fields_json()},
    )
    assert resp.status_code == 400
    assert "不支持" in resp.json().get("detail", "")


# ================================================================
# 4. 任务失败（损坏 PDF）
# ================================================================


def test_corrupted_pdf_fails(client: TestClient, tmp_path: Path):
    """扩展名 .pdf 但内容是垃圾字节 → 201 创建 → GET 后 status=failed。"""
    garbage = tmp_path / "broken.pdf"
    garbage.write_bytes(b"\x00\x01\x02\x03this is not a pdf at all\x00\x00\x00")

    resp = client.post(
        "/api/jobs",
        files=[("files", ("broken.pdf", garbage.read_bytes(), "application/pdf"))],
        data={"fields": _fields_json()},
    )
    assert resp.status_code == 201, resp.text
    job_id = resp.json()["job_id"]

    detail = client.get(f"/api/jobs/{job_id}")
    assert detail.status_code == 200
    data = detail.json()
    assert data["status"] == "failed"
    assert data["error"] is not None
    assert len(data["error"]) > 0
    # result 可以是 None 或空列表 — 任一都合法
    assert data["result"] is None or data["result"] == []


# ================================================================
# 5. 边界场景
# ================================================================


def test_no_files_returns_400(client: TestClient):
    """无文件上传 → 400。"""
    resp = client.post(
        "/api/jobs",
        files=[],
        data={"fields": _fields_json()},
    )
    assert resp.status_code == 400


def test_missing_fields_returns_422(client: TestClient, tmp_path: Path):
    """fields 缺失 → FastAPI 表单校验层返回 422（Form(...) 是必填参数）。

    注：实际返回 422 是因为 fields: str = Form(...) 声明了必填，
    FastAPI/Starlette 的表单校验会拦截。如果未来改为 400 则同步更新断言。
    """
    pdf = tmp_path / "f.pdf"
    _make_invoice_pdf(pdf)

    resp = client.post(
        "/api/jobs",
        files=[("files", ("f.pdf", pdf.read_bytes(), "application/pdf"))],
        # 不传 fields
    )
    assert resp.status_code == 422, (
        f"期望 422（缺少必填表单字段），实际 {resp.status_code}"
    )


def test_invalid_json_fields_returns_400(client: TestClient, tmp_path: Path):
    """fields 不是合法 JSON → 400。"""
    pdf = tmp_path / "f.pdf"
    _make_invoice_pdf(pdf)

    resp = client.post(
        "/api/jobs",
        files=[("files", ("f.pdf", pdf.read_bytes(), "application/pdf"))],
        data={"fields": "NOT-VALID-JSON"},
    )
    assert resp.status_code == 400
    assert "JSON" in resp.json().get("detail", "")


def test_empty_array_fields_returns_400(client: TestClient, tmp_path: Path):
    """fields=[] → validate_fields 抛 ValueError → 400。"""
    pdf = tmp_path / "f.pdf"
    _make_invoice_pdf(pdf)

    resp = client.post(
        "/api/jobs",
        files=[("files", ("f.pdf", pdf.read_bytes(), "application/pdf"))],
        data={"fields": _fields_json([])},
    )
    assert resp.status_code == 400
    assert "空" in resp.json().get("detail", "") or "字段" in resp.json().get(
        "detail", ""
    )


def test_multi_file_batch(client: TestClient, tmp_path: Path):
    """多文件批量上传（2 个不同发票号）→ result 2 行，各自 values 正确。"""
    inv_a = "20250112003040001111"
    inv_b = "20250112003040002222"

    pdf_a = tmp_path / "batch_a.pdf"
    pdf_b = tmp_path / "batch_b.pdf"
    _make_invoice_pdf(pdf_a, invoice_no=inv_a)
    _make_invoice_pdf(pdf_b, invoice_no=inv_b)

    resp = client.post(
        "/api/jobs",
        files=[
            ("files", ("batch_a.pdf", pdf_a.read_bytes(), "application/pdf")),
            ("files", ("batch_b.pdf", pdf_b.read_bytes(), "application/pdf")),
        ],
        data={"fields": _fields_json()},
    )
    assert resp.status_code == 201, resp.text
    job_id = resp.json()["job_id"]

    detail = client.get(f"/api/jobs/{job_id}")
    assert detail.status_code == 200
    data = detail.json()
    assert data["status"] == "succeeded"
    assert isinstance(data["result"], list)
    assert len(data["result"]) == 2

    # 按文件名定位结果行（顺序由 pipeline 决定，可能与上传顺序一致也可能不一致）
    by_file = {r["file"]: r for r in data["result"]}
    assert "batch_a.pdf" in by_file
    assert "batch_b.pdf" in by_file
    assert by_file["batch_a.pdf"]["values"].get("发票号码") == inv_a
    assert by_file["batch_b.pdf"]["values"].get("发票号码") == inv_b


def test_get_job_result_endpoint(client: TestClient, tmp_path: Path):
    """GET /api/jobs/{id}/result 返回结构与 GET /api/jobs/{id} 中的 status 一致。"""
    pdf = tmp_path / "r.pdf"
    _make_invoice_pdf(pdf)

    resp = client.post(
        "/api/jobs",
        files=[("files", ("r.pdf", pdf.read_bytes(), "application/pdf"))],
        data={"fields": _fields_json()},
    )
    job_id = resp.json()["job_id"]

    result_resp = client.get(f"/api/jobs/{job_id}/result")
    assert result_resp.status_code == 200
    r = result_resp.json()
    # 结构包含必要键
    for key in ("job_id", "status", "progress", "result", "warnings", "error"):
        assert key in r, f"缺少键: {key}"
    assert r["job_id"] == job_id
    assert r["status"] == "succeeded"
    assert r["progress"] == "done"
    assert isinstance(r["result"], list) and len(r["result"]) == 1


def test_get_job_result_not_found(client: TestClient):
    """GET /api/jobs/{不存在}/result → 404。"""
    fake_id = uuid.uuid4().hex[:12]
    resp = client.get(f"/api/jobs/{fake_id}/result")
    assert resp.status_code == 404


def test_index_page(client: TestClient):
    """GET / → 200 + 包含 doctype（接受大小写）。"""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "doctype" in resp.text.lower()


# ================================================================
# 6. 边界补充（Reviewer 建议补测的缺口）
# ================================================================


def test_oversize_file_returns_413(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """单文件超过 MAX_UPLOAD_MB → 413。缩小阈值以便快速构造大文件。"""
    monkeypatch.setattr("app.api.routes.MAX_UPLOAD_MB", 1)  # 1MB 上限
    big = b"x" * (1024 * 1024 + 1)

    resp = client.post(
        "/api/jobs",
        files=[("files", ("big.pdf", big, "application/pdf"))],
        data={"fields": _fields_json()},
    )
    assert resp.status_code == 413
    assert "文件过大" in resp.json()["detail"]


def test_too_many_files_returns_400(client: TestClient, tmp_path: Path):
    """文件数量超过 MAX_FILES_PER_JOB → 400。"""
    pdf = tmp_path / "f.pdf"
    _make_invoice_pdf(pdf)
    payload = pdf.read_bytes()

    files = [("files", (f"f{i}.pdf", payload, "application/pdf")) for i in range(11)]
    resp = client.post(
        "/api/jobs",
        files=files,
        data={"fields": _fields_json()},
    )
    assert resp.status_code == 400
    assert "数量" in resp.json()["detail"]


def test_duplicate_filename_returns_400(client: TestClient, tmp_path: Path):
    """同任务内同名文件 → 400（防静默覆盖）。"""
    pdf = tmp_path / "f.pdf"
    _make_invoice_pdf(pdf)
    payload = pdf.read_bytes()

    resp = client.post(
        "/api/jobs",
        files=[
            ("files", ("same.pdf", payload, "application/pdf")),
            ("files", ("same.pdf", payload, "application/pdf")),
        ],
        data={"fields": _fields_json()},
    )
    assert resp.status_code == 400
    assert "重名" in resp.json()["detail"]


def test_oversize_fields_count_returns_400(client: TestClient):
    """字段数超过上限（validate_fields 内部 30 个）→ 400。"""
    many = [{"name": f"字段{i}", "type": "text", "description": "x"} for i in range(31)]
    tiny_pdf = b"%PDF-1.7 fake"  # 校验 fields 失败发生在写盘前，文件内容无关

    resp = client.post(
        "/api/jobs",
        files=[("files", ("f.pdf", tiny_pdf, "application/pdf"))],
        data={"fields": json.dumps(many, ensure_ascii=False)},
    )
    assert resp.status_code == 400
    assert "不能超过" in resp.json()["detail"]


# ================================================================
# 7. Excel 下载端点（GET /api/jobs/{job_id}/excel）
# ================================================================


_EXCEL_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_download_excel_success(client: TestClient, tmp_path: Path):
    """完整链路：上传真实发票 PDF → succeeded → GET /excel → 200 + 合法 xlsx 读回正确。"""
    invoice_no = "20250112003040009999"
    pdf = tmp_path / "dl.pdf"
    _make_invoice_pdf(pdf, invoice_no=invoice_no)

    resp = client.post(
        "/api/jobs",
        files=[("files", ("dl.pdf", pdf.read_bytes(), "application/pdf"))],
        data={"fields": _fields_json()},
    )
    assert resp.status_code == 201, resp.text
    job_id = resp.json()["job_id"]

    detail = client.get(f"/api/jobs/{job_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "succeeded"

    dl = client.get(f"/api/jobs/{job_id}/excel")
    assert dl.status_code == 200
    ct = dl.headers.get("content-type", "")
    assert ct.startswith(_EXCEL_MEDIA_TYPE), f"Content-Type={ct!r}"
    cd = dl.headers.get("content-disposition", "")
    assert "attachment" in cd, f"Content-Disposition={cd!r}"
    assert f"{job_id}.xlsx" in cd, f"Content-Disposition={cd!r}"

    # 响应体是合法 xlsx：openpyxl 读回，行/列正确
    wb = load_workbook(BytesIO(dl.content))
    ws = wb.active
    assert ws is not None
    headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
    assert headers == ["filename", *(f["name"] for f in INVOICE_TEMPLATE)]
    assert ws.max_row == 2
    assert ws["A2"].value == "dl.pdf"
    assert ws["B2"].value == invoice_no
    # 数字类型保真：价税合计列为数值而非字符串
    header_col = {ws.cell(1, c).value: c for c in range(1, ws.max_column + 1)}
    total = ws.cell(2, header_col["价税合计"]).value
    assert isinstance(total, (int, float)) and not isinstance(total, bool), (
        f"价税合计应为数字，实际 {total!r}"
    )


def test_download_excel_job_not_found(client: TestClient):
    """GET /excel 对不存在的 job → 404 '任务不存在'。"""
    fake_id = uuid.uuid4().hex[:12]
    resp = client.get(f"/api/jobs/{fake_id}/excel")
    assert resp.status_code == 404
    assert "任务不存在" in resp.json().get("detail", "")


def test_download_excel_failed_job_returns_404(client: TestClient, tmp_path: Path):
    """上传损坏 PDF → job failed → GET /excel → 404 '任务未完成或失败'。"""
    garbage = tmp_path / "broken.pdf"
    garbage.write_bytes(b"\x00\x01\x02\x03this is not a pdf at all\x00\x00\x00")

    resp = client.post(
        "/api/jobs",
        files=[("files", ("broken.pdf", garbage.read_bytes(), "application/pdf"))],
        data={"fields": _fields_json()},
    )
    assert resp.status_code == 201, resp.text
    job_id = resp.json()["job_id"]

    detail = client.get(f"/api/jobs/{job_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "failed"

    dl = client.get(f"/api/jobs/{job_id}/excel")
    assert dl.status_code == 404
    assert "任务未完成或失败" in dl.json().get("detail", "")
    # failed 任务不应落盘 Excel
    assert not (tmp_path / f"{job_id}.xlsx").exists()


def test_download_excel_idempotent_and_sets_excel_path(client: TestClient, tmp_path: Path):
    """幂等：两次 GET /excel → 两次 200 且字节一致；job.excel_path 被设置且文件在。"""
    pdf = tmp_path / "idem.pdf"
    _make_invoice_pdf(pdf)

    resp = client.post(
        "/api/jobs",
        files=[("files", ("idem.pdf", pdf.read_bytes(), "application/pdf"))],
        data={"fields": _fields_json()},
    )
    assert resp.status_code == 201, resp.text
    job_id = resp.json()["job_id"]

    first = client.get(f"/api/jobs/{job_id}/excel")
    second = client.get(f"/api/jobs/{job_id}/excel")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.content == second.content, "两次下载字节应一致"

    # 落盘位置：OUTPUT_DIR（已 monkeypatch 到 tmp_path）/<job_id>.xlsx，并回填 excel_path
    job = get_job_manager().get_job(job_id)
    assert job.excel_path is not None, "job.excel_path 应被设置"
    excel_file = Path(job.excel_path)
    assert excel_file.is_file()
    assert excel_file.name == f"{job_id}.xlsx"
    assert excel_file.read_bytes() == first.content
