"""运营读模型：用量聚合与就绪判定，全部从业务表现算，不建第二真相（DOC-DD-007 §4）。"""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import config
from app.contexts.automation.domain import EventRule, RuleRun
from app.contexts.processing.domain import JobAttempt
from app.contexts.review.domain import ExtractionResult, ResultRevision
from app.contexts.search.domain import SearchEntry
from app.errors import AccessError
from app.models.account import Tenant
from app.models.document import Document, DocumentVersion
from app.models.outbox import DomainEventRecord
from app.models.persistent_job import PersistentJob
from app.services.job_repository import incomplete_rebuild_count
from app.services.timestamps import iso_utc

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
        return {
            "window_days": window,
            "total_tenants": total_tenants,
            "job_rebuild_incomplete": incomplete_rebuild_count(),
            "tenants": [],
        }

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
        # 快照退役 R5 的观察期判据（docs/20 §6.1）：不可重建的任务读一次记一次。
        # 它是**进程内**计数，重启归零、多进程各算各的，所以只用来回答"这段时间里
        # 有没有出现过读不出来的任务"，不能被当成跨进程的累计指标。
        "job_rebuild_incomplete": incomplete_rebuild_count(),
        "tenants": rows,
    }


def storage_health() -> dict[str, Any]:
    """/readyz 的对象存储段（R-018 的探针侧）。

    口径与队列段一致：不声明的东西不参与判定。`STORAGE_BACKEND=local` 时文件就在本机
    目录里，没有外部依赖可问，段里只写 `checked: false`；声明走 s3 就必须真能列出对象，
    列不出就是故障——此时上传与下载都会失败，不能只让数据库健康把服务标成 ready。
    """
    from app.services.object_storage import get_object_storage

    backend = config.STORAGE_BACKEND
    if backend != "s3":
        return {"enabled": False, "backend": backend, "checked": False}
    try:
        sample = next(itertools.islice(get_object_storage().list_keys(""), 1), None)
    except Exception as exc:  # 探针自身不许把 /readyz 变成 500
        return {
            "enabled": True,
            "backend": "s3",
            "bucket": config.S3_BUCKET,
            "reachable": False,
            "checked": True,
            "error": type(exc).__name__,
        }
    return {
        "enabled": True,
        "backend": "s3",
        "bucket": config.S3_BUCKET,
        "reachable": True,
        "checked": True,
        "listed_sample": sample is not None,
    }


def readiness(
    session_factory,
    head_revision: str,
    queue_probe: Callable[[], dict[str, Any]] | None = None,
    storage_probe: Callable[[], dict[str, Any]] | None = None,
) -> tuple[int, dict[str, Any]]:
    """就绪判定：数据库不可用、迁移未对齐、队列卡住或对象存储不可达都返回 503，但绝不抛异常。

    `queue_probe` / `storage_probe` 默认取各自上下文的真实探针；测试里换成假探针，才能把
    "有积压没消费者"、"桶问不到"和"broker 不可达"几种故障分别钉住。
    """
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
    if queue_probe is None:
        from app.contexts.processing import service as processing

        queue_probe = processing.queue_health
    try:
        queue = queue_probe()
    except Exception as exc:  # 探针自身不许把 /readyz 变成 500
        queue = {"enabled": True, "consumers": None, "pending": None, "stuck": True}
        error = error or f"queue_probe:{type(exc).__name__}"
    queue_stuck = bool(queue.get("enabled") and queue.get("stuck"))
    if storage_probe is None:
        storage_probe = storage_health
    try:
        storage = storage_probe()
    except Exception as exc:  # 同队列段：探针不能自己变成 500 的来源
        storage = {"enabled": True, "reachable": False, "error": f"storage_probe:{type(exc).__name__}"}
    storage_down = bool(storage.get("enabled") and storage.get("reachable") is False)
    ready = in_sync and not queue_stuck and not storage_down
    status = "ready" if ready else "unavailable"
    body = {
        "status": status,
        "database": database_ok,
        "migration": {"current": current, "head": head_revision, "in_sync": in_sync},
        "queue": queue,
        "storage": storage,
    }
    if error is not None:
        body["error"] = error
    return (200 if ready else 503), body


def _alembic_version_column():
    from sqlalchemy import column, table

    return table("alembic_version", column("version_num")).c.version_num


# ---------- 失败明细下钻（FR-042，DOC-DD-007 §5）----------

FAILURE_SCAN_CAP = 5000

# 分类是启发式的：错误文本由我们自己那几条处理链路写出，已知形状给稳定代号，其余只给指纹。
# 顺序有意义——先命中的规则赢，所以更具体的词排在泛化词前面。
_FAILURE_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("timeout", "执行超时", ("超时", "timeout")),
    ("queue_unavailable", "队列不可用", ("broker", "amqp", "rabbitmq")),
    ("engine_unavailable", "提取引擎不可用", ("401", "403", "429", "rate limit", "unauthorized", "invalid api key")),
    ("document_unreadable", "文档无法读取", ("pdf", "解析", "损坏", "corrupt", "ocr", "image")),
    ("no_results", "未提取到结果", ("无结果", "no result", "empty")),
)


def classify_failure(error: str | None) -> dict[str, str]:
    """把错误文本归成代号与标签。

    平台视图跨租户，不能把文件名、正文或原文带出去；而 DOC-DD-007 §3 的"失败摘要不返回错误
    文本"这条不变式又要求运营看得出错在哪一类。代号同时满足两边：已知形状给代号加中文标签，
    未知形状只给文本指纹与长度——原文一个字都不回显。
    """
    text = (error or "").strip()
    if not text:
        return {"code": "unreported", "label": "无错误信息记录"}
    lowered = text.lower()
    for code, label, markers in _FAILURE_RULES:
        if any(marker in lowered for marker in markers):
            return {"code": code, "label": label}
    fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return {
        "code": f"unclassified-{fingerprint}",
        "label": f"未分类错误（指纹 {fingerprint}，{len(text)} 字符）",
    }


def _attempt_errors(db: Session, job_ids: list[str]) -> dict[str, str | None]:
    """每个任务取 `attempt_no` 最大的那条运行记录的错误文本。

    分两次普通查询而不是相关子查询：SQLite 与 PostgreSQL 在"每组取最后一行"上的写法差异是
    已知的方言坑，而这里要读的行数已被分页与扫描上限夹住。
    """
    if not job_ids:
        return {}
    best: dict[str, tuple[int, str | None]] = {}
    for attempt_no, job_id, error in db.execute(
        select(JobAttempt.attempt_no, JobAttempt.job_id, JobAttempt.error).where(
            JobAttempt.job_id.in_(job_ids)
        )
    ).all():
        current = best.get(job_id)
        if current is None or int(attempt_no) > current[0]:
            best[job_id] = (int(attempt_no), error)
    return {job_id: value[1] for job_id, value in best.items()}


def _window(days: int) -> tuple[int, datetime]:
    window_days = min(max(days, 1), MAX_WINDOW_DAYS)
    return window_days, _now() - timedelta(days=window_days)


def tenant_failures(
    db: Session,
    *,
    tenant_id: str,
    days: int = DEFAULT_WINDOW_DAYS,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """本租户的失败明细：可以出现该租户自己的错误文本与任务号，但不越到别的租户。"""
    window_days, since = _window(days)
    size = min(max(limit, 1), PAGE_MAX)
    skip = max(offset, 0)

    total = int(
        db.scalar(
            select(func.count(PersistentJob.id)).where(
                PersistentJob.tenant_id == tenant_id,
                PersistentJob.status.in_(["failed", "partial"]),
                PersistentJob.finished_at >= since,
            )
        )
        or 0
    )
    jobs = list(
        db.scalars(
            select(PersistentJob)
            .where(
                PersistentJob.tenant_id == tenant_id,
                PersistentJob.status.in_(["failed", "partial"]),
                PersistentJob.finished_at >= since,
            )
            .order_by(PersistentJob.finished_at.desc(), PersistentJob.id.desc())
            .limit(size)
            .offset(skip)
        ).all()
    )
    errors = _attempt_errors(db, [job.id for job in jobs])
    items = [
        {
            "job_id": job.id,
            "status": job.status,
            "attempt_count": job.attempt_count,
            "error": errors.get(job.id),
            "finished_at": iso_utc(job.finished_at),
            **classify_failure(errors.get(job.id)),
        }
        for job in jobs
    ]

    runs_total = int(
        db.scalar(
            select(func.count(RuleRun.id)).where(
                RuleRun.tenant_id == tenant_id,
                RuleRun.status == "failed",
                RuleRun.created_at >= since,
            )
        )
        or 0
    )
    run_items = [
        {
            "run_id": run_id,
            "event_type": event_type,
            "detail": detail,
            "created_at": iso_utc(created_at),
            **classify_failure(detail),
        }
        for run_id, event_type, detail, created_at in db.execute(
            select(RuleRun.id, RuleRun.event_type, RuleRun.detail, RuleRun.created_at)
            .where(
                RuleRun.tenant_id == tenant_id,
                RuleRun.status == "failed",
                RuleRun.created_at >= since,
            )
            .order_by(RuleRun.created_at.desc())
            .limit(size)
            .offset(skip)
        ).all()
    ]

    return {
        "window_days": window_days,
        "page": {"limit": size, "offset": skip},
        "jobs": {"total": total, "items": items},
        "rule_runs": {"total": runs_total, "items": run_items},
        "generated_at": iso_utc(_now()),
    }


def platform_failures(
    db: Session, *, days: int = DEFAULT_WINDOW_DAYS, scan_cap: int = FAILURE_SCAN_CAP
) -> dict[str, Any]:
    """跨租户的失败分布：只有代号与计数，不含错误原文、文件名、正文、邮箱（DOC-DD-007 §3）。

    下钻到这里就到"哪一类错误、影响了几个租户"为止；要看某条任务的原文必须回到租户端点，
    那里有成员关系与角色矩阵把关。
    """
    window_days, since = _window(days)
    rows = db.execute(
        select(
            PersistentJob.id,
            PersistentJob.tenant_id,
            PersistentJob.finished_at,
        )
        .where(
            PersistentJob.status.in_(["failed", "partial"]),
            PersistentJob.finished_at >= since,
        )
        .order_by(PersistentJob.finished_at.desc())
        .limit(scan_cap)
    ).all()
    errors = _attempt_errors(db, [row.id for row in rows])

    buckets: dict[str, dict[str, Any]] = {}
    tenants: set[str] = set()
    for row in rows:
        tenants.add(row.tenant_id)
        kind = classify_failure(errors.get(row.id))
        bucket = buckets.setdefault(
            kind["code"],
            {
                "code": kind["code"],
                "label": kind["label"],
                "jobs": 0,
                "tenants": set(),
                "last_seen": None,
            },
        )
        bucket["jobs"] += 1
        bucket["tenants"].add(row.tenant_id)
        if bucket["last_seen"] is None or row.finished_at > bucket["last_seen"]:
            bucket["last_seen"] = row.finished_at

    codes = [
        {
            "code": bucket["code"],
            "label": bucket["label"],
            "jobs": bucket["jobs"],
            "tenants_affected": len(bucket["tenants"]),
            "last_seen": iso_utc(bucket["last_seen"]),
        }
        for bucket in sorted(buckets.values(), key=lambda b: (-b["jobs"], b["code"]))
    ]
    return {
        "window_days": window_days,
        "scanned_rows": len(rows),
        # 撞到扫描上限就如实说明：宁可报"看不全"，也不给一个看起来完整的分布。
        "truncated": len(rows) >= scan_cap,
        "scan_cap": scan_cap,
        "tenants_with_failures": len(tenants),
        "tenant_ids": sorted(tenants),
        "codes": codes,
        "generated_at": iso_utc(_now()),
    }
