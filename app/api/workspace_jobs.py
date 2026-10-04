"""带身份认证和租户授权的版本化文档任务 API。"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import uuid
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.dependencies import TenantContext, require_csrf, require_roles
from app.config import MAX_FILES_PER_JOB, MAX_UPLOAD_MB, OUTPUT_DIR, QUEUE_ENABLED
from app.contexts.processing import service as processing
from app.contexts.quota import service as quota_service
from app.contexts.templates import service as templates
from app.db import get_db
from app.models.document import Document, DocumentVersion
from app.models.job import Job, JobStatus
from app.models.schema import FieldDefinition, validate_fields
from app.contexts.review import service as review
from app.services import ExcelBuilder, audit, csv_builder
from app.services.export_files import build_atomically
from app.services.job_manager import get_job_manager
from app.services.job_repository import IncompleteJobFacts
from app.services.object_storage import ObjectStorage, build_object_storage

router = APIRouter(prefix="/api/v1/tenants/{tenant_id}/jobs", tags=["jobs"])
READ_ROLES = ("owner", "admin", "member", "reviewer")
SUBMIT_ROLES = ("owner", "admin", "member")
_ALLOWED_EXTS = {".pdf", ".png", ".jpg", ".jpeg"}
_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


@router.post("", status_code=201)
async def create_job(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] | None = File(default=None),
    fields: str | None = Form(default=None),
    template_version_id: str | None = Form(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles(*SUBMIT_ROLES)),
    db: Session = Depends(get_db),
):
    """创建处理任务：字段来自内联定义或已发布模板版本，支持租户内幂等键。"""
    tenant_id = context.tenant.id
    actor_id = context.auth.user.id
    key = idempotency_key.strip() if idempotency_key is not None else None
    if key is not None and not 1 <= len(key) <= 200:
        raise HTTPException(status_code=422, detail="Idempotency-Key 长度需为 1-200 个字符")
    if key:
        existing = processing.find_by_idempotency_key(db, tenant_id, key)
        if existing is not None:
            return _reused(existing)

    if not files:
        raise HTTPException(status_code=400, detail="未上传任何文件")
    if len(files) > MAX_FILES_PER_JOB:
        raise HTTPException(
            status_code=400, detail=f"文件数量超过限制（最多 {MAX_FILES_PER_JOB} 个）"
        )

    checked: list[tuple[str, bytes]] = []
    seen_names: set[str] = set()
    limit = MAX_UPLOAD_MB * 1024 * 1024
    for upload in files:
        filename = Path((upload.filename or "unnamed").replace("\\", "/")).name
        if filename.casefold() in seen_names:
            raise HTTPException(status_code=400, detail=f"文件重名: {filename}")
        seen_names.add(filename.casefold())
        ext = Path(filename).suffix.lower()
        if ext not in _ALLOWED_EXTS:
            raise HTTPException(status_code=400, detail=f"不支持的文件格式: {ext}")
        chunks: list[bytes] = []
        size = 0
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise HTTPException(
                    status_code=413, detail=f"文件过大: {filename}（最大 {MAX_UPLOAD_MB} MB）"
                )
            chunks.append(chunk)
        checked.append((filename, b"".join(chunks)))

    frozen_version_id: str | None = None
    if template_version_id is not None:
        version = templates.resolve_for_job(
            db, tenant_id=tenant_id, template_version_id=template_version_id
        )
        validated_fields = [FieldDefinition(**item) for item in version["fields"]]
        frozen_version_id = version["id"]
        if fields is not None and _parse_fields(fields) != validated_fields:
            raise HTTPException(status_code=422, detail="fields 与模板版本字段不一致")
    elif fields is not None:
        validated_fields = _parse_fields(fields)
    else:
        raise HTTPException(status_code=422, detail="必须提供 fields 或 template_version_id")

    # 配额判定必须在这之前：这里之后才开始写对象与行，被拒的请求要能一条痕迹都不留。
    quota_service.enforce_upload(
        db,
        tenant_id=tenant_id,
        documents=len(checked),
        total_bytes=sum(len(content) for _name, content in checked),
    )

    manager = get_job_manager()
    job = manager.create_job(
        files=[(name, "") for name, _content in checked],
        fields=validated_fields,
        tenant_id=tenant_id,
        owner_id=actor_id,
        persist=False,
    )
    job.template_version_id = frozen_version_id
    job.idempotency_key = key

    storage = build_object_storage()
    object_prefix = f"{tenant_id}/{job.id}"
    documents: list[Document] = []
    versions: list[DocumentVersion] = []
    events: list[dict[str, Any]] = []
    try:
        for (filename, content), finfo in zip(checked, job.files, strict=True):
            suffix = Path(filename).suffix.lower()
            media_type = _MEDIA_TYPES[suffix]
            # 对象键只由租户/任务/随机 UUID 组成：客户端文件名不参与寻址，
            # 也就没有"文件名里带 .."之类的路径注入面。
            object_key = f"{object_prefix}/{uuid.uuid4().hex}{suffix}"
            storage.put(
                object_key, io.BytesIO(content), content_type=media_type
            )
            finfo.storage_key = object_key
            now = datetime.now(timezone.utc)
            document = Document(
                id=uuid.uuid4().hex,
                tenant_id=tenant_id,
                owner_id=actor_id,
                status="active",
                created_at=now,
            )
            document_version = DocumentVersion(
                id=uuid.uuid4().hex,
                document_id=document.id,
                tenant_id=tenant_id,
                job_id=job.id,
                revision=1,
                original_filename=filename,
                storage_key=object_key,
                checksum=hashlib.sha256(content).hexdigest(),
                media_type=media_type,
                size_bytes=len(content),
                created_at=now,
            )
            documents.append(document)
            versions.append(document_version)
            events.append(
                {
                    "tenant_id": tenant_id,
                    "event_type": "document.uploaded",
                    "aggregate_type": "document_version",
                    "aggregate_id": document_version.id,
                    "payload": {
                        "document_id": document.id,
                        "job_id": job.id,
                        "size_bytes": document_version.size_bytes,
                    },
                }
            )
        events.append(
            {
                "tenant_id": tenant_id,
                "event_type": "job.queued",
                "aggregate_type": "job",
                "aggregate_id": job.id,
                "payload": {
                    "status": job.status.value,
                    "file_count": len(versions),
                    "template_version_id": frozen_version_id,
                },
            }
        )
        manager.save_with_documents(
            job,
            documents,
            versions,
            audit_events=[
                audit.new_event(
                    tenant_id=tenant_id,
                    actor_user_id=actor_id,
                    action="job.created",
                    object_type="job",
                    object_id=job.id,
                    metadata={"file_count": len(versions)},
                )
            ],
            events=events,
        )
    except (OSError, SQLAlchemyError, ValueError, RuntimeError) as exc:
        # 对外只给通用文案，但服务端必须留下真实原因，否则 500 无法定位。
        logging.getLogger("docpilot.upload").exception(
            "任务保存失败 tenant_id=%s job_id=%s", tenant_id, job.id
        )
        _compensate_upload(storage, object_prefix)
        manager.discard_unpersisted(job.id)
        if key:
            db.rollback()
            raced = processing.find_by_idempotency_key(db, tenant_id, key)
            if raced is not None:
                return _reused(raced)
        raise HTTPException(status_code=500, detail="文件或任务元数据保存失败") from exc

    if not QUEUE_ENABLED:
        # 队列未启用时才由 API 进程内执行；启用后执行权交给 Worker 的租约领取，
        # 否则同一任务会被两个执行者争抢。
        background_tasks.add_task(manager.process, job.id)
    return {"job_id": job.id, "status": "queued", "reused": False}


@router.get("/{job_id}")
def get_job(
    job_id: str,
    context: TenantContext = Depends(require_roles(*READ_ROLES)),
    db: Session = Depends(get_db),
):
    job = _load_job(job_id, context)
    payload = job.to_dict()
    payload["attempts"] = processing.attempts_payload(db, job.id, context.tenant.id)
    return payload


@router.get("/{job_id}/result")
def get_job_result(
    job_id: str,
    context: TenantContext = Depends(require_roles(*READ_ROLES)),
    db: Session = Depends(get_db),
):
    job = _load_job(job_id, context)
    return {
        "job_id": job.id,
        "status": job.status.value,
        "progress": job.progress.value,
        "result": job.result,
        "warnings": job.warnings,
        "error": job.error,
        "template_version_id": job.template_version_id,
        "attempt_count": job.attempt_count,
        "finished_at": job.finished_at,
    }


@router.post("/{job_id}/retry", status_code=202)
def retry_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(require_roles(*SUBMIT_ROLES)),
    db: Session = Depends(get_db),
):
    _load_job(job_id, context)
    try:
        attempt_count = processing.requeue(db, job_id, context.tenant.id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit.record_now(
        tenant_id=context.tenant.id,
        actor_user_id=context.auth.user.id,
        action="job.retried",
        object_type="job",
        object_id=job_id,
        metadata={"attempt_count": attempt_count},
    )
    background_tasks.add_task(get_job_manager().process, job_id)
    return {"job_id": job_id, "status": "queued", "attempt_count": attempt_count}


@router.get("/{job_id}/excel")
def get_job_excel(
    job_id: str,
    context: TenantContext = Depends(require_roles(*READ_ROLES)),
    db: Session = Depends(get_db),
):
    job = _load_job(job_id, context)
    if job.status not in (JobStatus.SUCCEEDED, JobStatus.PARTIAL):
        raise HTTPException(status_code=404, detail="任务未完成或失败，无法导出 Excel")

    output_dir = OUTPUT_DIR / context.tenant.id
    output_dir.mkdir(parents=True, exist_ok=True)
    # 每次按当前结果值重建，不再复用 `excel_path` 缓存：缓存文件会在人工修订之后继续给出
    # 机器值，那正是"快照与规范化结果不一致"本身。文件名固定，重建不改变下载链接。
    path = build_atomically(
        lambda staging: ExcelBuilder().build(job, staging),
        output_dir / f"{job.id}.xlsx",
    )
    audit.record_now(
        tenant_id=context.tenant.id,
        actor_user_id=context.auth.user.id,
        action="job.excel_exported",
        object_type="job",
        object_id=job.id,
    )
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{job.id}.xlsx",
    )


@router.get("")
def list_jobs(
    status: str | None = None,
    template_version_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    context: TenantContext = Depends(require_roles(*READ_ROLES)),
    db: Session = Depends(get_db),
):
    return processing.list_jobs(
        db,
        tenant_id=context.tenant.id,
        status=status,
        template_version_id=template_version_id,
        limit=limit,
        offset=offset,
    )


@router.get("/{job_id}/export")
def export_job(
    job_id: str,
    format: str = Query(default="xlsx", pattern="^(xlsx|csv)$"),
    context: TenantContext = Depends(require_roles(*READ_ROLES)),
    db: Session = Depends(get_db),
):
    """按当前结果值导出（存在人工修订时不再回退机器快照），并保持注入防护。"""
    job = _load_job(job_id, context)
    if job.status not in (JobStatus.SUCCEEDED, JobStatus.PARTIAL):
        raise HTTPException(status_code=404, detail="任务未完成或失败，无法导出")

    output_dir = OUTPUT_DIR / context.tenant.id
    output_dir.mkdir(parents=True, exist_ok=True)
    # 导出文件与 /excel 各自命名：两个端点各自有稳定链接，共用一个文件会让其中一次的
    # 格式选择覆盖掉另一次的产物。
    if format == "csv":
        path = build_atomically(
            lambda staging: csv_builder.build(job, staging),
            output_dir / f"{job.id}.csv",
        )
        media = "text/csv"
    else:
        path = build_atomically(
            lambda staging: ExcelBuilder().build(job, staging),
            output_dir / f"{job.id}.export.xlsx",
        )
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    audit.record_now(
        tenant_id=context.tenant.id,
        actor_user_id=context.auth.user.id,
        action="job.exported",
        object_type="job",
        object_id=job.id,
        metadata={"format": format},
    )
    return FileResponse(path, media_type=media, filename=f"{job.id}.{format}")


def _compensate_upload(storage: ObjectStorage, prefix: str) -> None:
    """收回已写入但没有任何记录指向的对象。

    清理本身失败时只记日志：这次请求的失败原因已经确定，不能被补偿异常顶掉；
    残留对象由 `scripts/storage_orphan_audit.py` 报出（只报告，不自动删）。
    """
    try:
        storage.delete_prefix(prefix)
    except (OSError, RuntimeError):
        logging.getLogger("docpilot.upload").exception(
            "孤儿对象清理失败 object_prefix=%s", prefix
        )


def _load_job(job_id: str, context: TenantContext) -> Job:
    try:
        return get_job_manager().get_job(job_id, context.tenant.id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except IncompleteJobFacts as exc:
        # 重建不出来时不返回一个"字段为空、文件为零"的正常任务：那等于把数据缺陷伪装成
        # 业务事实（docs/20 §8.5）。503 而不是 500：这是可修复的存储状态，不是请求错误。
        raise HTTPException(status_code=503, detail="任务数据不完整，需先按 docs/20 回填") from exc


def _parse_fields(raw: str) -> list[FieldDefinition]:
    try:
        return validate_fields(json.loads(raw))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"字段定义无效: {exc}") from exc


def _reused(record: Any) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={"job_id": record.id, "status": record.status, "reused": True},
    )
