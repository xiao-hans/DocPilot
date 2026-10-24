"""内容签名核对：读侧再算一次哈希（FR-010 的"内容签名"那一半）。

这里最要紧的是判据能不能失败：等长改写必须被抓到（字节数没变，只有摘要变），对象缺失必须
报成 `missing` 而不是 `mismatched`（两者对运维是完全不同的动作）。核对本身只读，测试也断言
它真的没碰对象。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.contexts.documents import integrity
from app.platform.db import SessionLocal
from app.models.access import AuditEvent
from app.models.document import DocumentVersion
from app.models.schema import INVOICE_TEMPLATE
from app.adapters.storage.document_storage import resolve_storage_path

SERIAL = "01100200011100045239"
FIELDS = json.dumps(INVOICE_TEMPLATE)


def _upload_and_locate(workspace: dict, pdf: Path) -> tuple[str, str]:
    response = workspace["client"].post(
        f"/api/v1/tenants/{workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": workspace["csrf"]},
        files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))],
        data={"fields": FIELDS},
    )
    assert response.status_code == 201, response.text
    job_id = response.json()["job_id"]
    with SessionLocal() as db:
        version = db.scalar(
            select(DocumentVersion).where(DocumentVersion.job_id == job_id)
        )
        assert version is not None, "上传后应留下版本记录"
        return version.document_id, version.storage_key


def _check(workspace: dict, document_id: str) -> dict:
    response = workspace["client"].post(
        f"/api/v1/tenants/{workspace['tenant_id']}/documents/{document_id}/integrity/check",
        headers={"X-CSRF-Token": workspace["csrf"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_clean_after_upload(owner_workspace, sandbox, make_invoice_pdf):
    pdf = make_invoice_pdf(SERIAL)
    document_id, storage_key = _upload_and_locate(owner_workspace, pdf)
    body = _check(owner_workspace, document_id)
    assert body["checked"] == 1 and body["clean"] is True
    item = body["items"][0]
    assert item["status"] == integrity.STATUS_MATCHED
    assert item["actual_size"] == item["recorded_size"] and item["size_matches"] is True
    assert item["actual_checksum"] == hashlib.sha256(pdf.read_bytes()).hexdigest()
    # 只读承诺：核对完，对象还是那串字节
    target = resolve_storage_path(storage_key, sandbox)
    assert target.read_bytes() == pdf.read_bytes()


def test_equal_length_overwrite_is_caught(owner_workspace, sandbox, make_invoice_pdf):
    """等长改写——备份回滚或键被覆写的典型形状，字节数拦不住，只能靠摘要。"""
    pdf = make_invoice_pdf(SERIAL)
    document_id, storage_key = _upload_and_locate(owner_workspace, pdf)
    target = resolve_storage_path(storage_key, sandbox)
    original = bytearray(target.read_bytes())
    original[-1] = original[-1] ^ 0x01
    target.write_bytes(bytes(original))

    body = _check(owner_workspace, document_id)
    assert body["clean"] is False
    item = body["items"][0]
    assert item["status"] == integrity.STATUS_MISMATCHED
    assert item["size_matches"] is True, "这条用例必须只靠摘要差异被抓到"
    assert item["recorded_checksum"] != item["actual_checksum"]


def test_missing_object_is_not_reported_as_mismatch(owner_workspace, sandbox, make_invoice_pdf):
    pdf = make_invoice_pdf(SERIAL)
    document_id, storage_key = _upload_and_locate(owner_workspace, pdf)
    resolve_storage_path(storage_key, sandbox).unlink()

    body = _check(owner_workspace, document_id)
    item = body["items"][0]
    assert item["status"] == integrity.STATUS_MISSING
    assert item["actual_checksum"] is None
    assert body["counts"] == {integrity.STATUS_MISSING: 1}


def test_unreadable_object_carries_the_error_class(owner_workspace, make_invoice_pdf):
    """桶在、对象读不动（权限/端点抖动）要和"对象不在"分开报。"""
    document_id, _ = _upload_and_locate(owner_workspace, make_invoice_pdf(SERIAL))
    import app.adapters.storage.object_storage as object_storage

    class Exploding:
        def read_bytes(self, key: str) -> bytes:
            raise object_storage.StorageError("端点不可达")

    with SessionLocal() as db:
        version = db.scalar(
            select(DocumentVersion).where(DocumentVersion.document_id == document_id)
        )
    health = integrity.check_version(Exploding(), version)
    assert health["status"] == integrity.STATUS_UNREADABLE
    assert health["error"] == "StorageError"


def test_roles_and_cross_tenant_visibility(owner_workspace, other_workspace, make_invoice_pdf, member_of):
    document_id, _ = _upload_and_locate(owner_workspace, make_invoice_pdf(SERIAL))

    # 别的租户拿同一个 ID：404，不透露它是否存在于别处
    response = other_workspace["client"].post(
        f"/api/v1/tenants/{other_workspace['tenant_id']}/documents/{document_id}/integrity/check",
        headers={"X-CSRF-Token": other_workspace["csrf"]},
    )
    assert response.status_code == 404

    reviewer = member_of("integrity-reviewer@example.com", owner_workspace["tenant_id"], role="reviewer")
    denied = reviewer["client"].post(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/documents/{document_id}/integrity/check",
        headers={"X-CSRF-Token": reviewer["csrf"]},
    )
    assert denied.status_code == 403


def test_check_writes_an_audit_row(owner_workspace, make_invoice_pdf):
    document_id, _ = _upload_and_locate(owner_workspace, make_invoice_pdf(SERIAL))
    body = _check(owner_workspace, document_id)
    with SessionLocal() as db:
        events = db.scalars(
            select(AuditEvent).where(
                AuditEvent.object_type == "document", AuditEvent.object_id == document_id
            )
        ).all()
    actions = {event.action for event in events}
    assert "document.integrity_checked" in actions
    event = next(item for item in events if item.action == "document.integrity_checked")
    assert event.metadata_["checked"] == body["checked"]
    assert event.metadata_["clean"] is True
