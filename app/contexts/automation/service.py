"""事件规则用例：规则维护、事件消费与安全动作执行（DOC-DD-006 §6.3/§6.4）。"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.contexts.automation.domain import (
    ACTION_TYPES,
    ALLOWED_EVENT_TYPES,
    MAX_CONDITION_KEYS,
    MAX_NAME_CHARS,
    EventRule,
    NotificationRequest,
    RuleRun,
)
from app.contexts.automation import repository
from app.contexts.documents import service as documents
from app.platform.errors import AccessError
from app.models.document import DocumentVersion
from app.models.outbox import DomainEventRecord
from app.shared import audit
from app.shared.timestamps import iso_utc

MAX_DISPATCH_BATCH = 100
MAX_DETAIL_CHARS = 500
# 条件键必须落在真实存在的事件载荷字段上：拼错的键会让规则永远不匹配却看不出原因。
CONDITION_KEYS: dict[str, frozenset[str]] = {
    "job.succeeded": frozenset({"attempt_no", "status", "engine"}),
    "job.partial": frozenset({"attempt_no", "status", "engine"}),
    "job.failed": frozenset({"attempt_no", "status", "engine", "reason"}),
    "result.confirmed": frozenset({"job_id", "revision_no"}),
    "result.rejected": frozenset({"job_id", "revision_no"}),
    "result.revised": frozenset({"job_id", "revision_no"}),
}


# 失败详情会原样进日志接口：异常文本可能带着服务器路径（OSError 尤其常见）。
_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\/]|\\)[^\s'\",，;]+|(?:/[^\s/]+){2,}")


def _safe_detail(text: str | None) -> str | None:
    if not text:
        return None
    return _ABSOLUTE_PATH.sub("[路径已隐藏]", text)[:MAX_DETAIL_CHARS]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


def _clean_name(raw: Any) -> str:
    name = (raw or "").strip() if isinstance(raw, str) else ""
    if not 1 <= len(name) <= MAX_NAME_CHARS:
        raise AccessError(422, f"规则名称长度需为 1-{MAX_NAME_CHARS} 个字符")
    return name


def _clean_condition(raw: Any, event_type: str) -> dict[str, Any]:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise AccessError(422, "condition 必须是键值对")
    if len(raw) > MAX_CONDITION_KEYS:
        raise AccessError(422, f"条件键数量不得超过 {MAX_CONDITION_KEYS} 个")
    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not 1 <= len(key.strip()) <= 60:
            raise AccessError(422, "条件键需为 1-60 个字符的字符串")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            # 只比对标量：嵌套条件会让"规则匹配了什么"无法在日志里复原。
            raise AccessError(422, f"条件值只能是标量: {key}")
        allowed = CONDITION_KEYS.get(event_type, frozenset())
        if key.strip() not in allowed:
            raise AccessError(422, f"事件 {event_type} 没有可比较的字段: {key.strip()}")
        cleaned[key.strip()] = value
    return cleaned


def _clean_string(value: Any, *, where: str, maximum: int) -> str:
    text = value if isinstance(value, str) else ""
    if not 1 <= len(text.strip()) <= maximum:
        raise AccessError(422, f"{where} 长度需为 1-{maximum} 个字符")
    return text.strip()


def _clean_action(raw: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise AccessError(422, "action 必须是对象")
    action_type = raw.get("type")
    if action_type not in ACTION_TYPES:
        raise AccessError(422, f"不支持的动作类型: {action_type}")
    params = raw.get("params") or {}
    if not isinstance(params, dict):
        raise AccessError(422, "action.params 必须是对象")
    if action_type == "tag_document":
        return action_type, {"label": _clean_string(params.get("label"), where="label", maximum=30)}
    return action_type, {
        "subject": _clean_string(params.get("subject"), where="subject", maximum=120),
        "body": _clean_string(params.get("body"), where="body", maximum=MAX_DETAIL_CHARS),
    }


def serialize_rule(rule: EventRule, stats: dict[str, int] | None = None) -> dict[str, Any]:
    payload = {
        "id": rule.id,
        "name": rule.name,
        "event_type": rule.event_type,
        "condition": rule.condition,
        "action": {"type": rule.action_type, "params": rule.action_params},
        "status": rule.status,
        "version": rule.version,
        "created_by": rule.created_by,
        "created_at": iso_utc(rule.created_at),
        "updated_at": iso_utc(rule.updated_at),
    }
    if stats is not None:
        payload["runs"] = stats
    return payload


def get_rule(db: Session, tenant_id: str, rule_id: str) -> EventRule:
    rule = repository.get_rule(db, tenant_id, rule_id)
    if rule is None:
        raise AccessError(404, "规则不存在")
    return rule


def create_rule(
    db: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    name: str,
    event_type: str,
    condition: Any = None,
    action: Any = None,
) -> dict[str, Any]:
    if event_type not in ALLOWED_EVENT_TYPES:
        raise AccessError(422, f"不可订阅的事件类型: {event_type}")
    clean_name = _clean_name(name)
    action_type, params = _clean_action(action)
    now = _now()
    rule = EventRule(
        id=_new_id(),
        tenant_id=tenant_id,
        name=clean_name,
        event_type=event_type,
        condition=_clean_condition(condition, event_type),
        action_type=action_type,
        action_params=params,
        status="active",
        version=1,
        created_by=actor_user_id,
        created_at=now,
        updated_at=now,
    )
    db.add(rule)
    audit.record(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="rule.created",
        object_type="event_rule",
        object_id=rule.id,
        metadata={"event_type": event_type, "action_type": action_type, "version": 1},
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise AccessError(409, "同租户已存在同名规则") from exc
    db.expire(rule)
    return serialize_rule(rule)


def update_rule(
    db: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    rule_id: str,
    name: Any = None,
    event_type: Any = None,
    condition: Any = None,
    action: Any = None,
    status: Any = None,
) -> dict[str, Any]:
    rule = get_rule(db, tenant_id, rule_id)
    changed: list[str] = []
    if name is not None and _clean_name(name) != rule.name:
        rule.name = _clean_name(name)
        changed.append("name")
    if event_type is not None:
        if event_type not in ALLOWED_EVENT_TYPES:
            raise AccessError(422, f"不可订阅的事件类型: {event_type}")
        if event_type != rule.event_type:
            rule.event_type = event_type
            changed.append("event_type")
    if condition is not None:
        cleaned = _clean_condition(condition, event_type or rule.event_type)
        if cleaned != rule.condition:
            rule.condition = cleaned
            changed.append("condition")
    if action is not None:
        action_type, params = _clean_action(action)
        if action_type != rule.action_type or params != rule.action_params:
            rule.action_type, rule.action_params = action_type, params
            changed.append("action")
    if status is not None:
        if status not in ("active", "paused"):
            raise AccessError(422, "status 只能是 active 或 paused")
        if status != rule.status:
            rule.status = status
            changed.append("status")
    if not changed:
        raise AccessError(422, "没有需要更新的规则字段")

    rule.version += 1  # 运行日志按当时版本记录，改配置不会改写历史
    rule.updated_at = _now()
    audit.record(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="rule.updated",
        object_type="event_rule",
        object_id=rule.id,
        metadata={"changed": sorted(changed), "version": rule.version},
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise AccessError(409, "同租户已存在同名规则") from exc
    db.expire(rule)
    return serialize_rule(rule)


def list_rules(
    db: Session, *, tenant_id: str, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    total, rules = repository.list_rules(db, tenant_id, limit=limit, offset=offset)
    stats = repository.run_stats(db, tenant_id, [rule.id for rule in rules])
    return {
        "total": total,
        "rules": [serialize_rule(rule, stats[rule.id]) for rule in rules],
    }


def list_runs(
    db: Session,
    *,
    tenant_id: str,
    rule_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    if status is not None and status not in ("applied", "skipped", "failed"):
        raise AccessError(422, "status 非法")
    if rule_id is not None and get_rule(db, tenant_id, rule_id) is None:  # pragma: no cover
        raise AccessError(404, "规则不存在")
    total, runs = repository.list_runs(
        db, tenant_id, rule_id=rule_id, status=status, limit=limit, offset=offset
    )
    return {
        "total": total,
        "runs": [
            {
                "id": run.id,
                "rule_id": run.rule_id,
                "rule_version": run.rule_version,
                "event_id": run.event_id,
                "event_type": run.event_type,
                "status": run.status,
                "detail": run.detail,
                "attempt_count": run.attempt_count,
                "created_at": iso_utc(run.created_at),
                "finished_at": iso_utc(run.finished_at),
            }
            for run in runs
        ],
    }


def list_notifications(
    db: Session,
    *,
    tenant_id: str,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    total, rows = repository.list_notifications(
        db, tenant_id, status=status, limit=limit, offset=offset
    )
    return {
        "total": total,
        "notifications": [
            {
                "id": row.id,
                "recipient_user_id": row.recipient_user_id,
                "rule_id": row.rule_id,
                "subject": row.subject,
                "body": row.body,
                "status": row.status,
                "object_type": row.object_type,
                "object_id": row.object_id,
                "created_at": iso_utc(row.created_at),
            }
            for row in rows
        ],
    }


def _matches(rule: EventRule, event: DomainEventRecord) -> bool:
    payload = event.payload if isinstance(event.payload, dict) else {}
    return all(payload.get(key) == value for key, value in (rule.condition or {}).items())


def _job_id_for(event: DomainEventRecord) -> str | None:
    if event.aggregate_type == "job":
        return event.aggregate_id
    payload = event.payload if isinstance(event.payload, dict) else {}
    value = payload.get("job_id")
    return value if isinstance(value, str) else None


def _apply(db: Session, *, rule: EventRule, event: DomainEventRecord, now: datetime) -> str:
    """执行动作；领域级失败以异常上抛，由运行日志记录后重试。"""
    if rule.action_type == "tag_document":
        job_id = _job_id_for(event)
        if job_id is None:
            return "tagged=0"
        document_ids = list(
            db.scalars(
                select(DocumentVersion.document_id).where(
                    DocumentVersion.job_id == job_id,
                    DocumentVersion.tenant_id == event.tenant_id,
                )
            ).all()
        )
        tagged = 0
        for document_id in dict.fromkeys(document_ids):
            if documents.add_label(
                db,
                tenant_id=event.tenant_id,
                document_id=document_id,
                label=rule.action_params.get("label", ""),
                actor_user_id=rule.created_by,
                rule_id=rule.id,
                now=now,
            ):
                tagged += 1
        return f"tagged={tagged}"

    db.add(
        NotificationRequest(
            id=_new_id(),
            tenant_id=event.tenant_id,
            recipient_user_id=rule.created_by,
            rule_id=rule.id,
            subject=rule.action_params.get("subject", "")[:120],
            body=rule.action_params.get("body", "")[:MAX_DETAIL_CHARS],
            status="pending",
            object_type=event.aggregate_type,
            object_id=event.aggregate_id,
            created_at=now,
        )
    )
    return "notified=1"


def _record_run(
    db: Session,
    *,
    event: DomainEventRecord,
    rule: EventRule,
    delivery_key: str,
    status: str,
    detail: str | None,
    existing: RuleRun | None,
    now: datetime,
) -> bool:
    """登记运行记录；返回 False 表示该 (事件, 规则, 版本) 已被并发消费者写入。"""
    if existing is not None:
        existing.status = status
        existing.detail = _safe_detail(detail)
        existing.attempt_count += 1
        if status != "failed":
            existing.finished_at = now
        return True
    try:
        with db.begin_nested():
            db.add(
                RuleRun(
                    id=_new_id(),
                    tenant_id=event.tenant_id,
                    rule_id=rule.id,
                    rule_version=rule.version,
                    event_id=event.id,
                    event_type=event.event_type,
                    delivery_key=delivery_key,
                    status=status,
                    detail=_safe_detail(detail),
                    attempt_count=1,
                    created_at=now,
                    finished_at=None if status == "failed" else now,
                )
            )
            db.flush()
    except IntegrityError:
        return False
    return True


def _run_action(
    db: Session, *, rule: EventRule, event: DomainEventRecord, now: datetime
) -> tuple[str | None, str | None]:
    """在保存点里执行动作：失败时回滚动作已写入的一半内容。

    不回滚就会出现"运行记录是 failed，但前几份文档已经打上标签并写了审计"，
    重试时那几份会被再动一次，FR-040 的失败重试承诺随之失效。
    """
    try:
        with db.begin_nested():
            return _apply(db, rule=rule, event=event, now=now), None
    except (AccessError, ValueError) as exc:
        return None, str(exc)


def dispatch(
    db: Session,
    *,
    tenant_id: str,
    rule_id: str | None = None,
    limit: int = 50,
) -> dict[str, int]:
    """消费本租户待投递事件；`delivery_key` 唯一让重试不重复执行已完成的动作。

    指定 `rule_id` 的手动试跑只看该规则订阅的事件类型，也不推进任何事件的投递
    时间：跑一条规则不该把整个 outbox 烧掉，留给后续规则的事件由整批 dispatch 处理。
    """
    now = _now()
    summary = {"consumed": 0, "applied": 0, "skipped": 0, "failed": 0, "published": 0}
    only: EventRule | None = None
    if rule_id is not None:
        only = get_rule(db, tenant_id, rule_id)
        if only.status != "active":
            return summary
    events = repository.pending_events(
        db, tenant_id, event_type=only.event_type if only is not None else None, limit=limit
    )
    for event in events:
        summary["consumed"] += 1
        rules = (
            [only]
            if only is not None
            else repository.active_rules(db, tenant_id, event_type=event.event_type)
        )
        blocked = False
        for rule in rules:
            key = f"{event.id}:{rule.id}:{rule.version}"
            existing = repository.find_run(db, tenant_id, key)
            if existing is not None and existing.status in ("applied", "skipped"):
                continue
            if not _matches(rule, event):
                if existing is None and _record_run(
                    db,
                    event=event,
                    rule=rule,
                    delivery_key=key,
                    status="skipped",
                    detail=None,
                    existing=None,
                    now=now,
                ):
                    summary["skipped"] += 1
                continue
            detail, failure = _run_action(db, rule=rule, event=event, now=now)
            if failure is not None:
                # 单个动作失败只影响这一条运行记录：事件留在队列里等下次 dispatch。
                _record_run(
                    db,
                    event=event,
                    rule=rule,
                    delivery_key=key,
                    status="failed",
                    detail=failure,
                    existing=existing,
                    now=now,
                )
                summary["failed"] += 1
                blocked = True
                continue
            if _record_run(
                db,
                event=event,
                rule=rule,
                delivery_key=key,
                status="applied",
                detail=detail,
                existing=existing,
                now=now,
            ):
                summary["applied"] += 1
        if only is None and not blocked:
            event.published_at = now
            event.publish_attempts += 1
            summary["published"] += 1
    db.commit()
    return summary
