"""Webhook 端点管理与投递状态机（DOC-DD-021 §3/§4）。

扇出为什么不用 `domain_events.published_at`：那个字段现在的含义是"事件规则引擎已经消费过"
（`automation/service.py` 跑完规则时写它）。Webhook 是第二个独立消费者，借用同一个闸门会让
两边互相吞掉事件，所以这里用 `webhook_deliveries` 的唯一约束当幂等键。
"""

from __future__ import annotations

import secrets as _secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import config
from app.contexts.automation.domain import ALLOWED_EVENT_TYPES
from app.contexts.webhooks import signing, targets
from app.contexts.webhooks.domain import (
    MAX_DESCRIPTION_CHARS,
    MAX_EVENT_TYPES,
    WebhookDelivery,
    WebhookEndpoint,
)
from app.errors import AccessError
from app.models.outbox import DomainEventRecord
from app.services import audit
from app.services.timestamps import iso_utc


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _endpoint_view(endpoint: WebhookEndpoint, *, secret: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": endpoint.id,
        "url": endpoint.url,
        "url_host": _host(endpoint.url),
        "description": endpoint.description,
        "event_types": list(endpoint.event_types or []),
        "status": endpoint.status,
        "secret_version": endpoint.secret_version,
        "secret_fingerprint": None,
        "grace_seconds": endpoint.grace_seconds,
        "created_at": iso_utc(_aware(endpoint.created_at)),
        "updated_at": iso_utc(_aware(endpoint.updated_at)),
        "rotated_at": iso_utc(_aware(endpoint.rotated_at)),
    }
    if secret is not None:
        body["secret_fingerprint"] = signing.fingerprint(secret)
    return body


def _host(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc


def _validate_event_types(values: list[str]) -> list[str]:
    cleaned = list(dict.fromkeys((value or "").strip() for value in values))
    cleaned = [value for value in cleaned if value]
    if not cleaned:
        raise AccessError(422, "至少要订阅一个事件类型")
    if len(cleaned) > MAX_EVENT_TYPES:
        raise AccessError(422, f"订阅事件类型不得超过 {MAX_EVENT_TYPES} 个")
    unknown = sorted(set(cleaned) - set(ALLOWED_EVENT_TYPES))
    if unknown:
        raise AccessError(422, f"未知的事件类型：{'、'.join(unknown)}")
    return sorted(cleaned)


def endpoint_view(endpoint: WebhookEndpoint) -> dict[str, Any]:
    return _endpoint_view(endpoint)


def require_master_key() -> str:
    if not config.WEBHOOK_MASTER_KEY:
        raise AccessError(409, "未配置 config.WEBHOOK_MASTER_KEY：出站签名不可用，端点未创建")
    return config.WEBHOOK_MASTER_KEY


def create_endpoint(
    db: Session,
    *,
    tenant_id: str,
    actor_user_id: str | None,
    url: str,
    event_types: list[str],
    description: str = "",
) -> dict[str, Any]:
    checked_url = targets.check_target(url)
    types = _validate_event_types(event_types)
    key = require_master_key()
    now = _now()
    endpoint = WebhookEndpoint(
        id=_new_id(),
        tenant_id=tenant_id,
        url=checked_url,
        description=(description or "").strip()[:MAX_DESCRIPTION_CHARS],
        event_types=types,
        status="active",
        secret_version=1,
        created_by=actor_user_id,
        created_at=now,
        updated_at=now,
        rotated_at=None,
        grace_seconds=config.WEBHOOK_ROTATION_GRACE_SECONDS,
    )
    db.add(endpoint)
    audit.record(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="webhook.endpoint_created",
        object_type="webhook_endpoint",
        object_id=endpoint.id,
        metadata={
            "url_host": _host(checked_url),
            "secret_version": 1,
            "event_types": len(types),
        },
    )
    try:
        db.commit()
    except IntegrityError as exc:
        # 同一租户重复登记同一地址会变成"同一事件投两次"，唯一约束在这里就是它的兜底。
        db.rollback()
        raise AccessError(409, "该回调地址已登记") from exc
    db.refresh(endpoint)
    # 明文只在创建与轮换时返回一次，之后列表里只有末 6 位指纹。
    secret = signing.derive_secret(key, endpoint.id, endpoint.secret_version)
    return {"endpoint": _endpoint_view(endpoint, secret=secret), "secret": secret}


def get_endpoint(db: Session, tenant_id: str, endpoint_id: str) -> WebhookEndpoint:
    endpoint = db.get(WebhookEndpoint, endpoint_id)
    # 跨租户与不存在给同一个 404：存在性本身就是信息。
    if endpoint is None or endpoint.tenant_id != tenant_id:
        raise AccessError(404, "回调端点不存在")
    return endpoint


def list_endpoints(db: Session, *, tenant_id: str, limit: int, offset: int) -> dict[str, Any]:
    stmt = select(WebhookEndpoint).where(WebhookEndpoint.tenant_id == tenant_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(WebhookEndpoint.created_at.desc(), WebhookEndpoint.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    key = config.WEBHOOK_MASTER_KEY
    return {
        "total": total,
        "items": [
            _endpoint_view(
                row,
                secret=signing.derive_secret(key, row.id, row.secret_version) if key else None,
            )
            for row in rows
        ],
    }


def set_status(
    db: Session, *, tenant_id: str, endpoint_id: str, status: str, actor_user_id: str | None
) -> dict[str, Any]:
    if status not in ("active", "paused"):
        raise AccessError(422, "状态只能是 active 或 paused")
    endpoint = get_endpoint(db, tenant_id, endpoint_id)
    previous = endpoint.status
    endpoint.status = status
    endpoint.updated_at = _now()
    audit.record(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="webhook.endpoint_status_changed",
        object_type="webhook_endpoint",
        object_id=endpoint.id,
        metadata={"from": previous, "to": status},
    )
    db.commit()
    db.refresh(endpoint)
    return _endpoint_view(endpoint)


def rotate_secret(
    db: Session,
    *,
    tenant_id: str,
    endpoint_id: str,
    actor_user_id: str | None,
    grace_seconds: int | None = None,
) -> dict[str, Any]:
    key = require_master_key()
    endpoint = get_endpoint(db, tenant_id, endpoint_id)
    endpoint.secret_version += 1
    endpoint.rotated_at = _now()
    endpoint.updated_at = endpoint.rotated_at
    if grace_seconds is not None:
        endpoint.grace_seconds = max(0, min(grace_seconds, 86400))
    audit.record(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="webhook.endpoint_rotated",
        object_type="webhook_endpoint",
        object_id=endpoint.id,
        metadata={"secret_version": endpoint.secret_version, "grace_seconds": endpoint.grace_seconds},
    )
    db.commit()
    db.refresh(endpoint)
    secret = signing.derive_secret(key, endpoint.id, endpoint.secret_version)
    return {"endpoint": _endpoint_view(endpoint, secret=secret), "secret": secret}


def delete_endpoint(
    db: Session, *, tenant_id: str, endpoint_id: str, actor_user_id: str | None
) -> dict[str, Any]:
    endpoint = get_endpoint(db, tenant_id, endpoint_id)
    metadata = {"url_host": _host(endpoint.url), "event_types": len(endpoint.event_types or [])}
    endpoint_id_value = endpoint.id
    db.delete(endpoint)
    audit.record(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="webhook.endpoint_deleted",
        object_type="webhook_endpoint",
        object_id=endpoint_id_value,
        metadata=metadata,
    )
    db.commit()
    return {"deleted": endpoint_id_value}


def signature_headers(
    endpoint: WebhookEndpoint, body: bytes, timestamp: int
) -> dict[str, str]:
    """当前密钥必签；宽限窗口内额外给一把旧密钥的签名，让接收方无停机切换。"""
    key = require_master_key()
    headers = {
        signing.SIGNATURE_HEADER: signing.sign(
            signing.derive_secret(key, endpoint.id, endpoint.secret_version), timestamp, body
        ),
        signing.TIMESTAMP_HEADER: str(timestamp),
    }
    rotated = _aware(endpoint.rotated_at)
    if endpoint.secret_version > 1 and rotated is not None:
        window = endpoint.grace_seconds if endpoint.grace_seconds is not None else 0
        if (_now() - rotated) <= timedelta(seconds=window):
            previous = signing.derive_secret(key, endpoint.id, endpoint.secret_version - 1)
            headers[f"{signing.SIGNATURE_HEADER}-Previous"] = signing.sign(
                previous, timestamp, body
            )
    return headers


def fan_out(db: Session, *, tenant_id: str | None = None, limit: int = 200) -> int:
    """给"活动端点 × 尚未建过投递行的事件"补上 pending 行。

    唯一约束 `uq_webhook_delivery_endpoint_event` 是这里的底气：并发扇出最多让一次插入撞约束，
    不会产生第二条投递。
    """
    now = _now()
    created = 0
    endpoint_stmt = select(WebhookEndpoint).where(WebhookEndpoint.status == "active")
    if tenant_id:
        endpoint_stmt = endpoint_stmt.where(WebhookEndpoint.tenant_id == tenant_id)
    for endpoint in db.scalars(endpoint_stmt.order_by(WebhookEndpoint.created_at)).all():
        wanted = set(endpoint.event_types or [])
        if not wanted:
            continue
        already = exists().where(
            and_(
                WebhookDelivery.endpoint_id == endpoint.id,
                WebhookDelivery.event_id == DomainEventRecord.id,
            )
        )
        stmt = (
            select(DomainEventRecord)
            .where(
                DomainEventRecord.tenant_id == endpoint.tenant_id,
                DomainEventRecord.event_type.in_(sorted(wanted)),
                DomainEventRecord.occurred_at >= endpoint.created_at,
                ~already,
            )
            .order_by(DomainEventRecord.occurred_at, DomainEventRecord.id)
            .limit(limit)
        )
        for event in db.scalars(stmt).all():
            db.add(
                WebhookDelivery(
                    id=_new_id(),
                    endpoint_id=endpoint.id,
                    tenant_id=endpoint.tenant_id,
                    event_id=event.id,
                    event_type=event.event_type,
                    payload=_wire_payload(endpoint, event),
                    status="pending",
                    attempts=0,
                    next_attempt_at=now,
                    created_at=now,
                )
            )
            created += 1
    if created:
        db.commit()
    return created


def _wire_payload(endpoint: WebhookEndpoint, event: DomainEventRecord) -> dict[str, Any]:
    """信封一次成形并落库：重试发的是同一份字节，不会因为重新读事件而改变形状。"""
    return {
        "id": event.id,
        "type": event.event_type,
        "occurred_at": iso_utc(_aware(event.occurred_at)),
        "tenant_id": event.tenant_id,
        "endpoint_id": endpoint.id,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": event.aggregate_id,
        "data": dict(event.payload or {}),
    }


def reclaim_expired(db: Session) -> int:
    """租约到期的 inflight 行退回 pending：投递进程被杀不能把行永久卡住。"""
    now = _now()
    result = db.execute(
        update(WebhookDelivery)
        .where(
            WebhookDelivery.status == "inflight",
            WebhookDelivery.claimed_until < now,
        )
        .values(status="pending", claimed_until=None, lease_token=None, next_attempt_at=now)
    )
    db.commit()
    return int(result.rowcount or 0)


def claim_due(db: Session, *, limit: int = 20, tenant_id: str | None = None) -> list[dict[str, Any]]:
    """条件更新认领：只有一个胜者能把 pending 改成 inflight（与 G2 的 Worker 租约同构）。"""
    now = _now()
    lease_until = now + timedelta(seconds=config.WEBHOOK_LEASE_SECONDS)
    claimed: list[dict[str, Any]] = []
    stmt = (
        select(WebhookDelivery)
        .where(
            WebhookDelivery.status == "pending",
            WebhookDelivery.next_attempt_at <= now,
        )
        .order_by(WebhookDelivery.next_attempt_at, WebhookDelivery.id)
        .limit(limit)
    )
    if tenant_id:
        stmt = stmt.where(WebhookDelivery.tenant_id == tenant_id)
    for delivery in db.scalars(stmt).all():
        result = db.execute(
            update(WebhookDelivery)
            .where(
                WebhookDelivery.id == delivery.id,
                WebhookDelivery.status == "pending",
            )
            .values(
                status="inflight",
                claimed_until=lease_until,
                lease_token=_secrets.token_hex(16),
                attempts=WebhookDelivery.attempts + 1,
            )
        )
        if int(result.rowcount or 0) == 1:
            db.refresh(delivery)
            claimed.append(
                {
                    "delivery_id": delivery.id,
                    "endpoint_id": delivery.endpoint_id,
                    "tenant_id": delivery.tenant_id,
                    "lease_token": delivery.lease_token,
                    "payload": delivery.payload,
                    "attempts": delivery.attempts,
                }
            )
    db.commit()
    for item in claimed:
        delivery = db.get(WebhookDelivery, item["delivery_id"])
        endpoint = db.get(WebhookEndpoint, delivery.endpoint_id)
        item["url"] = endpoint.url if endpoint else ""
        item["secret_version"] = endpoint.secret_version if endpoint else 0
        item["endpoint_status"] = endpoint.status if endpoint else "missing"
    return claimed


def finish(
    db: Session,
    *,
    delivery_id: str,
    lease_token: str | None,
    outcome: str,
    response_status: int | None = None,
    error_kind: str | None = None,
    last_error: str | None = None,
) -> str:
    """收尾一次投递：succeeded / retry / failed。持有者不对就直接放弃，绝不覆盖别人的状态。"""
    delivery = db.get(WebhookDelivery, delivery_id)
    if delivery is None or delivery.status != "inflight" or delivery.lease_token != lease_token:
        return "lost"
    now = _now()
    if outcome == "succeeded":
        delivery.status = "succeeded"
        delivery.response_status = response_status
        delivery.completed_at = now
        delivery.claimed_until = None
        delivery.lease_token = None
        delivery.error_kind = None
    elif outcome == "terminal":
        delivery.status = "failed"
        delivery.response_status = response_status
        delivery.error_kind = error_kind or "http_status"
        delivery.last_error = (last_error or "")[:300]
        delivery.completed_at = now
        delivery.claimed_until = None
        delivery.lease_token = None
    else:
        if delivery.attempts >= config.WEBHOOK_MAX_ATTEMPTS:
            delivery.status = "failed"
            delivery.error_kind = error_kind or "http_status"
            delivery.last_error = (last_error or "")[:300]
            delivery.completed_at = now
            delivery.claimed_until = None
            delivery.lease_token = None
            # 预算花光就如实报 terminal：把它算成"还会重试"会让调用方的计数与排障都读错。
            outcome = "terminal"
        else:
            backoff = min(
                config.WEBHOOK_BACKOFF_SECONDS * (2 ** (delivery.attempts - 1)),
                config.WEBHOOK_BACKOFF_MAX_SECONDS,
            )
            delivery.status = "pending"
            delivery.response_status = response_status
            delivery.error_kind = error_kind or "http_status"
            delivery.last_error = (last_error or "")[:300]
            delivery.next_attempt_at = now + timedelta(seconds=backoff)
            delivery.claimed_until = None
            delivery.lease_token = None
    db.commit()
    return outcome


def list_deliveries(
    db: Session,
    *,
    tenant_id: str,
    endpoint_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    stmt = select(WebhookDelivery).where(WebhookDelivery.tenant_id == tenant_id)
    if endpoint_id:
        get_endpoint(db, tenant_id, endpoint_id)
        stmt = stmt.where(WebhookDelivery.endpoint_id == endpoint_id)
    if status:
        if status not in ("pending", "inflight", "succeeded", "failed"):
            raise AccessError(422, "未知的投递状态")
        stmt = stmt.where(WebhookDelivery.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    counts = dict(
        db.execute(
            select(WebhookDelivery.status, func.count())
            .where(WebhookDelivery.tenant_id == tenant_id)
            .group_by(WebhookDelivery.status)
        ).all()
    )
    return {
        "total": total,
        "counts": counts,
        "allow_private_targets": config.WEBHOOK_ALLOW_PRIVATE_TARGETS,
        "max_attempts": config.WEBHOOK_MAX_ATTEMPTS,
        "items": [
            {
                "id": row.id,
                "endpoint_id": row.endpoint_id,
                "event_id": row.event_id,
                "event_type": row.event_type,
                "status": row.status,
                "attempts": row.attempts,
                "next_attempt_at": iso_utc(_aware(row.next_attempt_at)),
                "response_status": row.response_status,
                "error_kind": row.error_kind,
                "last_error": row.last_error,
                "created_at": iso_utc(_aware(row.created_at)),
                "completed_at": iso_utc(_aware(row.completed_at)),
            }
            for row in rows
        ],
    }


def delivery_counts(db: Session, *, tenant_id: str) -> dict[str, int]:
    return dict(
        db.execute(
            select(WebhookDelivery.status, func.count())
            .where(WebhookDelivery.tenant_id == tenant_id)
            .group_by(WebhookDelivery.status)
        ).all()
    )


__all__ = [
    "claim_due",
    "create_endpoint",
    "delete_endpoint",
    "delivery_counts",
    "fan_out",
    "finish",
    "get_endpoint",
    "list_deliveries",
    "list_endpoints",
    "reclaim_expired",
    "require_master_key",
    "rotate_secret",
    "set_status",
    "signature_headers",
]
