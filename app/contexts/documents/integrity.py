"""内容签名核对（FR-010 的"内容签名"那一半，DOC-DD §5 上传校验的读侧延伸）。

上传时已经记了 `sha256(content)` 与字节数，缺的是**读回来再算一次**：对象存储换了字节、
键被覆写、备份回滚到旧副本，这些在数据库里都看不出来——记录仍然"完好"，内容已经不是当初
那份。这个模块只做一件事：把存储里的真字节重新哈希并与登记值比对。

刻意保持只读：不改状态、不删对象、不标记 quarantined。"发现不一致之后怎么办"属于保留与
处置政策（FR-013），那是业主决定，不在这里替它做。与阶段 G3 的孤儿审计同一个口径——工具
负责发现并如实报告。
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.platform.errors import AccessError
from app.models.document import Document, DocumentVersion
from app.adapters.storage.object_storage import get_object_storage

STATUS_MATCHED = "matched"
STATUS_MISMATCHED = "mismatched"
STATUS_MISSING = "missing"
STATUS_UNREADABLE = "unreadable"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_version(storage, version: DocumentVersion) -> dict[str, Any]:
    row: dict[str, Any] = {
        "version_id": version.id,
        "document_id": version.document_id,
        "revision": version.revision,
        "filename": version.original_filename,
        "recorded_checksum": version.checksum,
        "recorded_size": version.size_bytes,
    }
    try:
        data = storage.read_bytes(version.storage_key)
    except FileNotFoundError:
        # 对象不在了：这是"缺"，不是"内容不符"，两者对运维是完全不同的动作。
        return {**row, "status": STATUS_MISSING, "actual_checksum": None, "actual_size": None}
    except Exception as exc:
        return {
            **row,
            "status": STATUS_UNREADABLE,
            "actual_checksum": None,
            "actual_size": None,
            "error": type(exc).__name__,
        }
    actual = _digest(data)
    size = len(data)
    # 判定只看摘要：等长改写（备份回滚、键被覆写）正是最常见的坏法，字节数拦不住它。
    # 摘要相等而长度不同意味着哈希碰撞，不值得为它造一个永远不会走到的状态。
    status = STATUS_MATCHED if actual == version.checksum else STATUS_MISMATCHED
    return {
        **row,
        "status": status,
        "actual_checksum": actual,
        "actual_size": size,
        "size_matches": size == version.size_bytes,
    }


def check_document(db: Session, *, tenant_id: str, document_id: str) -> dict[str, Any]:
    document = db.get(Document, document_id)
    if document is None or document.tenant_id != tenant_id:
        raise AccessError(404, "文档不存在")
    versions = db.scalars(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document_id, DocumentVersion.tenant_id == tenant_id)
        .order_by(DocumentVersion.revision)
    ).all()
    if not versions:
        raise AccessError(404, "文档没有可核对的版本记录")
    storage = get_object_storage()
    rows = [check_version(storage, version) for version in versions]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "document_id": document_id,
        "checked": len(rows),
        "clean": all(row["status"] == STATUS_MATCHED for row in rows),
        "counts": counts,
        "items": rows,
    }
