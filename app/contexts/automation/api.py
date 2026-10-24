"""事件规则 API：规则维护、手动消费与运行日志（DOC-DD-006 §5）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.platform.http.dependencies import TenantContext, require_csrf, require_roles
from app.contexts.automation import service
from app.platform.db import get_db
from app.shared import audit

router = APIRouter(prefix="/api/v1/tenants/{tenant_id}", tags=["automation"])
ALL_ROLES = ("owner", "admin", "member", "reviewer")
ADMIN_ROLES = ("owner", "admin")


class ActionSpec(BaseModel):
    type: str
    params: dict[str, Any] = Field(default_factory=dict)


class RuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    event_type: str
    condition: dict[str, Any] | None = None
    action: ActionSpec


class RuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    event_type: str | None = None
    condition: dict[str, Any] | None = None
    action: ActionSpec | None = None
    status: str | None = None


@router.get("/automation/meta")
def rule_meta(context: TenantContext = Depends(require_roles(*ALL_ROLES))) -> dict[str, Any]:
    """规则表单的可选值来自后端同一份白名单。

    界面不复制这份清单，否则新增事件类型或收紧条件键时两边会静默漂移。
    """
    return {
        "event_types": list(service.ALLOWED_EVENT_TYPES),
        "action_types": list(service.ACTION_TYPES),
        "condition_keys": {
            event_type: sorted(keys) for event_type, keys in service.CONDITION_KEYS.items()
        },
        "max_condition_keys": service.MAX_CONDITION_KEYS,
    }


@router.get("/automation/rules")
def list_rules(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    context: TenantContext = Depends(require_roles(*ALL_ROLES)),
    db: Session = Depends(get_db),
):
    return service.list_rules(
        db, tenant_id=context.tenant.id, limit=limit, offset=offset
    )


@router.post("/automation/rules", status_code=201)
def create_rule(
    payload: RuleCreate,
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles(*ADMIN_ROLES)),
    db: Session = Depends(get_db),
):
    return service.create_rule(
        db,
        tenant_id=context.tenant.id,
        actor_user_id=context.auth.user.id,
        name=payload.name,
        event_type=payload.event_type,
        condition=payload.condition,
        action=payload.action.model_dump(),
    )


@router.patch("/automation/rules/{rule_id}")
def update_rule(
    rule_id: str,
    payload: RuleUpdate,
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles(*ADMIN_ROLES)),
    db: Session = Depends(get_db),
):
    body: dict[str, Any] = payload.model_dump(exclude_none=True)
    if body.get("action") is not None:
        body["action"] = payload.action.model_dump()
    return service.update_rule(
        db,
        tenant_id=context.tenant.id,
        actor_user_id=context.auth.user.id,
        rule_id=rule_id,
        **body,
    )


@router.post("/automation/rules/{rule_id}/run")
def run_rule(
    rule_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles(*ADMIN_ROLES)),
    db: Session = Depends(get_db),
):
    service.get_rule(db, context.tenant.id, rule_id)
    return service.dispatch(
        db, tenant_id=context.tenant.id, rule_id=rule_id, limit=limit
    )


@router.post("/automation/dispatch")
def dispatch_events(
    limit: int = Query(default=50, ge=1, le=100),
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles(*ADMIN_ROLES)),
    db: Session = Depends(get_db),
):
    summary = service.dispatch(db, tenant_id=context.tenant.id, limit=limit)
    audit.record(
        db,
        tenant_id=context.tenant.id,
        actor_user_id=context.auth.user.id,
        action="automation.dispatched",
        object_type="tenant",
        object_id=context.tenant.id,
        metadata={key: summary[key] for key in ("consumed", "applied", "skipped", "failed")},
    )
    db.commit()
    return summary


@router.get("/automation/runs")
def list_runs(
    rule_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    context: TenantContext = Depends(require_roles(*ALL_ROLES)),
    db: Session = Depends(get_db),
):
    return service.list_runs(
        db,
        tenant_id=context.tenant.id,
        rule_id=rule_id,
        status=status,
        limit=limit,
        offset=offset,
    )


@router.get("/automation/notifications")
def list_notifications(
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    context: TenantContext = Depends(require_roles(*ADMIN_ROLES)),
    db: Session = Depends(get_db),
):
    return service.list_notifications(
        db, tenant_id=context.tenant.id, status=status, limit=limit, offset=offset
    )
