"""处理编排上下文：任务尝试、幂等提交与超时回收（DOC-DD-004 §4.3/§4.4/§5）。"""

from __future__ import annotations

from datetime import datetime

from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

ATTEMPT_STATUSES = ("running", "succeeded", "failed", "timed_out")


class JobAttempt(Base):
    """一次执行尝试；并发正确性由任务状态条件更新保证。"""

    __tablename__ = "job_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_no", name="uq_job_attempt_number"),
        Index("ix_job_attempts_job_started", "job_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    engine: Mapped[str] = mapped_column(String(120), nullable=False)
    parse_modes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    error: Mapped[str | None] = mapped_column(String(500))
    # 快照退役 R1（docs/20 §2）：告警属于"某一次尝试的解析产物"，
    # 过去只能塞进任务级快照，重试一次就被下一次的告警覆盖掉。
    warnings: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
