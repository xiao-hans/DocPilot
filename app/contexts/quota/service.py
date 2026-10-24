"""配额读写与上传前判定（FR-042，`docs/21` 之外的第二处业主口径落地：默认不限、值由管理员设）。

判定发生在**任何对象写入与任何行落库之前**：被拒的请求不留残迹，也就不需要 G3 那套按前缀补偿。
上限是含等号的（`已用 + 本次 <= 上限`）。并发下可能小幅超出——两条请求各自看到旧计数；这
是护栏不是不变量，数据库层没有"求和不得超过 n"这种约束可以声明，说清楚比假装它严格更好。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.contexts.quota.domain import UNLIMITED, TenantQuota
from app.platform.errors import AccessError
from app.models.document import Document, DocumentVersion
from app.shared import audit
from app.shared.timestamps import iso_utc

# 上限本身也要有上限：没有它，一个手滑的 10^18 会一路乘成负数（BigInteger 也是有限位宽）。
MAX_LIMIT = 10**14


def _now() -> datetime:
    return datetime.now(timezone.utc)


def usage(db: Session, *, tenant_id: str) -> dict[str, int]:
    """当前用量。两个数都从业务表现算，配额侧不存第二真相。"""
    documents = db.scalar(
        select(func.count(Document.id)).where(Document.tenant_id == tenant_id)
    ) or 0
    storage_bytes = db.scalar(
        select(func.coalesce(func.sum(DocumentVersion.size_bytes), 0)).where(
            DocumentVersion.tenant_id == tenant_id
        )
    ) or 0
    return {"documents": int(documents), "storage_bytes": int(storage_bytes)}


def _clamp(value: int) -> int:
    cleaned = int(value)
    if cleaned < 0:
        raise AccessError(422, "配额不能为负数；不限请填 0")
    if cleaned > MAX_LIMIT:
        raise AccessError(422, f"配额上限不能超过 {MAX_LIMIT}")
    return cleaned


def get_row(db: Session, tenant_id: str) -> TenantQuota | None:
    return db.get(TenantQuota, tenant_id)


def view(db: Session, *, tenant_id: str) -> dict[str, Any]:
    row = get_row(db, tenant_id)
    limits = {
        "max_documents": row.max_documents if row else UNLIMITED,
        "max_storage_bytes": row.max_storage_bytes if row else UNLIMITED,
    }
    current = usage(db, tenant_id=tenant_id)
    return {
        "limits": limits,
        "usage": current,
        # 每一项都给出"还剩多少"，不限时为 None：界面不需要自己再判一次 0 的语义。
        "remaining": {
            key: (None if value == UNLIMITED else max(value - current[key.replace("max_", "")], 0))
            for key, value in limits.items()
        },
        "unlimited": row is None
        or (row.max_documents == UNLIMITED and row.max_storage_bytes == UNLIMITED),
        "updated_at": iso_utc(row.updated_at) if row else None,
    }


def set_quota(
    db: Session,
    *,
    tenant_id: str,
    actor_user_id: str | None,
    max_documents: int,
    max_storage_bytes: int,
) -> dict[str, Any]:
    documents = _clamp(max_documents)
    storage_bytes = _clamp(max_storage_bytes)
    now = _now()
    row = get_row(db, tenant_id)
    if row is None:
        row = TenantQuota(
            tenant_id=tenant_id,
            max_documents=documents,
            max_storage_bytes=storage_bytes,
            set_by=actor_user_id,
            updated_at=now,
        )
        db.add(row)
    else:
        row.max_documents = documents
        row.max_storage_bytes = storage_bytes
        row.set_by = actor_user_id
        row.updated_at = now
    audit.record(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="tenant.quota_updated",
        object_type="tenant",
        object_id=tenant_id,
        metadata={"max_documents": documents, "max_storage_bytes": storage_bytes},
    )
    db.commit()
    return view(db, tenant_id=tenant_id)


def enforce_upload(db: Session, *, tenant_id: str, documents: int, total_bytes: int) -> None:
    """上传落任何东西之前调用；被拒时抛 409，调用方不需要做任何回滚。"""
    row = get_row(db, tenant_id)
    if row is None:
        return
    if row.max_documents != UNLIMITED or row.max_storage_bytes != UNLIMITED:
        current = usage(db, tenant_id=tenant_id)
        if row.max_documents != UNLIMITED:
            projected = current["documents"] + documents
            if projected > row.max_documents:
                raise AccessError(
                    409,
                    "配额已满（quota_documents）："
                    f"上限 {row.max_documents}，已用 {current['documents']}，本次需要 {documents}",
                )
        if row.max_storage_bytes != UNLIMITED:
            projected = current["storage_bytes"] + total_bytes
            if projected > row.max_storage_bytes:
                raise AccessError(
                    409,
                    "配额已满（quota_storage）："
                    f"上限 {row.max_storage_bytes} 字节，已用 {current['storage_bytes']}，"
                    f"本次需要 {total_bytes}",
                )
