"""复核持久化：所有查询显式携带租户（DOC-DDD-001 §7 规则 1）。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.contexts.review.domain import DocumentText, ExtractionResult, ResultRevision
from app.services.text_safety import without_nul

CONFIRMABLE = ("machine", "revised")
REJECTABLE = ("machine", "revised", "confirmed")
EDITABLE = ("machine", "revised", "rejected")


def get_result(db: Session, tenant_id: str, result_id: str) -> ExtractionResult | None:
    return db.scalar(
        select(ExtractionResult).where(
            ExtractionResult.id == result_id, ExtractionResult.tenant_id == tenant_id
        )
    )


def by_job_and_version(
    db: Session, tenant_id: str, job_id: str, document_version_id: str
) -> ExtractionResult | None:
    return db.scalar(
        select(ExtractionResult).where(
            ExtractionResult.job_id == job_id,
            ExtractionResult.document_version_id == document_version_id,
            ExtractionResult.tenant_id == tenant_id,
        )
    )


def list_results(
    db: Session,
    tenant_id: str,
    *,
    states: Sequence[str] = (),
    template_version_id: str | None = None,
    job_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ExtractionResult]:
    stmt = select(ExtractionResult).where(ExtractionResult.tenant_id == tenant_id)
    if states:
        stmt = stmt.where(ExtractionResult.state.in_(list(states)))
    if template_version_id:
        stmt = stmt.where(ExtractionResult.template_version_id == template_version_id)
    if job_id:
        stmt = stmt.where(ExtractionResult.job_id == job_id)
    stmt = stmt.order_by(
        ExtractionResult.updated_at.desc(), ExtractionResult.id.desc()
    ).limit(max(1, min(limit, 100))).offset(max(0, offset))
    return list(db.scalars(stmt).all())


def list_revisions(
    db: Session, tenant_id: str, result_id: str, *, limit: int = 50
) -> list[ResultRevision]:
    return list(
        db.scalars(
            select(ResultRevision)
            .where(
                ResultRevision.result_id == result_id,
                ResultRevision.tenant_id == tenant_id,
            )
            .order_by(ResultRevision.revision_no.desc())
            .limit(max(1, min(limit, 100)))
        ).all()
    )


def get_text(db: Session, tenant_id: str, document_version_id: str) -> DocumentText | None:
    return db.scalar(
        select(DocumentText).where(
            DocumentText.document_version_id == document_version_id,
            DocumentText.tenant_id == tenant_id,
        )
    )


def upsert_text(
    db: Session,
    *,
    tenant_id: str,
    document_version_id: str,
    text: str,
    extracted_at: datetime,
) -> DocumentText:
    # 解析器/OCR 原文是 NUL 的最大来源，清洗必须早于 char_count：
    # 记录下来的字符数要与实际存储的内容一致。
    text = without_nul(text)
    existing = get_text(db, tenant_id, document_version_id)
    if existing is not None:
        existing.text = text
        existing.char_count = len(text)
        existing.extracted_at = extracted_at
        return existing
    row = DocumentText(
        id=_new_id(),
        tenant_id=tenant_id,
        document_version_id=document_version_id,
        text=text,
        char_count=len(text),
        extracted_at=extracted_at,
    )
    db.add(row)
    return row


def apply_revision(
    db: Session,
    *,
    tenant_id: str,
    result_id: str,
    expected_revision: int,
    values: dict[str, object],
    now: datetime,
) -> bool:
    """带并发条件的修订写入：只有 revision_no 与状态仍匹配时才前推版本号。

    乐观并发必须在数据库层判定，先读后写会在两个复核员同时保存时覆盖对方结果。
    """
    updated = db.execute(
        update(ExtractionResult)
        .where(
            ExtractionResult.id == result_id,
            ExtractionResult.tenant_id == tenant_id,
            ExtractionResult.revision_no == expected_revision,
            ExtractionResult.state.in_(list(EDITABLE)),
        )
        .values(
            revision_no=ExtractionResult.revision_no + 1,
            values=values,
            state="revised",
            updated_at=now,
        )
    )
    return updated.rowcount == 1


def apply_state_change(
    db: Session,
    *,
    tenant_id: str,
    result_id: str,
    allowed: Sequence[str],
    values: dict[str, object],
) -> bool:
    """状态迁移同样走条件更新：`WHERE state IN (允许的前态)` 保证状态机不被并发绕过。"""
    updated = db.execute(
        update(ExtractionResult)
        .where(
            ExtractionResult.id == result_id,
            ExtractionResult.tenant_id == tenant_id,
            ExtractionResult.state.in_(list(allowed)),
        )
        .values(**values)
    )
    return updated.rowcount == 1


def revision_taken(
    db: Session, *, tenant_id: str, result_id: str, revision_no: int
) -> bool:
    """修订链的唯一约束是 (result_id, revision_no)；用它判定并发下的重复追加。"""
    return (
        db.scalar(
            select(ResultRevision.id).where(
                ResultRevision.result_id == result_id,
                ResultRevision.tenant_id == tenant_id,
                ResultRevision.revision_no == revision_no,
            )
        )
        is not None
    )


def _new_id() -> str:
    import uuid

    return uuid.uuid4().hex
