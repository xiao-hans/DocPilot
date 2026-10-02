"""带身份认证和租户授权的版本化文档任务 API。"""

from __future__ import annotations

import json
import hashlib
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import TenantContext, get_tenant_context, require_csrf
from app.config import MAX_FILES_PER_JOB, MAX_UPLOAD_MB, OUTPUT_DIR, UPLOAD_DIR
from app.models.job import JobStatus
from app.models.document import Document, DocumentVersion
from app.models.schema import validate_fields
from app.services import ExcelBuilder
from app.services.job_manager import get_job_manager

router = APIRouter(prefix="/api/v1/tenants/{tenant_id}/jobs", tags=["jobs"])
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
    fields: str = Form(...),
    _csrf=Depends(require_csrf),
    context: TenantContext = Depends(get_tenant_context),
):
    if not files:
        raise HTTPException(status_code=400, detail="未上传任何文件")
    if len(files) > MAX_FILES_PER_JOB:
        raise HTTPException(status_code=400, detail=f"文件数量超过限制（最多 {MAX_FILES_PER_JOB} 个）")

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
                raise HTTPException(status_code=413, detail=f"文件过大: {filename}（最大 {MAX_UPLOAD_MB} MB）")
            chunks.append(chunk)
        checked.append((filename, b"".join(chunks)))

    try:
        fields_list = json.loads(fields)
        validated_fields = validate_fields(fields_list)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"字段定义无效: {exc}") from exc

    manager = get_job_manager()
    job = manager.create_job(
        files=[(name, "") for name, _content in checked],
        fields=validated_fields,
        tenant_id=context.tenant.id,
        owner_id=context.auth.user.id,
        persist=False,
    )
    upload_dir = UPLOAD_DIR / context.tenant.id / job.id
    documents: list[Document] = []
    versions: list[DocumentVersion] = []
    try:
        upload_dir.mkdir(parents=True, exist_ok=False)
        for (filename, content), finfo in zip(checked, job.files, strict=True):
            destination = upload_dir / f"{uuid.uuid4().hex}{Path(filename).suffix.lower()}"
            destination.write_bytes(content)
            finfo.path = str(destination.resolve())
            now = datetime.now(timezone.utc)
            document = Document(
                id=uuid.uuid4().hex,
                tenant_id=context.tenant.id,
                owner_id=context.auth.user.id,
                status="active",
                created_at=now,
            )
            documents.append(document)
            versions.append(
                DocumentVersion(
                    id=uuid.uuid4().hex,
                    document_id=document.id,
                    tenant_id=context.tenant.id,
                    job_id=job.id,
                    revision=1,
                    original_filename=filename,
                    storage_key=destination.resolve()
                    .relative_to(UPLOAD_DIR.resolve())
                    .as_posix(),
                    checksum=hashlib.sha256(content).hexdigest(),
                    media_type=_MEDIA_TYPES[Path(filename).suffix.lower()],
                    size_bytes=len(content),
                    created_at=now,
                )
            )
        manager.save_with_documents(job, documents, versions)
    except (OSError, SQLAlchemyError, ValueError, RuntimeError) as exc:
        shutil.rmtree(upload_dir, ignore_errors=True)
        manager.discard_unpersisted(job.id)
        raise HTTPException(status_code=500, detail="文件或任务元数据保存失败") from exc

    background_tasks.add_task(manager.process, job.id)
    return {"job_id": job.id, "status": "queued"}


@router.get("/{job_id}")
def get_job(job_id: str, context: TenantContext = Depends(get_tenant_context)):
    try:
        job = get_job_manager().get_job(job_id, context.tenant.id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    return job.to_dict()


@router.get("/{job_id}/result")
def get_job_result(job_id: str, context: TenantContext = Depends(get_tenant_context)):
    try:
        job = get_job_manager().get_job(job_id, context.tenant.id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    return {
        "job_id": job.id,
        "status": job.status.value,
        "progress": job.progress.value,
        "result": job.result,
        "warnings": job.warnings,
        "error": job.error,
    }


@router.get("/{job_id}/excel")
def get_job_excel(job_id: str, context: TenantContext = Depends(get_tenant_context)):
    try:
        job = get_job_manager().get_job(job_id, context.tenant.id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    if job.status is not JobStatus.SUCCEEDED:
        raise HTTPException(status_code=404, detail="任务未完成或失败，无法导出 Excel")

    output_dir = OUTPUT_DIR / context.tenant.id
    output_dir.mkdir(parents=True, exist_ok=True)
    path = Path(job.excel_path) if job.excel_path and Path(job.excel_path).is_file() else None
    if path is None:
        path = ExcelBuilder().build(job, output_dir / f"{job.id}.xlsx")
        job.excel_path = str(path)
        get_job_manager().save(job)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{job.id}.xlsx",
    )
