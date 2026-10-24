"""Webhook API（FR-041，DOC-DD-021 §6）。

角色矩阵：读端点列表与投递明细对 owner/admin/member 开放（排障需要 member 能看到失败），
`reviewer` 不开放；写操作只 owner/admin。跨租户取别人的端点一律 404。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.platform.http.dependencies import TenantContext, require_csrf, require_roles
from app.platform import config
from app.contexts.automation.domain import ALLOWED_EVENT_TYPES
from app.contexts.webhooks import service
from app.platform.db import get_db

router = APIRouter(prefix="/api/v1/tenants/{tenant_id}", tags=["webhooks"])
READ_ROLES = ("owner", "admin", "member")
WRITE_ROLES = ("owner", "admin")


class EndpointCreate(BaseModel):
    url: str
    event_types: list[str] = Field(default_factory=list)
    description: str = ""


class StatusUpdate(BaseModel):
    status: str


class RotateRequest(BaseModel):
    grace_seconds: int | None = Field(default=None, ge=0, le=86400)


@router.get("/webhooks/meta")
def webhook_meta(
    context: TenantContext = Depends(require_roles(*READ_ROLES)),
) -> dict[str, Any]:
    """可订阅事件与当前出站姿态都来自后端同一份清单，界面不复制常量。"""
    return {
        "event_types": list(ALLOWED_EVENT_TYPES),
        "enabled": bool(config.WEBHOOK_MASTER_KEY),
        "signing": {"algorithm": "HMAC-SHA256", "signed_over": "<timestamp>.<body>"},
        "limits": {
            "timeout_seconds": config.WEBHOOK_TIMEOUT_SECONDS,
            "max_attempts": config.WEBHOOK_MAX_ATTEMPTS,
            "lease_seconds": config.WEBHOOK_LEASE_SECONDS,
            "rotation_grace_seconds": config.WEBHOOK_ROTATION_GRACE_SECONDS,
        },
        "allow_private_targets": config.WEBHOOK_ALLOW_PRIVATE_TARGETS,
    }


@router.get("/webhooks")
def list_endpoints(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    context: TenantContext = Depends(require_roles(*READ_ROLES)),
    db: Session = Depends(get_db),
):
    return service.list_endpoints(
        db, tenant_id=context.tenant.id, limit=limit, offset=offset
    )


@router.get("/webhooks/deliveries")
def list_deliveries(
    endpoint_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    context: TenantContext = Depends(require_roles(*READ_ROLES)),
    db: Session = Depends(get_db),
):
    """声明在 `/webhooks/{endpoint_id}` 之前：FastAPI 按声明顺序匹配，否则 deliveries 会被当成 id。"""
    return service.list_deliveries(
        db,
        tenant_id=context.tenant.id,
        endpoint_id=endpoint_id,
        status=status,
        limit=limit,
        offset=offset,
    )


@router.post("/webhooks", status_code=201)
def create_endpoint(
    payload: EndpointCreate,
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles(*WRITE_ROLES)),
    db: Session = Depends(get_db),
):
    return service.create_endpoint(
        db,
        tenant_id=context.tenant.id,
        actor_user_id=context.auth.user.id,
        url=payload.url,
        event_types=payload.event_types,
        description=payload.description,
    )


@router.get("/webhooks/{endpoint_id}")
def get_endpoint(
    endpoint_id: str,
    context: TenantContext = Depends(require_roles(*READ_ROLES)),
    db: Session = Depends(get_db),
):
    return service.endpoint_view(service.get_endpoint(db, context.tenant.id, endpoint_id))


@router.post("/webhooks/{endpoint_id}/status")
def set_status(
    endpoint_id: str,
    payload: StatusUpdate,
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles(*WRITE_ROLES)),
    db: Session = Depends(get_db),
):
    return service.set_status(
        db,
        tenant_id=context.tenant.id,
        endpoint_id=endpoint_id,
        status=payload.status,
        actor_user_id=context.auth.user.id,
    )


@router.post("/webhooks/{endpoint_id}/rotate")
def rotate_secret(
    endpoint_id: str,
    payload: RotateRequest | None = None,
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles(*WRITE_ROLES)),
    db: Session = Depends(get_db),
):
    return service.rotate_secret(
        db,
        tenant_id=context.tenant.id,
        endpoint_id=endpoint_id,
        actor_user_id=context.auth.user.id,
        grace_seconds=payload.grace_seconds if payload else None,
    )


@router.delete("/webhooks/{endpoint_id}")
def delete_endpoint(
    endpoint_id: str,
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles(*WRITE_ROLES)),
    db: Session = Depends(get_db),
):
    return service.delete_endpoint(
        db,
        tenant_id=context.tenant.id,
        endpoint_id=endpoint_id,
        actor_user_id=context.auth.user.id,
    )
