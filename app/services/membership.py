"""成员、邀请与角色变更服务（DOC-DD-003 §5/§6）。

服务层负责业务保护规则与原子性；路由层只负责身份/角色判定和状态码翻译。
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import INVITE_TTL_DAYS
from app.errors import AccessError
from app.models.access import INVITABLE_ROLES, Invitation
from app.models.account import Membership, Tenant, User
from app.security import token_hash
from app.services.timestamps import iso_utc
from app.services import audit

INVALID_INVITATION = "邀请无效或已过期"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def list_members(db: Session, tenant_id: str) -> list[dict[str, Any]]:
    rows = db.execute(
        select(User, Membership)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.tenant_id == tenant_id)
        .order_by(Membership.created_at, User.email)
    ).all()
    return [
        {
            "user_id": user.id,
            "email": user.email,
            "role": membership.role,
            "status": membership.status,
            "joined_at": iso_utc(membership.created_at),
        }
        for user, membership in rows
    ]


def invite(db: Session, *, tenant: Tenant, actor_user_id: str, email: str, role: str) -> dict[str, Any]:
    if role not in INVITABLE_ROLES:
        raise AccessError(422, "邀请角色只能是 reviewer、member 或 admin")
    normalized = normalize_email(email)
    if not normalized or "@" not in normalized:
        raise AccessError(422, "邮箱格式无效")

    token = secrets.token_urlsafe(32)
    expires_at = _now() + timedelta(days=INVITE_TTL_DAYS)
    invitation = Invitation(
        id=uuid.uuid4().hex,
        tenant_id=tenant.id,
        email=normalized,
        role=role,
        token_hash=token_hash(token),
        status="pending",
        invited_by=actor_user_id,
        created_at=_now(),
        expires_at=expires_at,
    )
    db.add(invitation)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise AccessError(409, "该邮箱已有待接受邀请") from exc
    audit.record(
        db,
        tenant_id=tenant.id,
        actor_user_id=actor_user_id,
        action="member.invited",
        object_type="invitation",
        object_id=invitation.id,
        metadata={"role": role, "expires_at": iso_utc(expires_at)},
    )
    db.commit()
    return {
        "invitation_id": invitation.id,
        "email": normalized,
        "role": role,
        "token": token,
        "expires_at": iso_utc(expires_at),
    }


def list_invitations(db: Session, tenant_id: str) -> list[dict[str, Any]]:
    invitations = db.scalars(
        select(Invitation)
        .where(Invitation.tenant_id == tenant_id)
        .order_by(Invitation.created_at.desc())
    ).all()
    # 只读路径不写库：过期仅按展示语义归一化，落库发生在接受与撤销时。
    return [
        {
            "id": item.id,
            "email": item.email,
            "role": item.role,
            "status": _effective_status(item),
            "created_at": iso_utc(item.created_at),
            "expires_at": iso_utc(item.expires_at),
        }
        for item in invitations
    ]


def _is_expired(invitation: Invitation) -> bool:
    expires_at = invitation.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= _now()


def _effective_status(invitation: Invitation) -> str:
    if invitation.status == "pending" and _is_expired(invitation):
        return "expired"
    return invitation.status


def _inviter_role(db: Session, invitation: Invitation) -> str | None:
    """邀请人在该租户内的当前角色；无成员关系时视为 None。"""
    if invitation.invited_by is None:
        return None
    row = db.scalar(
        select(Membership).where(
            Membership.tenant_id == invitation.tenant_id,
            Membership.user_id == invitation.invited_by,
        )
    )
    return row.role if row is not None else None


def _expire_if_stale(db: Session, invitation: Invitation) -> bool:
    """状态改变路径上才落库：pending 且已过有效期即写为 expired。"""
    if invitation.status == "pending" and _is_expired(invitation):
        db.query(Invitation).filter(
            Invitation.id == invitation.id, Invitation.status == "pending"
        ).update({"status": "expired"}, synchronize_session=False)
        invitation.status = "expired"
        return True
    return False


def revoke_invitation(db: Session, *, tenant_id: str, actor_user_id: str, invitation_id: str) -> None:
    invitation = db.get(Invitation, invitation_id)
    if invitation is None or invitation.tenant_id != tenant_id:
        raise AccessError(404, "邀请不存在")
    _expire_if_stale(db, invitation)
    updated = db.query(Invitation).filter(
        Invitation.id == invitation.id, Invitation.status == "pending"
    ).update({"status": "revoked"}, synchronize_session=False)
    if not updated:
        db.commit()
        raise AccessError(409, "邀请已使用或已失效")
    audit.record(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="member.invitation_revoked",
        object_type="invitation",
        object_id=invitation.id,
        metadata={"role": invitation.role},
    )
    db.commit()


def accept_invitation(db: Session, *, user: User, token: str) -> dict[str, Any]:
    invitation = db.scalar(select(Invitation).where(Invitation.token_hash == token_hash(token)))
    if invitation is None:
        raise AccessError(410, INVALID_INVITATION)
    _expire_if_stale(db, invitation)
    if invitation.status != "pending":
        db.commit()
        raise AccessError(410, INVALID_INVITATION)
    if normalize_email(user.email) != invitation.email:
        raise AccessError(403, "登录账号与邀请邮箱不一致")

    existing = db.scalar(
        select(Membership).where(
            Membership.tenant_id == invitation.tenant_id, Membership.user_id == user.id
        )
    )
    if existing is not None and existing.status == "active":
        # 不消耗一次性令牌：拒绝原因与令牌状态无关。
        raise AccessError(409, "你已是该工作空间的成员")

    # 条件更新保证并发下只有一个请求能消耗该邀请。
    claimed = db.query(Invitation).filter(
        Invitation.id == invitation.id, Invitation.status == "pending"
    ).update(
        {"status": "accepted", "accepted_at": _now(), "accepted_by": user.id},
        synchronize_session=False,
    )
    if not claimed:
        db.commit()
        raise AccessError(410, INVALID_INVITATION)

    try:
        if existing is not None:
            # 复活沿用原角色：邀请不得成为 admin 变更既有成员权限的通道。
            if existing.role == "owner" and _inviter_role(db, invitation) != "owner":
                db.rollback()
                raise AccessError(409, "曾为所有者的成员需由所有者重新邀请")
            existing.status = "active"
            granted_role = existing.role
        else:
            granted_role = invitation.role
            db.add(
                Membership(
                    id=uuid.uuid4().hex,
                    tenant_id=invitation.tenant_id,
                    user_id=user.id,
                    role=granted_role,
                    status="active",
                    created_at=_now(),
                )
            )
        audit.record(
            db,
            tenant_id=invitation.tenant_id,
            actor_user_id=user.id,
            action="member.invitation_accepted",
            object_type="invitation",
            object_id=invitation.id,
            metadata={"role": granted_role, "user_id": user.id},
        )
        tenant = db.get(Tenant, invitation.tenant_id)
        result = {
            "tenant_id": invitation.tenant_id,
            "tenant_name": tenant.name if tenant else "",
            "role": granted_role,
        }
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise AccessError(409, "你已是该工作空间的成员") from exc
    return result


def change_role(
    db: Session, *, tenant_id: str, actor: Membership, target_user_id: str, role: str
) -> dict[str, Any]:
    if role not in INVITABLE_ROLES:
        raise AccessError(422, "角色只能是 reviewer、member 或 admin")
    membership = db.scalar(
        select(Membership).where(
            Membership.tenant_id == tenant_id,
            Membership.user_id == target_user_id,
            Membership.status == "active",
        )
    )
    if membership is None:
        raise AccessError(404, "该成员不在此工作空间")
    if target_user_id == actor.user_id:
        raise AccessError(409, "不能变更自己的角色")
    if membership.role == "owner":
        raise AccessError(409, "所有者角色变更需通过所有权转让")
    before = membership.role
    membership.role = role
    audit.record(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor.user_id,
        action="member.role_changed",
        object_type="membership",
        object_id=target_user_id,
        metadata={"from": before, "to": role},
    )
    db.commit()
    return {"user_id": target_user_id, "role": role}


def _active_owner_count(db: Session, tenant_id: str) -> int:
    return len(
        db.scalars(
            select(Membership.id).where(
                Membership.tenant_id == tenant_id,
                Membership.status == "active",
                Membership.role == "owner",
            )
        ).all()
    )


def remove_member(
    db: Session, *, tenant_id: str, actor: Membership, target_user_id: str
) -> dict[str, Any]:
    membership = db.scalar(
        select(Membership).where(
            Membership.tenant_id == tenant_id,
            Membership.user_id == target_user_id,
            Membership.status == "active",
        )
    )
    if membership is None:
        raise AccessError(404, "该成员不在此工作空间")
    if target_user_id == actor.user_id:
        raise AccessError(409, "不能移除自己")
    if actor.role == "admin" and membership.role in {"admin", "owner"}:
        raise AccessError(403, "管理员不能移除同级或所有者")
    if membership.role == "owner" and _active_owner_count(db, tenant_id) <= 1:
        raise AccessError(409, "工作空间必须保留至少一名所有者")

    removed_role = membership.role
    membership.status = "removed"
    audit.record(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor.user_id,
        action="member.removed",
        object_type="membership",
        object_id=target_user_id,
        metadata={"role": removed_role},
    )
    db.commit()
    return {"user_id": target_user_id, "removed": True}


def list_audit_events(
    db: Session, tenant_id: str, *, limit: int = 50, before: datetime | None = None
) -> list[dict[str, Any]]:
    stmt = select(audit.AuditEvent).where(audit.AuditEvent.tenant_id == tenant_id)
    if before is not None:
        if before.tzinfo is None:
            before = before.replace(tzinfo=timezone.utc)
        stmt = stmt.where(audit.AuditEvent.created_at < before)
    stmt = stmt.order_by(audit.AuditEvent.created_at.desc(), audit.AuditEvent.id.desc()).limit(
        max(1, min(limit, 100))
    )
    return [
        {
            "id": event.id,
            "actor_user_id": event.actor_user_id,
            "action": event.action,
            "object_type": event.object_type,
            "object_id": event.object_id,
            # 关联 ID（NFR-005）：历史行为 null，不编造值。
            "request_id": event.request_id,
            "metadata": event.metadata_,
            "created_at": iso_utc(event.created_at),
        }
        for event in db.scalars(stmt).all()
    ]
