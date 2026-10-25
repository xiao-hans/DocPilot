"""租户作用域任务 API：处理闭环、持久化快照与跨租户不可见性（FR-010 / FR-020 / FR-032）。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.contexts.processing.domain import JobAttempt
from app.platform.db import SessionLocal
from app.main import app
from app.models.job import FileInfo, Job, JobStatus, Progress
from app.models.schema import FieldDefinition
from app.models.persistent_job import PersistentJob
from app.models.schema import INVOICE_TEMPLATE
from app.contexts.processing.manager import get_job_manager
from app.contexts.processing.repository import JobRepository

FIELDS = json.dumps(INVOICE_TEMPLATE)


def _create_job(workspace: dict, files: list[tuple[str, bytes]], fields: str = FIELDS):
    return workspace["client"].post(
        f"/api/v1/tenants/{workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": workspace["csrf"]},
        files=[("files", (name, content, "application/pdf")) for name, content in files],
        data={"fields": fields},
    )


def _upload(workspace: dict, pdf: Path) -> str:
    created = _create_job(workspace, [(pdf.name, pdf.read_bytes())])
    assert created.status_code == 201, created.text
    return created.json()["job_id"]


def test_member_can_process_invoice_and_export_excel(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    client, tenant_id = owner_workspace["client"], owner_workspace["tenant_id"]
    job_id = _upload(owner_workspace, make_invoice_pdf())
    assert len(job_id) == 32

    # TestClient 同步执行后台任务，POST 返回后任务已到终态。
    detail = client.get(f"/api/v1/tenants/{tenant_id}/jobs/{job_id}").json()
    assert detail["status"] == "succeeded"
    assert detail["progress"] == "done"
    assert detail["files"][0]["page_count"] >= 1

    result = client.get(f"/api/v1/tenants/{tenant_id}/jobs/{job_id}/result").json()
    row = result["result"][0]
    assert row["file"] == "invoice-5239.pdf"
    assert set(row["values"]) == {field["name"] for field in INVOICE_TEMPLATE}
    assert row["values"]["开票日期"] == "2025-01-12"

    excel = client.get(f"/api/v1/tenants/{tenant_id}/jobs/{job_id}/excel")
    assert excel.status_code == 200
    assert excel.content[:2] == b"PK"
    assert "attachment" in excel.headers["content-disposition"]

    # 文件按租户/任务分目录存放，不与其他客户混在同一层级。
    assert sorted(p.suffix for p in (sandbox / tenant_id / job_id).iterdir()) == [".pdf"]


def test_upload_validation_rejects_bad_requests(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    client, tenant_id = owner_workspace["client"], owner_workspace["tenant_id"]

    assert (
        client.post(
            f"/api/v1/tenants/{tenant_id}/jobs", data={"fields": FIELDS}
        ).status_code
        == 403
    )
    assert "不支持" in _create_job(owner_workspace, [("note.txt", b"hello")]).json()["detail"]
    assert "重名" in _create_job(owner_workspace, [("a.pdf", b"%PDF"), ("A.PDF", b"%PDF")]).json()["detail"]

    bad_fields = client.post(
        f"/api/v1/tenants/{tenant_id}/jobs",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        files=[("files", ("a.pdf", make_invoice_pdf().read_bytes(), "application/pdf"))],
        data={"fields": "not-json"},
    )
    assert bad_fields.status_code == 400
    assert (sandbox / tenant_id).exists() is False


def test_job_snapshot_is_scoped_to_its_tenant(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    job_id = _upload(owner_workspace, make_invoice_pdf())

    with SessionLocal() as db:
        record = db.get(PersistentJob, job_id)
        assert record is not None
        assert record.tenant_id == owner_workspace["tenant_id"]
        assert record.owner_id == owner_workspace["user_id"]
        assert record.status == "succeeded"

    repository = JobRepository()
    assert repository.get(job_id, owner_workspace["tenant_id"]) is not None
    assert repository.get(job_id, "f" * 32) is None


def test_job_is_invisible_from_another_tenant(
    owner_workspace: dict, other_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    job_id = _upload(owner_workspace, make_invoice_pdf())
    other_client, other_tenant = other_workspace["client"], other_workspace["tenant_id"]

    for suffix in ("", "/result", "/excel"):
        response = other_client.get(f"/api/v1/tenants/{other_tenant}/jobs/{job_id}{suffix}")
        assert response.status_code == 404, suffix
        assert "不存在" in response.json()["detail"] or suffix == "/excel"


def _seed_version(job: Job, tenant_id: str) -> None:
    """R5 之后读路径只认规范化源：给这条合成任务补一行版本记录。"""
    from datetime import datetime, timezone

    from app.platform.db import SessionLocal as _SessionLocal
    from app.models.document import Document, DocumentVersion

    now = datetime.now(timezone.utc)
    with _SessionLocal() as db:
        document_id = "d" * 32
        db.add(Document(id=document_id, tenant_id=tenant_id, created_at=now))
        db.add(
            DocumentVersion(
                id="v" * 32,
                document_id=document_id,
                tenant_id=tenant_id,
                job_id=job.id,
                revision=1,
                original_filename=job.files[0].name,
                storage_key=job.files[0].storage_key,
                checksum="0" * 64,
                media_type="application/pdf",
                size_bytes=1024,
                created_at=now,
            )
        )
        db.commit()


def test_interrupted_job_returns_to_queue_on_startup(
    owner_workspace: dict, sandbox: Path
) -> None:
    """进程中断留下的 processing 记录：超时的由回收退回 queued，未超时的不动（DOC-DD-004 §7.3）。"""
    from app.contexts.processing import service as processing

    job = Job(
        id="a" * 32,
        fields=[FieldDefinition(name="发票号码", type="text", description="20 位发票号码")],
        created_at=Job.now(),
        files=[FileInfo(name="invoice.pdf", storage_key=f"{owner_workspace['tenant_id']}/{'a' * 32}/invoice.pdf")],
        status=JobStatus.PROCESSING,
        progress=Progress.PARSING,
        tenant_id=owner_workspace["tenant_id"],
        owner_id=owner_workspace["user_id"],
    )
    JobRepository().save(job, job.tenant_id or "", job.owner_id)
    _seed_version(job, owner_workspace["tenant_id"])

    manager = get_job_manager()
    manager._jobs.pop(job.id, None)
    # 没有尝试记录且未超时前不得被盲目重新排队：恢复入口统一由 reclaim 决定。
    assert job.id not in manager.pending_job_ids()

    processing.begin_attempt(job.id, job.tenant_id or "")
    with SessionLocal() as db:
        attempt = db.scalar(
            select(JobAttempt).where(JobAttempt.job_id == job.id)
        )
        attempt.started_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        db.commit()
        processing.reclaim_timeouts(db)

    recovered = manager.get_job(job.id, job.tenant_id or "")
    assert recovered.status is JobStatus.QUEUED
    assert recovered.tenant_id == job.tenant_id
    assert job.id in manager.pending_job_ids()


"""退役旧版无租户 `/api/jobs` 时从 `tests/test_api.py` 迁来的边界用例：这些行为在租户作用域
API 上早就存在（校验逻辑是旧版的超集），缺的只是断言。迁移在先、删除在后，避免"删了旧接口
也顺手删掉了它唯一 covering 的用例"这种静默掉覆盖。
"""

import pytest


def _detail(workspace: dict, job_id: str) -> dict:
    return workspace["client"].get(
        f"/api/v1/tenants/{workspace['tenant_id']}/jobs/{job_id}"
    ).json()


def test_upload_rejects_oversize_file_with_413(
    owner_workspace: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超过 MAX_UPLOAD_MB → 413，且是"先校验后落盘"，租户目录不留半成品。"""
    monkeypatch.setattr("app.contexts.processing.api.MAX_UPLOAD_MB", 1)
    resp = _create_job(owner_workspace, [("big.pdf", b"x" * (1024 * 1024 + 1))])
    assert resp.status_code == 413
    assert "文件过大" in resp.json()["detail"]


def test_upload_rejects_too_many_files(owner_workspace: dict, make_invoice_pdf) -> None:
    payload = make_invoice_pdf().read_bytes()
    resp = _create_job(owner_workspace, [(f"f{i}.pdf", payload) for i in range(11)])
    assert resp.status_code == 400
    assert "数量" in resp.json()["detail"]


def test_upload_rejects_empty_field_list(owner_workspace: dict, make_invoice_pdf) -> None:
    resp = _create_job(
        owner_workspace,
        [("f.pdf", make_invoice_pdf().read_bytes())],
        fields=json.dumps([]),
    )
    assert resp.status_code == 400
    assert "字段" in resp.json()["detail"] or "空" in resp.json()["detail"]


def test_upload_rejects_too_many_fields(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """字段数上限的拒绝发生在写盘之前，所以文件内容无关紧要也不能留下对象。"""
    many = [{"name": f"字段{i}", "type": "text", "description": "x"} for i in range(31)]
    resp = _create_job(
        owner_workspace, [("f.pdf", make_invoice_pdf().read_bytes())],
        fields=json.dumps(many, ensure_ascii=False),
    )
    assert resp.status_code == 400
    assert "不能超过" in resp.json()["detail"]
    assert (sandbox / owner_workspace["tenant_id"]).exists() is False


def test_corrupted_document_marks_job_failed(
    owner_workspace: dict, sandbox: Path
) -> None:
    """扩展名合法但内容不是 PDF：任务创建成功、执行判为 failed，错误可读。"""
    resp = _create_job(owner_workspace, [("broken.pdf", b"\x00\x01\x02 not a pdf at all")])
    assert resp.status_code == 201, resp.text
    detail = _detail(owner_workspace, resp.json()["job_id"])
    assert detail["status"] == "failed"
    assert isinstance(detail.get("error"), str) and len(detail["error"]) > 0
    assert detail.get("result") in (None, [])


def test_multi_file_job_returns_one_result_row_per_file(
    owner_workspace: dict, make_invoice_pdf
) -> None:
    """一次任务两份文件 → 每个文件一行结果，值按各自的文档产生，不串。"""
    inv_a, inv_b = "20250112003040001111", "20250112003040002222"
    resp = _create_job(
        owner_workspace,
        [
            ("batch_a.pdf", make_invoice_pdf(inv_a).read_bytes()),
            ("batch_b.pdf", make_invoice_pdf(inv_b).read_bytes()),
        ],
    )
    assert resp.status_code == 201, resp.text
    job_id = resp.json()["job_id"]
    assert _detail(owner_workspace, job_id)["status"] == "succeeded"

    rows = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{job_id}/result"
    ).json()["result"]
    by_file = {row["file"]: row["values"] for row in rows}
    assert set(by_file) == {"batch_a.pdf", "batch_b.pdf"}
    assert by_file["batch_a.pdf"]["发票号码"] == inv_a
    assert by_file["batch_b.pdf"]["发票号码"] == inv_b


def test_anonymous_and_foreign_tenant_cannot_create_jobs(other_workspace: dict) -> None:
    anonymous = TestClient(app)
    assert anonymous.post(
        f"/api/v1/tenants/{other_workspace['tenant_id']}/jobs",
        files=[("files", ("a.pdf", b"%PDF", "application/pdf"))],
        data={"fields": FIELDS},
    ).status_code == 401

    owner_id = "b" * 32
    assert other_workspace["client"].post(
        f"/api/v1/tenants/{owner_id}/jobs",
        headers={"X-CSRF-Token": other_workspace["csrf"]},
        files=[("files", ("a.pdf", b"%PDF", "application/pdf"))],
        data={"fields": FIELDS},
    ).status_code == 403
