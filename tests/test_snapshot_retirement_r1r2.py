"""快照退役留下的规范化写入侧保证（R1/R2 列语义在 R6 删列后仍然成立）。

R1 的列判据（可空、不猜历史值）与 R2 的写入判据（逐文件事实、告警归属尝试、进度与
状态同事务）都活在规范化列上，不依赖已删除的 `snapshot` 列；本文件只保留这些在
R6 之后仍然成立的部分。双写镜像与"历史行列必须为 NULL"两条随列一起退役。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.contexts.processing.domain import JobAttempt
from app.contexts.templates.domain import TemplateVersion
from app.platform.db import SessionLocal
from app.models.document import DocumentVersion
from app.models.job import FileInfo, Job, JobStatus, Progress
from app.models.persistent_job import PersistentJob
from app.models.schema import FieldDefinition, INVOICE_TEMPLATE
from app.contexts.processing.repository import JobRepository

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

    列值必须与冻结字段逐键相等——否则"以模板版本为准"这条规则会掩盖列里已经漂移的值。
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
        assert row.field_definitions == frozen


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
        assert row.status == "partial" and row.progress == "done"
        by_name = {
            version.original_filename: version
            for version in db.scalars(select(DocumentVersion).where(DocumentVersion.job_id == job_id))
        }
        assert set(by_name) == {good.name, broken.name}

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


def test_r5_makes_the_progress_column_the_read_source(owner_workspace, make_invoice_pdf):
    """R5 切读、R6 删列之后，`progress` 列是读路径的唯一源。

    这条替代了 R2 时期那条"手工改列会被下一次保存覆盖回来"的断言：那时列是镜像、快照是
    源；换源之后改列就等于改可见进度。方向反了，判据也就要重写，否则测试会去保护一个
    已经不存在的设计。
    """
    job_id = _upload(owner_workspace, make_invoice_pdf("01100200011100045239"))
    with SessionLocal() as db:
        row = db.get(PersistentJob, job_id)
        assert row.status == "succeeded" and row.progress == "done"
        row.progress = "parsing"
        db.commit()

    with SessionLocal() as db:
        job = JobRepository().get(job_id, owner_workspace["tenant_id"])
    assert job.progress.value == "parsing", "读路径只认 progress 列"
