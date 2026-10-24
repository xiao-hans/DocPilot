"""成员、邀请与审计 API（DOC-DD-003 §5）。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.platform.http.dependencies import TenantContext, get_auth_context, require_csrf, require_roles
from app.platform.db import get_db
from app.contexts.identity import service as membership

router = APIRouter(prefix="/api/v1/tenants/{tenant_id}", tags=["membership"])
invitations_router = APIRouter(prefix="/api/v1/invitations", tags=["membership"])


def _id_path() -> Path:
    """每个参数必须持有自己的 Path 实例：FastAPI 会把参数名写进共享的 FieldInfo 对象。"""
    return Path(min_length=32, max_length=32)


class InviteRequest(BaseModel):
    email: EmailStr = Field(max_length=320)
    role: str = Field(min_length=1, max_length=32)


class RoleRequest(BaseModel):
    role: str = Field(min_length=1, max_length=32)


class AcceptRequest(BaseModel):
    token: str = Field(min_length=16, max_length=200)


@router.get("/members")
def list_members(
    context: TenantContext = Depends(require_roles("owner", "admin", "member", "reviewer")),
    db: Session = Depends(get_db),
):
    return {"members": membership.list_members(db, context.tenant.id)}


@router.patch("/members/{user_id}")
def change_member_role(
    payload: RoleRequest,
    user_id: str = _id_path(),
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles("owner")),
    db: Session = Depends(get_db),
):
    return membership.change_role(
        db,
        tenant_id=context.tenant.id,
        actor=context.membership,
        target_user_id=user_id,
        role=payload.role,
    )


@router.delete("/members/{user_id}")
def remove_member(
    user_id: str = _id_path(),
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
):
    return membership.remove_member(
        db, tenant_id=context.tenant.id, actor=context.membership, target_user_id=user_id
    )


@router.post("/invitations", status_code=201)
def create_invitation(
    payload: InviteRequest,
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
):
    return membership.invite(
        db,
        tenant=context.tenant,
        actor_user_id=context.auth.user.id,
        email=payload.email,
        role=payload.role,
    )


@router.get("/invitations")
def list_invitations(
    context: TenantContext = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
):
    return {"invitations": membership.list_invitations(db, context.tenant.id)}


@router.delete("/invitations/{invitation_id}")
def revoke_invitation(
    invitation_id: str = _id_path(),
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
):
    membership.revoke_invitation(
        db,
        tenant_id=context.tenant.id,
        actor_user_id=context.auth.user.id,
        invitation_id=invitation_id,
    )
    return {"invitation_id": invitation_id, "revoked": True}


@router.get("/audit-events")
def list_audit_events(
    limit: int = Query(default=50, ge=1, le=100),
    before: datetime | None = Query(default=None),
    context: TenantContext = Depends(require_roles("owner", "admin")),
    db: Session = Depends(get_db),
):
    return {
        "events": membership.list_audit_events(db, context.tenant.id, limit=limit, before=before)
    }


@invitations_router.post("/accept")
def accept_invitation(
    payload: AcceptRequest,
    _csrf=Depends(require_csrf),
    auth=Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    return membership.accept_invitation(db, user=auth.user, token=payload.token)
