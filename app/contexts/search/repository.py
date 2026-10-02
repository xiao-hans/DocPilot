"""检索持久化：查询与索引重建都显式携带租户（DOC-DDD-001 §7 规则 1）。"""

from __future__ import annotations

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.contexts.search.domain import SearchEntry
from app.models.document import Document

PAGE_MAX = 100


def entries_for_version(
    db: Session, tenant_id: str, document_version_id: str
) -> list[SearchEntry]:
    return list(
        db.scalars(
            select(SearchEntry).where(
                SearchEntry.document_version_id == document_version_id,
                SearchEntry.tenant_id == tenant_id,
            )
        ).all()
    )


def find_entry(
    db: Session, tenant_id: str, document_version_id: str, kind: str, field_name: str
) -> SearchEntry | None:
    return db.scalar(
        select(SearchEntry).where(
            SearchEntry.tenant_id == tenant_id,
            SearchEntry.document_version_id == document_version_id,
            SearchEntry.kind == kind,
            SearchEntry.field_name == field_name,
        )
    )


def search_entries(
    db: Session,
    *,
    tenant_id: str,
    pattern: str,
    kind: str | None,
    doc_type: str | None,
    limit: int,
    offset: int,
) -> tuple[int, list[tuple[SearchEntry, str | None]]]:
    stmt: Select = (
        select(SearchEntry, Document.doc_type)
        .join(Document, Document.id == SearchEntry.document_id)
        .where(
            SearchEntry.tenant_id == tenant_id,
            func.lower(SearchEntry.content).like(pattern, escape="\\"),
        )
    )
    if kind:
        stmt = stmt.where(SearchEntry.kind == kind)
    if doc_type:
        stmt = stmt.where(Document.doc_type == doc_type)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = db.execute(
        stmt.order_by(SearchEntry.document_id, SearchEntry.kind, SearchEntry.field_name)
        .limit(max(1, min(limit, PAGE_MAX)))
        .offset(max(0, offset))
    ).all()
    return total, [(row[0], row[1]) for row in rows]
