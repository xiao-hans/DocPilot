"""检索 API：租户内关键词搜索（DOC-DD-006 §5）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.platform.http.dependencies import TenantContext, require_roles
from app.contexts.search import service
from app.platform.db import get_db

router = APIRouter(prefix="/api/v1/tenants/{tenant_id}", tags=["search"])
ALL_ROLES = ("owner", "admin", "member", "reviewer")


@router.get("/search")
def search_documents(
    q: str = Query(min_length=1, max_length=120),
    kind: str | None = None,
    doc_type: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    context: TenantContext = Depends(require_roles(*ALL_ROLES)),
    db: Session = Depends(get_db),
):
    return service.search(
        db,
        tenant_id=context.tenant.id,
        q=q,
        kind=kind,
        doc_type=doc_type,
        limit=limit,
        offset=offset,
    )
