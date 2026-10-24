"""模板用例：草稿编辑、发布不可变版本、停用与复制（DOC-DD-004 §6/§7.1）。"""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.contexts.templates import repository
from app.contexts.templates.domain import Template, TemplateVersion
from app.platform.errors import AccessError
from app.models.schema import validate_fields
from app.shared import audit
from app.shared.timestamps import iso_utc
from app.shared import outbox


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validated(fields: Any) -> list[dict[str, Any]]:
    try:
        validated = validate_fields(fields)
    except (TypeError, ValueError) as exc:
        raise AccessError(422, f"字段定义无效: {exc}") from exc
    if not validated:
        raise AccessError(422, "模板至少需要一个字段")
    return [field.model_dump() for field in validated]


def _name(value: str) -> str:
    cleaned = (value or "").strip()
    if not 1 <= len(cleaned) <= 120:
        raise AccessError(422, "模板名称需为 1-120 个字符")
    return cleaned


def serialize(template: Template, latest_revision: int) -> dict[str, Any]:
    return {
        "id": template.id,
        "name": template.name,
        "status": template.status,
        "draft_fields": template.draft_fields,
        "latest_revision": latest_revision,
        "created_at": iso_utc(template.created_at),
        "updated_at": iso_utc(template.updated_at),
    }


def create(
    db: Session, *, tenant_id: str, actor_user_id: str, name: str, fields: Any
) -> dict[str, Any]:
    template = Template(
        id=uuid.uuid4().hex,
        tenant_id=tenant_id,
        name=_name(name),
        status="draft",
        draft_fields=_validated(fields),
        created_by=actor_user_id,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(template)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise AccessError(409, "同名模板已存在") from exc
    audit.record(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="template.created",
        object_type="template",
        object_id=template.id,
        metadata={"name": template.name},
    )
    db.commit()
    return serialize(template, 0)


def list_all(
    db: Session, *, tenant_id: str, statuses: list[str] | None = None
) -> list[dict[str, Any]]:
    return [
        serialize(template, repository.latest_revision(db, tenant_id, template.id))
        for template in repository.list_templates(db, tenant_id, statuses)
    ]


def detail(db: Session, *, tenant_id: str, template_id: str) -> dict[str, Any]:
    template = repository.get_template(db, tenant_id, template_id)
    if template is None:
        raise AccessError(404, "模板不存在")
    versions = repository.list_versions(db, tenant_id, template.id)
    payload = serialize(template, repository.latest_revision(db, tenant_id, template.id))
    payload["versions"] = [
        {
            "id": version.id,
            "revision": version.revision,
            "fields": version.fields,
            "published_at": iso_utc(version.published_at),
        }
        for version in versions
    ]
    return payload


def update(
    db: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    template_id: str,
    name: str | None = None,
    fields: Any | None = None,
) -> dict[str, Any]:
    template = repository.get_template(db, tenant_id, template_id)
    if template is None:
        raise AccessError(404, "模板不存在")
    if template.status == "retired":
        raise AccessError(409, "已停用模板不可编辑")
    if name is not None:
        template.name = _name(name)
    if fields is not None:
        template.draft_fields = _validated(fields)
    template.updated_at = _now()
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise AccessError(409, "同名模板已存在") from exc
    audit.record(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="template.updated",
        object_type="template",
        object_id=template.id,
        metadata={"name": template.name},
    )
    db.commit()
    return serialize(template, repository.latest_revision(db, tenant_id, template.id))


def publish(
    db: Session, *, tenant_id: str, actor_user_id: str, template_id: str
) -> dict[str, Any]:
    template = repository.get_template(db, tenant_id, template_id)
    if template is None:
        raise AccessError(404, "模板不存在")
    if template.status == "retired":
        raise AccessError(409, "已停用模板不可发布")
    fields = _validated(template.draft_fields)
    version = TemplateVersion(
        id=uuid.uuid4().hex,
        template_id=template.id,
        tenant_id=tenant_id,
        revision=repository.latest_revision(db, tenant_id, template.id) + 1,
        fields=fields,
        published_by=actor_user_id,
        published_at=_now(),
    )
    template.status = "active"
    template.updated_at = _now()
    db.add(version)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise AccessError(409, "并发发布冲突，请重试") from exc
    audit.record(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="template.published",
        object_type="template_version",
        object_id=version.id,
        metadata={"revision": version.revision},
    )
    outbox.record(
        db,
        tenant_id=tenant_id,
        event_type="template.version_published",
        aggregate_type="template_version",
        aggregate_id=version.id,
        payload={"template_id": template.id, "revision": version.revision},
    )
    db.commit()
    return {
        "template_version_id": version.id,
        "template_id": template.id,
        "revision": version.revision,
        "fields": version.fields,
    }


def retire(db: Session, *, tenant_id: str, actor_user_id: str, template_id: str) -> dict[str, Any]:
    template = repository.get_template(db, tenant_id, template_id)
    if template is None:
        raise AccessError(404, "模板不存在")
    template.status = "retired"
    template.updated_at = _now()
    audit.record(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="template.retired",
        object_type="template",
        object_id=template.id,
        metadata={"name": template.name},
    )
    db.commit()
    return {"id": template.id, "status": template.status}


def copy(
    db: Session, *, tenant_id: str, actor_user_id: str, template_id: str, name: str | None
) -> dict[str, Any]:
    source = repository.get_template(db, tenant_id, template_id)
    if source is None:
        raise AccessError(404, "模板不存在")
    target_name = _name(name or f"{source.name} 副本")
    clone = Template(
        id=uuid.uuid4().hex,
        tenant_id=tenant_id,
        name=target_name,
        status="draft",
        draft_fields=deepcopy(source.draft_fields),
        created_by=actor_user_id,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(clone)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise AccessError(409, "同名模板已存在") from exc
    audit.record(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="template.copied",
        object_type="template",
        object_id=clone.id,
        metadata={"name": clone.name},
    )
    db.commit()
    return serialize(clone, 0)


def resolve_for_job(db: Session, *, tenant_id: str, template_version_id: str) -> dict[str, Any]:
    """任务创建时的版本解析：必须同租户且所属模板未停用。

    返回普通字典而非 ORM 对象，避免模板上下文的实体泄漏到处理编排（DOC-DDD-001 §7 规则 3）。
    """
    version = repository.get_version(db, tenant_id, template_version_id)
    if version is None:
        raise AccessError(404, "模板版本不存在")
    template = repository.get_template(db, tenant_id, version.template_id)
    if template is None or template.status == "retired":
        raise AccessError(409, "模板已停用，不能用于新任务")
    return {
        "id": version.id,
        "template_id": version.template_id,
        "revision": version.revision,
        "fields": [dict(field) for field in version.fields],
    }
