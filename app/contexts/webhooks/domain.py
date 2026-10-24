"""出站 Webhook 聚合（DOC-DD-021 §3）。

两张表都是"投递状态"的权威源，不是第二真相：事件本体仍在 `domain_events`，这里只记
"某个端点对某个事件的投递走到哪一步"。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base

ENDPOINT_STATUSES = ("active", "paused")
DELIVERY_STATUSES = ("pending", "inflight", "succeeded", "failed")
# 失败归因只到"类别"为止：响应正文由第三方给出，可能带他们自己的秘密，不落库。
ERROR_KINDS = ("timeout", "connect", "http_status", "blocked_target", "disabled")
MAX_URL_CHARS = 500
MAX_DESCRIPTION_CHARS = 120
MAX_EVENT_TYPES = 12
SIGNATURE_PREFIX = "v1="


class WebhookEndpoint(Base):
    """租户登记的一个出站端点。

    签名密钥不落库：`secret = HMAC(WEBHOOK_MASTER_KEY, f"{id}|{secret_version}")`，
    轮换只推进 `secret_version`。这样单独的数据库读取不构成密钥泄露。
    """

    __tablename__ = "webhook_endpoints"
    __table_args__ = (
        UniqueConstraint("tenant_id", "url", name="uq_webhook_endpoint_url"),
        Index("ix_webhook_endpoint_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(String(MAX_URL_CHARS), nullable=False)
    description: Mapped[str] = mapped_column(String(MAX_DESCRIPTION_CHARS), nullable=False, default="")
    event_types: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    secret_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    grace_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)


class WebhookDelivery(Base):
    """一个 (端点, 事件) 的投递行；唯一约束就是扇出的幂等键。"""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("endpoint_id", "event_id", name="uq_webhook_delivery_endpoint_event"),
        Index("ix_webhook_delivery_due", "status", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    endpoint_id: Mapped[str] = mapped_column(
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_id: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
