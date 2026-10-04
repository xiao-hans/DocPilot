"""只追加审计事件（DOC-DD-003 §4.2/§4.3/§7）。"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.access import AuditEvent

logger = logging.getLogger(__name__)

# 每个动作允许的 metadata 键；新动作必须先在 DOC-DD-003 §4.3 登记再在此处登记。
METADATA_KEYS: dict[str, frozenset[str]] = {
    "member.invited": frozenset({"role", "expires_at"}),
    "member.invitation_revoked": frozenset({"role"}),
    "member.invitation_accepted": frozenset({"role", "user_id"}),
    "member.role_changed": frozenset({"from", "to"}),
    "member.removed": frozenset({"role"}),
    "job.created": frozenset({"file_count"}),
    "job.excel_exported": frozenset(),
    "job.retried": frozenset({"attempt_count"}),
    "template.created": frozenset({"name"}),
    "template.updated": frozenset({"name"}),
    "template.published": frozenset({"revision"}),
    "template.retired": frozenset({"name"}),
    "template.copied": frozenset({"name"}),
    "document.metadata_updated": frozenset({"changed", "rule_id"}),
    "rule.created": frozenset({"event_type", "action_type", "version"}),
    "rule.updated": frozenset({"changed", "version"}),
    "platform.usage_viewed": frozenset({"scope", "window_days", "rows"}),
    "platform.usage_failures_viewed": frozenset({"scope", "window_days", "codes", "truncated"}),
    # 出站 Webhook（FR-041）：只记主机名与计数，完整 URL 的 path/query 可能自带 token。
    # 投递本身不写审计——一次事件 × N 端点 × 重试会把审计面淹成噪声，明细在 webhook_deliveries。
    "webhook.endpoint_created": frozenset({"url_host", "secret_version", "event_types"}),
    "webhook.endpoint_status_changed": frozenset({"from", "to"}),
    "webhook.endpoint_rotated": frozenset({"secret_version", "grace_seconds"}),
    "webhook.endpoint_deleted": frozenset({"url_host", "event_types"}),
    "automation.dispatched": frozenset({"consumed", "applied", "skipped", "failed"}),
    "document.exported": frozenset({"format"}),
    "job.exported": frozenset({"format"}),
    "result.revised": frozenset({"revision_no"}),
    "result.confirmed": frozenset({"revision_no"}),
    "result.rejected": frozenset({"revision_no"}),
}


def _payload(action: str, metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    allowed = METADATA_KEYS.get(action)
    if allowed is None:
        raise ValueError(f"未登记的审计动作: {action}")
    supplied = dict(metadata or {})
    payload = {key: value for key, value in supplied.items() if key in allowed}
    dropped = sorted(set(supplied) - set(payload))
    if dropped:
        logger.debug("丢弃未登记审计键: %s", dropped)
    return payload


def new_event(
    *,
    tenant_id: str,
    actor_user_id: str | None,
    action: str,
    object_type: str,
    object_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> AuditEvent:
    """构造事件对象但不写库，便于调用方并入同一事务。"""
    return AuditEvent(
        id=uuid.uuid4().hex,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        metadata_=_payload(action, metadata),
        created_at=datetime.now(timezone.utc),
    )


def record(db: Session, **kwargs: Any) -> AuditEvent:
    event = new_event(**kwargs)
    db.add(event)
    return event


def record_now(**kwargs: Any) -> AuditEvent:
    """独立事务写入：用于没有随业务事务提交的场景（如导出下载）。"""
    event = new_event(**kwargs)
    with SessionLocal() as db:
        db.add(event)
        db.commit()
    return event
