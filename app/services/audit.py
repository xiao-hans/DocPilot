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
