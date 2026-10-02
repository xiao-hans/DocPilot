"""任务缓存、持久化仓库和处理入口。"""

from __future__ import annotations

import threading
from collections.abc import Sequence

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
        self._lock = threading.Lock()
        # 正在处理中的 job_id 集合：锁外执行 pipeline 时的并发重入门闩
        self._processing: set[str] = set()

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
    ) -> None:
        if self._repository is None:
            raise RuntimeError("持久化任务仓库未配置")
        self._repository.save_with_documents(job, documents, versions, audit_events)

    def discard_unpersisted(self, job_id: str) -> None:
        """清除上传事务失败后尚未调度的任务缓存。"""
        with self._lock:
            self._jobs.pop(job_id, None)

    def save(self, job: Job, audit_events: Sequence[AuditEvent] = ()) -> None:
        if job.tenant_id is not None and self._repository is not None:
            self._repository.save(job, job.tenant_id, job.owner_id, audit_events)

    def pending_job_ids(self) -> list[str]:
        if self._repository is None:
            return []
        ids = self._repository.list_queued_ids()
        for job_id in ids:
            try:
                job = self._repository.get_internal(job_id)
                if job is None:
                    continue
                if job.status is JobStatus.PROCESSING:
                    job.status = JobStatus.QUEUED
                    job.progress = Progress.QUEUED
                    self._repository.save(job, job.tenant_id or "", job.owner_id)
                with self._lock:
                    self._jobs[job_id] = job
            except (KeyError, ValueError):
                continue
        return ids

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

    def process(self, job_id: str) -> Job:
        """调 pipeline.run；线程安全 + 幂等：非 queued 或已在处理中直接返回。

        设计权衡：pipeline.run 可能耗时（OCR/解析秒级），故只在锁内完成
        “检查 queued + 标记 processing”原子门闩，执行放在锁外，避免阻塞
        create_job/list_jobs；_processing 集合防止同一任务并发重入。
        """
        job = self.load_for_processing(job_id)
        with self._lock:
            if job.status is not JobStatus.QUEUED or job_id in self._processing:
                return job
            self._processing.add(job_id)
        try:
            return self._pipeline.run(job)
        finally:
            try:
                self.save(job)
            finally:
                with self._lock:
                    self._processing.discard(job_id)

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
