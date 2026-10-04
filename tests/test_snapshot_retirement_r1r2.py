"""快照退役 R1+R2（`docs/20 §3`）：规范化列存在，且与快照同事务镜像。

R1 的判据是"没回填"——新列必须可空，历史行不能被迁移编出值来。R2 的判据是"镜像一致"——
同一份 `Job` 派生出的快照与新列必须逐键相等，不一致就是当场可发现的 bug，而不是等 R4 对账
才发现的漂移。本文件不删快照、也不切读路径（R5/R6 另有门禁）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.contexts.processing.domain import JobAttempt
from app.contexts.templates.domain import TemplateVersion
from app.db import SessionLocal
from app.models.document import Document, DocumentVersion
from app.models.job import FileInfo, Job, JobStatus, Progress
from app.models.persistent_job import PersistentJob
from app.models.schema import FieldDefinition, INVOICE_TEMPLATE
from app.services.job_repository import JobRepository

FIELDS = [FieldDefinition(**item) for item in json.loads(json.dumps(INVOICE_TEMPLATE))]


def _templates_base(workspace: dict) -> str:
    return f"/api/v1/tenants/{workspace['tenant_id']}/templates"


def _upload(workspace: dict, pdf) -> str:
    response = workspace["client"].post(
        f"/api/v1/tenants/{workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": workspace["csrf"]},
        files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))],
        data={"fields": json.dumps([item.model_dump() for item in FIELDS])},
    )
    assert response.status_code == 201, response.text
    return response.json()["job_id"]


def test_r1_columns_start_empty_for_rows_created_before_the_migration(owner_workspace):
    """历史行的新列必须是 NULL：可空 = 还没回填，不允许用默认值猜历史数据的意思。"""
    now = datetime.now(timezone.utc)
    legacy_id = uuid.uuid4().hex
    with SessionLocal() as db:
        db.add(
            PersistentJob(
                id=legacy_id,
                tenant_id=owner_workspace["tenant_id"],
                owner_id=owner_workspace["user_id"],
                status="succeeded",
                created_at=now,
                attempt_count=1,
                snapshot={"fields": [], "files": [], "status": "succeeded", "progress": "done"},
            )
        )
        db.commit()
        row = db.get(PersistentJob, legacy_id)
        assert row.field_definitions is None and row.progress is None


def test_r2_dual_write_mirrors_the_snapshot_key_for_key(owner_workspace, make_invoice_pdf):
    job_id = _upload(owner_workspace, make_invoice_pdf("01100200011100045239"))
    with SessionLocal() as db:
        row = db.get(PersistentJob, job_id)
        snapshot = row.snapshot
        assert row.progress == snapshot["progress"], "进度与快照必须同事务派生"
        assert row.field_definitions == snapshot["fields"]

        version = db.scalar(select(DocumentVersion).where(DocumentVersion.job_id == job_id))
        assert version is not None
        snap_file = snapshot["files"][0]
        assert version.page_count == snap_file["page_count"]
        assert version.text_chars == snap_file["chars"]
        assert version.ocr_applied == snap_file["ocr_applied"]
        # 没有错误时列留在 NULL，而不是空串：空串会被读成"有一条空错误"。
        assert version.parse_error == (snap_file["error"] or None)


def test_r2_warnings_land_on_the_attempt_that_produced_them(owner_workspace):
    """告警属于某一次尝试：写在任务级快照里会被下一次尝试覆盖，这是 R1/R2 要修的那类归属错误。"""
    now = datetime.now(timezone.utc)
    job_id = uuid.uuid4().hex
    repository = JobRepository()
    job = Job(
        id=job_id,
        fields=FIELDS,
        created_at=now.timestamp(),
        files=[FileInfo(name="a.pdf", storage_key="", path="")],
        status=JobStatus.SUCCEEDED,
        progress=Progress.DONE,
        result=None,
        warnings=["第 2 页文本层缺失，已走 OCR"],
        error=None,
        excel_path=None,
        tenant_id=owner_workspace["tenant_id"],
        owner_id=owner_workspace["user_id"],
        attempt_count=1,
    )
    repository.save(job, owner_workspace["tenant_id"], owner_workspace["user_id"])
    with SessionLocal() as db:
        db.add(
            JobAttempt(
                id=uuid.uuid4().hex,
                job_id=job_id,
                tenant_id=owner_workspace["tenant_id"],
                attempt_no=1,
                status="succeeded",
                started_at=now,
                finished_at=now,
                engine="pytest",
                parse_modes=["text"],
                error=None,
            )
        )
        db.commit()

    repository.save(job, owner_workspace["tenant_id"], owner_workspace["user_id"])
    with SessionLocal() as db:
        attempt = db.scalar(select(JobAttempt).where(JobAttempt.job_id == job_id))
        assert attempt.warnings == ["第 2 页文本层缺失，已走 OCR"]
        # 内存任务的 files[].storage_key 为空：不该因此误伤任何版本行
        assert db.scalar(select(DocumentVersion).limit(1)) is None


def test_r2_template_version_job_keeps_a_copy_that_agrees_with_the_frozen_fields(
    owner_workspace, make_invoice_pdf
):
    """docs/20 §2 的字段规则：`template_version_id` 非空就只信模板版本。

    双写因此必须与冻结字段逐键相等——否则 R5 切读后，"以模板版本为准"这条规则会掩盖
    列里已经漂移的值，而 R4 对账是唯一能发现它的地方，不该等到那一步。
    """
    created = owner_workspace["client"].post(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/templates",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        json={
            "name": "退役核对模板",
            "fields": [item.model_dump() for item in FIELDS],
        },
    )
    assert created.status_code == 201, created.text
    published = owner_workspace["client"].post(
        f"{_templates_base(owner_workspace)}/{created.json()['id']}/publish",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
    )
    assert published.status_code == 201, published.text
    version_id = published.json()["template_version_id"]

    response = owner_workspace["client"].post(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        files=[("files", (make_invoice_pdf().name, make_invoice_pdf().read_bytes(), "application/pdf"))],
        data={"template_version_id": version_id},
    )
    assert response.status_code == 201, response.text

    with SessionLocal() as db:
        row = db.get(PersistentJob, response.json()["job_id"])
        frozen = db.get(TemplateVersion, version_id).fields
        assert row.template_version_id == version_id
        assert row.field_definitions == row.snapshot["fields"] == frozen


def test_r2_parse_columns_separate_the_failed_file_in_a_partial_batch(
    owner_workspace, make_invoice_pdf, tmp_path
):
    """部分成功场景：失败文件必须有非空 `parse_error`，成功文件必须留在 NULL。

    这条是逐文件列存在的理由：任务级 status=partial 说不出"哪一个坏了、为什么坏"。
    """
    good = make_invoice_pdf()
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 broken content")

    response = owner_workspace["client"].post(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        files=[
            ("files", (good.name, good.read_bytes(), "application/pdf")),
            ("files", (broken.name, broken.read_bytes(), "application/pdf")),
        ],
        data={"fields": json.dumps([item.model_dump() for item in FIELDS])},
    )
    assert response.status_code == 201, response.text
    job_id = response.json()["job_id"]

    with SessionLocal() as db:
        row = db.get(PersistentJob, job_id)
        assert row.status == "partial" and row.progress == row.snapshot["progress"]
        snap_files = {item["name"]: item for item in row.snapshot["files"]}
        by_name = {
            version.original_filename: version
            for version in db.scalars(select(DocumentVersion).where(DocumentVersion.job_id == job_id))
        }
        assert set(by_name) == set(snap_files)

        for name, version in by_name.items():
            snapshot_entry = snap_files[name]
            assert version.page_count == snapshot_entry["page_count"]
            assert version.text_chars == snapshot_entry["chars"]
            assert version.ocr_applied == snapshot_entry["ocr_applied"]
            assert version.parse_error == (snapshot_entry["error"] or None)

        assert by_name[good.name].parse_error is None
        assert by_name[broken.name].parse_error, "失败文件的错误原因必须落在版本行上"


def test_r2_warnings_attribute_to_the_attempt_that_ran(owner_workspace):
    """重试之后旧尝试的告警不能被覆盖：`warnings` 按 `attempt_no` 归属，不是任务级最新值。"""
    now = datetime.now(timezone.utc)
    job_id = uuid.uuid4().hex
    repository = JobRepository()

    def _job(attempt_count: int, warnings: list[str]) -> Job:
        return Job(
            id=job_id,
            fields=FIELDS,
            created_at=now.timestamp(),
            files=[FileInfo(name="a.pdf", storage_key="", path="")],
            status=JobStatus.SUCCEEDED,
            progress=Progress.DONE,
            result=None,
            warnings=warnings,
            error=None,
            excel_path=None,
            tenant_id=owner_workspace["tenant_id"],
            owner_id=owner_workspace["user_id"],
            attempt_count=attempt_count,
        )

    repository.save(_job(1, []), owner_workspace["tenant_id"], owner_workspace["user_id"])
    with SessionLocal() as db:
        for attempt_no in (1, 2):
            db.add(
                JobAttempt(
                    id=uuid.uuid4().hex,
                    job_id=job_id,
                    tenant_id=owner_workspace["tenant_id"],
                    attempt_no=attempt_no,
                    status="succeeded",
                    started_at=now,
                    finished_at=now,
                    engine="pytest",
                    parse_modes=["text"],
                    error=None,
                )
            )
        db.commit()

    repository.save(_job(1, ["第一次的告警"]), owner_workspace["tenant_id"], owner_workspace["user_id"])
    repository.save(_job(2, ["第二次的告警"]), owner_workspace["tenant_id"], owner_workspace["user_id"])
    with SessionLocal() as db:
        attempts = {
            attempt.attempt_no: attempt.warnings
            for attempt in db.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id))
        }
    assert attempts == {1: ["第一次的告警"], 2: ["第二次的告警"]}


def test_r2_progress_moves_with_status_in_the_same_transaction(owner_workspace, make_invoice_pdf):
    """status 与 progress 不可分开更新：分开了就会进度说出状态没承认的话。"""
    job_id = _upload(owner_workspace, make_invoice_pdf("01100200011100045239"))
    with SessionLocal() as db:
        row = db.get(PersistentJob, job_id)
        assert row.status == "succeeded" and row.progress == "done"
        repository = JobRepository()
        record = db.get(PersistentJob, job_id)
        record.progress = "lying"
        db.commit()
    # 手工制造的偏差必须能被下一次保存覆盖回一致——镜像来自同一份 Job，不来自列本身
    with SessionLocal() as db:
        job = repository.get(job_id, owner_workspace["tenant_id"])
    repository.save(job, owner_workspace["tenant_id"], owner_workspace["user_id"])
    with SessionLocal() as db:
        row = db.get(PersistentJob, job_id)
        assert row.progress == row.snapshot["progress"] == "done"
