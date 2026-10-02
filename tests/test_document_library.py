"""文档库列表、筛选与元数据（TC-FUNC-026/027/029/030、TC-SEC-015）。"""

from __future__ import annotations

import json
from pathlib import Path

from app.db import SessionLocal
from app.models.access import AuditEvent
from app.models.schema import INVOICE_TEMPLATE

FIELDS = json.dumps(INVOICE_TEMPLATE)


def _upload(workspace: dict, pdf: Path, name: str | None = None):
    return workspace["client"].post(
        f"/api/v1/tenants/{workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": workspace["csrf"]},
        files=[("files", (name or pdf.name, pdf.read_bytes(), "application/pdf"))],
        data={"fields": FIELDS},
    )


def _documents(workspace: dict, **params):
    query = "&".join(f"{key}={value}" for key, value in params.items())
    url = f"/api/v1/tenants/{workspace['tenant_id']}/documents"
    return workspace["client"].get(f"{url}?{query}" if query else url)


def _patch_document(workspace: dict, document_id: str, payload: dict):
    return workspace["client"].patch(
        f"/api/v1/tenants/{workspace['tenant_id']}/documents/{document_id}",
        headers={"X-CSRF-Token": workspace["csrf"]},
        json=payload,
    )


def _single_document_id(workspace: dict) -> str:
    return _documents(workspace).json()["items"][0]["id"]


def test_library_listing_and_filters(owner_workspace: dict, sandbox: Path, make_invoice_pdf) -> None:
    """TC-FUNC-026：组合筛选只返回满足全部条件的本租户文档。"""
    assert _upload(owner_workspace, make_invoice_pdf("01100200011100045239")).status_code == 201
    assert _upload(owner_workspace, make_invoice_pdf("01100200011100045240")).status_code == 201

    listing = _documents(owner_workspace).json()
    assert listing["total"] == 2
    document_id = listing["items"][0]["id"]
    assert listing["items"][0]["versions"][0]["checksum"]
    assert listing["items"][0]["retention_status"] == "active"
    assert listing["items"][0]["result_states"] == ["machine"]
    dumped = json.dumps(listing, ensure_ascii=False)
    assert str(sandbox) not in dumped and "storage_key" not in dumped

    assert _patch_document(owner_workspace, document_id, {"labels": ["待复核", "待复核", "发票"]}).status_code == 200
    assert [item["id"] for item in _documents(owner_workspace, label="发票").json()["items"]] == [document_id]
    assert _documents(owner_workspace, label="不存在").json()["items"] == []

    named = listing["items"][0]["versions"][0]["original_filename"]
    assert _documents(owner_workspace, q=named).json()["total"] == 1
    assert _documents(owner_workspace, q="无此文件.pdf").json()["total"] == 0

    assert _documents(owner_workspace, doc_type="发票模板").json()["total"] == 0
    assert (
        _patch_document(owner_workspace, document_id, {"doc_type": "增值税发票"}).status_code == 200
    )
    assert _documents(owner_workspace, doc_type="增值税发票").json()["total"] == 1

    assert _documents(owner_workspace, limit=1).json()["total"] == 2
    page_one = _documents(owner_workspace, limit=1).json()["items"]
    page_two = _documents(owner_workspace, limit=1, offset=1).json()["items"]
    assert len(page_one) == len(page_two) == 1
    assert page_one[0]["id"] != page_two[0]["id"]

    assert _documents(owner_workspace, retention_status="weird").status_code == 422


def test_document_metadata_validation_and_audit(owner_workspace: dict, sandbox: Path, make_invoice_pdf) -> None:
    """TC-FUNC-029：标签数量/长度与保留状态校验，变更写审计。"""
    assert _upload(owner_workspace, make_invoice_pdf()).status_code == 201
    document_id = _single_document_id(owner_workspace)

    assert _patch_document(owner_workspace, document_id, {}).status_code == 422
    assert _patch_document(owner_workspace, document_id, {"labels": ["x" * 31]}).status_code == 422
    too_many = {"labels": [f"标签{i}" for i in range(11)]}
    assert _patch_document(owner_workspace, document_id, too_many).status_code == 422
    assert _patch_document(owner_workspace, document_id, {"labels": "不是数组"}).status_code == 422
    assert (
        _patch_document(
            owner_workspace, document_id, {"retention_status": "purge_scheduled"}
        ).status_code
        == 200
    )
    assert _patch_document(owner_workspace, document_id, {"retention_status": "nope"}).status_code == 422
    assert _patch_document(owner_workspace, "0" * 32, {"doc_type": "X"}).status_code == 404

    actions = [
        event.action
        for event in SessionLocal().query(AuditEvent).filter(AuditEvent.action == "document.metadata_updated").all()
    ]
    assert len(actions) == 1  # 本用例只有一次成功更新：其余请求都在写审计前被拒绝
    with SessionLocal() as db:
        event = (
            db.query(AuditEvent)
            .filter(AuditEvent.action == "document.metadata_updated")
            .order_by(AuditEvent.created_at.desc())
            .first()
        )
        assert event.metadata_ == {"changed": ["retention_status"]}
        assert str(document_id) == event.object_id


def test_job_listing_is_scoped_filtered_and_path_free(
    owner_workspace: dict, other_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-FUNC-027：任务列表分页与状态过滤，且不泄露存储路径。"""
    ok = _upload(owner_workspace, make_invoice_pdf("01100200011100045239")).json()["job_id"]
    assert _upload(owner_workspace, make_invoice_pdf("01100200011100045240")).status_code == 201

    listing = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs"
    ).json()
    assert listing["total"] == 2
    assert ok in {item["id"] for item in listing["jobs"]}
    assert all(item["status"] == "succeeded" for item in listing["jobs"])
    assert all(item["file_count"] == 1 for item in listing["jobs"])
    assert "storage_key" not in json.dumps(listing)

    page = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs?limit=1"
    ).json()
    assert len(page["jobs"]) == 1 and page["total"] == 2

    foreign = other_workspace["client"].get(
        f"/api/v1/tenants/{other_workspace['tenant_id']}/jobs"
    ).json()
    assert foreign["total"] == 0

    assert owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs?status=failed"
    ).json()["jobs"] == []
    assert (
        owner_workspace["client"].get(
            f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs?limit=500"
        ).status_code
        == 422
    )


def test_pre_normalized_jobs_still_read_from_snapshot(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-FUNC-030：没有规范化结果的历史任务仍按快照读取，新端点返回空而非报错。"""
    from app.contexts.review.domain import ExtractionResult
    from app.models.persistent_job import PersistentJob

    job_id = _upload(owner_workspace, make_invoice_pdf()).json()["job_id"]
    with SessionLocal() as db:
        db.query(ExtractionResult).filter(ExtractionResult.job_id == job_id).delete()
        db.commit()
        assert db.get(PersistentJob, job_id) is not None

    base = f"/api/v1/tenants/{owner_workspace['tenant_id']}"
    assert owner_workspace["client"].get(f"{base}/results?job_id={job_id}").json() == {"results": []}
    legacy = owner_workspace["client"].get(f"{base}/jobs/{job_id}/result").json()
    assert legacy["status"] == "succeeded"
    assert legacy["result"][0]["values"]["发票号码"]
    assert owner_workspace["client"].get(f"{base}/jobs/{job_id}/export?format=csv").status_code == 200


def test_library_isolates_tenants_and_pages_stably(
    owner_workspace: dict, other_workspace: dict, sandbox: Path, make_invoice_pdf, member_of
) -> None:
    """TC-FUNC-026 强化：跨租户不可见、分页不重不漏，且筛选非法值被拒。"""
    for serial in ("01100200011100045239", "01100200011100045240", "01100200011100045241"):
        assert _upload(owner_workspace, make_invoice_pdf(serial)).status_code == 201
    mine = _documents(owner_workspace).json()
    assert mine["total"] == 3
    assert _documents(other_workspace).json()["items"] == []

    foreign_id = mine["items"][0]["id"]
    assert (
        other_workspace["client"].patch(
            f"/api/v1/tenants/{other_workspace['tenant_id']}/documents/{foreign_id}",
            headers={"X-CSRF-Token": other_workspace["csrf"]},
            json={"doc_type": "越权"},
        ).status_code
        == 404
    )

    pages = [
        _documents(owner_workspace, limit=2, offset=offset).json()["items"]
        for offset in (0, 2)
    ]
    ids = [item["id"] for page in pages for item in page]
    assert len(ids) == 3 and len(set(ids)) == 3
    assert set(ids) == {item["id"] for item in mine["items"]}

    assert _documents(owner_workspace, source="upload").json()["total"] == 3
    assert _documents(owner_workspace, source="接口导入").status_code == 422


def test_retention_status_is_admin_only(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, member_of
) -> None:
    """TC-FUNC-030 强化：保留状态属于治理面，成员与复核员只能改业务标注。"""
    assert _upload(owner_workspace, make_invoice_pdf()).status_code == 201
    document_id = _single_document_id(owner_workspace)

    member = member_of("catalog-member@example.com", owner_workspace["tenant_id"], "member")
    forbidden = member["client"].patch(
        f"/api/v1/tenants/{member['tenant_id']}/documents/{document_id}",
        headers={"X-CSRF-Token": member["csrf"]},
        json={"retention_status": "purge_scheduled"},
    )
    assert forbidden.status_code == 403, forbidden.text
    allowed = member["client"].patch(
        f"/api/v1/tenants/{member['tenant_id']}/documents/{document_id}",
        headers={"X-CSRF-Token": member["csrf"]},
        json={"labels": ["业务标注"]},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["labels"] == ["业务标注"]

    # 复核员在文档库只做确认：写接口整面对其关闭（docs/11 角色矩阵）
    reviewer = member_of("catalog-reviewer@example.com", owner_workspace["tenant_id"], "reviewer")
    for payload in ({"retention_status": "purged"}, {"labels": ["复核标注"]}):
        assert (
            reviewer["client"].patch(
                f"/api/v1/tenants/{reviewer['tenant_id']}/documents/{document_id}",
                headers={"X-CSRF-Token": reviewer["csrf"]},
                json=payload,
            ).status_code
            == 403
        )

    by_owner = _patch_document(owner_workspace, document_id, {"retention_status": "purged"})
    assert by_owner.status_code == 200
    assert by_owner.json()["retention_status"] == "purged"
