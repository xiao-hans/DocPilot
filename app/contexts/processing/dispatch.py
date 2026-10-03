"""派发记录状态机（docs/17 §4）。

每一次推进都是条件更新：并发派发器、重复投递、被接管的旧 Worker 都表现为
"条件更新没命中"，正确性不依赖进程内锁、消息顺序或"只投递一次"的假设。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.config import DISPATCH_REPUBLISH_SECONDS, JOB_LEASE_SECONDS, QUEUE_NAME
from app.db import SessionLocal
from app.models.dispatch import JobDispatch
from app.models.persistent_job import PersistentJob


@dataclass(frozen=True)
class Lease:
    """一次执行租约的凭据：只有令牌仍有效的执行者才能写结果。"""

    dispatch_id: str
    token: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_pending(
    db: Session, *, tenant_id: str, job_id: str, attempt_no: int
) -> JobDispatch:
    """在调用方事务内登记一次待派发意图；同一 (job, attempt) 重复登记返回既有行。"""
    # 派发行以外键引用任务行，而这条外键没有 ORM relationship，单元工作不会保证
    # 父行先插入（实测同一事务内"新增任务 + 登记派发"在 SQLite 与 PostgreSQL 都被外键拒绝）。
    # 因此先把调用方挂起的对象落库，再查存在性、再插入派发行。
    # 不能用 db.get(PersistentJob, ...) 取父行后单独 flush：父行尚未 flush 时它返回 None。
    db.flush()
    existing = db.scalar(
        select(JobDispatch).where(
            JobDispatch.job_id == job_id, JobDispatch.attempt_no == attempt_no
        )
    )
    if existing is not None:
        # 槽位按 (job, attempt) 唯一。若该槽位此前已被收口（例如超时回收先关闭了旧行，
        # 而重试次数还没推进），必须重新打开它；否则任务退回 queued 后没有可派发的记录，
        # 会静默卡死。
        if existing.status == "settled":
            existing.status = "ready"
            existing.publish_confirmed_at = None
            existing.lease_token = None
            existing.lease_expires_at = None
            existing.updated_at = _now()
        return existing
    now = _now()
    row = JobDispatch(
        id=uuid.uuid4().hex,
        tenant_id=tenant_id,
        job_id=job_id,
        attempt_no=attempt_no,
        queue_name=QUEUE_NAME,
        status="ready",
        created_at=now,
        updated_at=now,
        publish_attempts=0,
    )
    db.add(row)
    db.flush()
    return row


def list_ready(limit: int) -> list[JobDispatch]:
    """按登记顺序取待派发记录；顺序本身不影响正确性，只让积压可预测。"""
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(JobDispatch)
                .where(JobDispatch.status == "ready")
                .order_by(JobDispatch.created_at, JobDispatch.id)
                .limit(max(1, limit))
            ).all()
        )


def mark_published(dispatch_id: str) -> bool:
    """发布并拿到 confirm 后推进为 published；未命中说明该行已被其他派发器处理。"""
    now = _now()
    with SessionLocal() as db:
        result = db.execute(
            update(JobDispatch)
            .where(JobDispatch.id == dispatch_id, JobDispatch.status == "ready")
            .values(
                status="published",
                publish_confirmed_at=now,
                publish_attempts=JobDispatch.publish_attempts + 1,
                updated_at=now,
            )
        )
        db.commit()
        return result.rowcount == 1


def note_publish_failure(dispatch_id: str) -> None:
    """发布失败时留在 ready 等重放，只累计次数，避免把失败伪装成已投递。"""
    now = _now()
    with SessionLocal() as db:
        db.execute(
            update(JobDispatch)
            .where(JobDispatch.id == dispatch_id, JobDispatch.status == "ready")
            .values(publish_attempts=JobDispatch.publish_attempts + 1, updated_at=now)
        )
        db.commit()


def claim_lease(
    dispatch_id: str, *, tenant_id: str, job_id: str, attempt_no: int
) -> str | None:
    """领取执行租约并返回令牌。

    可领取的条件是"已发布且无人认领"或"上一个持有者租约已过期"；令牌随机生成，
    因此同一任务的重复消息最多让一个执行者拿到租约。
    """
    token = uuid.uuid4().hex
    now = _now()
    with SessionLocal() as db:
        result = db.execute(
            update(JobDispatch)
            .where(
                JobDispatch.id == dispatch_id,
                JobDispatch.tenant_id == tenant_id,
                JobDispatch.job_id == job_id,
                JobDispatch.attempt_no == attempt_no,
                or_(
                    JobDispatch.status == "published",
                    (JobDispatch.status == "claimed")
                    & (JobDispatch.lease_expires_at < now),
                ),
            )
            .values(
                status="claimed",
                lease_token=token,
                lease_expires_at=now + timedelta(seconds=JOB_LEASE_SECONDS),
                updated_at=now,
            )
        )
        db.commit()
        return token if result.rowcount == 1 else None


def renew_lease(dispatch_id: str, token: str) -> bool:
    """续租必须匹配当前令牌；已被接管的旧 Worker 无法再延长自己的租约。"""
    now = _now()
    with SessionLocal() as db:
        result = db.execute(
            update(JobDispatch)
            .where(
                JobDispatch.id == dispatch_id,
                JobDispatch.status == "claimed",
                JobDispatch.lease_token == token,
            )
            .values(lease_expires_at=now + timedelta(seconds=JOB_LEASE_SECONDS), updated_at=now)
        )
        db.commit()
        return result.rowcount == 1


def settle(db: Session, dispatch_id: str, token: str) -> bool:
    """在调用方事务内收口租约。

    必须与业务结果同事务：返回 False 表示令牌已失效（执行者被取代），调用方
    必须放弃本次结果写入，否则旧 Worker 会覆盖新尝试。
    """
    result = db.execute(
        update(JobDispatch)
        .where(
            JobDispatch.id == dispatch_id,
            JobDispatch.status == "claimed",
            JobDispatch.lease_token == token,
        )
        .values(status="settled", lease_token=None, lease_expires_at=None, updated_at=_now())
    )
    return result.rowcount == 1


def close_open_for_job(db: Session, *, job_id: str, tenant_id: str) -> int:
    """任务已有结局时关闭其未完成派发行，避免终态任务被再次叫醒。"""
    result = db.execute(
        update(JobDispatch)
        .where(
            JobDispatch.job_id == job_id,
            JobDispatch.tenant_id == tenant_id,
            JobDispatch.status != "settled",
        )
        .values(
            status="settled",
            lease_token=None,
            lease_expires_at=None,
            updated_at=_now(),
        )
    )
    return int(result.rowcount or 0)


def queue_backlog() -> dict[str, int | float | None]:
    """待执行的派发意图数量与最老一条的等待秒数，供就绪判定使用。

    只统计 `ready`（还没投出去）与 `published`（投了但没人领）：已 `claimed` 的行由
    租约超时回收负责，把它们算进积压会让"有一个慢任务"看起来像队列故障。
    `updated_at` 在 SQLite 下是 naive、PostgreSQL 下是 tz-aware，年龄必须统一按 UTC 算，
    否则同一份代码在两种方言下会差出一个时区。
    """
    now = _now()
    with SessionLocal() as db:
        rows = db.execute(
            select(
                func.count(),
                func.min(JobDispatch.updated_at),
            ).where(JobDispatch.status.in_(("ready", "published")))
        ).one()
    count = int(rows[0] or 0)
    oldest = rows[1]
    if oldest is None:
        return {"pending": count, "oldest_pending_seconds": None}
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    return {"pending": count, "oldest_pending_seconds": max(0.0, (now - oldest).total_seconds())}


def republish_stalled() -> list[str]:
    """把长时间无人认领、且任务仍停留在 queued 的派发行退回 ready。

    覆盖两类静默丢失：消息随 broker 数据卷损毁而消失；Worker 领了租约却在占用任务
    之前就退出。已进入 processing 的任务交给 `reclaim_timeouts`，两处不重叠，
    避免恢复器与派发器对同一任务各自动手。重投本身是安全的：执行权仍由条件更新判定。
    """
    now = _now()
    cutoff = now - timedelta(seconds=DISPATCH_REPUBLISH_SECONDS)
    republished: list[str] = []
    with SessionLocal() as db:
        rows = db.execute(
            select(JobDispatch, PersistentJob.status)
            .join(PersistentJob, PersistentJob.id == JobDispatch.job_id)
            .where(
                JobDispatch.status.in_(("published", "claimed")),
                PersistentJob.status == "queued",
                or_(
                    JobDispatch.updated_at < cutoff,
                    (JobDispatch.status == "claimed")
                    & (JobDispatch.lease_expires_at < now),
                ),
            )
            .order_by(JobDispatch.updated_at, JobDispatch.id)
            .limit(50)
        ).all()
        for row, _job_status in rows:
            db.execute(
                update(JobDispatch)
                .where(JobDispatch.id == row.id, JobDispatch.status == row.status)
                .values(
                    status="ready",
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            republished.append(row.id)
        db.commit()
    return republished
