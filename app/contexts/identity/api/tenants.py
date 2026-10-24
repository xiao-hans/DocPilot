"""租户工作空间 API。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.platform.http.dependencies import AuthContext, get_auth_context, require_csrf
from app.platform.db import get_db
from app.models.account import Membership, Tenant

router = APIRouter(prefix="/api/v1/tenants", tags=["tenants"])


class CreateTenantRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@router.get("")
def list_tenants(auth: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)):
    rows = db.execute(
        select(Tenant, Membership)
        .join(Membership, Membership.tenant_id == Tenant.id)
        .where(
            Membership.user_id == auth.user.id,
            Membership.status == "active",
            Tenant.status == "active",
        )
        .order_by(Tenant.name)
    ).all()
    return {
        "tenants": [
            {"id": tenant.id, "name": tenant.name, "role": membership.role}
            for tenant, membership in rows
        ]
    }


@router.post("", status_code=201)
def create_tenant(
    payload: CreateTenantRequest,
    auth: AuthContext = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="工作空间名称不能为空")
    tenant = Tenant(
        id=uuid.uuid4().hex,
        name=name,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    membership = Membership(
        id=uuid.uuid4().hex,
        tenant_id=tenant.id,
        user_id=auth.user.id,
        role="owner",
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db.add_all([tenant, membership])
    db.commit()
    return {"id": tenant.id, "name": tenant.name, "role": membership.role}
