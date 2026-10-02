"""模板 API：读面向全部成员，写面向 admin/owner（DOC-DD-004 §6）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.dependencies import TenantContext, require_csrf, require_roles
from app.contexts.templates import service
from app.db import get_db

router = APIRouter(prefix="/api/v1/tenants/{tenant_id}/templates", tags=["templates"])

ALL_ROLES = ("owner", "admin", "member", "reviewer")
MANAGE_ROLES = ("owner", "admin")


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    fields: list[dict[str, Any]] = Field(min_length=1, max_length=30)


class TemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    fields: list[dict[str, Any]] | None = Field(default=None, max_length=30)


class TemplateCopy(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)


@router.get("")
def list_templates(
    status: str | None = None,
    context: TenantContext = Depends(require_roles(*ALL_ROLES)),
    db: Session = Depends(get_db),
):
    statuses = [item for item in (status.split(",") if status else []) if item]
    return {"templates": service.list_all(db, tenant_id=context.tenant.id, statuses=statuses or None)}


@router.post("", status_code=201)
def create_template(
    payload: TemplateCreate,
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles(*MANAGE_ROLES)),
    db: Session = Depends(get_db),
):
    return service.create(
        db,
        tenant_id=context.tenant.id,
        actor_user_id=context.auth.user.id,
        name=payload.name,
        fields=payload.fields,
    )


@router.get("/{template_id}")
def get_template(
    template_id: str,
    context: TenantContext = Depends(require_roles(*ALL_ROLES)),
    db: Session = Depends(get_db),
):
    return service.detail(db, tenant_id=context.tenant.id, template_id=template_id)


@router.patch("/{template_id}")
def update_template(
    template_id: str,
    payload: TemplateUpdate,
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles(*MANAGE_ROLES)),
    db: Session = Depends(get_db),
):
    return service.update(
        db,
        tenant_id=context.tenant.id,
        actor_user_id=context.auth.user.id,
        template_id=template_id,
        name=payload.name,
        fields=payload.fields,
    )


@router.post("/{template_id}/publish", status_code=201)
def publish_template(
    template_id: str,
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles(*MANAGE_ROLES)),
    db: Session = Depends(get_db),
):
    return service.publish(
        db,
        tenant_id=context.tenant.id,
        actor_user_id=context.auth.user.id,
        template_id=template_id,
    )


@router.post("/{template_id}/retire")
def retire_template(
    template_id: str,
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles(*MANAGE_ROLES)),
    db: Session = Depends(get_db),
):
    return service.retire(
        db,
        tenant_id=context.tenant.id,
        actor_user_id=context.auth.user.id,
        template_id=template_id,
    )


@router.post("/{template_id}/copy", status_code=201)
def copy_template(
    template_id: str,
    payload: TemplateCopy | None = None,
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles(*MANAGE_ROLES)),
    db: Session = Depends(get_db),
):
    return service.copy(
        db,
        tenant_id=context.tenant.id,
        actor_user_id=context.auth.user.id,
        template_id=template_id,
        name=payload.name if payload else None,
    )
