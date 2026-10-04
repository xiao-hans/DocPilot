"""认证和租户授权依赖。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import secrets

from fastapi import Depends, Header, HTTPException, Path, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import AUTH_COOKIE_NAME
from app.db import get_db
from app.models.account import AuthSession, Membership, Tenant, User
from app.security import token_hash


@dataclass(frozen=True)
class AuthContext:
    user: User
    session: AuthSession


@dataclass(frozen=True)
class TenantContext:
    auth: AuthContext
    tenant: Tenant
    membership: Membership


def get_auth_context(
    request: Request, db: Session = Depends(get_db)
) -> AuthContext:
    raw_token = request.cookies.get(AUTH_COOKIE_NAME)
    if not raw_token:
        raise HTTPException(status_code=401, detail="请先登录")

    auth_session = db.scalar(
        select(AuthSession).where(AuthSession.token_hash == token_hash(raw_token))
    )
    now = datetime.now(timezone.utc)
    expires_at = auth_session.expires_at if auth_session is not None else now
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if (
        auth_session is None
        or auth_session.revoked_at is not None
        or expires_at <= now
        or not auth_session.user.is_active
    ):
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
    return AuthContext(user=auth_session.user, session=auth_session)


def require_csrf(
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    auth: AuthContext = Depends(get_auth_context),
) -> AuthContext:
    if not x_csrf_token or not secrets.compare_digest(
        token_hash(x_csrf_token), auth.session.csrf_hash
    ):
        raise HTTPException(status_code=403, detail="CSRF 校验失败")
    return auth


def get_tenant_context(
    tenant_id: str = Path(min_length=32, max_length=32),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> TenantContext:
    """路径中的租户必须是当前用户 active 成员关系下的 active 租户。"""
    row = db.execute(
        select(Tenant, Membership)
        .join(Membership, Membership.tenant_id == Tenant.id)
        .where(
            Tenant.id == tenant_id,
            Membership.user_id == auth.user.id,
            Membership.status == "active",
            Tenant.status == "active",
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=403, detail="没有此工作空间的访问权限")
    tenant, membership = row
    return TenantContext(auth=auth, tenant=tenant, membership=membership)


def require_roles(*roles: str):
    """DOC-DD-003 §3 角色矩阵判定入口：矩阵外的动作默认拒绝。"""
    allowed = frozenset(roles)

    def _check(context: TenantContext = Depends(get_tenant_context)) -> TenantContext:
        if context.membership.role not in allowed:
            raise HTTPException(status_code=403, detail="当前角色没有此操作权限")
        return context

    return _check


def require_platform_admin(
    auth: AuthContext = Depends(get_auth_context),
) -> AuthContext:
    """平台身份只用于运营视图：它不等于任何租户的数据访问权（docs/03 §5）。"""
    if not auth.user.is_platform_admin:
        raise HTTPException(status_code=403, detail="需要平台管理员身份")
    return auth
