"""内存任务存储与处理入口（无数据库/Redis/Celery）。"""

from __future__ import annotations

import threading

from app.models.job import FileInfo, Job, JobStatus
from app.models.schema import FieldDefinition
from app.services.pipeline import DocumentPipeline


class JobManager:
    """内存任务存储与处理入口（无数据库/Redis/Celery）。"""

    def __init__(self, pipeline: DocumentPipeline | None = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._pipeline = pipeline or DocumentPipeline()
        self._lock = threading.Lock()
        # 正在处理中的 job_id 集合：锁外执行 pipeline 时的并发重入门闩
        self._processing: set[str] = set()

    def create_job(
        self,
        files: list[tuple[str, str]],
        fields: list[FieldDefinition],
    ) -> Job:
        """files: [(原始文件名, 磁盘路径), ...]；fields: 已校验的 FieldDefinition 列表。"""
        job = Job(
            id=Job.new_id(),
            fields=fields,
            created_at=Job.now(),
            files=[FileInfo(name=name, path=path) for name, path in files],
        )
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get_job(self, job_id: str) -> Job:
        """不存在抛 KeyError（API 层转 404）。"""
        try:
            return self._jobs[job_id]
        except KeyError as err:
            raise KeyError(f"任务不存在: {job_id}") from err

    def process(self, job_id: str) -> Job:
        """调 pipeline.run；线程安全 + 幂等：非 queued 或已在处理中直接返回。

        设计权衡：pipeline.run 可能耗时（OCR/解析秒级），故只在锁内完成
        “检查 queued + 标记 processing”原子门闩，执行放在锁外，避免阻塞
        create_job/list_jobs；_processing 集合防止同一任务并发重入。
        """
        with self._lock:
            job = self.get_job(job_id)
            if job.status is not JobStatus.QUEUED or job_id in self._processing:
                return job
            self._processing.add(job_id)
        try:
            return self._pipeline.run(job)
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
        _job_manager = JobManager()
    return _job_manager
