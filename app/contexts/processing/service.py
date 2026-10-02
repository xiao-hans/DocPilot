"""处理编排用例：幂等提交、状态占用、尝试记录与超时回收（DOC-DD-004 §5/§7.2/§7.3）。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import AI_MODEL, EXTRACTOR_MODE, JOB_ATTEMPT_TIMEOUT_SECONDS, JOB_MAX_ATTEMPTS
from app.contexts.processing.domain import JobAttempt
from app.db import SessionLocal
from app.models.persistent_job import PersistentJob
from app.services import outbox

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
) -> None:
    """同一事务内收口尝试、推进任务状态列并追加终态事件。

    状态列与尝试必须原子推进，否则崩溃会留下 processing 却已结束尝试的死态。
    """
    now = _now()
    with SessionLocal() as db:
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
        db.commit()


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
        snapshot_status = str((job.snapshot or {}).get("status") or "")

        if attempt is None or attempt.status != "running":
            if snapshot_status in TERMINAL_STATUSES:
                _transition(db, job, snapshot_status)
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
            snapshot = dict(job.snapshot or {})
            snapshot.update({"status": "failed", "progress": "done", "error": "尝试超时且已达上限"})
            job.snapshot = snapshot
            _transition(db, job, "failed")
            outcome["failed"].append(job.id)
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
            "started_at": _as_utc(attempt.started_at).isoformat(),
            "finished_at": _as_utc(attempt.finished_at).isoformat() if attempt.finished_at else None,
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
    from sqlalchemy import func

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
    return {
        "total": total,
        "jobs": [
            {
                "id": row.id,
                "status": row.status,
                "template_version_id": row.template_version_id,
                "attempt_count": row.attempt_count,
                "file_count": len((row.snapshot or {}).get("files") or []),
                "created_at": row.created_at.timestamp(),
                "finished_at": row.finished_at.timestamp() if row.finished_at else None,
            }
            for row in rows
        ],
    }
