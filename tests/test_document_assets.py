"""规范化文档元数据的租户约束与上传持久化测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models.document import Document, DocumentVersion
from app.models.job import Job
from app.services.job_repository import JobRepository


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
