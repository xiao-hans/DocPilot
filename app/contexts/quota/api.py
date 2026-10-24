"""配额 API（FR-042）：读看当前值与用量，写只 owner/admin。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.platform.http.dependencies import TenantContext, require_csrf, require_roles
from app.contexts.quota import service
from app.platform.db import get_db

router = APIRouter(prefix="/api/v1/tenants/{tenant_id}", tags=["quota"])
READ_ROLES = ("owner", "admin", "member")
WRITE_ROLES = ("owner", "admin")


class QuotaUpdate(BaseModel):
    max_documents: int
    max_storage_bytes: int


@router.get("/quota")
def get_quota(
    context: TenantContext = Depends(require_roles(*READ_ROLES)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return service.view(db, tenant_id=context.tenant.id)


@router.put("/quota")
def put_quota(
    payload: QuotaUpdate,
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles(*WRITE_ROLES)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """两个值都必须显式给：漏一个会被读成「改成不限」，那不是「没动那一项」。

    PUT 语义要的是整体覆盖；只想改一个轴时也请把另一个当前值一并传来，可从 GET 拿。
    """
    return service.set_quota(
        db,
        tenant_id=context.tenant.id,
        actor_user_id=context.auth.user.id,
        max_documents=payload.max_documents,
        max_storage_bytes=payload.max_storage_bytes,
    )
