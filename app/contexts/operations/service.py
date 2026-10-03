"""运营读模型：用量聚合与就绪判定，全部从业务表现算，不建第二真相（DOC-DD-007 §4）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.contexts.automation.domain import EventRule, RuleRun
from app.contexts.review.domain import ExtractionResult, ResultRevision
from app.contexts.search.domain import SearchEntry
from app.errors import AccessError
from app.models.account import Tenant
from app.models.document import Document, DocumentVersion
from app.models.outbox import DomainEventRecord
from app.models.persistent_job import PersistentJob

MAX_WINDOW_DAYS = 365
DEFAULT_WINDOW_DAYS = 30
PAGE_MAX = 100
JOB_STATUSES = ("queued", "processing", "succeeded", "partial", "failed")
RESULT_STATES = ("machine", "revised", "confirmed", "rejected")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_window(days: int) -> int:
    if not 1 <= days <= MAX_WINDOW_DAYS:
        raise AccessError(422, f"days 需为 1-{MAX_WINDOW_DAYS} 之间的整数")
    return days


def _grouped(db: Session, value, where: list) -> dict[Any, int]:
    # 显式排序：SQLite 实际按插入顺序返回，PostgreSQL 不保证任何顺序，而这里的 dict
    # 会原样变成响应里的 JSON 键序。`IS NULL` 排在前面是为了让两种方言对 NULL 键的
    # 默认差异（SQLite ASC 置前、PG 置后）不影响最终顺序。
    rows = db.execute(
        select(value, func.count())
        .where(*where)
        .group_by(value)
        .order_by(value.is_(None), value)
    ).all()
    return {row[0]: int(row[1]) for row in rows}


def _by_tenant(db: Session, value, key, where: list) -> dict[str, dict[Any, int]]:
    rows = db.execute(
        select(key, value, func.count())
        .where(*where)
        .group_by(key, value)
        .order_by(key, value.is_(None), value)
    ).all()
    grouped: dict[str, dict[Any, int]] = {}
    for tenant_id, bucket, count in rows:
        grouped.setdefault(tenant_id, {})[bucket] = int(count)
    return grouped


def tenant_usage(db: Session, *, tenant_id: str, days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    window = _clean_window(days)
    since = _now() - timedelta(days=window)

    documents = int(db.scalar(select(func.count(Document.id)).where(Document.tenant_id == tenant_id)) or 0)
    versions = int(
        db.scalar(select(func.count(DocumentVersion.id)).where(DocumentVersion.tenant_id == tenant_id)) or 0
    )
    storage_bytes = int(
        db.scalar(select(func.coalesce(func.sum(DocumentVersion.size_bytes), 0)).where(
            DocumentVersion.tenant_id == tenant_id
        ))
        or 0
    )
    jobs = _grouped(db, PersistentJob.status, [PersistentJob.tenant_id == tenant_id])
    results = _grouped(db, ExtractionResult.state, [ExtractionResult.tenant_id == tenant_id])
    pending_events = int(
        db.scalar(
            select(func.count(DomainEventRecord.id)).where(
                DomainEventRecord.tenant_id == tenant_id,
                DomainEventRecord.published_at.is_(None),
            )
        )
        or 0
    )
    return {
        "tenant_id": tenant_id,
        "window_days": window,
        "documents": {
            "total": documents,
            "recent_created": int(
                db.scalar(
                    select(func.count(Document.id)).where(
                        Document.tenant_id == tenant_id, Document.created_at >= since
                    )
                )
                or 0
            ),
            "retention": _grouped(db, Document.retention_status, [Document.tenant_id == tenant_id]),
        },
        "file_versions": {"total": versions, "storage_bytes": storage_bytes},
        "jobs": {
            "total": sum(jobs.values()),
            "by_status": {status: jobs.get(status, 0) for status in JOB_STATUSES},
            "recent_failed": int(
                db.scalar(
                    select(func.count(PersistentJob.id)).where(
                        PersistentJob.tenant_id == tenant_id,
                        PersistentJob.status.in_(["failed", "partial"]),
                        PersistentJob.finished_at >= since,
                    )
                )
                or 0
            ),
            "attempts": int(
                db.scalar(select(func.coalesce(func.sum(PersistentJob.attempt_count), 0)).where(
                    PersistentJob.tenant_id == tenant_id
                ))
                or 0
            ),
        },
        "results": {
            "total": sum(results.values()),
            "by_state": {state: results.get(state, 0) for state in RESULT_STATES},
            "human_revisions": int(
                db.scalar(
                    select(func.count(ResultRevision.id)).where(
                        ResultRevision.tenant_id == tenant_id, ResultRevision.source == "human"
                    )
                )
                or 0
            ),
        },
        "automation": {
            "rules_active": int(
                db.scalar(
                    select(func.count(EventRule.id)).where(
                        EventRule.tenant_id == tenant_id, EventRule.status == "active"
                    )
                )
                or 0
            ),
            "runs": _grouped(db, RuleRun.status, [RuleRun.tenant_id == tenant_id]),
            "pending_events": pending_events,
        },
        "search_entries": int(
            db.scalar(select(func.count(SearchEntry.id)).where(SearchEntry.tenant_id == tenant_id)) or 0
        ),
    }


def platform_usage(
    db: Session, *, days: int = DEFAULT_WINDOW_DAYS, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    """跨租户聚合：只输出计数与租户标识，客户内容一概不出现在这里。"""
    window = _clean_window(days)
    since = _now() - timedelta(days=window)
    page = max(1, min(limit, PAGE_MAX))

    tenants = list(db.scalars(select(Tenant).order_by(Tenant.id).limit(page).offset(max(0, offset))).all())
    total_tenants = int(db.scalar(select(func.count(Tenant.id))) or 0)
    tenant_ids = [tenant.id for tenant in tenants]
    if not tenant_ids:
        return {"window_days": window, "total_tenants": total_tenants, "tenants": []}

    document_counts = dict(
        db.execute(
            select(Document.tenant_id, func.count(Document.id))
            .where(Document.tenant_id.in_(tenant_ids))
            .group_by(Document.tenant_id)
        ).all()
    )
    byte_sums = dict(
        db.execute(
            select(DocumentVersion.tenant_id, func.coalesce(func.sum(DocumentVersion.size_bytes), 0))
            .where(DocumentVersion.tenant_id.in_(tenant_ids))
            .group_by(DocumentVersion.tenant_id)
        ).all()
    )
    jobs = _by_tenant(db, PersistentJob.status, PersistentJob.tenant_id, [PersistentJob.tenant_id.in_(tenant_ids)])
    recent_failed = dict(
        db.execute(
            select(PersistentJob.tenant_id, func.count(PersistentJob.id))
            .where(
                PersistentJob.tenant_id.in_(tenant_ids),
                PersistentJob.status.in_(["failed", "partial"]),
                PersistentJob.finished_at >= since,
            )
            .group_by(PersistentJob.tenant_id)
        ).all()
    )
    pending = dict(
        db.execute(
            select(DomainEventRecord.tenant_id, func.count(DomainEventRecord.id))
            .where(
                DomainEventRecord.tenant_id.in_(tenant_ids),
                DomainEventRecord.published_at.is_(None),
            )
            .group_by(DomainEventRecord.tenant_id)
        ).all()
    )
    failed_runs = dict(
        db.execute(
            select(RuleRun.tenant_id, func.count(RuleRun.id))
            .where(RuleRun.tenant_id.in_(tenant_ids), RuleRun.status == "failed")
            .group_by(RuleRun.tenant_id)
        ).all()
    )

    rows = [
        {
            "tenant_id": tenant.id,
            "documents": document_counts.get(tenant.id, 0),
            "storage_bytes": int(byte_sums.get(tenant.id, 0) or 0),
            "jobs": jobs.get(tenant.id, {}),
            "jobs_failed_in_window": int(recent_failed.get(tenant.id, 0)),
            "pending_events": int(pending.get(tenant.id, 0)),
            "rule_runs_failed": int(failed_runs.get(tenant.id, 0)),
        }
        for tenant in tenants
    ]
    rows.sort(key=lambda row: (-row["documents"], -row["storage_bytes"], row["tenant_id"]))
    return {
        "window_days": window,
        "total_tenants": total_tenants,
        "limit": page,
        "offset": max(0, offset),
        "totals": {
            "documents": sum(row["documents"] for row in rows),
            "storage_bytes": sum(row["storage_bytes"] for row in rows),
            "pending_events": sum(row["pending_events"] for row in rows),
        },
        "tenants": rows,
    }


def readiness(session_factory, head_revision: str) -> tuple[int, dict[str, Any]]:
    """就绪判定：数据库不可用或迁移未对齐都返回 503，但绝不抛异常。"""
    database_ok = False
    current: str | None = None
    error: str | None = None
    try:
        with session_factory() as db:
            db.execute(select(1))
            database_ok = True
            current = db.scalar(select(func.max(_alembic_version_column())))
    except SQLAlchemyError as exc:
        error = type(exc).__name__
    in_sync = database_ok and current == head_revision
    status = "ready" if in_sync else "unavailable"
    body = {
        "status": status,
        "database": database_ok,
        "migration": {"current": current, "head": head_revision, "in_sync": in_sync},
    }
    if error is not None:
        body["error"] = error
    return (200 if status == "ready" else 503), body


def _alembic_version_column():
    from sqlalchemy import column, table

    return table("alembic_version", column("version_num")).c.version_num
