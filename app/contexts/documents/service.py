"""文档库：列表筛选与元数据维护（DOC-DD-005 §5/§7）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.contexts.review.domain import ExtractionResult
from app.platform.errors import AccessError
from app.models.document import Document, DocumentLabel, DocumentVersion
from app.shared import audit
from app.shared.timestamps import iso_utc

RETENTION_STATUSES = ("active", "purge_scheduled", "purged")
DOCUMENT_SOURCES = ("upload",)
# 保留期是治理决定，属于租户管理面：普通成员与复核员只能改业务标注。
RETENTION_ROLES = ("owner", "admin")
MAX_LABELS = 10
MAX_LABEL_CHARS = 30
PAGE_MAX = 100


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _clean_labels(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        raise AccessError(422, "labels 必须是字符串数组")
    cleaned: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise AccessError(422, "labels 只能是字符串")
        value = item.strip()
        if not value:
            continue
        if len(value) > MAX_LABEL_CHARS:
            raise AccessError(422, f"单个标签不得超过 {MAX_LABEL_CHARS} 个字符")
        if value not in cleaned:
            cleaned.append(value)
    if len(cleaned) > MAX_LABELS:
        raise AccessError(422, f"标签数量不得超过 {MAX_LABELS} 个")
    return cleaned


def labels_of(db: Session, tenant_id: str, document_ids: list[str]) -> dict[str, list[str]]:
    if not document_ids:
        return {}
    rows = db.execute(
        select(DocumentLabel.document_id, DocumentLabel.label, DocumentLabel.created_at)
        .where(
            DocumentLabel.document_id.in_(document_ids),
            DocumentLabel.tenant_id == tenant_id,
        )
        .order_by(DocumentLabel.created_at, DocumentLabel.label)
    ).all()
    grouped: dict[str, list[str]] = {document_id: [] for document_id in document_ids}
    for document_id, label, _created in rows:
        grouped[document_id].append(label)
    return grouped


def list_documents(
    db: Session,
    *,
    tenant_id: str,
    label: str | None = None,
    doc_type: str | None = None,
    retention_status: str | None = None,
    source: str | None = None,
    q: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """文档库分页列表。

    `q` 本期只做原始文件名精确匹配；已提取字段值的检索随阶段 E 的检索索引交付。
    """
    stmt = select(Document).where(Document.tenant_id == tenant_id)
    if doc_type:
        stmt = stmt.where(Document.doc_type == doc_type)
    if source:
        if source not in DOCUMENT_SOURCES:
            raise AccessError(422, "source 非法")
        stmt = stmt.where(Document.source == source)
    if retention_status:
        if retention_status not in RETENTION_STATUSES:
            raise AccessError(422, "retention_status 非法")
        stmt = stmt.where(Document.retention_status == retention_status)
    if date_from:
        stmt = stmt.where(Document.created_at >= _as_utc(date_from))
    if date_to:
        stmt = stmt.where(Document.created_at <= _as_utc(date_to))
    if label:
        stmt = stmt.where(
            Document.id.in_(
                select(DocumentLabel.document_id).where(
                    DocumentLabel.tenant_id == tenant_id, DocumentLabel.label == label
                )
            )
        )
    if q:
        stmt = stmt.where(
            Document.id.in_(
                select(DocumentVersion.document_id).where(
                    DocumentVersion.tenant_id == tenant_id,
                    DocumentVersion.original_filename == q,
                )
            )
        )

    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    documents = list(
        db.scalars(
            stmt.order_by(Document.created_at.desc(), Document.id.desc())
            .limit(max(1, min(limit, PAGE_MAX)))
            .offset(max(0, offset))
        ).all()
    )
    document_ids = [document.id for document in documents]
    labels = labels_of(db, tenant_id, document_ids)
    # 版本与结果状态各一次批量查询：逐文档查询会在满页时放大成几百条 SQL。
    versions_by_document: dict[str, list[DocumentVersion]] = {}
    if document_ids:
        for version in db.scalars(
            select(DocumentVersion)
            .where(
                DocumentVersion.document_id.in_(document_ids),
                DocumentVersion.tenant_id == tenant_id,
            )
            .order_by(DocumentVersion.revision.desc())
        ).all():
            versions_by_document.setdefault(version.document_id, []).append(version)

    all_version_ids = [
        version.id for versions in versions_by_document.values() for version in versions
    ]
    states_by_version: dict[str, list[str]] = {}
    if all_version_ids:
        for version_id, state in db.execute(
            select(ExtractionResult.document_version_id, ExtractionResult.state).where(
                ExtractionResult.document_version_id.in_(all_version_ids),
                ExtractionResult.tenant_id == tenant_id,
            )
        ).all():
            states_by_version.setdefault(version_id, []).append(state)

    items: list[dict[str, Any]] = []
    for document in documents:
        versions = versions_by_document.get(document.id, [])
        states = [
            state
            for version in versions
            for state in states_by_version.get(version.id, [])
        ]
        items.append(
            {
                "id": document.id,
                "status": document.status,
                "doc_type": document.doc_type,
                "labels": labels.get(document.id, []),
                "source": document.source,
                "retention_status": document.retention_status,
                "created_at": iso_utc(document.created_at),
                "updated_at": iso_utc(document.updated_at),
                "versions": [
                    {
                        "id": version.id,
                        "revision": version.revision,
                        "original_filename": version.original_filename,
                        "media_type": version.media_type,
                        "size_bytes": version.size_bytes,
                        "checksum": version.checksum,
                        "malware_status": version.malware_status,
                        "created_at": iso_utc(version.created_at),
                    }
                    for version in versions
                ],
                "result_states": sorted(set(states)),
            }
        )
    return {"total": total, "items": items}


def add_label(
    db: Session,
    *,
    tenant_id: str,
    document_id: str,
    label: str,
    actor_user_id: str | None,
    rule_id: str,
    now: datetime,
) -> bool:
    """规则动作追加单个标签：已存在则不动，标签位已满则让本次运行失败可查。"""
    value = (label or "").strip()
    if not 1 <= len(value) <= MAX_LABEL_CHARS:
        raise AccessError(422, f"单个标签不得超过 {MAX_LABEL_CHARS} 个字符")
    document = db.scalar(
        select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id)
    )
    if document is None:
        raise AccessError(404, "文档不存在")
    current = labels_of(db, tenant_id, [document_id]).get(document_id, [])
    if value in current:
        return False
    if len(current) >= MAX_LABELS:
        raise AccessError(422, f"标签数量不得超过 {MAX_LABELS} 个")
    db.add(
        DocumentLabel(
            document_id=document.id, tenant_id=tenant_id, label=value, created_at=now
        )
    )
    document.updated_at = now
    audit.record(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="document.metadata_updated",
        object_type="document",
        object_id=document.id,
        metadata={"changed": ["labels"], "rule_id": rule_id},
    )
    return True


def update_metadata(
    db: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    actor_role: str,
    document_id: str,
    labels: Any | None = None,
    doc_type: str | None = None,
    retention_status: str | None = None,
) -> dict[str, Any]:
    document = db.scalar(
        select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id)
    )
    if document is None:
        raise AccessError(404, "文档不存在")

    changed: list[str] = []
    now = datetime.now(timezone.utc)
    if retention_status is not None and actor_role not in RETENTION_ROLES:
        raise AccessError(403, "只有管理员或所有者可以调整保留状态")
    if labels is not None:
        cleaned = _clean_labels(labels)
        db.execute(
            delete(DocumentLabel).where(
                DocumentLabel.document_id == document.id, DocumentLabel.tenant_id == tenant_id
            )
        )
        for index, value in enumerate(cleaned):
            db.add(
                DocumentLabel(
                    document_id=document.id,
                    tenant_id=tenant_id,
                    label=value,
                    created_at=now,
                )
            )
        changed.append("labels")
    if doc_type is not None:
        cleaned_type = doc_type.strip()
        if not 1 <= len(cleaned_type) <= 120:
            raise AccessError(422, "doc_type 长度需为 1-120")
        document.doc_type = cleaned_type
        changed.append("doc_type")
    if retention_status is not None:
        if retention_status not in RETENTION_STATUSES:
            raise AccessError(422, "retention_status 非法")
        document.retention_status = retention_status
        changed.append("retention_status")
    if not changed:
        raise AccessError(422, "没有需要更新的元数据")

    document.updated_at = now
    audit.record(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="document.metadata_updated",
        object_type="document",
        object_id=document.id,
        metadata={"changed": sorted(changed)},
    )
    db.commit()
    return {
        "id": document.id,
        "labels": labels_of(db, tenant_id, [document.id])[document.id],
        "doc_type": document.doc_type,
        "retention_status": document.retention_status,
        "updated_at": iso_utc(document.updated_at),
    }
