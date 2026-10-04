"""运营 API：健康/就绪探针与用量视图（DOC-DD-007 §5）。"""

from __future__ import annotations

from functools import lru_cache

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.dependencies import (
    TenantContext,
    require_platform_admin,
    require_roles,
    AuthContext,
)
from app.config import BASE_DIR
from app.contexts.operations import service
from app.db import SessionLocal, get_db
from app.services import audit

system_router = APIRouter(tags=["system"])
router = APIRouter(prefix="/api/v1/tenants/{tenant_id}", tags=["operations"])
platform_router = APIRouter(prefix="/api/v1/platform", tags=["operations"])


@lru_cache(maxsize=1)
def _head_revision() -> str:
    """脚本目录的 head 在进程生命周期内不变，探针不该每次请求都去扫文件。"""
    config = AlembicConfig()
    config.set_main_option("script_location", str(BASE_DIR / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head() or ""


@system_router.get("/healthz")
def healthz() -> dict[str, str]:
    """进程存活：不碰数据库，避免依赖抖动被读成服务未启动。"""
    return {"status": "ok"}


@system_router.get("/readyz")
def readyz() -> JSONResponse:
    status_code, body = service.readiness(SessionLocal, _head_revision())
    return JSONResponse(status_code=status_code, content=body)


@router.get("/usage")
def tenant_usage_view(
    days: int = Query(default=service.DEFAULT_WINDOW_DAYS, ge=1, le=service.MAX_WINDOW_DAYS),
    context: TenantContext = Depends(require_roles("owner", "admin", "member", "reviewer")),
    db: Session = Depends(get_db),
):
    return service.tenant_usage(db, tenant_id=context.tenant.id, days=days)


@router.get("/usage/failures")
def tenant_failure_details(
    days: int = Query(default=service.DEFAULT_WINDOW_DAYS, ge=1, le=service.MAX_WINDOW_DAYS),
    limit: int = Query(default=50, ge=1, le=service.PAGE_MAX),
    offset: int = Query(default=0, ge=0),
    context: TenantContext = Depends(require_roles("owner", "admin", "member", "reviewer")),
    db: Session = Depends(get_db),
):
    """本租户的失败明细（FR-042 下钻）。

    错误文本只回到自己的租户：这里出现的是该租户自己的任务号与错误原文。跨租户一律不给原文，
    平台侧走 `/api/v1/platform/usage/failures`，那里只有代号与计数。
    """
    return service.tenant_failures(
        db, tenant_id=context.tenant.id, days=days, limit=limit, offset=offset
    )


@platform_router.get("/usage")
def platform_usage_view(
    days: int = Query(default=service.DEFAULT_WINDOW_DAYS, ge=1, le=service.MAX_WINDOW_DAYS),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    payload = service.platform_usage(db, days=days, limit=limit, offset=offset)
    # 审计表按租户非空：平台每看一次，被列出的每个租户都留下一条"平台读过我的聚合"。
    for row in payload["tenants"]:
        audit.record(
            db,
            tenant_id=row["tenant_id"],
            actor_user_id=auth.user.id,
            action="platform.usage_viewed",
            object_type="tenant",
            object_id=row["tenant_id"],
            metadata={
                "scope": "platform",
                "window_days": payload["window_days"],
                "rows": len(payload["tenants"]),
            },
        )
    db.commit()
    return payload


@platform_router.get("/usage/failures")
def platform_failure_details(
    days: int = Query(default=service.DEFAULT_WINDOW_DAYS, ge=1, le=service.MAX_WINDOW_DAYS),
    auth: AuthContext = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """跨租户失败分布：只有代号与计数。

    平台身份不授予任何客户数据读取权（DOC-DD-007 §3），所以这里既不回显错误原文也不给文件名；
    要看原文必须回到租户端点，那里有成员关系与角色矩阵。审计口径与 `/platform/usage` 一致：
    被列出的每个租户各留一条"平台读过我的失败分布"。
    """
    payload = service.platform_failures(db, days=days)
    for tenant_id in payload["tenant_ids"]:
        audit.record(
            db,
            tenant_id=tenant_id,
            actor_user_id=auth.user.id,
            action="platform.usage_failures_viewed",
            object_type="tenant",
            object_id=tenant_id,
            metadata={
                "scope": "platform",
                "window_days": payload["window_days"],
                "codes": len(payload["codes"]),
                "truncated": payload["truncated"],
            },
        )
    db.commit()
    return payload
