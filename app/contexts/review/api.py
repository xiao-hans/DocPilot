"""复核 API：结果队列、修订链、原文预览与确认/退回（DOC-DD-005 §5）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.dependencies import TenantContext, require_csrf, require_roles
from app.contexts.review import service
from app.db import get_db

router = APIRouter(prefix="/api/v1/tenants/{tenant_id}", tags=["review"])
ALL_ROLES = ("owner", "admin", "member", "reviewer")


class ReviseRequest(BaseModel):
    values: dict[str, Any]
    expected_revision: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=500)


class RejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@router.get("/results")
def list_results(
    state: str | None = None,
    template_version_id: str | None = None,
    job_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    context: TenantContext = Depends(require_roles(*ALL_ROLES)),
    db: Session = Depends(get_db),
):
    return {
        "results": service.list_results(
            db,
            tenant_id=context.tenant.id,
            state=state,
            template_version_id=template_version_id,
            job_id=job_id,
            limit=limit,
            offset=offset,
        )
    }


@router.get("/results/{result_id}")
def get_result(
    result_id: str,
    context: TenantContext = Depends(require_roles(*ALL_ROLES)),
    db: Session = Depends(get_db),
):
    return service.detail(db, tenant_id=context.tenant.id, result_id=result_id)


@router.get("/results/{result_id}/revisions")
def get_revisions(
    result_id: str,
    context: TenantContext = Depends(require_roles(*ALL_ROLES)),
    db: Session = Depends(get_db),
):
    return {"revisions": service.revisions(db, tenant_id=context.tenant.id, result_id=result_id)}


@router.get("/document-versions/{version_id}/text")
def get_version_text(
    version_id: str,
    context: TenantContext = Depends(require_roles(*ALL_ROLES)),
    db: Session = Depends(get_db),
):
    return service.preview_text(
        db, tenant_id=context.tenant.id, document_version_id=version_id
    )


@router.patch("/results/{result_id}")
def revise_result(
    result_id: str,
    payload: ReviseRequest,
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles(*service.REVIEW_ROLES)),
    db: Session = Depends(get_db),
):
    return service.revise(
        db,
        tenant_id=context.tenant.id,
        actor_user_id=context.auth.user.id,
        result_id=result_id,
        values=payload.values,
        expected_revision=payload.expected_revision,
        reason=payload.reason,
    )


@router.post("/results/{result_id}/confirm")
def confirm_result(
    result_id: str,
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles(*service.REVIEW_ROLES)),
    db: Session = Depends(get_db),
):
    return service.confirm(
        db, tenant_id=context.tenant.id, actor_user_id=context.auth.user.id, result_id=result_id
    )


@router.post("/results/{result_id}/reject")
def reject_result(
    result_id: str,
    payload: RejectRequest,
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles(*service.REVIEW_ROLES)),
    db: Session = Depends(get_db),
):
    return service.reject(
        db,
        tenant_id=context.tenant.id,
        actor_user_id=context.auth.user.id,
        result_id=result_id,
        reason=payload.reason,
    )
