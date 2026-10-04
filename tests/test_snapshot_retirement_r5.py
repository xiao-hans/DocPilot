"""快照退役 R5（`docs/20 §3`）：读路径只认规范化源，重建不出来就报错。

这条改的是行为不是等价搬迁，所以判据要能钉住两件事：把快照清空之后读路径**照样正确**
（证明没有任何代码还读它），以及规范化源不完整时**必须失败**（证明不会拼出一个
"字段为空、文件为零"的假任务）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.contexts.processing.domain import JobAttempt
from app.db import SessionLocal
from app.models.document import DocumentVersion
from app.models.job import JobStatus
from app.models.persistent_job import PersistentJob
from app.contexts.review.domain import ExtractionResult
from app.services import job_repository
from app.services.job_repository import IncompleteJobFacts, JobRepository


def _upload(workspace, make_invoice_pdf, serial="01100200011100045239"):
    from app.models.schema import INVOICE_TEMPLATE

    response = workspace["client"].post(
        f"/api/v1/tenants/{workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": workspace["csrf"]},
        files=[("files", (make_invoice_pdf(serial).name, make_invoice_pdf(serial).read_bytes(), "application/pdf"))],
        data={"fields": json.dumps(INVOICE_TEMPLATE)},
    )
    assert response.status_code == 201, response.text
    return response.json()["job_id"]


def _blank_snapshot(job_id: str) -> None:
    with SessionLocal() as db:
        db.get(PersistentJob, job_id).snapshot = {}
        db.commit()


def test_read_path_survives_an_emptied_snapshot(owner_workspace, make_invoice_pdf):
    job_id = _upload(owner_workspace, make_invoice_pdf)
    before = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{job_id}"
    ).json()
    _blank_snapshot(job_id)
    after = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{job_id}"
    ).json()

    assert after == before, "读路径仍依赖快照：清空快照后响应变了"
    assert after["status"] == "succeeded" and after["progress"] == "done"
    assert [item["name"] for item in after["files"]] == [item["name"] for item in before["files"]]
    assert after["result"] and after["result"][0]["values"]["发票号码"] == "01100200011100045239"


def test_rebuilt_files_carry_the_real_storage_key(owner_workspace, make_invoice_pdf):
    """快照换源后 `storage_key` 来自版本行的唯一列：执行链路据此读对象。"""
    job_id = _upload(owner_workspace, make_invoice_pdf)
    job = JobRepository().get(job_id, owner_workspace["tenant_id"])
    with SessionLocal() as db:
        keys = set(
            db.scalars(select(DocumentVersion.storage_key).where(DocumentVersion.job_id == job_id))
        )
    assert keys and {f.storage_key for f in job.files} == keys
    assert all(f.storage_key for f in job.files)


def test_incomplete_normalized_source_fails_loudly(owner_workspace):
    """缺字段定义与版本行的历史行：报错 + 计数，而不是被拼成一条看起来正常的任务。"""
    now = datetime.now(timezone.utc)
    legacy_id = "legacy" + "0" * 26
    with SessionLocal() as db:
        db.add(
            PersistentJob(
                id=legacy_id,
                tenant_id=owner_workspace["tenant_id"],
                owner_id=owner_workspace["user_id"],
                status="succeeded",
                created_at=now,
                attempt_count=1,
                snapshot={"fields": [], "files": [], "status": "succeeded", "progress": "done"},
            )
        )
        db.commit()

    before = job_repository.incomplete_rebuild_count()
    try:
        JobRepository().get(legacy_id, owner_workspace["tenant_id"])
        raise AssertionError("规范化源不完整时必须抛 IncompleteJobFacts")
    except IncompleteJobFacts as exc:
        assert "fields" in str(exc) and "progress" in str(exc) and "files" in str(exc)
    assert job_repository.incomplete_rebuild_count() == before + 1

    response = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{legacy_id}",
    )
    assert response.status_code == 503, response.text


def test_result_reflects_the_authoritative_rows_not_the_run_output(owner_workspace, make_invoice_pdf):
    """`Job.result` 换源到 `extraction_results`：修订值立刻出现在任务详情里。"""
    job_id = _upload(owner_workspace, make_invoice_pdf)
    with SessionLocal() as db:
        for row in db.scalars(select(ExtractionResult).where(ExtractionResult.job_id == job_id)):
            row.values = {**row.values, "发票号码": "人工修订后的号码"}
        db.commit()
    detail = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{job_id}"
    ).json()
    assert detail["result"][0]["values"]["发票号码"] == "人工修订后的号码"


def test_attempt_error_survives_the_rebuild(owner_workspace, make_invoice_pdf):
    """终态任务的 error 来自当次尝试行，不再来自快照。"""
    job_id = _upload(owner_workspace, make_invoice_pdf)
    with SessionLocal() as db:
        attempt = db.scalar(
            select(JobAttempt).where(JobAttempt.job_id == job_id).order_by(JobAttempt.attempt_no.desc())
        )
        attempt.error = "第 3 页无法解析"
        attempt.warnings = ["带过一页"]
        db.commit()
    job = JobRepository().get(job_id, owner_workspace["tenant_id"])
    assert job.error == "第 3 页无法解析"
    assert job.warnings == ["带过一页"]
    assert job.status is JobStatus.SUCCEEDED
