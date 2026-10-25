"""处理任务的持久化仓库。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from app.platform.db import SessionLocal
from app.models.access import AuditEvent
from app.models.document import Document, DocumentVersion
from app.shared import audit, outbox
from app.contexts.processing import dispatch
from app.contexts.processing.domain import JobAttempt
from app.models.job import FileInfo, Job, JobStatus, Progress
from app.contexts.templates.domain import TemplateVersion
from app.models.persistent_job import PersistentJob
from app.models.schema import FieldDefinition


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


_INCOMPLETE_REBUILDS = 0


class IncompleteJobFacts(RuntimeError):
    """规范化源不足以重建一条任务；`docs/20 §8.5` 选择报错而不是拼一个假对象。"""


def incomplete_rebuild_count() -> int:
    """R5 观察期的判据：这个计数必须为 0，才谈得上 §6.1 的"没有回退读路径的需要"。"""
    return _INCOMPLETE_REBUILDS


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
        """任务、审计与 after 回调内的写入共享同一事务。"""
        with self._sessions() as db:
            self._save_job_record(db, job, tenant_id, owner_id)
            self._sync_normalized_facts(db, job)
            db.add_all(audit_events)
            if after is not None:
                after(db)
            db.commit()

    @staticmethod
    def _save_job_record(db, job: Job, tenant_id: str, owner_id: str | None) -> None:
        fields = [field.model_dump() for field in job.fields]
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
                    field_definitions=fields,
                    # progress 与 status 同事务写入：进度不能说出状态没承认的话。
                    progress=job.progress.value,
                )
            )
            return
        if record.tenant_id != tenant_id:
            raise ValueError("任务租户归属不可变更")
        record.status = job.status.value
        record.progress = job.progress.value
        if record.field_definitions is None:
            # 字段定义创建后不可变；已有值就不动。
            record.field_definitions = fields

    @staticmethod
    def _sync_normalized_facts(db, job: Job) -> None:
        """把逐文件解析事实与当次尝试的告警镜像到规范化源（R2）。

        按 `storage_key` 定位版本行：纯内存任务（没有落库版本）时更新 0 行，不是错误。
        """
        for info in job.files:
            if not info.storage_key:
                continue
            db.execute(
                update(DocumentVersion)
                .where(DocumentVersion.storage_key == info.storage_key)
                .values(
                    page_count=info.page_count,
                    text_chars=info.chars,
                    ocr_applied=info.ocr_applied,
                    parse_error=(info.error or None) and info.error[:300],
                )
            )
        attempt = db.scalar(
            select(JobAttempt).where(
                JobAttempt.job_id == job.id, JobAttempt.attempt_no == job.attempt_count
            )
        )
        if attempt is None:
            # 执行链路里内存对象的 attempt_count 可能落后于刚创建的尝试行；告警属于
            # "最近这一次跑出来的东西"，所以退回最近一条，而不是把告警丢掉。
            attempt = db.scalar(
                select(JobAttempt)
                .where(JobAttempt.job_id == job.id)
                .order_by(JobAttempt.attempt_no.desc())
                .limit(1)
            )
        if attempt is not None:
            attempt.warnings = list(job.warnings)

    def save_with_documents(
        self,
        job: Job,
        documents: list[Document],
        versions: list[DocumentVersion],
        audit_events: Sequence[AuditEvent] = (),
        events: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        """在一个事务中保存任务、文档、文件版本、审计与领域事件。"""
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

        with self._sessions() as db:
            self._save_job_record(db, job, tenant_id, owner_id)
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
            # 版本行是本次事务里新加的，必须在 add 之后再镜像逐文件事实，否则按 storage_key
            # 更新只会命中 0 行（autoflush 发生在 update 之前，但 add_all 必须已经排进会话）。
            self._sync_normalized_facts(db, job)
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
            return self._rebuild(db, record)

    @staticmethod
    def _rebuild(db, record: PersistentJob) -> Job:
        """R5 切读：只从规范化源重建 `Job`，`record.snapshot` 一个字都不读。

        重建不出来时抛 `IncompleteJobFacts` 而不是回落快照（`docs/20 §8.5`）：一条缺字段定义
        或缺版本行的任务，若被拼成"字段为空、文件为零"的正常对象，界面就会把它当成正常任务
        展示——那正是 R-013 双真相的原始症状。计数是给 §6.1 的观察期用的判据。
        """
        from app.contexts.review.service import export_rows

        if record.template_version_id:
            frozen = db.get(TemplateVersion, record.template_version_id)
            raw_fields = frozen.fields if frozen is not None else None
        else:
            raw_fields = record.field_definitions
        versions = list(
            db.scalars(
                select(DocumentVersion)
                .where(DocumentVersion.job_id == record.id)
                .order_by(DocumentVersion.original_filename, DocumentVersion.id)
            ).all()
        )
        attempt = db.scalar(
            select(JobAttempt).where(
                JobAttempt.job_id == record.id,
                JobAttempt.attempt_no == record.attempt_count,
            )
        )
        missing = [
            name
            for name, present in (
                ("fields", bool(raw_fields)),
                ("progress", record.progress is not None),
                ("files", bool(versions)),
            )
            if not present
        ]
        if missing:
            global _INCOMPLETE_REBUILDS
            _INCOMPLETE_REBUILDS += 1
            JobRepository._record_incomplete_rebuild(db, record, missing)
            raise IncompleteJobFacts(
                f"任务 {record.id} 的规范化源不完整，缺 {'、'.join(missing)}"
            )

        created_at = record.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        result = export_rows(db, tenant_id=record.tenant_id, job_id=record.id)
        return Job(
            id=record.id,
            fields=[FieldDefinition(**item) for item in raw_fields],
            created_at=created_at.timestamp(),
            files=[
                FileInfo(
                    name=row.original_filename,
                    storage_key=row.storage_key,
                    page_count=row.page_count or 0,
                    chars=row.text_chars or 0,
                    ocr_applied=bool(row.ocr_applied),
                    error=row.parse_error,
                )
                for row in versions
            ],
            status=JobStatus(record.status),
            progress=Progress(record.progress),
            result=result or None,
            warnings=list(attempt.warnings or []) if attempt is not None else [],
            error=attempt.error if attempt is not None else None,
            tenant_id=record.tenant_id,
            owner_id=record.owner_id,
            template_version_id=record.template_version_id,
            idempotency_key=record.idempotency_key,
            attempt_count=record.attempt_count,
            finished_at=_as_utc(record.finished_at).timestamp() if record.finished_at else None,
        )

    @staticmethod
    def _record_incomplete_rebuild(db, record: PersistentJob, missing: list[str]) -> None:
        """把"重建不出来"写进审计，让 §6.1 的观察期能跨进程重启被核对。

        进程内计数活不过一次重启，用它做删列门禁等于把证据放在最容易丢的地方。
        审计写失败不该掩盖真正的问题，所以异常一律吞掉并就地记日志：判据是附加物，
        不是这条读路径的主张。
        """
        try:
            audit.record_now(
                tenant_id=record.tenant_id,
                actor_user_id=None,
                action="job.rebuild_incomplete",
                object_type="job",
                object_id=record.id,
                metadata={"missing": list(missing)},
            )
        except Exception:  # noqa: BLE001 - 主异常必须是 IncompleteJobFacts
            logging.getLogger(__name__).warning("重建失败审计写入失败 job_id=%s", record.id, exc_info=True)

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
