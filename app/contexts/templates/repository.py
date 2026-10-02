"""模板持久化：所有查询必须显式携带租户（DOC-DDD-001 §7 规则 1）。"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.contexts.templates.domain import Template, TemplateVersion


def get_template(db: Session, tenant_id: str, template_id: str) -> Template | None:
    return db.scalar(
        select(Template).where(Template.id == template_id, Template.tenant_id == tenant_id)
    )


def get_version(db: Session, tenant_id: str, version_id: str) -> TemplateVersion | None:
    return db.scalar(
        select(TemplateVersion).where(
            TemplateVersion.id == version_id, TemplateVersion.tenant_id == tenant_id
        )
    )


def list_templates(
    db: Session, tenant_id: str, statuses: Sequence[str] | None = None
) -> list[Template]:
    stmt = select(Template).where(Template.tenant_id == tenant_id)
    if statuses:
        stmt = stmt.where(Template.status.in_(list(statuses)))
    return list(db.scalars(stmt.order_by(Template.name)).all())


def list_versions(db: Session, tenant_id: str, template_id: str) -> list[TemplateVersion]:
    return list(
        db.scalars(
            select(TemplateVersion)
            .where(
                TemplateVersion.template_id == template_id,
                TemplateVersion.tenant_id == tenant_id,
            )
            .order_by(TemplateVersion.revision.desc())
        ).all()
    )


def latest_revision(db: Session, tenant_id: str, template_id: str) -> int:
    return int(
        db.scalar(
            select(func.max(TemplateVersion.revision)).where(
                TemplateVersion.template_id == template_id,
                TemplateVersion.tenant_id == tenant_id,
            )
        )
        or 0
    )
