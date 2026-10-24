"""幂等提交、尝试记录、超时回收与 outbox 一致性（TC-FUNC-017..021、TC-SEC-013、TC-REC-004）。"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.platform.config import JOB_MAX_ATTEMPTS
from app.contexts.processing import service as processing
from app.contexts.processing.domain import JobAttempt
from app.platform.db import SessionLocal
from app.main import app
from app.models.access import AuditEvent
from app.models.document import Document, DocumentVersion
from app.models.job import FileInfo, Job, JobStatus
from app.models.outbox import DomainEventRecord
from app.models.persistent_job import PersistentJob
from app.models.schema import FieldDefinition
from app.models.schema import INVOICE_TEMPLATE
from app.shared import outbox
from app.contexts.processing.manager import get_job_manager
from app.contexts.processing.repository import JobRepository

FIELDS = json.dumps(INVOICE_TEMPLATE)
BS = chr(92)
UNC_PATH = BS * 2 + "fileserver" + BS + "share" + BS + "a.pdf"  # UNC 形式：两个反斜杠开头
TENANT_ID = "t" + "0" * 31
JOB_ID = "j" + "0" * 31


def _upload(workspace: dict, pdf: Path, key: str | None = None, tenant_id: str | None = None):
    headers = {"X-CSRF-Token": workspace["csrf"]}
    if key:
        headers["Idempotency-Key"] = key
    return workspace["client"].post(
        f"/api/v1/tenants/{tenant_id or workspace['tenant_id']}/jobs",
        headers=headers,
        files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))],
        data={"fields": FIELDS},
    )


def _job_rows() -> list[PersistentJob]:
    with SessionLocal() as db:
        return list(db.scalars(select(PersistentJob)).all())


def test_idempotency_key_reuses_job_without_second_upload(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    tenant_id = owner_workspace["tenant_id"]
    first = _upload(owner_workspace, make_invoice_pdf(), key="batch-001")
    assert first.status_code == 201
    assert first.json()["reused"] is False

    second = _upload(owner_workspace, make_invoice_pdf("01100200011100045240"), key="batch-001")
    assert second.status_code == 200
    assert second.json()["reused"] is True
    assert second.json()["job_id"] == first.json()["job_id"]

    rows = _job_rows()
    assert len(rows) == 1
    assert rows[0].idempotency_key == "batch-001"
    assert len(list((sandbox / tenant_id).iterdir())) == 1
    with SessionLocal() as db:
        assert db.query(DocumentVersion).count() == 1


def test_concurrent_idempotent_submits_create_one_job(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """同一用户两个客户端并发提交同一幂等键：只允许落一个任务。"""
    outcomes: list[tuple[int, dict]] = []
    barrier = threading.Barrier(2)

    def submit(client: TestClient, csrf: str) -> None:
        pdf = make_invoice_pdf()
        barrier.wait()
        response = _upload(
            {"client": client, "csrf": csrf, "tenant_id": owner_workspace["tenant_id"]}, pdf, key="race-001"
        )
        outcomes.append((response.status_code, response.json()))

    other = TestClient(app)
    login = other.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "test-password-1234"})
    assert login.status_code == 200

    threads = [
        threading.Thread(target=submit, args=(owner_workspace["client"], owner_workspace["csrf"])),
        threading.Thread(target=submit, args=(other, login.json()["csrf_token"])),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(_job_rows()) == 1, outcomes
    statuses = sorted(code for code, _ in outcomes)
    assert statuses == [200, 201], outcomes
    reused = [body for code, body in outcomes if code == 200]
    assert reused[0]["job_id"] == [body for _, body in outcomes if _ == 201][0]["job_id"]

def test_idempotency_key_is_scoped_per_tenant(
    owner_workspace: dict, other_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    owner_job = _upload(owner_workspace, make_invoice_pdf(), key="shared-key")
    other_job = _upload(other_workspace, make_invoice_pdf("01100200011100045240"), key="shared-key")
    assert owner_job.status_code == 201
    assert other_job.status_code == 201
    assert owner_job.json()["job_id"] != other_job.json()["job_id"]
    assert len(_job_rows()) == 2


def test_retry_creates_attempts_and_stops_at_limit(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    tenant_id = owner_workspace["tenant_id"]
    job_id = _upload(owner_workspace, make_invoice_pdf()).json()["job_id"]

    succeeded = owner_workspace["client"].post(
        f"/api/v1/tenants/{tenant_id}/jobs/{job_id}/retry",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
    )
    assert succeeded.status_code == 409, succeeded.text

    _force_failed(job_id, attempt_count=1)
    retried = owner_workspace["client"].post(
        f"/api/v1/tenants/{tenant_id}/jobs/{job_id}/retry",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
    )
    assert retried.status_code == 202, retried.text
    detail = owner_workspace["client"].get(f"/api/v1/tenants/{tenant_id}/jobs/{job_id}").json()
    assert detail["attempt_count"] == 2
    assert [item["attempt_no"] for item in detail["attempts"]] == [1, 2]
    assert detail["status"] == "succeeded"

    actions = [
        event.action
        for event in SessionLocal().query(AuditEvent).filter(AuditEvent.action == "job.retried").all()
    ]
    assert len(actions) == 1

    _force_failed(job_id, attempt_count=JOB_MAX_ATTEMPTS)
    exhausted = owner_workspace["client"].post(
        f"/api/v1/tenants/{tenant_id}/jobs/{job_id}/retry",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
    )
    assert exhausted.status_code == 409


def test_timeout_reclaim_respects_attempt_limit(sandbox: Path) -> None:
    job = _seed_processing_job(attempt_count=1)
    _backdate_running_attempt(job.id, seconds=3600)

    reclaimed = processing.reclaim_timeouts(SessionLocal())
    assert job.id in reclaimed["requeued"]
    with SessionLocal() as db:
        assert db.get(PersistentJob, job.id).status == "queued"
        attempt = db.scalar(select(JobAttempt).where(JobAttempt.job_id == job.id))
        assert attempt.status == "timed_out"

    job2 = _seed_processing_job(attempt_count=JOB_MAX_ATTEMPTS)
    _backdate_running_attempt(job2.id, seconds=3600)
    reclaimed = processing.reclaim_timeouts(SessionLocal())
    assert job2.id in reclaimed["failed"]
    with SessionLocal() as db:
        record = db.get(PersistentJob, job2.id)
        assert record.status == "failed"
        assert record.finished_at is not None
        attempt = db.scalar(
            select(JobAttempt)
            .where(JobAttempt.job_id == job2.id)
            .order_by(JobAttempt.attempt_no.desc())
        )
        assert attempt.error == "尝试超时且已达上限", "错误原因归尝试行，不再写进快照"


def test_terminal_job_is_never_requeued_by_reclaim(sandbox: Path) -> None:
    job = _seed_processing_job(attempt_count=1, status="succeeded")
    _backdate_running_attempt(job.id, seconds=3600)

    reclaimed = processing.reclaim_timeouts(SessionLocal())
    assert reclaimed["requeued"] == [] and reclaimed["failed"] == []
    with SessionLocal() as db:
        assert db.get(PersistentJob, job.id).status == "succeeded"


def test_partial_batch_is_its_own_terminal_state(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, tmp_path: Path
) -> None:
    good = make_invoice_pdf()
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 broken content")

    created = owner_workspace["client"].post(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        files=[
            ("files", (good.name, good.read_bytes(), "application/pdf")),
            ("files", (broken.name, broken.read_bytes(), "application/pdf")),
        ],
        data={"fields": FIELDS},
    )
    assert created.status_code == 201
    job_id = created.json()["job_id"]

    detail = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{job_id}"
    ).json()
    assert detail["status"] == "partial"
    assert len(detail["result"]) == 1
    assert any("失败" in warning for warning in detail["warnings"])

    # 部分成功仍应可导出已完成的部分。
    assert owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{job_id}/excel"
    ).status_code == 200
    with SessionLocal() as db:
        events = [
            event.event_type
            for event in db.scalars(
                select(DomainEventRecord).where(DomainEventRecord.aggregate_id == job_id)
            ).all()
        ]
    assert "job.partial" in events


def test_outbox_events_are_written_with_the_business_transaction(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    from app.contexts.processing.repository import JobRepository

    def fail(self, job, documents, versions, audit_events=(), events=()):
        raise IntegrityError("INSERT", {}, RuntimeError("db down"))

    original = JobRepository.save_with_documents
    JobRepository.save_with_documents = fail
    try:
        assert _upload(owner_workspace, make_invoice_pdf()).status_code == 500
    finally:
        JobRepository.save_with_documents = original

    with SessionLocal() as db:
        assert db.query(DomainEventRecord).count() == 0
        assert db.query(PersistentJob).count() == 0

    job_id = _upload(owner_workspace, make_invoice_pdf()).json()["job_id"]
    with SessionLocal() as db:
        events = list(
            db.scalars(select(DomainEventRecord).where(DomainEventRecord.tenant_id == owner_workspace["tenant_id"])).all()
        )
    types = {event.event_type for event in events}
    assert {"document.uploaded", "job.queued", "job.succeeded"} <= types
    assert all(event.published_at is None for event in events)
    assert all(event.aggregate_id for event in events)
    dumped = json.dumps([event.payload for event in events], ensure_ascii=False)
    assert str(sandbox) not in dumped and ".sqlite3" not in dumped


def test_outbox_rejects_absolute_paths_in_payload() -> None:
    with pytest.raises(ValueError):
        outbox.record(
            SessionLocal(),
            tenant_id="t" * 32,
            event_type="job.queued",
            aggregate_type="job",
            aggregate_id="j" * 32,
            payload={"path": "C:/srv/uploads/x.pdf"},
        )


def _force_failed(job_id: str, attempt_count: int) -> None:
    with SessionLocal() as db:
        record = db.get(PersistentJob, job_id)
        record.status = "failed"
        record.attempt_count = attempt_count
        snapshot = dict(record.snapshot)
        snapshot.update({"status": "failed", "progress": "done", "error": "模拟失败"})
        record.snapshot = snapshot
        db.commit()
    get_job_manager()._jobs.pop(job_id, None)


def _seed_processing_job(attempt_count: int, status: str = "processing") -> Job:
    from app.models.account import Tenant

    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        if db.get(Tenant, TENANT_ID) is None:
            db.add(Tenant(id=TENANT_ID, name="回收测试", status="active", created_at=now))
            db.commit()
    with SessionLocal() as db:
        job = Job(
            id=JOB_ID,
            fields=[FieldDefinition(name="发票号码", type="text", description="20 位发票号码")],
            created_at=now.timestamp(),
            files=[FileInfo(name="invoice.pdf", storage_key=f"{TENANT_ID}/{JOB_ID}/invoice.pdf")],
            status=JobStatus(status),
            tenant_id=TENANT_ID,
            attempt_count=attempt_count,
        )
        JobRepository().save(job, TENANT_ID, None)
        # R5 之后读路径只认规范化源：这里补一行版本记录，等价于上传链路本来会留下的事实。
        if db.scalar(select(DocumentVersion.id).where(DocumentVersion.job_id == JOB_ID)) is not None:
            return job  # 同一个 JOB_ID 被复用过，事实已经在库里
        document_id = "d" + "0" * 31
        db.add(
            Document(
                id=document_id,
                tenant_id=TENANT_ID,
                created_at=now,
            )
        )
        db.add(
            DocumentVersion(
                id="v" + "0" * 31,
                document_id=document_id,
                tenant_id=TENANT_ID,
                job_id=JOB_ID,
                revision=1,
                original_filename="invoice.pdf",
                storage_key=job.files[0].storage_key,
                checksum="0" * 64,
                media_type="application/pdf",
                size_bytes=1024,
                created_at=now,
            )
        )
        db.commit()
    return job



def _backdate_running_attempt(job_id: str, seconds: int) -> None:
    with SessionLocal() as db:
        attempt = processing.begin_attempt(job_id, TENANT_ID)
        attempt.started_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        db.merge(attempt)
        db.commit()


def _add_finished_attempt(job_id: str, *, status: str, snapshot_status: str) -> None:
    from datetime import timedelta

    with SessionLocal() as db:
        attempt = db.get(JobAttempt, None)  # 占位：下面直接插入新行
        record = db.get(PersistentJob, job_id)
        db.add(
            JobAttempt(
                id="a" + "0" * 31,
                job_id=job_id,
                tenant_id=TENANT_ID,
                attempt_no=(record.attempt_count or 0) + 1,
                status=status,
                started_at=datetime.now(timezone.utc) - timedelta(seconds=7200),
                finished_at=datetime.now(timezone.utc),
                engine="mock",
                parse_modes=[],
            )
        )
        record.attempt_count = (record.attempt_count or 0) + 1
        snapshot = dict(record.snapshot or {})
        snapshot.update({"status": snapshot_status, "progress": "done"})
        record.snapshot = snapshot
        record.progress = "done"  # R2 的双写在生产里与快照同事务，这里跟着一起补
        db.commit()
    assert attempt is None


def test_startup_converges_crashed_job_without_requeue(sandbox: Path) -> None:
    """崩溃在两次提交之间的任务应被收敛到终态，而不是重新排队（TC-REC-004）。"""
    job = _seed_processing_job(attempt_count=1)
    _add_finished_attempt(job.id, status="succeeded", snapshot_status="succeeded")

    with TestClient(app):  # 触发 lifespan 的恢复逻辑
        pass

    with SessionLocal() as db:
        record = db.get(PersistentJob, job.id)
        assert record.status == "succeeded"
        assert record.finished_at is not None
        attempts = db.scalars(select(JobAttempt).where(JobAttempt.job_id == job.id)).all()
        assert len(attempts) == 1  # 没有新增尝试


def test_startup_does_not_resurrect_processing_job_within_timeout(sandbox: Path) -> None:
    job = _seed_processing_job(attempt_count=1)
    processing.begin_attempt(job.id, TENANT_ID)  # 新鲜的 running 尝试，未超时

    with TestClient(app):
        pass

    with SessionLocal() as db:
        assert db.get(PersistentJob, job.id).status == "processing"
        assert db.get(PersistentJob, job.id).finished_at is None


def test_transaction_failure_leaves_no_events_or_rows(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真正验证 outbox 与业务同事务：让 commit 失败（TC-SEC-013）。"""
    from sqlalchemy.orm import Session as SqlSession

    def boom(self, *args, **kwargs):
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(SqlSession, "commit", boom)
    # 端点把提交失败收敛成稳定的 500，而不是把原始异常抛给调用方。
    assert _upload(owner_workspace, make_invoice_pdf()).status_code == 500

    with SessionLocal() as db:
        assert db.query(DomainEventRecord).count() == 0
        assert db.query(PersistentJob).count() == 0
        assert db.query(DocumentVersion).count() == 0


def test_outbox_rejects_nested_and_unc_paths() -> None:
    db = SessionLocal()
    with pytest.raises(ValueError):
        outbox.record(
            db,
            tenant_id=TENANT_ID,
            event_type="job.queued",
            aggregate_type="job",
            aggregate_id=JOB_ID,
            payload={"nested": {"stored": "/var/tmp/x.pdf"}},
        )
    with pytest.raises(ValueError):
        outbox.record(
            db,
            tenant_id=TENANT_ID,
            event_type="job.queued",
            aggregate_type="job",
            aggregate_id=JOB_ID,
            payload={"items": [UNC_PATH]},
        )
    db.close()


def test_retry_response_and_state_are_consistent(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """幂等键长度契约与重试后状态一致（TC-FUNC-017/018 补强）。"""
    client = owner_workspace["client"]
    tenant_id = owner_workspace["tenant_id"]
    long_key = "k" * 201
    response = _upload(owner_workspace, make_invoice_pdf(), key=long_key)
    assert response.status_code == 422
    assert client.post(
        f"/api/v1/tenants/{tenant_id}/jobs",
        headers={"X-CSRF-Token": owner_workspace["csrf"], "Idempotency-Key": "   "},
        files=[("files", ("a.pdf", make_invoice_pdf().read_bytes(), "application/pdf"))],
        data={"fields": FIELDS},
    ).status_code == 422
    with SessionLocal() as db:
        assert db.query(PersistentJob).count() == 0

    job_id = _upload(owner_workspace, make_invoice_pdf("01100200011100045250"), key="ok-key").json()["job_id"]
    _force_failed(job_id, attempt_count=JOB_MAX_ATTEMPTS)
    denied = client.post(
        f"/api/v1/tenants/{tenant_id}/jobs/{job_id}/retry",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
    )
    assert denied.status_code == 409
    assert client.get(f"/api/v1/tenants/{tenant_id}/jobs/{job_id}").json()["status"] == "failed"


def test_job_state_and_export_survive_restart(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """重启后任务、模板引用、尝试历史与导出保持一致（TC-FUNC-021）。"""
    pdf = make_invoice_pdf()
    job_id = _upload(owner_workspace, pdf).json()["job_id"]
    before = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{job_id}"
    ).json()
    excel_before = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{job_id}/excel"
    ).content

    fresh = TestClient(app)
    login = fresh.post(
        "/api/v1/auth/login", json={"email": "owner@example.com", "password": "test-password-1234"}
    )
    assert login.status_code == 200
    after = fresh.get(f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{job_id}").json()
    assert after["status"] == before["status"] == "succeeded"
    assert after["attempt_count"] == before["attempt_count"] == 1
    assert [item["attempt_no"] for item in after["attempts"]] == [1]
    excel_after = fresh.get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{job_id}/excel"
    ).content
    assert excel_after[:2] == b"PK" and len(excel_after) > 0 and excel_before[:2] == b"PK"
