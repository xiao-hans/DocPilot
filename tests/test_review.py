"""复核队列、修订链与确认/退回（DOC-DD-005 §6/§9：TC-FUNC-022..025、TC-SEC-014）。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.contexts.review import service as review
from app.contexts.review.domain import DocumentText, ExtractionResult, ResultRevision
from app.platform.db import SessionLocal
from app.platform.errors import AccessError
from app.main import app
from app.models.access import AuditEvent
from app.models.schema import INVOICE_TEMPLATE

FIELDS = json.dumps(INVOICE_TEMPLATE)


def _upload(workspace: dict, pdf: Path) -> str:
    response = workspace["client"].post(
        f"/api/v1/tenants/{workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": workspace["csrf"]},
        files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))],
        data={"fields": FIELDS},
    )
    assert response.status_code == 201, response.text
    return response.json()["job_id"]


def _results(workspace: dict, suffix: str = "", **params):
    query = "&".join(f"{key}={value}" for key, value in params.items())
    url = f"/api/v1/tenants/{workspace['tenant_id']}/results{suffix}"
    url = f"{url}?{query}" if query else url
    return workspace["client"].get(url)


def _first_result(workspace: dict, job_id: str) -> dict:
    listed = _results(workspace, job_id=job_id).json()["results"]
    assert len(listed) == 1
    return listed[0]


def _patch(workspace: dict, result_id: str, payload: dict):
    return workspace["client"].patch(
        f"/api/v1/tenants/{workspace['tenant_id']}/results/{result_id}",
        headers={"X-CSRF-Token": workspace["csrf"]},
        json=payload,
    )


def test_machine_result_text_and_first_revision_are_persisted(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-FUNC-022：处理完成后规范化结果、文本与首修订齐全。"""
    job_id = _upload(owner_workspace, make_invoice_pdf())
    result = _first_result(owner_workspace, job_id)

    assert result["state"] == "machine"
    assert result["revision_no"] == 1
    assert result["job_id"] == job_id
    assert set(result["values"]) == {field["name"] for field in INVOICE_TEMPLATE}

    detail = _results(owner_workspace, suffix=f"/{result['id']}").json()
    assert [item["source"] for item in detail["revisions"]] == ["machine"]

    preview = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/document-versions/{result['document_version_id']}/text"
    ).json()
    assert preview["char_count"] > 0
    assert "01100200011100045239" in preview["text"]
    assert preview["text"].startswith("--- 第 1 页 ---")
    assert preview["filename"].endswith(".pdf")

    with SessionLocal() as db:
        assert db.get(ExtractionResult, result["id"]) is not None
        assert db.query(DocumentText).count() == 1
        assert db.query(ResultRevision).count() == 1


def test_revision_chain_with_optimistic_concurrency(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-FUNC-023：正确并发号成功修订，旧并发号被拒绝且不覆盖历史。"""
    job_id = _upload(owner_workspace, make_invoice_pdf())
    result = _first_result(owner_workspace, job_id)

    first = _patch(
        owner_workspace,
        result["id"],
        {"values": {**result["values"], "销售方名称": "已核对公司"}, "expected_revision": 1},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["revision_no"] == 2
    assert body["state"] == "revised"
    assert body["values"]["销售方名称"] == "已核对公司"

    stale = _patch(
        owner_workspace,
        result["id"],
        {"values": result["values"], "expected_revision": 1},
    )
    assert stale.status_code == 409

    detail = _results(owner_workspace, suffix=f"/{result['id']}").json()
    assert [item["revision_no"] for item in detail["revisions"]] == [2, 1]
    assert detail["revisions"][1]["values"]["销售方名称"] == result["values"]["销售方名称"]

    unknown = _patch(
        owner_workspace,
        result["id"],
        {"values": {"不存在的字段": "x"}, "expected_revision": 2},
    )
    assert unknown.status_code == 422


@pytest.mark.parametrize(
    "role, can_review",
    [("member", False), ("reviewer", True), ("admin", True), ("owner", True)],
)
def test_review_roles(
    owner_workspace: dict,
    sandbox: Path,
    make_invoice_pdf,
    member_of,
    role: str,
    can_review: bool,
) -> None:
    """TC-FUNC-024：只有 reviewer 及以上可修订与确认。"""
    job_id = _upload(owner_workspace, make_invoice_pdf())
    result = _first_result(owner_workspace, job_id)
    reviewer = member_of(f"rev-{role}@example.com", owner_workspace["tenant_id"], role)

    payload = {"values": result["values"], "expected_revision": 1}
    revised = reviewer["client"].patch(
        f"/api/v1/tenants/{reviewer['tenant_id']}/results/{result['id']}",
        headers={"X-CSRF-Token": reviewer["csrf"]},
        json=payload,
    )
    confirmed = reviewer["client"].post(
        f"/api/v1/tenants/{reviewer['tenant_id']}/results/{result['id']}/confirm",
        headers={"X-CSRF-Token": reviewer["csrf"]},
    )
    assert revised.status_code == (200 if can_review else 403), revised.text
    assert confirmed.status_code == (200 if can_review else 403), confirmed.text
    if can_review:
        assert confirmed.json()["state"] == "confirmed"
        assert confirmed.json()["confirmed_by"] == reviewer["user_id"]
        again = reviewer["client"].post(
            f"/api/v1/tenants/{reviewer['tenant_id']}/results/{result['id']}/confirm",
            headers={"X-CSRF-Token": reviewer["csrf"]},
        )
        assert again.status_code == 409
        direct = reviewer["client"].patch(
            f"/api/v1/tenants/{reviewer['tenant_id']}/results/{result['id']}",
            headers={"X-CSRF-Token": reviewer["csrf"]},
            json=payload,
        )
        assert direct.status_code == 409  # 已确认需先退回


def test_reject_requires_reason_and_audits(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-FUNC-025：退回必须给理由，且产生审计与事件。"""
    from app.models.access import AuditEvent
    from app.models.outbox import DomainEventRecord

    job_id = _upload(owner_workspace, make_invoice_pdf())
    result = _first_result(owner_workspace, job_id)
    url = f"/api/v1/tenants/{owner_workspace['tenant_id']}/results/{result['id']}/reject"

    missing = owner_workspace["client"].post(
        url, headers={"X-CSRF-Token": owner_workspace["csrf"]}, json={}
    )
    assert missing.status_code == 422

    rejected = owner_workspace["client"].post(
        url,
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        json={"reason": "金额与发票不符，需重跑"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["state"] == "rejected"

    events = [
        event["action"]
        for event in owner_workspace["client"].get(
            f"/api/v1/tenants/{owner_workspace['tenant_id']}/audit-events"
        ).json()["events"]
    ]
    assert "result.rejected" in events
    with SessionLocal() as db:
        assert db.query(AuditEvent).filter(AuditEvent.action == "result.rejected").count() == 1
        types = {
            row.event_type
            for row in db.scalars(select(DomainEventRecord)).all()
        }
    assert "result.rejected" in types


def test_results_are_tenant_scoped(
    owner_workspace: dict, identity, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-SEC-014：他租户即使知道 result/版本 ID 也读不到、改不动。"""
    job_id = _upload(owner_workspace, make_invoice_pdf())
    result = _first_result(owner_workspace, job_id)
    outsider = identity("outsider-review@example.com", "外部空间", "owner")

    assert (
        outsider["client"]
        .get(f"/api/v1/tenants/{outsider['tenant_id']}/results/{result['id']}")
        .status_code
        == 404
    )
    assert (
        outsider["client"]
        .get(
            f"/api/v1/tenants/{outsider['tenant_id']}/document-versions/{result['document_version_id']}/text"
        )
        .status_code
        == 404
    )
    patched = outsider["client"].patch(
        f"/api/v1/tenants/{outsider['tenant_id']}/results/{result['id']}",
        headers={"X-CSRF-Token": outsider["csrf"]},
        json={"values": result["values"], "expected_revision": 1},
    )
    assert patched.status_code == 404
    assert outsider["client"].get(
        f"/api/v1/tenants/{outsider['tenant_id']}/results"
    ).json()["results"] == []

    with SessionLocal() as db:
        stored = db.get(ExtractionResult, result["id"])
        assert stored is not None and stored.revision_no == 1


def test_removed_member_loses_review_access(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, member_of
) -> None:
    """被移除成员即使持旧会话也不能修订（阶段 B 判定复用，无新增会话撤销）。"""
    job_id = _upload(owner_workspace, make_invoice_pdf())
    result = _first_result(owner_workspace, job_id)
    reviewer = member_of("gone-reviewer@example.com", owner_workspace["tenant_id"], "reviewer")

    allowed = reviewer["client"].patch(
        f"/api/v1/tenants/{reviewer['tenant_id']}/results/{result['id']}",
        headers={"X-CSRF-Token": reviewer["csrf"]},
        json={"values": result["values"], "expected_revision": 1},
    )
    assert allowed.status_code == 200

    owner_workspace["client"].delete(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/members/{reviewer['user_id']}",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
    )
    after = reviewer["client"].patch(
        f"/api/v1/tenants/{reviewer['tenant_id']}/results/{result['id']}",
        headers={"X-CSRF-Token": reviewer["csrf"]},
        json={"values": result["values"], "expected_revision": 2},
    )
    assert after.status_code == 403


def test_crash_between_writes_does_not_duplicate_results(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-REC-005：重复处理同一任务时结果与修订保持幂等。"""
    from app.contexts.review import service as review
    from app.contexts.processing.manager import get_job_manager

    job_id = _upload(owner_workspace, make_invoice_pdf())
    manager = get_job_manager()
    job = manager.get_job(job_id, owner_workspace["tenant_id"])
    finfo = job.files[0]

    with SessionLocal() as db:
        review.record_machine_result(
            db,
            tenant_id=owner_workspace["tenant_id"],
            job_id=job_id,
            filename=finfo.name,
            text="重复写入同一份机器结果",
            fields=[field.model_dump() for field in job.fields],
            values=dict(job.result[0]["values"]),
            warnings=[],
        )
        db.commit()

    with SessionLocal() as db:
        rows = db.scalars(
            select(ExtractionResult).where(ExtractionResult.job_id == job_id)
        ).all()
        assert len(rows) == 1
        assert db.query(ResultRevision).count() == 1
        assert rows[0].state == "machine"


def test_human_revision_is_not_overwritten_by_reprocessing(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    job_id = _upload(owner_workspace, make_invoice_pdf())
    result = _first_result(owner_workspace, job_id)
    assert (
        _patch(
            owner_workspace,
            result["id"],
            {"values": {**result["values"], "销售方名称": "人工核对"}, "expected_revision": 1},
        ).status_code
        == 200
    )

    from app.contexts.review import service as review
    from app.contexts.processing.manager import get_job_manager

    job = get_job_manager().get_job(job_id, owner_workspace["tenant_id"])
    with SessionLocal() as db:
        review.record_machine_result(
            db,
            tenant_id=owner_workspace["tenant_id"],
            job_id=job_id,
            filename=job.files[0].name,
            text="机器重跑",
            fields=[field.model_dump() for field in job.fields],
            values={**result["values"], "销售方名称": "机器覆盖"},
            warnings=[],
        )
        db.commit()

    after = _results(owner_workspace, suffix=f"/{result['id']}").json()
    assert after["values"]["销售方名称"] == "人工核对"
    assert after["state"] == "revised"


def _race(two_workers) -> list[int]:
    """让两个工作线程在同一关卡后同时提交，返回各自的状态码。"""
    codes: list[int] = []
    gate = threading.Barrier(2)

    def worker(index: int) -> None:
        try:
            gate.wait()
            two_workers(index)
        except AccessError as exc:
            codes.append(exc.status_code)
        else:
            codes.append(200)

    threads = [threading.Thread(target=worker, args=(index,)) for index in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return codes


def test_concurrent_revise_admits_exactly_one_writer(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-FUNC-023 强化：同一并发号下并发改值，只有一人写得进去。"""
    job_id = _upload(owner_workspace, make_invoice_pdf())
    result = _first_result(owner_workspace, job_id)
    tenant_id = owner_workspace["tenant_id"]

    def save(index: int) -> None:
        db = SessionLocal()
        try:
            review.revise(
                db,
                tenant_id=tenant_id,
                actor_user_id=owner_workspace["user_id"],
                result_id=result["id"],
                values={**result["values"], "销售方名称": f"并发方{index}"},
                expected_revision=1,
            )
        finally:
            db.close()

    assert sorted(_race(save)) == [200, 409]
    with SessionLocal() as db:
        stored = db.get(ExtractionResult, result["id"])
        assert stored.revision_no == 2  # 失败的一方不得把版本号前推
        assert (
            db.query(ResultRevision).filter(ResultRevision.result_id == result["id"]).count() == 2
        )


def test_concurrent_confirm_audits_exactly_once(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-FUNC-024 强化：并发确认只产生一次状态变更与一条审计。"""
    job_id = _upload(owner_workspace, make_invoice_pdf())
    result = _first_result(owner_workspace, job_id)
    tenant_id = owner_workspace["tenant_id"]

    def confirm(_index: int) -> None:
        db = SessionLocal()
        try:
            review.confirm(
                db,
                tenant_id=tenant_id,
                actor_user_id=owner_workspace["user_id"],
                result_id=result["id"],
            )
        finally:
            db.close()

    assert sorted(_race(confirm)) == [200, 409]
    with SessionLocal() as db:
        assert (
            db.query(AuditEvent).filter(AuditEvent.action == "result.confirmed").count() == 1
        )
        assert db.get(ExtractionResult, result["id"]).state == "confirmed"


def test_reject_clears_confirmation_and_blocks_direct_confirm(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-FUNC-025 强化：退回清空确认痕迹，且退回态必须先修订再确认。"""
    job_id = _upload(owner_workspace, make_invoice_pdf())
    result = _first_result(owner_workspace, job_id)
    base = f"/api/v1/tenants/{owner_workspace['tenant_id']}"
    csrf = {"X-CSRF-Token": owner_workspace["csrf"]}

    assert owner_workspace["client"].post(
        f"{base}/results/{result['id']}/confirm", headers=csrf
    ).status_code == 200
    rejected = owner_workspace["client"].post(
        f"{base}/results/{result['id']}/reject", headers=csrf, json={"reason": "金额不符"}
    ).json()
    assert rejected["state"] == "rejected"
    assert rejected["confirmed_by"] is None and rejected["confirmed_at"] is None

    direct = owner_workspace["client"].post(
        f"{base}/results/{result['id']}/confirm", headers=csrf
    )
    assert direct.status_code == 409

    revised = _patch(
        owner_workspace,
        result["id"],
        {"values": {**result["values"], "税额": "12.00"}, "expected_revision": rejected["revision_no"]},
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["state"] == "revised"
    assert owner_workspace["client"].post(
        f"{base}/results/{result['id']}/confirm", headers=csrf
    ).json()["state"] == "confirmed"


def test_revise_preserves_numbers_and_rejects_blobs_and_partial_payloads(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-FUNC-023 补充：标量原值保留，嵌套值与缺字段都不得静默改写。"""
    job_id = _upload(owner_workspace, make_invoice_pdf())
    detail = _first_result(owner_workspace, job_id)
    numeric = next(
        name for name, value in detail["values"].items() if isinstance(value, (int, float))
    )

    ok = _patch(
        owner_workspace,
        detail["id"],
        {"values": {**detail["values"], "销售方名称": "同一公司"}, "expected_revision": 1},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["values"][numeric] == detail["values"][numeric]

    blob = _patch(
        owner_workspace,
        detail["id"],
        {"values": {**detail["values"], numeric: {"嵌套": "值"}}, "expected_revision": 2},
    )
    assert blob.status_code == 422

    partial = {name: value for name, value in detail["values"].items() if name != numeric}
    missing = _patch(
        owner_workspace, detail["id"], {"values": partial, "expected_revision": 2}
    )
    assert missing.status_code == 422
    assert "缺少字段" in missing.json()["detail"]

    after = _results(owner_workspace, suffix=f"/{detail['id']}").json()
    assert after["revision_no"] == 2  # 两次被拒的提交都没有追加修订
    assert after["values"][numeric] == detail["values"][numeric]


def test_results_state_filter_rejects_unknown_values(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-FUNC-022 补充：非法 state 直接 422，而不是静默返回全集。"""
    job_id = _upload(owner_workspace, make_invoice_pdf())
    listing = _results(owner_workspace, job_id=job_id, state="machine")
    assert len(listing.json()["results"]) == 1
    assert _results(owner_workspace, state="已确认").status_code == 422


def test_result_write_failure_leaves_no_snapshot_without_result(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, monkeypatch
) -> None:
    """TC-REC-005：规范化结果写入失败时，快照不得先行推进成"有结果无规范化行"。"""
    from app.contexts.processing.domain import JobAttempt
    from app.models.persistent_job import PersistentJob

    original = review.record_machine_result
    failures = {"left": 1}

    def flaky(db, **kwargs):
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("模拟规范化结果写入失败")
        return original(db, **kwargs)

    monkeypatch.setattr(review, "record_machine_result", flaky)
    job_id = _upload(owner_workspace, make_invoice_pdf())

    with SessionLocal() as db:
        snapshot = db.get(PersistentJob, job_id).snapshot
        results = db.query(ExtractionResult).filter(ExtractionResult.job_id == job_id).all()
        attempts = db.query(JobAttempt).filter(JobAttempt.job_id == job_id).all()

    # R5 之后快照根本不带 result：这条不变量从"两边必须一致"升级为"只有一边有值"。
    assert "result" not in snapshot, "快照不再承载提取结果，双真相少了一处"
    assert len(results) == 1
    assert results[0].state == "machine"
    assert attempts and attempts[0].status in ("succeeded", "failed")
