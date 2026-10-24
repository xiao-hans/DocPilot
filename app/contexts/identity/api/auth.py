"""账号引导、登录、登出与当前用户 API。"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.platform.config import (
    AUTH_COOKIE_NAME,
    AUTH_COOKIE_SECURE,
    AUTH_SESSION_DAYS,
    BOOTSTRAP_TOKEN,
)
from app.platform.db import get_db
from app.models.account import AuthSession, BootstrapState, Membership, Tenant, User
from app.platform.http.dependencies import AuthContext, get_auth_context, require_csrf
from app.platform.security import hash_password, new_token, token_hash, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])


class BootstrapRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    tenant_name: str = Field(min_length=1, max_length=120)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_session(db: Session, user: User, response: Response) -> str:
    raw_token = new_token()
    csrf_token = new_token()
    expires_at = _now() + timedelta(days=AUTH_SESSION_DAYS)
    db.add(
        AuthSession(
            id=uuid.uuid4().hex,
            user_id=user.id,
            token_hash=token_hash(raw_token),
            csrf_hash=token_hash(csrf_token),
            expires_at=expires_at,
            created_at=_now(),
        )
    )
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=raw_token,
        max_age=AUTH_SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=AUTH_COOKIE_SECURE,
        samesite="strict",
        path="/api",
    )
    return csrf_token


def _require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin:
        source = urlsplit(origin)
        host = request.headers.get("host", "")
        if source.netloc.casefold() != host.casefold() or source.scheme != request.url.scheme:
            raise HTTPException(status_code=403, detail="请求来源无效")


@router.post("/bootstrap", status_code=201)
def bootstrap(
    payload: BootstrapRequest,
    request: Request,
    response: Response,
    x_bootstrap_token: str | None = Header(default=None, alias="X-Bootstrap-Token"),
    db: Session = Depends(get_db),
):
    """使用部署密钥创建首个客户管理员和工作空间；仅可成功一次。"""
    if not BOOTSTRAP_TOKEN:
        raise HTTPException(status_code=404, detail="初始化入口未启用")
    if not x_bootstrap_token or not secrets.compare_digest(x_bootstrap_token, BOOTSTRAP_TOKEN):
        raise HTTPException(status_code=403, detail="初始化凭据无效")
    _require_same_origin(request)
    if db.get(BootstrapState, "initial-admin") is not None or db.scalar(
        select(func.count()).select_from(User)
    ):
        raise HTTPException(status_code=409, detail="系统已初始化")

    user = User(
        id=uuid.uuid4().hex,
        email=str(payload.email).strip().lower(),
        password_hash=hash_password(payload.password),
        is_active=True,
        is_platform_admin=True,
        created_at=_now(),
    )
    tenant = Tenant(
        id=uuid.uuid4().hex,
        name=payload.tenant_name.strip(),
        status="active",
        created_at=_now(),
    )
    db.add_all(
        [
            user,
            tenant,
            Membership(
                id=uuid.uuid4().hex,
                tenant_id=tenant.id,
                user_id=user.id,
                role="owner",
                status="active",
                created_at=_now(),
            ),
            BootstrapState(key="initial-admin", initialized_at=_now()),
        ]
    )
    try:
        db.flush()
        csrf_token = _new_session(db, user, response)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        response.delete_cookie(AUTH_COOKIE_NAME, path="/api")
        raise HTTPException(status_code=409, detail="系统已初始化或账号已存在") from exc
    return {"user_id": user.id, "tenant_id": tenant.id, "csrf_token": csrf_token}


@router.post("/login")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    _require_same_origin(request)
    email = str(payload.email).strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="邮箱或密码错误")
    csrf_token = _new_session(db, user, response)
    db.commit()
    return {"user_id": user.id, "csrf_token": csrf_token}


@router.get("/csrf")
def refresh_csrf(auth: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)):
    csrf_token = new_token()
    auth.session.csrf_hash = token_hash(csrf_token)
    db.commit()
    return {"csrf_token": csrf_token}


@router.post("/logout")
def logout(
    response: Response,
    auth: AuthContext = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    auth.session.revoked_at = _now()
    db.commit()
    response.delete_cookie(
        AUTH_COOKIE_NAME,
        path="/api",
        secure=AUTH_COOKIE_SECURE,
        httponly=True,
        samesite="strict",
    )
    return {"logged_out": True}


@router.get("/me")
def me(auth: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)):
    memberships = db.execute(
        select(Tenant, Membership)
        .join(Membership, Membership.tenant_id == Tenant.id)
        .where(Membership.user_id == auth.user.id, Membership.status == "active")
        .order_by(Tenant.name)
    ).all()
    return {
        "user": {"id": auth.user.id, "email": auth.user.email, "is_platform_admin": auth.user.is_platform_admin},
        "tenants": [
            {"id": tenant.id, "name": tenant.name, "role": membership.role}
            for tenant, membership in memberships
        ],
    }
