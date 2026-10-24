"""只追加审计事件（DOC-DD-003 §4.2/§4.3/§7）。"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.platform.db import SessionLocal
from app.models.access import AuditEvent
from app.platform.request_context import current as current_request_id

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
    # 快照退役 R5 的观察期判据（docs/20 §6.1）：只记"哪条任务缺哪些规范化事实"，
    # 不记字段值、文件名或错误正文——那些都在租户数据里，平台审计不该把它们带出来。
    "job.rebuild_incomplete": frozenset({"missing"}),
    "template.created": frozenset({"name"}),
    "template.updated": frozenset({"name"}),
    "template.published": frozenset({"revision"}),
    "template.retired": frozenset({"name"}),
    "template.copied": frozenset({"name"}),
    "document.metadata_updated": frozenset({"changed", "rule_id"}),
    # 内容签名核对是只读检查，审计记的是"查过几次、结果形状"，不记文件名与摘要正文。
    "document.integrity_checked": frozenset({"checked", "clean", "counts"}),
    "rule.created": frozenset({"event_type", "action_type", "version"}),
    "rule.updated": frozenset({"changed", "version"}),
    "platform.usage_viewed": frozenset({"scope", "window_days", "rows"}),
    "platform.usage_failures_viewed": frozenset({"scope", "window_days", "codes", "truncated"}),
    # 配额只有两个轴，且都是"设成多少"的裸数值，不含任何口径推导。
    "tenant.quota_updated": frozenset({"max_documents", "max_storage_bytes"}),
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
    request_id: str | None = None,
) -> AuditEvent:
    """构造事件对象但不写库，便于调用方并入同一事务。

    `request_id` 默认从当前请求上下文取（NFR-005）：调用方不必逐个传参，
    而 Worker、派发器、脚本这些没有请求上下文的地方取到 None，不会继承某个
    已结束请求的 ID。显式传值优先，供"一个请求内的动作要挂到另一个 ID"这类
    极少数场景使用。
    """
    resolved = request_id if request_id is not None else current_request_id()
    return AuditEvent(
        id=uuid.uuid4().hex,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        request_id=resolved,
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
