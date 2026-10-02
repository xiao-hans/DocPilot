"""规范化文档元数据的租户约束与上传持久化测试。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models.document import Document, DocumentVersion
from app.models.job import Job
from app.models.persistent_job import PersistentJob
from app.models.schema import INVOICE_TEMPLATE
from app.services.job_repository import JobRepository
from app.services.job_manager import get_job_manager


def test_document_version_rejects_cross_tenant_document(owner_workspace, other_workspace):
    now = datetime.now(timezone.utc)
    owner_job = Job(
        id=uuid4().hex,
        fields=[],
        created_at=now.timestamp(),
        tenant_id=owner_workspace["tenant_id"],
        owner_id=owner_workspace["user_id"],
    )
    other_job = Job(
        id=uuid4().hex,
        fields=[],
        created_at=now.timestamp(),
        tenant_id=other_workspace["tenant_id"],
        owner_id=other_workspace["user_id"],
    )
    JobRepository().save(owner_job, owner_job.tenant_id, owner_job.owner_id)
    JobRepository().save(other_job, other_job.tenant_id, other_job.owner_id)
    document_id = uuid4().hex
    with SessionLocal() as db:
        db.add(
            Document(
                id=document_id,
                tenant_id=other_workspace["tenant_id"],
                owner_id=other_workspace["user_id"],
                status="active",
                created_at=now,
            )
        )
        db.commit()

    with SessionLocal() as db:
        db.add(
            DocumentVersion(
                id=uuid4().hex,
                document_id=document_id,
                tenant_id=owner_workspace["tenant_id"],
                job_id=owner_job.id,
                revision=1,
                original_filename="invoice.pdf",
                storage_key=f"{owner_job.tenant_id}/{owner_job.id}/random.pdf",
                checksum="a" * 64,
                media_type="application/pdf",
                size_bytes=12,
                created_at=now,
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    with SessionLocal() as db:
        assert db.query(DocumentVersion).count() == 0


def test_upload_persists_document_metadata_with_job(owner_workspace, sandbox, make_invoice_pdf):
    pdf = make_invoice_pdf()
    response = owner_workspace["client"].post(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))],
        data={"fields": json.dumps(INVOICE_TEMPLATE)},
    )
    assert response.status_code == 201, response.text
    job_id = response.json()["job_id"]

    with SessionLocal() as db:
        job = db.get(PersistentJob, job_id)
        document = db.query(Document).one()
        version = db.query(DocumentVersion).one()
        assert job is not None
        assert job.tenant_id == owner_workspace["tenant_id"]
        assert document.tenant_id == job.tenant_id
        assert document.owner_id == owner_workspace["user_id"]
        assert version.document_id == document.id
        assert version.tenant_id == job.tenant_id
        assert version.job_id == job_id
        assert version.original_filename == pdf.name
        assert version.storage_key.endswith(".pdf")
        assert version.storage_key.startswith(f"{job.tenant_id}/{job_id}/")
        assert str(sandbox) not in version.storage_key
        assert version.checksum == hashlib.sha256(pdf.read_bytes()).hexdigest()
        assert version.media_type == "application/pdf"
        assert version.size_bytes == pdf.stat().st_size


def test_multi_file_upload_persists_one_document_per_file(
    owner_workspace, sandbox, make_invoice_pdf
):
    first, second = make_invoice_pdf("01100200011100045239"), make_invoice_pdf(
        "01100200011100045240"
    )
    response = owner_workspace["client"].post(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        files=[
            ("files", (first.name, first.read_bytes(), "application/pdf")),
            ("files", (second.name, second.read_bytes(), "application/pdf")),
        ],
        data={"fields": json.dumps(INVOICE_TEMPLATE)},
    )
    assert response.status_code == 201, response.text
    job_id = response.json()["job_id"]
    with SessionLocal() as db:
        documents = db.query(Document).order_by(Document.id).all()
        versions = db.query(DocumentVersion).order_by(DocumentVersion.id).all()
        assert len(documents) == len(versions) == 2
        assert {version.document_id for version in versions} == {doc.id for doc in documents}
        assert {version.job_id for version in versions} == {job_id}
        assert len({version.storage_key for version in versions}) == 2


def test_metadata_transaction_failure_cleans_files_and_memory_job(
    owner_workspace, sandbox, make_invoice_pdf, monkeypatch
):
    pdf = make_invoice_pdf()

    def fail_persist(self, job, documents, versions):
        raise IntegrityError("INSERT", {}, RuntimeError("simulated database failure"))

    monkeypatch.setattr(JobRepository, "save_with_documents", fail_persist)
    response = owner_workspace["client"].post(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))],
        data={"fields": json.dumps(INVOICE_TEMPLATE)},
    )
    assert response.status_code == 500
    assert "数据库" not in response.text
    assert list((sandbox / owner_workspace["tenant_id"]).glob("*")) == []
    manager = get_job_manager()
    assert manager._jobs == {}
    with SessionLocal() as db:
        assert db.query(PersistentJob).count() == 0
        assert db.query(Document).count() == 0
        assert db.query(DocumentVersion).count() == 0


def test_partial_file_write_failure_cleans_entire_upload_directory(
    owner_workspace, sandbox, make_invoice_pdf, monkeypatch
):
    first, second = make_invoice_pdf("01100200011100045239"), make_invoice_pdf(
        "01100200011100045240"
    )
    original_write_bytes = Path.write_bytes
    upload_writes = 0

    def fail_second_upload_write(path, content):
        nonlocal upload_writes
        if len(path.parent.name) == 32:
            upload_writes += 1
            if upload_writes == 2:
                raise OSError("simulated disk failure")
        return original_write_bytes(path, content)

    monkeypatch.setattr(Path, "write_bytes", fail_second_upload_write)
    response = owner_workspace["client"].post(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        files=[
            ("files", (first.name, first.read_bytes(), "application/pdf")),
            ("files", (second.name, second.read_bytes(), "application/pdf")),
        ],
        data={"fields": json.dumps(INVOICE_TEMPLATE)},
    )
    assert response.status_code == 500
    assert list((sandbox / owner_workspace["tenant_id"]).glob("*")) == []
    assert get_job_manager()._jobs == {}
    with SessionLocal() as db:
        assert db.query(PersistentJob).count() == 0
        assert db.query(Document).count() == 0
        assert db.query(DocumentVersion).count() == 0


def test_repository_rejects_version_linked_to_a_different_job(owner_workspace):
    now = datetime.now(timezone.utc)
    job = Job(
        id=uuid4().hex,
        fields=[],
        created_at=now.timestamp(),
        tenant_id=owner_workspace["tenant_id"],
        owner_id=owner_workspace["user_id"],
    )
    document = Document(
        id=uuid4().hex,
        tenant_id=owner_workspace["tenant_id"],
        owner_id=owner_workspace["user_id"],
        status="active",
        created_at=now,
    )
    version = DocumentVersion(
        id=uuid4().hex,
        document_id=document.id,
        tenant_id=owner_workspace["tenant_id"],
        job_id=uuid4().hex,
        revision=1,
        original_filename="invoice.pdf",
        storage_key=f"{job.tenant_id}/{job.id}/random.pdf",
        checksum="a" * 64,
        media_type="application/pdf",
        size_bytes=12,
        created_at=now,
    )
    with pytest.raises(ValueError, match="任务关联不一致"):
        JobRepository().save_with_documents(job, [document], [version])
    with SessionLocal() as db:
        assert db.query(PersistentJob).count() == 0
        assert db.query(Document).count() == 0


def test_legacy_job_snapshot_remains_readable_without_document_rows(owner_workspace):
    job = Job(
        id=uuid4().hex,
        fields=[],
        created_at=Job.now(),
        tenant_id=owner_workspace["tenant_id"],
        owner_id=owner_workspace["user_id"],
    )
    JobRepository().save(job, job.tenant_id, job.owner_id)
    response = owner_workspace["client"].get(
        f"/api/v1/tenants/{job.tenant_id}/jobs/{job.id}"
    )
    assert response.status_code == 200
    assert response.json()["id"] == job.id
    with SessionLocal() as db:
        assert db.query(Document).count() == 0
        assert db.query(DocumentVersion).count() == 0
