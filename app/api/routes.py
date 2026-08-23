"""任务 API：创建、查询、获取结果。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.config import MAX_FILES_PER_JOB, MAX_UPLOAD_MB, OUTPUT_DIR, UPLOAD_DIR
from app.models.job import JobStatus
from app.models.schema import validate_fields
from app.services import ExcelBuilder
from app.services.job_manager import get_job_manager

router = APIRouter(prefix="/api/jobs")

# API 层收紧后的允许扩展名（比 config.ALLOWED_EXTENSIONS 更窄）
_ALLOWED_EXTS = {".pdf", ".png", ".jpg", ".jpeg"}


@router.post("", status_code=201)
async def create_job(
    files: list[UploadFile] | None = File(default=None),
    fields: str = Form(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
):
    # 1. 无文件（File(None) 使 FastAPI 不在校验层拦截，统一走 400）
    if not files:
        raise HTTPException(status_code=400, detail="未上传任何文件")

    # 2. 数量超限
    if len(files) > MAX_FILES_PER_JOB:
        raise HTTPException(
            status_code=400,
            detail=f"文件数量超过限制（最多 {MAX_FILES_PER_JOB} 个）",
        )

    # 3 & 4: 逐文件校验扩展名 + 大小（先校验后落盘，避免残留文件）
    # 大小限制用分块读取：超限即中止，避免超大文件整读进内存（P2）
    checked: list[tuple[UploadFile, str, bytes]] = []  # (UploadFile, 消毒名, 内容)
    seen_names: set[str] = set()  # 同任务内重名文件拒绝（P2：防静默覆盖）
    for uf in files:
        filename = Path(uf.filename or "unnamed").name  # 消毒路径穿越
        if filename in seen_names:
            raise HTTPException(status_code=400, detail=f"文件重名: {filename}")
        seen_names.add(filename)
        ext = Path(filename).suffix.lower()
        if ext not in _ALLOWED_EXTS:
            raise HTTPException(status_code=400, detail=f"不支持的文件格式: {ext}")
        limit = MAX_UPLOAD_MB * 1024 * 1024
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await uf.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise HTTPException(
                    status_code=413,
                    detail=f"文件过大: {filename}（最大 {MAX_UPLOAD_MB} MB）",
                )
            chunks.append(chunk)
        checked.append((uf, filename, b"".join(chunks)))

    # 5. fields 校验
    try:
        fields_list = json.loads(fields)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="fields 不是合法 JSON") from exc
    try:
        validated_fields = validate_fields(fields_list)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 先创建任务拿到真实 job.id，再用它命名目录，保证 uploads/<job_id>/ 与返回一致
    files_meta = [(filename, "") for _, filename, _ in checked]
    jm = get_job_manager()
    job = jm.create_job(files=files_meta, fields=validated_fields)

    # P1：写盘异常时清理残留（删目录 + 任务置 failed），不留孤儿 QUEUED job
    upload_dir = UPLOAD_DIR / job.id
    try:
        upload_dir.mkdir(parents=True, exist_ok=True)
        for (_, filename, content), finfo in zip(checked, job.files, strict=False):
            dest = upload_dir / filename
            dest.write_bytes(content)
            finfo.path = str(dest.resolve())  # 让 pipeline 用真实路径读取
    except OSError as exc:
        import shutil

        shutil.rmtree(upload_dir, ignore_errors=True)
        job.status = JobStatus.FAILED
        job.error = f"文件保存失败: {exc}"
        raise HTTPException(status_code=500, detail="文件保存失败") from exc

    background_tasks.add_task(jm.process, job.id)

    return {"job_id": job.id, "status": "queued"}


@router.get("/{job_id}")
async def get_job(job_id: str):
    try:
        job = get_job_manager().get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    return job.to_dict()


@router.get("/{job_id}/result")
async def get_job_result(job_id: str):
    try:
        job = get_job_manager().get_job(job_id)
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
async def get_job_excel(job_id: str):
    """下载任务生成的 Excel 文件。幂等：excel_path 已存在且文件在则复用。"""
    try:
        job = get_job_manager().get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    if job.status is not JobStatus.SUCCEEDED:
        raise HTTPException(status_code=404, detail="任务未完成或失败，无法生成 Excel")

    if job.excel_path and Path(job.excel_path).is_file():
        path = Path(job.excel_path)
    else:
        try:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            path = ExcelBuilder().build(job, OUTPUT_DIR / f"{job.id}.xlsx")
        except OSError as exc:
            raise HTTPException(status_code=500, detail="Excel 生成失败") from exc
        job.excel_path = str(path)

    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{job_id}.xlsx",
    )
