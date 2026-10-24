"""快照退役 R3+R4（`docs/20 §3`）：回填幂等、对账能失败。

判据的形状是"能失败"：对账脚本只有在真的会报出漂移时才值钱，所以这里每条正向都配一条
被人为破坏的反向。不可判定与"仅存在于快照"两类必须阻塞 R6——这是 `docs/20 §4` 那条硬边界
唯一的执行者。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.contexts.processing.domain import JobAttempt
from app.contexts.templates.domain import Template, TemplateVersion
from app.platform.db import SessionLocal
from app.models.document import Document, DocumentVersion
from app.models.persistent_job import PersistentJob
from app.models.schema import INVOICE_TEMPLATE
from app.contexts.processing.retirement import backfill, reconcile

FIELDS = json.loads(json.dumps(INVOICE_TEMPLATE))
NOW = datetime.now(timezone.utc)


def _snapshot(*, files, warnings=None, error=None, attempt_count=1, progress="done"):
    return {
        "fields": FIELDS,
        "files": files,
        "status": "succeeded",
        "progress": progress,
        "created_at": NOW.timestamp(),
        "result": None,
        "warnings": warnings or [],
        "error": error,
        "template_version_id": None,
        "idempotency_key": None,
        "attempt_count": attempt_count,
        "finished_at": None,
    }


def _file(name="a.pdf", *, storage_key="auto", page_count=3, chars=420, ocr_applied=False, error=None):
    """一条快照 `files[]` 记录。`storage_key="auto"` 生成唯一键；`""` 模拟没有键的历史行。"""
    entry = {
        "name": name,
        "page_count": page_count,
        "chars": chars,
        "ocr_applied": ocr_applied,
        "error": error,
    }
    if storage_key == "auto":
        entry["storage_key"] = f"t/{uuid.uuid4().hex}.pdf"
    elif storage_key:
        entry["storage_key"] = storage_key
    return entry


def _seed(owner_workspace, snapshot, *, with_attempt=True, drop_version_for=()):
    """造一条"迁移之前"的任务：只有快照，规范化列全空。

    默认按快照 `files[]` 逐条建版本行（有键就用同一个键），因为 R3/R4 的关联键就是它。
    `drop_version_for` 点名某些条目不建行，用来造"仅存在于快照"。
    """
    job_id = uuid.uuid4().hex
    dropped = set(drop_version_for)
    with SessionLocal() as db:
        db.add(
            PersistentJob(
                id=job_id,
                tenant_id=owner_workspace["tenant_id"],
                owner_id=owner_workspace["user_id"],
                status="succeeded",
                created_at=NOW,
                attempt_count=snapshot["attempt_count"],
                snapshot=snapshot,
            )
        )
        # 任务行先落库：`job_attempts.job_id` 与 `document_versions.job_id` 都指向它，
        # 同一批 flush 里依赖顺序不由 add() 的先后决定。
        db.commit()
        for entry in snapshot["files"]:
            if entry["name"] in dropped:
                continue
            document_id = uuid.uuid4().hex
            db.add(
                Document(
                    id=document_id,
                    tenant_id=owner_workspace["tenant_id"],
                    owner_id=owner_workspace["user_id"],
                    created_at=NOW,
                )
            )
            db.add(
                DocumentVersion(
                    id=uuid.uuid4().hex,
                    document_id=document_id,
                    tenant_id=owner_workspace["tenant_id"],
                    job_id=job_id,
                    revision=1,
                    original_filename=entry["name"],
                    storage_key=entry.get("storage_key") or f"{job_id}/{uuid.uuid4().hex}.pdf",
                    checksum="0" * 64,
                    media_type="application/pdf",
                    size_bytes=1024,
                    created_at=NOW,
                )
            )
        if with_attempt:
            db.add(
                JobAttempt(
                    id=uuid.uuid4().hex,
                    job_id=job_id,
                    tenant_id=owner_workspace["tenant_id"],
                    attempt_no=snapshot["attempt_count"],
                    status="succeeded",
                    started_at=NOW,
                    finished_at=NOW,
                    engine="pytest",
                    parse_modes=["text"],
                    error=snapshot.get("error"),
                )
            )
        db.commit()
    return job_id


def _version_rows(job_id):
    with SessionLocal() as db:
        return list(db.scalars(select(DocumentVersion).where(DocumentVersion.job_id == job_id)))


def test_r3_backfills_a_row_created_before_the_migration(owner_workspace):
    job_id = _seed(owner_workspace, _snapshot(files=[_file()], warnings=["第 2 页走 OCR"]))
    report = backfill(SessionLocal)

    assert report.fields_filled == 1 and report.progress_filled == 1
    assert report.file_facts_filled == 1 and report.warnings_filled == 1
    assert reconcile(SessionLocal).clean, "回填后逐键对账必须干净，否则 R6 的证据不成立"
    version = _version_rows(job_id)[0]
    assert (version.page_count, version.text_chars, version.ocr_applied) == (3, 420, False)


def test_r3_is_idempotent_and_dry_run_writes_nothing(owner_workspace):
    _seed(owner_workspace, _snapshot(files=[_file()], warnings=["缺文本层"]))
    assert backfill(SessionLocal, dry_run=True).total_filled == 4
    with SessionLocal() as db:
        row = db.scalars(select(PersistentJob)).one()
        assert row.progress is None and row.field_definitions is None, "预演不该落库一行"

    backfill(SessionLocal)
    second = backfill(SessionLocal)
    assert second.total_filled == 0, "已有非空值即跳过，重跑不重复写"


def test_r4_catches_drift_introduced_after_the_backfill(owner_workspace):
    job_id = _seed(owner_workspace, _snapshot(files=[_file()]))
    backfill(SessionLocal)
    with SessionLocal() as db:
        row = db.scalars(select(DocumentVersion).where(DocumentVersion.job_id == job_id)).one()
        row.page_count = 99
        db.commit()
    report = reconcile(SessionLocal)
    assert not report.clean
    assert any(f"job={job_id}" in line and "page_count" in line for line in report.drift)


def test_r4_lists_rows_that_exist_only_in_the_snapshot(owner_workspace):
    """阶段 C 之前的任务没有版本行：这类行必须被单列出来，而不是被当成"对账通过"。"""
    job_id = _seed(
        owner_workspace,
        _snapshot(files=[_file(), _file(name="b.pdf")]),
        drop_version_for=["b.pdf"],
    )
    backfill(SessionLocal)
    report = reconcile(SessionLocal)
    assert not report.clean
    assert any(f"job={job_id}" in line and "no_version_row" in line for line in report.snapshot_only)


def test_r3_matches_same_named_files_by_storage_key(owner_workspace):
    """快照带 `storage_key`，所以"一次上传两个同名文件"是可判定的：按文件名反而会配错。"""
    first = _file(name="same.pdf", page_count=3)
    second = _file(name="same.pdf", page_count=7, chars=99)
    job_id = _seed(owner_workspace, _snapshot(files=[first, second]))
    report = backfill(SessionLocal)
    assert report.ambiguous_files == 0
    assert report.file_facts_filled == 2
    assert reconcile(SessionLocal).clean
    with SessionLocal() as db:
        rows = {
            row.storage_key: row
            for row in db.scalars(select(DocumentVersion).where(DocumentVersion.job_id == job_id))
        }
    assert rows[first["storage_key"]].page_count == 3
    assert rows[second["storage_key"]].page_count == 7


def test_r4_treats_keyless_duplicate_names_as_undecidable(owner_workspace):
    """没有 `storage_key` 的历史行才真的没答案：同名两行既不猜也不写。"""
    snapshot = _snapshot(
        files=[
            _file(name="same.pdf", storage_key="", page_count=3),
            _file(name="same.pdf", storage_key="", page_count=7),
        ]
    )
    job_id = _seed(owner_workspace, snapshot)
    report_backfill = backfill(SessionLocal)
    assert report_backfill.ambiguous_files == 2
    assert report_backfill.file_facts_filled == 0, "不可判定时一个值都不该写"
    report = reconcile(SessionLocal)
    assert any(f"job={job_id}" in line for line in report.ambiguous)


def test_r4_requires_an_attempt_row_for_warnings(owner_workspace):
    job_id = _seed(owner_workspace, _snapshot(files=[_file()], warnings=["缺文本层"]), with_attempt=False)
    backfill(SessionLocal)
    report = reconcile(SessionLocal)
    assert any(f"job={job_id}" in line and "warnings" in line for line in report.snapshot_only)


def test_r4_prefers_the_frozen_template_fields(owner_workspace):
    """`docs/20 §2` 的读规则：`template_version_id` 非空就只信模板版本。

    正向：冻结字段与快照一致时对账干净（列里那份副本不参与判定，也不该被误报）。
    反向：模板版本被改成另一套字段后，同一份数据必须报漂移——否则"只信模板版本"会
    把列里已经漂移的值盖住。
    """
    job_id = _seed(owner_workspace, _snapshot(files=[_file()]))
    with SessionLocal() as db:
        template = Template(
            id=uuid.uuid4().hex,
            tenant_id=owner_workspace["tenant_id"],
            name="退役核对模板",
            draft_fields=FIELDS,
            created_at=NOW,
            updated_at=NOW,
        )
        version = TemplateVersion(
            id=uuid.uuid4().hex,
            template_id=template.id,
            tenant_id=owner_workspace["tenant_id"],
            revision=1,
            fields=FIELDS,
            published_at=NOW,
        )
        db.add_all([template, version])
        db.commit()
        # 复合外键 (tenant_id, template_version_id) 要求版本行先可见，再改任务上的引用。
        db.get(PersistentJob, job_id).template_version_id = version.id
        db.commit()

    backfill(SessionLocal)
    assert reconcile(SessionLocal).clean

    with SessionLocal() as db:
        db.scalar(select(TemplateVersion)).fields = [dict(FIELDS[0], name="另一个字段")]
        db.commit()
    report = reconcile(SessionLocal)
    assert not report.clean
    assert any(f"job={job_id}" in line and "fields" in line for line in report.drift)
