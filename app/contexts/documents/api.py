"""文档库 API：列表筛选与元数据维护（DOC-DD-005 §5）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.platform.http.dependencies import TenantContext, require_csrf, require_roles
from app.contexts.documents import integrity, service
from app.shared import audit
from app.platform.db import get_db

router = APIRouter(prefix="/api/v1/tenants/{tenant_id}", tags=["documents"])
ALL_ROLES = ("owner", "admin", "member", "reviewer")


class DocumentMetadataUpdate(BaseModel):
    labels: list[str] | None = None
    doc_type: str | None = Field(default=None, max_length=120)
    retention_status: str | None = Field(default=None, max_length=20)


@router.get("/documents")
def list_documents(
    label: str | None = None,
    doc_type: str | None = None,
    retention_status: str | None = None,
    source: str | None = None,
    q: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    context: TenantContext = Depends(require_roles(*ALL_ROLES)),
    db: Session = Depends(get_db),
):
    return service.list_documents(
        db,
        tenant_id=context.tenant.id,
        label=label,
        doc_type=doc_type,
        retention_status=retention_status,
        source=source,
        q=q,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )


@router.patch("/documents/{document_id}")
def update_document(
    document_id: str,
    payload: DocumentMetadataUpdate,
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles("owner", "admin", "member")),
    db: Session = Depends(get_db),
):
    body: dict[str, Any] = payload.model_dump(exclude_none=True)
    return service.update_metadata(
        db,
        tenant_id=context.tenant.id,
        actor_user_id=context.auth.user.id,
        actor_role=context.membership.role,
        document_id=document_id,
        **body,
    )


@router.post("/documents/{document_id}/integrity/check")
def check_document_integrity(
    document_id: str,
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles("owner", "admin", "member")),
    db: Session = Depends(get_db),
):
    """内容签名核对（FR-010）：把存储里的真字节重新 SHA-256 一次，与登记值比对。

    只读：不改文档、不删对象、不做处置标记——"发现不一致之后怎么办"属于保留与处置政策
    （FR-013），不在这个端点里替业主决定。`_csrf` 是必需的：它会对外发一趟存储读，
    跨站请求不该有这个副作用面。
    """
    result = integrity.check_document(
        db, tenant_id=context.tenant.id, document_id=document_id
    )
    audit.record(
        db,
        tenant_id=context.tenant.id,
        actor_user_id=context.auth.user.id,
        action="document.integrity_checked",
        object_type="document",
        object_id=document_id,
        metadata={
            "checked": result["checked"],
            "clean": result["clean"],
            "counts": result["counts"],
        },
    )
    db.commit()
    return result
