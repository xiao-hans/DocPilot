"""持久化处理任务索引；完整任务快照暂以 JSON 保存。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base


class PersistentJob(Base):
    __tablename__ = "processing_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), index=True)
    # 引用创建任务时冻结的模板版本；跨租户引用由复合外键在数据库层阻止。
    template_version_id: Mapped[str | None] = mapped_column(String(32))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # 快照退役 R1（docs/20 §3）：可空 = 还没回填，不用默认值去猜历史行的意思。
    # field_definitions 只在创建时写入，之后不可变；template_version_id 非空时以模板版本为准。
    field_definitions: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    # progress 与 status 同事务写入：二者不可分开更新，否则进度会说出状态没承认的话。
    progress: Mapped[str | None] = mapped_column(String(24), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "template_version_id"],
            ["template_versions.tenant_id", "template_versions.id"],
            name="fk_processing_job_template_version",
            ondelete="RESTRICT",
        ),
        # 幂等键可空，因此只在非空时约束唯一（DOC-DD-004 §4.3）。
        Index(
            "uq_job_idempotency_key",
            "tenant_id",
            "idempotency_key",
            unique=True,
            sqlite_where=text("idempotency_key IS NOT NULL"),
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )
