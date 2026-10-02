"""处理任务快照的持久化仓库。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db import SessionLocal
from app.models.job import FileInfo, Job, JobStatus, Progress
from app.models.persistent_job import PersistentJob
from app.models.schema import FieldDefinition


class JobRepository:
    def __init__(self, sessions: sessionmaker = SessionLocal) -> None:
        self._sessions = sessions

    def save(self, job: Job, tenant_id: str, owner_id: str | None) -> None:
        snapshot = {
            "fields": [field.model_dump() for field in job.fields],
            "files": [
                {
                    "name": f.name,
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
        with self._sessions() as db:
            record = db.get(PersistentJob, job.id)
            if record is None:
                record = PersistentJob(
                    id=job.id,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    status=job.status.value,
                    created_at=datetime.fromtimestamp(job.created_at, tz=timezone.utc),
                    snapshot=snapshot,
                )
                db.add(record)
            else:
                if record.tenant_id != tenant_id:
                    raise ValueError("任务租户归属不可变更")
                record.status = job.status.value
                record.snapshot = snapshot
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
                status=JobStatus(snap["status"]),
                progress=Progress(snap["progress"]),
                result=snap.get("result"),
                warnings=list(snap.get("warnings") or []),
                error=snap.get("error"),
                excel_path=snap.get("excel_path"),
                tenant_id=record.tenant_id,
                owner_id=record.owner_id,
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
