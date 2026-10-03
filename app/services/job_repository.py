"""处理任务快照的持久化仓库。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db import SessionLocal
from app.models.access import AuditEvent
from app.models.document import Document, DocumentVersion
from app.services import outbox
from app.contexts.processing import dispatch
from app.models.job import FileInfo, Job, JobStatus, Progress
from app.models.persistent_job import PersistentJob
from app.models.schema import FieldDefinition


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class JobRepository:
    def __init__(self, sessions: sessionmaker = SessionLocal) -> None:
        self._sessions = sessions

    def save(
        self,
        job: Job,
        tenant_id: str,
        owner_id: str | None,
        audit_events: Sequence[AuditEvent] = (),
        after: Callable[[Any], None] | None = None,
    ) -> None:
        """快照、审计与 after 回调内的写入共享同一事务。"""
        snapshot = self._snapshot(job)
        with self._sessions() as db:
            self._save_job_record(db, job, tenant_id, owner_id, snapshot)
            db.add_all(audit_events)
            if after is not None:
                after(db)
            db.commit()

    @staticmethod
    def _snapshot(job: Job) -> dict:
        return {
            "fields": [field.model_dump() for field in job.fields],
            "files": [
                {
                    "name": f.name,
                    "storage_key": f.storage_key,
                    "path": f.path,
                    "page_count": f.page_count,
                    "chars": f.chars,
                    "ocr_applied": f.ocr_applied,
                    "error": f.error,
                }
                for f in job.files
            ],
            "status": job.status.value,
            "progress": job.progress.value,
            "result": job.result,
            "warnings": job.warnings,
            "error": job.error,
            "excel_path": job.excel_path,
        }

    @staticmethod
    def _save_job_record(db, job: Job, tenant_id: str, owner_id: str | None, snapshot: dict) -> None:
        record = db.get(PersistentJob, job.id)
        if record is None:
            db.add(
                PersistentJob(
                    id=job.id,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    status=job.status.value,
                    created_at=datetime.fromtimestamp(job.created_at, tz=timezone.utc),
                    attempt_count=job.attempt_count,
                    idempotency_key=job.idempotency_key,
                    template_version_id=job.template_version_id,
                    snapshot=snapshot,
                )
            )
            return
        if record.tenant_id != tenant_id:
            raise ValueError("任务租户归属不可变更")
        record.status = job.status.value
        record.snapshot = snapshot

    def save_with_documents(
        self,
        job: Job,
        documents: list[Document],
        versions: list[DocumentVersion],
        audit_events: Sequence[AuditEvent] = (),
        events: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        """在一个事务中保存任务快照、文档、文件版本、审计与领域事件。"""
        tenant_id = job.tenant_id
        owner_id = job.owner_id
        if tenant_id is None or len(documents) != len(versions) or not documents:
            raise ValueError("任务文档元数据不完整")

        document_ids = {document.id for document in documents}
        if len(document_ids) != len(documents):
            raise ValueError("文档 ID 重复")
        for document in documents:
            if document.tenant_id != tenant_id or document.owner_id != owner_id:
                raise ValueError("文档租户或所有者与任务不一致")
        version_document_ids = [version.document_id for version in versions]
        if len(set(version_document_ids)) != len(versions) or set(version_document_ids) != document_ids:
            raise ValueError("每份文档必须且只能关联一个新文件版本")
        for version in versions:
            if (
                version.tenant_id != tenant_id
                or version.document_id not in document_ids
                or version.job_id != job.id
            ):
                raise ValueError("文件版本租户、文档或任务关联不一致")

        snapshot = self._snapshot(job)
        with self._sessions() as db:
            self._save_job_record(db, job, tenant_id, owner_id, snapshot)
            db.add_all(documents)
            db.add_all(versions)
            db.add_all(audit_events)
            if job.status is JobStatus.QUEUED:
                # 任务与派发意图必须同事务落库：只有事务提交后，派发器才能看见这个任务。
                dispatch.create_pending(
                    db, tenant_id=tenant_id, job_id=job.id, attempt_no=job.attempt_count + 1
                )
            for spec in events:
                outbox.record(db, **spec)
            db.commit()

    def get(self, job_id: str, tenant_id: str) -> Job | None:
        with self._sessions() as db:
            record = db.scalar(
                select(PersistentJob).where(
                    PersistentJob.id == job_id,
                    PersistentJob.tenant_id == tenant_id,
                )
            )
            if record is None:
                return None
            snap = record.snapshot
            created_at = record.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            return Job(
                id=record.id,
                fields=[FieldDefinition(**item) for item in snap["fields"]],
                created_at=created_at.timestamp(),
                files=[FileInfo(**item) for item in snap["files"]],
                status=JobStatus(record.status),  # 状态列是权威来源，快照只作兼容读模型
                progress=Progress(snap["progress"]),
                result=snap.get("result"),
                warnings=list(snap.get("warnings") or []),
                error=snap.get("error"),
                excel_path=snap.get("excel_path"),
                tenant_id=record.tenant_id,
                owner_id=record.owner_id,
                template_version_id=record.template_version_id,
                idempotency_key=record.idempotency_key,
                attempt_count=record.attempt_count,
                finished_at=_as_utc(record.finished_at).timestamp() if record.finished_at else None,
            )

    def get_internal(self, job_id: str) -> Job | None:
        with self._sessions() as db:
            record = db.get(PersistentJob, job_id)
            if record is None:
                return None
            return self.get(record.id, record.tenant_id)

    def list_queued_ids(self) -> list[str]:
        with self._sessions() as db:
            return list(
                db.scalars(
                    select(PersistentJob.id).where(
                        PersistentJob.status.in_(
                            [JobStatus.QUEUED.value, JobStatus.PROCESSING.value]
                        )
                    )
                ).all()
            )
