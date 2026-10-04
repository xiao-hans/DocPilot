"""租户配额：默认不限、管理员设值、拒绝时不留残迹（FR-042 的配额强制那一半）。

判据要能失败，所以每条都盯着"没有悄悄放宽"：默认形态必须与本轮之前完全一致；被拒的上传必须
真的什么都没留下（文档、版本、任务、对象一个都不多）；上限是含等号的，正好用完不该被拒。
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import func, select

from app.contexts.quota import service
from app.contexts.quota.domain import TenantQuota
from app.contexts.quota.service import MAX_LIMIT
from app.db import SessionLocal
from app.models.access import AuditEvent
from app.models.document import Document, DocumentVersion
from app.models.persistent_job import PersistentJob
from app.models.schema import INVOICE_TEMPLATE

FIELDS = json.dumps(INVOICE_TEMPLATE)
SERIAL = "01100200011100045239"


def _base(workspace: dict) -> str:
    return f"/api/v1/tenants/{workspace['tenant_id']}"


def _upload(workspace: dict, pdf: Path, filename: str | None = None):
    name = filename or pdf.name
    return workspace["client"].post(
        f"{_base(workspace)}/jobs",
        headers={"X-CSRF-Token": workspace["csrf"]},
        files=[("files", (name, pdf.read_bytes(), "application/pdf"))],
        data={"fields": FIELDS},
    )


def _set_quota(workspace: dict, **payload):
    return workspace["client"].put(
        f"{_base(workspace)}/quota",
        headers={"X-CSRF-Token": workspace["csrf"]},
        json=payload,
    )


def _counts(tenant_id: str) -> tuple[int, int, int]:
    with SessionLocal() as db:
        return (
            db.scalar(select(func.count(Document.id)).where(Document.tenant_id == tenant_id)) or 0,
            db.scalar(
                select(func.count(DocumentVersion.id)).where(
                    DocumentVersion.tenant_id == tenant_id
                )
            )
            or 0,
            db.scalar(
                select(func.count(PersistentJob.id)).where(PersistentJob.tenant_id == tenant_id)
            )
            or 0,
        )


def test_no_row_means_unlimited_and_uploads_still_work(owner_workspace, make_invoice_pdf):
    body = owner_workspace["client"].get(
        f"{_base(owner_workspace)}/quota", headers={"X-CSRF-Token": owner_workspace["csrf"]}
    ).json()
    assert body["unlimited"] is True
    assert body["limits"] == {"max_documents": 0, "max_storage_bytes": 0}
    assert body["remaining"] == {"max_documents": None, "max_storage_bytes": None}

    assert _upload(owner_workspace, make_invoice_pdf(SERIAL)).status_code == 201
    with SessionLocal() as db:
        assert db.scalar(select(func.count(TenantQuota.tenant_id))) == 0


def test_document_cap_rejects_without_leaving_residue(owner_workspace, make_invoice_pdf):
    assert _upload(owner_workspace, make_invoice_pdf(SERIAL)).status_code == 201
    before = _counts(owner_workspace["tenant_id"])
    assert _set_quota(owner_workspace, max_documents=1, max_storage_bytes=0).status_code == 200

    rejected = _upload(owner_workspace, make_invoice_pdf("01100200011100045240"))
    assert rejected.status_code == 409, rejected.text
    assert "quota_documents" in rejected.json()["detail"]
    # 被拒的请求必须一条痕迹都不留：没有新文档、没有新版本、没有新任务
    assert _counts(owner_workspace["tenant_id"]) == before

    # 把上限放开一格，同一份请求就该通过——证明拦它的是数字而不是写死的拒绝
    assert _set_quota(owner_workspace, max_documents=2, max_storage_bytes=0).status_code == 200
    assert _upload(owner_workspace, make_invoice_pdf("01100200011100045241")).status_code == 201


def test_storage_cap_is_inclusive_and_counts_bytes(owner_workspace, make_invoice_pdf):
    pdf = make_invoice_pdf(SERIAL)
    assert _upload(owner_workspace, pdf).status_code == 201
    with SessionLocal() as db:
        used = db.scalar(
            select(func.sum(DocumentVersion.size_bytes)).where(
                DocumentVersion.tenant_id == owner_workspace["tenant_id"]
            )
        )
    size = len(pdf.read_bytes())

    # 正好用完：允许（上限含等号）
    assert _set_quota(owner_workspace, max_documents=0, max_storage_bytes=used + size).status_code == 200
    assert _upload(owner_workspace, pdf, "second.pdf").status_code == 201

    # 再要一格就不行
    rejected = _upload(owner_workspace, pdf, "third.pdf")
    assert rejected.status_code == 409 and "quota_storage" in rejected.json()["detail"]


def test_limits_are_validated_before_they_are_stored(owner_workspace):
    assert _set_quota(owner_workspace, max_documents=-1, max_storage_bytes=0).status_code == 422
    assert _set_quota(owner_workspace, max_documents=0, max_storage_bytes=-5).status_code == 422
    assert (
        _set_quota(owner_workspace, max_documents=MAX_LIMIT + 1, max_storage_bytes=0).status_code
        == 422
    )
    with SessionLocal() as db:
        assert db.scalar(select(func.count(TenantQuota.tenant_id))) == 0, "被拒的设置不该留下半行配置"


def test_role_matrix_and_tenant_scoping(owner_workspace, other_workspace, make_invoice_pdf, member_of):
    assert _upload(other_workspace, make_invoice_pdf(SERIAL)).status_code == 201

    member = member_of("quota-member@example.com", owner_workspace["tenant_id"], role="member")
    assert (
        member["client"].get(
            f"{_base(owner_workspace)}/quota", headers={"X-CSRF-Token": member["csrf"]}
        ).status_code
        == 200
    )
    assert (
        member["client"].put(
            f"{_base(owner_workspace)}/quota",
            headers={"X-CSRF-Token": member["csrf"]},
            json={"max_documents": 1, "max_storage_bytes": 0},
        ).status_code
        == 403
    )
    reviewer = member_of("quota-reviewer@example.com", owner_workspace["tenant_id"], role="reviewer")
    assert (
        reviewer["client"].get(
            f"{_base(owner_workspace)}/quota", headers={"X-CSRF-Token": reviewer["csrf"]}
        ).status_code
        == 403
    )

    # 给 A 租户收紧，不该影响 B 租户的上传
    assert _set_quota(owner_workspace, max_documents=0, max_storage_bytes=1).status_code == 200
    assert _upload(other_workspace, make_invoice_pdf("01100200011100045242")).status_code == 201


def test_setting_quota_is_audited_with_the_two_numbers(owner_workspace):
    body = _set_quota(owner_workspace, max_documents=5, max_storage_bytes=1024)
    assert body.status_code == 200
    assert body.json()["limits"] == {"max_documents": 5, "max_storage_bytes": 1024}
    assert body.json()["unlimited"] is False
    with SessionLocal() as db:
        event = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "tenant.quota_updated")
        ).one()
    assert event.metadata_ == {"max_documents": 5, "max_storage_bytes": 1024}


def test_usage_is_recomputed_not_stored(owner_workspace, make_invoice_pdf):
    assert _upload(owner_workspace, make_invoice_pdf(SERIAL)).status_code == 201
    first = service.usage(SessionLocal(), tenant_id=owner_workspace["tenant_id"])
    assert _set_quota(owner_workspace, max_documents=9, max_storage_bytes=0).status_code == 200
    again = service.usage(SessionLocal(), tenant_id=owner_workspace["tenant_id"])
    assert first == again
    with SessionLocal() as db:
        row = db.get(TenantQuota, owner_workspace["tenant_id"])
        # 配额行里只有上限两个数，用量永远现算：存一份就会与业务表漂移
        assert set(row.__table__.columns.keys()) == {
            "tenant_id",
            "max_documents",
            "max_storage_bytes",
            "set_by",
            "updated_at",
        }
