"""任务缓存、持久化仓库和处理入口。"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from typing import Any

from app.contexts.processing import dispatch
from app.contexts.processing import service as processing
from app.models.job import FileInfo, Job, JobStatus, Progress
from app.models.access import AuditEvent
from app.models.document import Document, DocumentVersion
from app.models.schema import FieldDefinition
from app.services.pipeline import DocumentPipeline
from app.services.job_repository import JobRepository


class JobManager:
    """进程内缓存处理任务，并可将租户任务快照持久化到关系数据库。"""

    def __init__(
        self,
        pipeline: DocumentPipeline | None = None,
        repository: JobRepository | None = None,
    ) -> None:
        self._jobs: dict[str, Job] = {}
        self._pipeline = pipeline or DocumentPipeline()
        self._repository = repository
        if repository is not None:
            self._pipeline.on_update = self.save
            self._pipeline.on_result = self._record_result
        self._lock = threading.Lock()
        # 正在处理中的 job_id 集合：锁外执行 pipeline 时的并发重入门闩
        self._processing: set[str] = set()
        # 待与快照同事务落库的规范化结果
        self._staged: dict[str, list[tuple[FileInfo, Any, Any]]] = {}

    def create_job(
        self,
        files: list[tuple[str, str]],
        fields: list[FieldDefinition],
        *,
        tenant_id: str | None = None,
        owner_id: str | None = None,
        persist: bool = True,
    ) -> Job:
        """files: [(原始文件名, 磁盘路径), ...]；fields: 已校验的 FieldDefinition 列表。"""
        job = Job(
            id=Job.new_id(),
            fields=fields,
            created_at=Job.now(),
            files=[FileInfo(name=name, path=path) for name, path in files],
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        with self._lock:
            self._jobs[job.id] = job
        if persist and tenant_id is not None and self._repository is not None:
            self._repository.save(job, tenant_id, owner_id)
        return job

    def save_with_documents(
        self,
        job: Job,
        documents: list[Document],
        versions: list[DocumentVersion],
        audit_events: Sequence[AuditEvent] = (),
        events: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        if self._repository is None:
            raise RuntimeError("持久化任务仓库未配置")
        self._repository.save_with_documents(job, documents, versions, audit_events, events)

    def discard_unpersisted(self, job_id: str) -> None:
        """清除上传事务失败后尚未调度的任务缓存。"""
        with self._lock:
            self._jobs.pop(job_id, None)
            self._staged.pop(job_id, None)

    def _record_result(self, job: Job, finfo: FileInfo, parse_result, extract_result) -> None:
        """暂存每个文件的规范化结果，由下一次快照写入在同一事务落库（DOC-DD-005 §6.1）。

        快照与规范化结果分两次提交时，崩溃会留下"新快照 + 旧结果"的混合读视图；
        暂存后两者共享同一事务，要么都可见要么都不可见。
        """
        if job.tenant_id is None or self._repository is None:
            return
        with self._lock:
            self._staged.setdefault(job.id, []).append((finfo, parse_result, extract_result))

    def _has_staged(self, job_id: str) -> bool:
        with self._lock:
            return bool(self._staged.get(job_id))

    def _flush_staged(self, db, job: Job) -> None:
        from app.contexts.review import service as review

        with self._lock:
            pending = self._staged.pop(job.id, [])
        if not pending:
            return
        try:
            for finfo, parse_result, extract_result in pending:
                review.record_machine_result(
                    db,
                    tenant_id=job.tenant_id or "",
                    job_id=job.id,
                    filename=finfo.name,
                    text=parse_result.text,
                    fields=[field.model_dump() for field in job.fields],
                    values=extract_result.values,
                    warnings=extract_result.warnings,
                )
        except BaseException:
            with self._lock:
                self._staged[job.id] = pending + self._staged.get(job.id, [])
            raise

    def save(self, job: Job, audit_events: Sequence[AuditEvent] = ()) -> None:
        if job.tenant_id is None or self._repository is None:
            return
        self._repository.save(
            job,
            job.tenant_id,
            job.owner_id,
            audit_events,
            after=(
                None
                if not self._has_staged(job.id)
                else lambda db: self._flush_staged(db, job)
            ),
        )

    def pending_job_ids(self) -> list[str]:
        """启动恢复：只重新调度 queued 任务。
        卡在 processing 的任务由 reclaim_timeouts 统一收敛，这里不改状态，
        否则崩溃循环会绕过尝试上限（DOC-DD-004 §5 规则 2）。
        """
        if self._repository is None:
            return []
        ids = self._repository.list_queued_ids()
        loaded: list[str] = []
        for job_id in ids:
            job = self._repository.get_internal(job_id)
            if job is None or job.status is not JobStatus.QUEUED:
                continue
            with self._lock:
                self._jobs[job_id] = job
            loaded.append(job_id)
        return loaded

    def get_job(self, job_id: str, tenant_id: str | None = None) -> Job:
        """不存在抛 KeyError（API 层转 404）。"""
        if tenant_id is not None and self._repository is not None:
            persisted = self._repository.get(job_id, tenant_id)
            if persisted is None:
                raise KeyError(f"任务不存在: {job_id}")
            with self._lock:
                self._jobs[job_id] = persisted
            return persisted
        job = self._jobs.get(job_id)
        if job is not None:
            # 租户归属必须完全相等：无租户上下文的历史入口不得读取租户任务（含内存缓存命中）。
            if job.tenant_id != tenant_id:
                raise KeyError(f"任务不存在: {job_id}")
            return job
        raise KeyError(f"任务不存在: {job_id}")

    def load_for_processing(self, job_id: str) -> Job:
        """Worker 内部装载路径：不受租户入口约束，但只用于处理已知 ID 的任务。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                return job
        if self._repository is None:
            raise KeyError(f"任务不存在: {job_id}")
        persisted = self._repository.get_internal(job_id)
        if persisted is None:
            raise KeyError(f"任务不存在: {job_id}")
        with self._lock:
            self._jobs[job_id] = persisted
        return persisted

    def process(self, job_id: str, *, lease: "dispatch.Lease | None" = None) -> Job:
        """执行一次任务：先占用状态，再记录尝试，最后写回快照。

        设计权衡：pipeline.run 可能耗时（OCR/解析秒级），故锁内只做门闩检查，
        执行放在锁外；跨进程的正确性由 ProcessingJob 状态条件更新保证
        （DOC-DD-004 §5），内存门闩只是同进程的快速路径。

        Worker 会带上执行租约：租约被接管后，本次结果不再写入，避免旧执行者
        覆盖新尝试（docs/17 §4.2）。
        """
        job = self.load_for_processing(job_id)
        with self._lock:
            if job_id in self._processing:
                return job
            self._processing.add(job_id)
        try:
            if job.tenant_id is None:
                if job.status is not JobStatus.QUEUED:
                    return job
                return self._pipeline.run(job)
            if not processing.claim_for_processing(job_id, job.tenant_id):
                return self.load_for_processing(job_id)
            job.status = JobStatus.PROCESSING
            attempt = processing.begin_attempt(job_id, job.tenant_id)
            try:
                result = self._pipeline.run(job)
            except Exception as exc:
                processing.finish_attempt(
                    attempt,
                    status="failed",
                    job_status=JobStatus.FAILED.value,
                    error=str(exc)[:500],
                    lease=lease,
                )
                raise
            processing.finish_attempt(
                attempt,
                status="succeeded" if result.status.value in ("succeeded", "partial") else "failed",
                job_status=result.status.value,
                error=result.error,
                parse_modes=["ocr" if finfo.ocr_applied else "text" for finfo in result.files],
                lease=lease,
            )
            return result
        finally:
            with self._lock:
                self._processing.discard(job_id)
            try:
                self.save(job)
            finally:
                with self._lock:
                    self._staged.pop(job_id, None)

    def list_jobs(self) -> list[Job]:
        """按创建时间倒序。保留最近 100 个。"""
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            # 清理策略：只保留最近 100 个
            if len(jobs) > 100:
                keep = {j.id for j in jobs[:100]}
                for jid in list(self._jobs.keys()):
                    if jid not in keep:
                        del self._jobs[jid]
                jobs = jobs[:100]
            return jobs


# 模块级单例
_job_manager: JobManager | None = None


def get_job_manager() -> JobManager:
    global _job_manager
    if _job_manager is None:
        _job_manager = JobManager(repository=JobRepository())
    return _job_manager
