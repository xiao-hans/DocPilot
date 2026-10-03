"""任务派发与执行租约（docs/17 §4）。

一次执行尝试对应一行派发记录。消息只是"叫醒"作用，业务状态的权威仍然在
`processing_jobs` 与 `job_attempts`：派发行负责回答"这件事有没有人 publish、
现在谁在跑、租约什么时候失效"，从而让重复投递、Worker 崩溃与旧 Worker 延迟提交
都能被数据库条件更新挡住。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# ready: 待派发；published: 已进队列且拿到发布确认；claimed: Worker 持租约执行中；
# settled: 该次派发已有结局（执行收口、任务已终态或被更高尝试取代）。
DISPATCH_STATUSES = ("ready", "published", "claimed", "settled")


class JobDispatch(Base):
    __tablename__ = "job_dispatches"

    __table_args__ = (
        UniqueConstraint("job_id", "attempt_no", name="uq_job_dispatch_attempt"),
        Index("ix_job_dispatches_status_created", "status", "created_at"),
        Index("ix_job_dispatches_lease_expires", "status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[str] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    queue_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ready", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 发布确认时间：只有拿到 publisher confirm 才写，未确认的行留在 ready 重放。
    publish_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 租约令牌由领取者随机生成；续租与收口都必须匹配当前令牌，旧执行者因此无法覆盖。
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
