"""事件规则聚合：规则、运行日志与通知登记（DOC-DD-006 §3/§4）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# 只允许订阅已经存在的事件类型：新增事件必须同时出现在这里与 docs/13 §4.5。
ALLOWED_EVENT_TYPES = (
    "job.succeeded",
    "job.partial",
    "job.failed",
    "result.confirmed",
    "result.rejected",
    "result.revised",
)
# 默认不执行不安全动作：没有自动确认、自动删除、自动改保留状态或外部调用。
ACTION_TYPES = ("tag_document", "create_notification")
RULE_STATUSES = ("active", "paused")
RUN_STATUSES = ("applied", "skipped", "failed")
MAX_CONDITION_KEYS = 8
MAX_NAME_CHARS = 80


class EventRule(Base):
    """租户内的一条事件规则；每次有效编辑递增 version，历史日志按当时版本记录。"""

    __tablename__ = "event_rules"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_event_rule_name"),
        Index("ix_event_rule_trigger", "tenant_id", "event_type", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(MAX_NAME_CHARS), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    condition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    action_params: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RuleRun(Base):
    """一次 (事件, 规则, 规则版本) 的运行记录；delivery_key 唯一保证重试幂等。"""

    __tablename__ = "rule_runs"
    __table_args__ = (
        UniqueConstraint("delivery_key", name="uq_rule_run_delivery_key"),
        Index("ix_rule_run_list", "tenant_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rule_id: Mapped[str] = mapped_column(
        ForeignKey("event_rules.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_id: Mapped[str] = mapped_column(
        ForeignKey("domain_events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(500))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delivery_key: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationRequest(Base):
    """通知意图：没有投递渠道前停在 pending，绝不假装已送达。"""

    __tablename__ = "notification_requests"
    __table_args__ = (
        Index("ix_notification_list", "tenant_id", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recipient_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    rule_id: Mapped[str | None] = mapped_column(
        ForeignKey("event_rules.id", ondelete="SET NULL"), nullable=True
    )
    subject: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    object_id: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
