"""复核用例：机器结果双写、人工修订链、确认与退回（DOC-DD-005 §6）。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.contexts.review import repository
from app.contexts.review.domain import RESULT_STATES, ExtractionResult, ResultRevision
from app.contexts.search import service as search
from app.platform.errors import AccessError
from app.models.document import DocumentVersion
from app.shared import audit, outbox
from app.shared.text_safety import clean_payload, without_nul
from app.shared.timestamps import iso_utc

REVIEW_ROLES = ("reviewer", "admin", "owner")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _reindex(
    db: Session,
    *,
    tenant_id: str,
    result: ExtractionResult,
    values: dict[str, Any],
    now: datetime,
) -> None:
    """把检索索引与结果写入绑在同一事务里：异步补索引会留下"搜不到刚改完的值"。"""
    version = db.get(DocumentVersion, result.document_version_id)
    if version is None or version.tenant_id != tenant_id:
        return
    text = repository.get_text(db, tenant_id, result.document_version_id)
    search.reindex_version(
        db,
        tenant_id=tenant_id,
        document_id=version.document_id,
        document_version_id=version.id,
        filename=result.filename,
        text=text.text if text is not None else "",
        values=values,
        now=now,
    )


def record_machine_result(
    db: Session,
    *,
    tenant_id: str,
    job_id: str,
    filename: str,
    text: str,
    fields: list[dict[str, Any]],
    values: dict[str, Any],
    warnings: list[str],
    doc_type: str | None = None,
) -> ExtractionResult | None:
    """处理管线每完成一个文件即双写规范化结果；重复写入保持幂等。

    已被人工修订或确认过的结果不被机器值覆盖。
    """
    # 机器值与告警直接来自解析器/模型，是最容易带进 NUL 的一段文本；PostgreSQL 会在
    # 写入时拒掉整个事务，所以清洗放在这个入库边界，而不是散落到每个调用点。
    values = clean_payload(values)
    warnings = clean_payload(warnings)
    version = db.scalar(
        select(DocumentVersion).where(
            DocumentVersion.job_id == job_id,
            DocumentVersion.original_filename == filename,
            DocumentVersion.tenant_id == tenant_id,
        )
    )
    if version is None:
        return None

    now = _now()
    repository.upsert_text(
        db,
        tenant_id=tenant_id,
        document_version_id=version.id,
        text=text,
        extracted_at=now,
    )

    result = repository.by_job_and_version(db, tenant_id, job_id, version.id)
    if result is not None and result.state not in ("machine",):
        _reindex(db, tenant_id=tenant_id, result=result, values=result.values, now=now)
        return result

    template_version_id = _template_version_id(db, tenant_id, job_id)
    doc_type = doc_type or _doc_type(db, tenant_id, template_version_id)
    if result is None:
        result = ExtractionResult(
            id=uuid.uuid4().hex,
            tenant_id=tenant_id,
            job_id=job_id,
            document_version_id=version.id,
            template_version_id=template_version_id,
            filename=filename,
            doc_type=doc_type,
            state="machine",
            revision_no=1,
            fields=fields,
            values=values,
            warnings=warnings,
            created_at=now,
            updated_at=now,
        )
        db.add(result)
        db.flush()
        db.add(
            ResultRevision(
                id=uuid.uuid4().hex,
                result_id=result.id,
                tenant_id=tenant_id,
                revision_no=1,
                source="machine",
                values=values,
                created_at=now,
            )
        )
    else:
        result.values = values
        result.fields = fields
        result.warnings = warnings
        result.doc_type = doc_type or result.doc_type
        result.updated_at = now
        first = db.scalar(
            select(ResultRevision).where(
                ResultRevision.result_id == result.id,
                ResultRevision.tenant_id == tenant_id,
                ResultRevision.revision_no == 1,
            )
        )
        if first is not None:
            first.values = values
    _reindex(db, tenant_id=tenant_id, result=result, values=values, now=now)
    return result


def _doc_type(db: Session, tenant_id: str, template_version_id: str | None) -> str | None:
    if template_version_id is None:
        return None
    from app.contexts.templates.domain import Template, TemplateVersion

    version = db.get(TemplateVersion, template_version_id)
    if version is None or version.tenant_id != tenant_id:
        return None
    template = db.get(Template, version.template_id)
    return template.name if template is not None else None


def _template_version_id(db: Session, tenant_id: str, job_id: str) -> str | None:
    from app.models.persistent_job import PersistentJob

    record = db.get(PersistentJob, job_id)
    if record is None or record.tenant_id != tenant_id:
        return None
    return record.template_version_id


def serialize(result: ExtractionResult, revisions: list[ResultRevision] | None = None) -> dict[str, Any]:
    payload = {
        "id": result.id,
        "job_id": result.job_id,
        "document_version_id": result.document_version_id,
        "template_version_id": result.template_version_id,
        "filename": result.filename,
        "doc_type": result.doc_type,
        "state": result.state,
        "revision_no": result.revision_no,
        "fields": result.fields,
        "values": result.values,
        "warnings": result.warnings,
        "confirmed_by": result.confirmed_by,
        "confirmed_at": iso_utc(result.confirmed_at),
        "created_at": iso_utc(result.created_at),
        "updated_at": iso_utc(result.updated_at),
    }
    if revisions is not None:
        payload["revisions"] = [
            {
                "revision_no": item.revision_no,
                "source": item.source,
                "actor_user_id": item.actor_user_id,
                "reason": item.reason,
                "values": item.values,
                "created_at": iso_utc(item.created_at),
            }
            for item in revisions
        ]
    return payload


def list_results(
    db: Session,
    *,
    tenant_id: str,
    state: str | None = None,
    template_version_id: str | None = None,
    job_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    states = [state] if state else []
    if state == "all":
        states = list(RESULT_STATES)
    if state is not None and state != "all" and state not in RESULT_STATES:
        raise AccessError(422, "state 非法")
    rows = repository.list_results(
        db,
        tenant_id,
        states=states if state != "all" else [],
        template_version_id=template_version_id,
        job_id=job_id,
        limit=limit,
        offset=offset,
    )
    return [serialize(row) for row in rows]


def detail(db: Session, *, tenant_id: str, result_id: str) -> dict[str, Any]:
    result = repository.get_result(db, tenant_id, result_id)
    if result is None:
        raise AccessError(404, "结果不存在")
    return serialize(result, repository.list_revisions(db, tenant_id, result.id))


def revisions(db: Session, *, tenant_id: str, result_id: str) -> list[dict[str, Any]]:
    result = repository.get_result(db, tenant_id, result_id)
    if result is None:
        raise AccessError(404, "结果不存在")
    return [
        {
            "revision_no": item.revision_no,
            "source": item.source,
            "actor_user_id": item.actor_user_id,
            "reason": item.reason,
            "values": item.values,
            "created_at": iso_utc(item.created_at),
        }
        for item in repository.list_revisions(db, tenant_id, result.id)
    ]


def preview_text(db: Session, *, tenant_id: str, document_version_id: str) -> dict[str, Any]:
    version = db.get(DocumentVersion, document_version_id)
    if version is None or version.tenant_id != tenant_id:
        raise AccessError(404, "文件版本不存在")
    row = repository.get_text(db, tenant_id, document_version_id)
    return {
        "document_version_id": document_version_id,
        "filename": version.original_filename,
        "char_count": row.char_count if row else 0,
        "text": row.text if row else "",
    }


def revise(
    db: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    result_id: str,
    values: dict[str, Any],
    expected_revision: int,
    reason: str | None = None,
) -> dict[str, Any]:
    result = repository.get_result(db, tenant_id, result_id)
    if result is None:
        raise AccessError(404, "结果不存在")
    if result.state == "confirmed":
        raise AccessError(409, "已确认结果需先退回才能修改")

    names = [field["name"] for field in result.fields or []]
    unknown = sorted(set(values) - set(names))
    if unknown:
        # 未登记字段直接拒绝，避免用导出通道夹带任意键。
        raise AccessError(422, f"未知字段: {', '.join(unknown)}")
    missing = [name for name in names if name not in values]
    if missing:
        # 缺失字段会被静默清空，因此要求整表单提交（复核台本来就是全字段编辑）。
        raise AccessError(422, f"缺少字段: {', '.join(missing)}")
    clean: dict[str, Any] = {}
    for name in names:
        raw = values[name]
        if raw is None:
            # null 是显式清空，与"未提交该字段"不同：后者已经按缺失拒绝。
            clean[name] = ""
        elif isinstance(raw, str):
            # 保留标量原值，但去掉 PostgreSQL 无法存储的 NUL 字节。
            clean[name] = without_nul(raw)
        elif isinstance(raw, (int, float, bool)):
            # 机器结果里数字就是数字，读出来改完写回应不改变类型。
            clean[name] = raw
        else:
            raise AccessError(422, f"字段 {name} 只接受标量值")

    now = _now()
    new_revision = expected_revision + 1
    try:
        if not repository.apply_revision(
            db,
            tenant_id=tenant_id,
            result_id=result.id,
            expected_revision=expected_revision,
            values=clean,
            now=now,
        ):
            db.rollback()
            raise AccessError(409, "结果已被他人修改，请重新加载")
        db.add(
            ResultRevision(
                id=uuid.uuid4().hex,
                result_id=result.id,
                tenant_id=tenant_id,
                revision_no=new_revision,
                source="human",
                actor_user_id=actor_user_id,
                values=clean,
                reason=reason,
                created_at=now,
            )
        )
        db.flush()
        audit.record(
            db,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="result.revised",
            object_type="extraction_result",
            object_id=result.id,
            metadata={"revision_no": new_revision},
        )
        outbox.record(
            db,
            tenant_id=tenant_id,
            event_type="result.revised",
            aggregate_type="extraction_result",
            aggregate_id=result.id,
            payload={"revision_no": new_revision, "job_id": result.job_id},
        )
        _reindex(db, tenant_id=tenant_id, result=result, values=clean, now=now)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise AccessError(409, "结果已被他人修改，请重新加载") from exc
    db.expire(result)
    return serialize(result, repository.list_revisions(db, tenant_id, result.id))


def confirm(
    db: Session, *, tenant_id: str, actor_user_id: str, result_id: str
) -> dict[str, Any]:
    result = repository.get_result(db, tenant_id, result_id)
    if result is None:
        raise AccessError(404, "结果不存在")
    if result.state == "confirmed":
        raise AccessError(409, "结果已确认")
    if result.state not in repository.CONFIRMABLE:
        raise AccessError(409, "已退回结果需先修订才能确认")
    now = _now()
    revision_no = result.revision_no
    if not repository.apply_state_change(
        db,
        tenant_id=tenant_id,
        result_id=result.id,
        allowed=repository.CONFIRMABLE,
        values={
            "state": "confirmed",
            "confirmed_by": actor_user_id,
            "confirmed_at": now,
            "updated_at": now,
        },
    ):
        db.rollback()
        raise AccessError(409, "结果已被他人修改，请重新加载")
    audit.record(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="result.confirmed",
        object_type="extraction_result",
        object_id=result.id,
        metadata={"revision_no": revision_no},
    )
    outbox.record(
        db,
        tenant_id=tenant_id,
        event_type="result.confirmed",
        aggregate_type="extraction_result",
        aggregate_id=result.id,
        payload={"job_id": result.job_id, "revision_no": revision_no},
    )
    db.commit()
    db.expire(result)
    return serialize(result, repository.list_revisions(db, tenant_id, result.id))


def reject(
    db: Session, *, tenant_id: str, actor_user_id: str, result_id: str, reason: str
) -> dict[str, Any]:
    cleaned = (reason or "").strip()
    if not 1 <= len(cleaned) <= 500:
        raise AccessError(422, "退回必须给出 1-500 字符的理由")
    result = repository.get_result(db, tenant_id, result_id)
    if result is None:
        raise AccessError(404, "结果不存在")
    if result.state not in repository.REJECTABLE:
        raise AccessError(409, "结果已处于退回状态")
    now = _now()
    revision_no = result.revision_no
    if not repository.apply_state_change(
        db,
        tenant_id=tenant_id,
        result_id=result.id,
        allowed=repository.REJECTABLE,
        values={
            "state": "rejected",
            "confirmed_by": None,
            "confirmed_at": None,
            "updated_at": now,
        },
    ):
        db.rollback()
        raise AccessError(409, "结果已被他人修改，请重新加载")
    audit.record(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="result.rejected",
        object_type="extraction_result",
        object_id=result.id,
        metadata={"revision_no": revision_no},
    )
    outbox.record(
        db,
        tenant_id=tenant_id,
        event_type="result.rejected",
        aggregate_type="extraction_result",
        aggregate_id=result.id,
        payload={"job_id": result.job_id, "revision_no": revision_no},
    )
    db.commit()
    db.expire(result)
    return serialize(result, repository.list_revisions(db, tenant_id, result.id))


def export_rows(db: Session, *, tenant_id: str, job_id: str) -> list[dict[str, Any]]:
    """存在规范化结果时以当前值为导出源，否则返回空列表由调用方回落快照。"""
    rows = db.scalars(
        select(ExtractionResult)
        .where(ExtractionResult.job_id == job_id, ExtractionResult.tenant_id == tenant_id)
        .order_by(ExtractionResult.filename)
    ).all()
    if not rows:
        return []
    return [
        {"file": row.filename, "values": row.values, "warnings": row.warnings}
        for row in rows
    ]
