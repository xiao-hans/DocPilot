"""自动化持久化：每个查询都显式携带租户（DOC-DDD-001 §7 规则 1）。"""

from __future__ import annotations

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.contexts.automation.domain import EventRule, NotificationRequest, RuleRun
from app.models.outbox import DomainEventRecord

RULE_RUN_STATUSES = ("applied", "skipped", "failed")
PAGE_MAX = 100


def get_rule(db: Session, tenant_id: str, rule_id: str) -> EventRule | None:
    return db.scalar(
        select(EventRule).where(EventRule.id == rule_id, EventRule.tenant_id == tenant_id)
    )


def list_rules(
    db: Session, tenant_id: str, *, limit: int, offset: int
) -> tuple[int, list[EventRule]]:
    stmt: Select = select(EventRule).where(EventRule.tenant_id == tenant_id)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = list(
        db.scalars(
            stmt.order_by(EventRule.created_at.desc(), EventRule.id.desc())
            .limit(max(1, min(limit, PAGE_MAX)))
            .offset(max(0, offset))
        ).all()
    )
    return total, rows


def run_stats(db: Session, tenant_id: str, rule_ids: list[str]) -> dict[str, dict[str, int]]:
    stats = {rule_id: {status: 0 for status in RULE_RUN_STATUSES} for rule_id in rule_ids}
    if not rule_ids:
        return stats
    for rule_id, status, count in db.execute(
        select(RuleRun.rule_id, RuleRun.status, func.count())
        .where(RuleRun.tenant_id == tenant_id, RuleRun.rule_id.in_(rule_ids))
        .group_by(RuleRun.rule_id, RuleRun.status)
    ).all():
        bucket = stats.setdefault(rule_id, {status: 0 for status in RULE_RUN_STATUSES})
        if status in bucket:
            bucket[status] = int(count)
    return stats


def list_runs(
    db: Session,
    tenant_id: str,
    *,
    rule_id: str | None,
    status: str | None,
    limit: int,
    offset: int,
) -> tuple[int, list[RuleRun]]:
    stmt: Select = select(RuleRun).where(RuleRun.tenant_id == tenant_id)
    if rule_id:
        stmt = stmt.where(RuleRun.rule_id == rule_id)
    if status:
        stmt = stmt.where(RuleRun.status == status)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = list(
        db.scalars(
            stmt.order_by(RuleRun.created_at.desc(), RuleRun.id.desc())
            .limit(max(1, min(limit, PAGE_MAX)))
            .offset(max(0, offset))
        ).all()
    )
    return total, rows


def list_notifications(
    db: Session, tenant_id: str, *, status: str | None, limit: int, offset: int
) -> tuple[int, list[NotificationRequest]]:
    stmt: Select = select(NotificationRequest).where(NotificationRequest.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(NotificationRequest.status == status)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = list(
        db.scalars(
            stmt.order_by(NotificationRequest.created_at.desc(), NotificationRequest.id.desc())
            .limit(max(1, min(limit, PAGE_MAX)))
            .offset(max(0, offset))
        ).all()
    )
    return total, rows


def pending_events(
    db: Session, tenant_id: str, *, event_type: str | None, limit: int
) -> list[DomainEventRecord]:
    stmt = select(DomainEventRecord).where(
        DomainEventRecord.tenant_id == tenant_id,
        DomainEventRecord.published_at.is_(None),
    )
    if event_type:
        stmt = stmt.where(DomainEventRecord.event_type == event_type)
    return list(
        db.scalars(
            stmt.order_by(DomainEventRecord.occurred_at, DomainEventRecord.id).limit(
                max(1, min(limit, PAGE_MAX))
            )
        ).all()
    )


def active_rules(db: Session, tenant_id: str, *, event_type: str) -> list[EventRule]:
    return list(
        db.scalars(
            select(EventRule)
            .where(
                EventRule.tenant_id == tenant_id,
                EventRule.status == "active",
                EventRule.event_type == event_type,
            )
            .order_by(EventRule.created_at)
        ).all()
    )


def find_run(db: Session, tenant_id: str, delivery_key: str) -> RuleRun | None:
    return db.scalar(
        select(RuleRun).where(
            RuleRun.tenant_id == tenant_id, RuleRun.delivery_key == delivery_key
        )
    )
