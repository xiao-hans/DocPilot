"""处理编排用例：幂等提交、状态占用、尝试记录与超时回收（DOC-DD-004 §5/§7.2/§7.3）。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import (
    AI_MODEL,
    EXTRACTOR_MODE,
    JOB_ATTEMPT_TIMEOUT_SECONDS,
    JOB_MAX_ATTEMPTS,
    QUEUE_ENABLED,
    QUEUE_NAME,
)
from app.contexts.processing import dispatch
from app.contexts.processing.domain import JobAttempt
from app.db import SessionLocal
from app.models.document import DocumentVersion
from app.models.persistent_job import PersistentJob
from app.services import outbox
from app.services.timestamps import iso_utc

TERMINAL_STATUSES = ("succeeded", "partial", "failed")
RETRYABLE_STATUSES = ("partial", "failed")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def engine_name() -> str:
    if EXTRACTOR_MODE == "llm" and AI_MODEL:
        return f"llm:{AI_MODEL}"
    return EXTRACTOR_MODE


def queue_health() -> dict[str, Any]:
    """/readyz 的队列段：队列模式下"有没有人在消费"与"有多少活排着"。

    口径刻意分开三种情况：
    - 未启用队列（`BROKER_URL` 为空）：API 进程内执行，队列不参与就绪判定。
    - 启用了但问不到 broker（`consumers is None`）：配置声称走队列却连状态都取不到，
      这就是故障，无论有没有积压。
    - 启用了、问到了消费者数：只有"确实有活排着而消费者为 0"才算卡住；没活时 0 个
      消费者是正常状态，不该被读成服务不可用。
    """
    if not QUEUE_ENABLED:
        return {"enabled": False, "reason": "queue_disabled"}
    from app.contexts.processing import broker

    backlog = dispatch.queue_backlog()
    consumers = broker.consumer_count()
    stuck = consumers is None or (backlog["pending"] > 0 and consumers == 0)
    return {
        "enabled": True,
        "queue": QUEUE_NAME,
        "consumers": consumers,
        "pending": backlog["pending"],
        "oldest_pending_seconds": backlog["oldest_pending_seconds"],
        "stuck": stuck,
    }


def find_by_idempotency_key(db: Session, tenant_id: str, key: str) -> PersistentJob | None:
    return db.scalar(
        select(PersistentJob).where(
            PersistentJob.tenant_id == tenant_id, PersistentJob.idempotency_key == key
        )
    )


def claim_for_processing(job_id: str, tenant_id: str) -> bool:
    """条件更新占用任务：并发下只有一个调用者能把 queued 推进为 processing。"""
    with SessionLocal() as db:
        updated = db.execute(
            update(PersistentJob)
            .where(
                PersistentJob.id == job_id,
                PersistentJob.tenant_id == tenant_id,
                PersistentJob.status == "queued",
            )
            .values(status="processing", finished_at=None)
        )
        result = updated.rowcount == 1
        db.commit()
    return result


def requeue(db: Session, job_id: str, tenant_id: str) -> int:
    """把可重试任务退回 queued，返回当前 attempt_count。"""
    job = db.get(PersistentJob, job_id)
    if job is None or job.tenant_id != tenant_id:
        raise LookupError(job_id)
    if job.attempt_count >= JOB_MAX_ATTEMPTS:
        raise ValueError("重试次数已达上限")
    updated = db.execute(
        update(PersistentJob)
        .where(
            PersistentJob.id == job_id,
            PersistentJob.tenant_id == tenant_id,
            PersistentJob.status.in_(RETRYABLE_STATUSES),
        )
        .values(status="queued", finished_at=None)
    )
    if updated.rowcount != 1:
        db.rollback()
        raise ValueError("任务当前状态不可重试")
    outbox.record(
        db,
        tenant_id=tenant_id,
        event_type="job.queued",
        aggregate_type="job",
        aggregate_id=job_id,
        payload={"status": "queued", "reason": "retry"},
    )
    # 退回 queued 意味着需要再一次执行尝试：同事务登记派发意图，派发器只认这张表。
    dispatch.create_pending(
        db, tenant_id=tenant_id, job_id=job_id, attempt_no=job.attempt_count + 1
    )
    db.commit()
    return job.attempt_count


def begin_attempt(job_id: str, tenant_id: str) -> JobAttempt:
    """开始一次尝试；超过上限直接拒绝，避免崩溃循环无限重试。"""
    with SessionLocal() as db:
        job = db.get(PersistentJob, job_id)
        if job is None or job.tenant_id != tenant_id:
            raise LookupError(job_id)
        if job.attempt_count >= JOB_MAX_ATTEMPTS:
            raise ValueError("尝试次数已达上限")
        attempt = JobAttempt(
            id=uuid.uuid4().hex,
            job_id=job_id,
            tenant_id=tenant_id,
            attempt_no=job.attempt_count + 1,
            status="running",
            started_at=_now(),
            engine=engine_name(),
            parse_modes=[],
        )
        job.attempt_count = attempt.attempt_no
        db.add(attempt)
        db.commit()
        db.expunge(attempt)
    return attempt


def finish_attempt(
    attempt: JobAttempt,
    *,
    status: str,
    job_status: str,
    error: str | None = None,
    parse_modes: list[str] | None = None,
    lease: "dispatch.Lease | None" = None,
) -> bool:
    """同一事务内收口尝试、推进任务状态列并追加终态事件。

    状态列与尝试必须原子推进，否则崩溃会留下 processing 却已结束尝试的死态。
    带租约时先证明租约仍属于当前执行者：已被接管的旧 Worker 不允许写结果，
    否则它会覆盖新尝试。返回 False 表示本次写入因租约失效而被放弃。
    """
    now = _now()
    with SessionLocal() as db:
        if lease is not None and not dispatch.settle(db, lease.dispatch_id, lease.token):
            db.rollback()
            return False
        stored = db.get(JobAttempt, attempt.id)
        if stored is not None and stored.status == "running":
            stored.status = status
            stored.finished_at = now
            stored.error = error
            stored.parse_modes = parse_modes or []
        db.execute(
            update(PersistentJob)
            .where(
                PersistentJob.id == attempt.job_id,
                PersistentJob.tenant_id == attempt.tenant_id,
                # 快照落库可能已先把状态列推到终态，因此以"尚未记录完成时间"为幂等条件。
                PersistentJob.finished_at.is_(None),
            )
            .values(
                status=job_status,
                finished_at=now if job_status in TERMINAL_STATUSES else None,
            )
        )
        outbox.record(
            db,
            tenant_id=attempt.tenant_id,
            event_type=f"job.{job_status}",
            aggregate_type="job",
            aggregate_id=attempt.job_id,
            payload={
                "attempt_no": attempt.attempt_no,
                "status": job_status,
                "engine": attempt.engine,
            },
        )
        if job_status in TERMINAL_STATUSES:
            # 任务已有结局，任何仍未了结的派发行都必须关闭，否则终态任务会被再次叫醒。
            dispatch.close_open_for_job(
                db, job_id=attempt.job_id, tenant_id=attempt.tenant_id
            )
        db.commit()
    return True


def _completed_terminal(db, job: PersistentJob, attempt: JobAttempt | None) -> str | None:
    """崩溃在"结果已写、状态列未推进"之间时，用规范化事实判定它本该是哪个终态。

    判据与 pipeline 汇总状态同源：`progress` 列走到 `done` 说明这一轮真的跑完了；
    有解析失败的文件即 `partial`，尝试带错误即 `failed`。R3 回填之前的历史行
    `progress` 为空，这里判定不出来，仍按超时回收处理——那是 §4 允许的保守分支。
    """
    if job.progress != "done" or attempt is None or attempt.status == "running":
        return None
    if attempt.error:
        return "failed"
    broken = db.scalar(
        select(func.count())
        .select_from(DocumentVersion)
        .where(DocumentVersion.job_id == job.id, DocumentVersion.parse_error.is_not(None))
    )
    return "partial" if broken else "succeeded"


def reclaim_timeouts(db: Session) -> dict[str, list[str]]:
    """收敛所有卡在 processing 的任务，是唯一的恢复入口（DOC-DD-004 §7.3）。

    - 最新尝试已结束而状态列仍是 processing：按快照终态收敛，不重新排队。
    - 尝试仍在时限内执行：不动。
    - 尝试超时或没有尝试记录：未达上限退回 queued，达上限置 failed。
    """
    outcome: dict[str, list[str]] = {"requeued": [], "failed": [], "converged": []}
    cutoff = _now() - timedelta(seconds=JOB_ATTEMPT_TIMEOUT_SECONDS)
    stuck = list(
        db.scalars(
            select(PersistentJob).where(PersistentJob.status == "processing")
        ).all()
    )
    for job in stuck:
        attempt = db.scalar(
            select(JobAttempt)
            .where(JobAttempt.job_id == job.id)
            .order_by(JobAttempt.attempt_no.desc())
            .limit(1)
        )
        if attempt is None or attempt.status != "running":
            terminal = _completed_terminal(db, job, attempt)
            if terminal is not None:
                _transition(db, job, terminal)
                outcome["converged"].append(job.id)
                continue
        elif _as_utc(attempt.started_at) > cutoff:
            continue  # 仍在执行时限内

        if attempt is not None and attempt.status == "running":
            attempt.status = "timed_out"
            attempt.finished_at = _now()
            attempt.error = "执行超时"

        if job.attempt_count < JOB_MAX_ATTEMPTS:
            _transition(db, job, "queued", finished_at=None)
            outcome["requeued"].append(job.id)
            # 超时回收后要重新派发；旧attempt 的租约行随之关闭，新尝试用新的派发行。
            dispatch.close_open_for_job(db, job_id=job.id, tenant_id=job.tenant_id)
            dispatch.create_pending(
                db, tenant_id=job.tenant_id, job_id=job.id, attempt_no=job.attempt_count + 1
            )
            outbox.record(
                db,
                tenant_id=job.tenant_id,
                event_type="job.queued",
                aggregate_type="job",
                aggregate_id=job.id,
                payload={
                    "reason": "timeout_reclaim",
                    "attempt_no": attempt.attempt_no if attempt else job.attempt_count,
                },
            )
        else:
            if attempt is not None:
                attempt.error = "尝试超时且已达上限"
            job.progress = "done"
            _transition(db, job, "failed")
            outcome["failed"].append(job.id)
            dispatch.close_open_for_job(db, job_id=job.id, tenant_id=job.tenant_id)
            outbox.record(
                db,
                tenant_id=job.tenant_id,
                event_type="job.failed",
                aggregate_type="job",
                aggregate_id=job.id,
                payload={"reason": "timeout_limit_reached", "attempt_no": job.attempt_count},
            )
    db.commit()
    return outcome


def _transition(
    db: Session, job: PersistentJob, status: str, *, finished_at: datetime | None = ...
) -> None:
    """状态列推进只允许从前态出发，避免把已变化的状态覆盖回去。"""
    if finished_at is ...:
        value = _now() if status in TERMINAL_STATUSES else None
    else:
        value = finished_at
    db.execute(
        update(PersistentJob)
        .where(PersistentJob.id == job.id, PersistentJob.status == job.status)
        .values(status=status, finished_at=value)
    )
    job.status = status
    job.finished_at = value


def attempts_payload(db: Session, job_id: str, tenant_id: str) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(JobAttempt)
        .where(JobAttempt.job_id == job_id, JobAttempt.tenant_id == tenant_id)
        .order_by(JobAttempt.attempt_no)
    ).all()
    return [
        {
            "attempt_no": attempt.attempt_no,
            "status": attempt.status,
            "engine": attempt.engine,
            "parse_modes": attempt.parse_modes,
            "started_at": iso_utc(attempt.started_at),
            "finished_at": iso_utc(attempt.finished_at),
            "error": attempt.error,
        }
        for attempt in rows
    ]


def list_jobs(
    db: Session,
    *,
    tenant_id: str,
    status: str | None = None,
    template_version_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """租户内任务列表：分页 + 状态/模板过滤，响应不含存储路径。"""
    stmt = select(PersistentJob).where(PersistentJob.tenant_id == tenant_id)
    if status and status != "all":
        stmt = stmt.where(PersistentJob.status == status)
    if template_version_id:
        stmt = stmt.where(PersistentJob.template_version_id == template_version_id)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = db.scalars(
        stmt.order_by(PersistentJob.created_at.desc(), PersistentJob.id.desc())
        .limit(max(1, min(limit, 100)))
        .offset(max(0, offset))
    ).all()
    job_ids = [row.id for row in rows]
    file_counts = (
        dict(
            db.execute(
                select(DocumentVersion.job_id, func.count())
                .where(DocumentVersion.job_id.in_(job_ids))
                .group_by(DocumentVersion.job_id)
            ).all()
        )
        if job_ids
        else {}
    )
    return {
        "total": total,
        "jobs": [
            {
                "id": row.id,
                "status": row.status,
                "template_version_id": row.template_version_id,
                "attempt_count": row.attempt_count,
                "file_count": file_counts.get(row.id, 0),
                "created_at": row.created_at.timestamp(),
                "finished_at": row.finished_at.timestamp() if row.finished_at else None,
            }
            for row in rows
        ],
    }
