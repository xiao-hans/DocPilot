"""租户作用域任务 API：处理闭环、持久化快照与跨租户不可见性（FR-010 / FR-020 / FR-032）。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models.job import Job, JobStatus, Progress
from app.models.persistent_job import PersistentJob
from app.models.schema import INVOICE_TEMPLATE
from app.services.job_manager import get_job_manager
from app.services.job_repository import JobRepository

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
        assert record.snapshot["status"] == "succeeded"

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


def test_interrupted_job_returns_to_queue_on_startup(
    owner_workspace: dict, sandbox: Path
) -> None:
    """进程中断时留下的 processing 记录应在下次启动恢复阶段降级为 queued。"""
    job = Job(
        id="a" * 32,
        fields=[],
        created_at=Job.now(),
        status=JobStatus.PROCESSING,
        progress=Progress.PARSING,
        tenant_id=owner_workspace["tenant_id"],
        owner_id=owner_workspace["user_id"],
    )
    JobRepository().save(job, job.tenant_id or "", job.owner_id)

    manager = get_job_manager()
    manager._jobs.pop(job.id, None)
    assert job.id in manager.pending_job_ids()

    recovered = manager.get_job(job.id, job.tenant_id or "")
    assert recovered.status is JobStatus.QUEUED
    assert recovered.tenant_id == job.tenant_id


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
