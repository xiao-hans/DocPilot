"""事件规则：订阅领域事件、执行安全动作与幂等重试（TC-FUNC-034..037、TC-SEC-016..018、TC-REC-006）。"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from app.contexts.automation.domain import EventRule, NotificationRequest, RuleRun
from app.contexts.documents import service as documents_service
from app.db import SessionLocal
from app.models.access import AuditEvent
from app.models.outbox import DomainEventRecord
from app.models.schema import INVOICE_TEMPLATE

FIELDS = json.dumps(INVOICE_TEMPLATE)
SERIAL = "01100200011100045239"


def _upload(workspace: dict, pdf: Path) -> str:
    response = workspace["client"].post(
        f"/api/v1/tenants/{workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": workspace["csrf"]},
        files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))],
        data={"fields": FIELDS},
    )
    assert response.status_code == 201, response.text
    return response.json()["job_id"]


def _base(workspace: dict) -> str:
    return f"/api/v1/tenants/{workspace['tenant_id']}"


def _create_rule(workspace: dict, **payload):
    return workspace["client"].post(
        f"{_base(workspace)}/automation/rules",
        headers={"X-CSRF-Token": workspace["csrf"]},
        json=payload,
    )


def _patch_rule(workspace: dict, rule_id: str, **payload):
    return workspace["client"].patch(
        f"{_base(workspace)}/automation/rules/{rule_id}",
        headers={"X-CSRF-Token": workspace["csrf"]},
        json=payload,
    )


def _dispatch(workspace: dict) -> dict:
    response = workspace["client"].post(
        f"{_base(workspace)}/automation/dispatch",
        headers={"X-CSRF-Token": workspace["csrf"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _runs(workspace: dict) -> list[dict]:
    return workspace["client"].get(f"{_base(workspace)}/automation/runs").json()["runs"]


def _labels(workspace: dict) -> list[str]:
    items = workspace["client"].get(f"{_base(workspace)}/documents").json()["items"]
    return [label for item in items for label in item["labels"]]


def _pending_events() -> int:
    with SessionLocal() as db:
        return db.query(DomainEventRecord).filter(DomainEventRecord.published_at.is_(None)).count()


def test_tag_document_rule_applies_once_and_publishes_event(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-FUNC-034：确认前的事件也能驱动动作；重复 dispatch 不重复执行。"""
    _upload(owner_workspace, make_invoice_pdf(SERIAL))
    created = _create_rule(
        owner_workspace,
        name="成功即标注",
        event_type="job.succeeded",
        action={"type": "tag_document", "params": {"label": "已处理"}},
    )
    assert created.status_code == 201, created.text

    summary = _dispatch(owner_workspace)
    assert summary["applied"] == 1 and summary["failed"] == 0
    assert "已处理" in _labels(owner_workspace)
    assert _pending_events() == 0

    runs = _runs(owner_workspace)
    assert len(runs) == 1
    assert runs[0]["status"] == "applied" and runs[0]["rule_version"] == 1
    assert runs[0]["attempt_count"] == 1

    again = _dispatch(owner_workspace)
    assert again["consumed"] == 0 and again["applied"] == 0
    assert _labels(owner_workspace).count("已处理") == 1

    with SessionLocal() as db:
        audit = (
            db.query(AuditEvent)
            .filter(AuditEvent.action == "document.metadata_updated")
            .order_by(AuditEvent.created_at.desc())
            .first()
        )
        assert audit is not None and audit.metadata_.get("rule_id") == runs[0]["rule_id"]
        assert audit.metadata_["changed"] == ["labels"]


def test_condition_mismatch_skips_and_paused_rule_is_ignored(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-FUNC-035：条件不符记 skipped，暂停规则完全不参与执行。"""
    _upload(owner_workspace, make_invoice_pdf(SERIAL))
    mismatch = _create_rule(
        owner_workspace,
        name="只标失败任务",
        event_type="job.succeeded",
        condition={"status": "partial"},
        action={"type": "tag_document", "params": {"label": "不该出现"}},
    ).json()
    paused = _create_rule(
        owner_workspace,
        name="暂停的规则",
        event_type="job.succeeded",
        action={"type": "tag_document", "params": {"label": "暂停标签"}},
    ).json()
    assert _patch_rule(owner_workspace, paused["id"], status="paused").status_code == 200

    summary = _dispatch(owner_workspace)
    assert summary["applied"] == 0 and summary["skipped"] == 1
    runs = _runs(owner_workspace)
    assert [run["rule_id"] for run in runs] == [mismatch["id"]]
    assert runs[0]["status"] == "skipped"
    assert _labels(owner_workspace) == []

    # 事件已投递：解除暂停不会回放历史事件，这是"不假装补跑"的代价与承诺
    assert _patch_rule(owner_workspace, paused["id"], status="active").status_code == 200
    assert _dispatch(owner_workspace)["consumed"] == 0
    assert "暂停标签" not in _labels(owner_workspace)


def test_failed_action_retries_the_same_run_row(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, monkeypatch
) -> None:
    """TC-FUNC-036 + TC-REC-006：动作失败留在队列，重试推进同一条运行记录。"""
    _upload(owner_workspace, make_invoice_pdf(SERIAL))
    rule = _create_rule(
        owner_workspace,
        name="标注重试",
        event_type="job.succeeded",
        action={"type": "tag_document", "params": {"label": "重试标签"}},
    ).json()

    original = documents_service.add_label
    boom = {"left": 1}

    def flaky(*args, **kwargs):
        if boom["left"]:
            boom["left"] -= 1
            raise ValueError("模拟标注失败")
        return original(*args, **kwargs)

    monkeypatch.setattr(documents_service, "add_label", flaky)
    summary = _dispatch(owner_workspace)
    assert summary["failed"] == 1 and summary["applied"] == 0
    assert _pending_events() == 1  # 失败的事件不推进投递时间

    runs = _runs(owner_workspace)
    assert len(runs) == 1 and runs[0]["status"] == "failed"
    assert runs[0]["attempt_count"] == 1 and runs[0]["detail"] == "模拟标注失败"
    assert runs[0]["finished_at"] is None

    monkeypatch.setattr(documents_service, "add_label", original)
    retried = _dispatch(owner_workspace)
    assert retried["applied"] == 1 and retried["failed"] == 0
    assert _pending_events() == 0

    runs = _runs(owner_workspace)
    assert len(runs) == 1  # 同一条记录被重试，不新增
    assert runs[0]["status"] == "applied" and runs[0]["attempt_count"] == 2
    assert runs[0]["rule_id"] == rule["id"] and runs[0]["finished_at"]
    assert "重试标签" in _labels(owner_workspace)


def test_notification_rule_records_intent_without_delivery(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-FUNC-034 补充：通知只登记意图，没有投递渠道时停在 pending。"""
    job_id = _upload(owner_workspace, make_invoice_pdf(SERIAL))
    _create_rule(
        owner_workspace,
        name="确认即通知",
        event_type="result.confirmed",
        action={
            "type": "create_notification",
            "params": {"subject": "结果已确认", "body": "请归档该发票"},
        },
    )
    result = owner_workspace["client"].get(
        f"{_base(owner_workspace)}/results?job_id={job_id}"
    ).json()["results"][0]
    assert owner_workspace["client"].post(
        f"{_base(owner_workspace)}/results/{result['id']}/confirm",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
    ).status_code == 200

    assert _dispatch(owner_workspace)["applied"] == 1
    listed = owner_workspace["client"].get(
        f"{_base(owner_workspace)}/automation/notifications"
    ).json()["notifications"]
    assert len(listed) == 1
    assert listed[0]["status"] == "pending"
    assert listed[0]["subject"] == "结果已确认"
    assert listed[0]["object_type"] == "extraction_result"

    with SessionLocal() as db:
        row = db.query(NotificationRequest).one()
        assert row.delivered_at is None and row.recipient_user_id == owner_workspace["user_id"]


def test_rule_versioning_keeps_history_at_executed_version(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-FUNC-037：改配置递增版本，历史日志仍记录执行时的版本。"""
    _upload(owner_workspace, make_invoice_pdf(SERIAL))
    rule = _create_rule(
        owner_workspace,
        name="版本规则",
        event_type="job.succeeded",
        action={"type": "tag_document", "params": {"label": "v1标签"}},
    ).json()
    _dispatch(owner_workspace)

    updated = _patch_rule(
        owner_workspace,
        rule["id"],
        name="版本规则改名",
        action={"type": "tag_document", "params": {"label": "v2标签"}},
    ).json()
    assert updated["version"] == 2 and updated["name"] == "版本规则改名"

    runs = _runs(owner_workspace)
    assert [run["rule_version"] for run in runs] == [1]
    listed = owner_workspace["client"].get(f"{_base(owner_workspace)}/automation/rules").json()
    assert listed["rules"][0]["runs"]["applied"] == 1

    with SessionLocal() as db:
        audit = (
            db.query(AuditEvent)
            .filter(AuditEvent.action == "rule.updated")
            .order_by(AuditEvent.created_at.desc())
            .first()
        )
        assert audit.metadata_ == {"changed": ["action", "name"], "version": 2}
    assert _dispatch(owner_workspace)["consumed"] == 0


def test_rule_write_matrix_and_payload_validation(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, member_of, identity
) -> None:
    """TC-SEC-017：写面只给管理员，非法事件类型/动作/嵌套条件都拒于库外。"""
    for role in ("member", "reviewer"):
        actor = member_of(f"auto-{role}@example.com", owner_workspace["tenant_id"], role)
        denied = actor["client"].post(
            f"/api/v1/tenants/{actor['tenant_id']}/automation/rules",
            headers={"X-CSRF-Token": actor["csrf"]},
            json={
                "name": "越权规则",
                "event_type": "job.succeeded",
                "action": {"type": "tag_document", "params": {"label": "x"}},
            },
        )
        assert denied.status_code == 403, denied.text
    with SessionLocal() as db:
        assert db.query(EventRule).count() == 0

    admin = member_of("auto-admin@example.com", owner_workspace["tenant_id"], "admin")
    valid = admin["client"].post(
        f"/api/v1/tenants/{admin['tenant_id']}/automation/rules",
        headers={"X-CSRF-Token": admin["csrf"]},
        json={
            "name": "管理员规则",
            "event_type": "job.failed",
            "action": {"type": "create_notification", "params": {"subject": "失败", "body": "请检查"}},
        },
    )
    assert valid.status_code == 201, valid.text

    base = f"/api/v1/tenants/{admin['tenant_id']}/automation/rules"
    rejected = [
        {"name": "非法事件", "event_type": "user.deleted", "action": {"type": "tag_document", "params": {"label": "x"}}},
        {"name": "非法动作", "event_type": "job.failed", "action": {"type": "delete_document", "params": {}}},
        {
            "name": "嵌套条件",
            "event_type": "job.failed",
            "condition": {"payload": {"nested": "值"}},
            "action": {"type": "tag_document", "params": {"label": "x"}},
        },
        {"name": "缺动作", "event_type": "job.failed", "action": {"type": "tag_document", "params": {}}},
    ]
    for payload in rejected:
        response = admin["client"].post(
            base, headers={"X-CSRF-Token": admin["csrf"]}, json=payload
        )
        assert response.status_code == 422, (payload["name"], response.text)

    duplicate = admin["client"].post(
        base,
        headers={"X-CSRF-Token": admin["csrf"]},
        json={
            "name": "管理员规则",
            "event_type": "job.failed",
            "action": {"type": "tag_document", "params": {"label": "x"}},
        },
    )
    assert duplicate.status_code == 409

    with SessionLocal() as db:
        assert db.query(EventRule).count() == 1  # 四条非法 + 一条同名都未落库
        assert db.query(AuditEvent).filter(AuditEvent.action == "rule.created").count() == 1

    outsider = identity("outsider-rule@example.com", "外部空间", "admin")
    forbidden = outsider["client"].patch(
        f"/api/v1/tenants/{outsider['tenant_id']}/automation/rules/{valid.json()['id']}",
        headers={"X-CSRF-Token": outsider["csrf"]},
        json={"name": "改别人的规则"},
    )
    assert forbidden.status_code == 404
    assert outsider["client"].get(
        f"/api/v1/tenants/{outsider['tenant_id']}/automation/rules"
    ).json()["rules"] == []


def test_dispatch_is_tenant_scoped_and_audited(
    owner_workspace: dict, identity, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-SEC-016 + TC-SEC-018：dispatch 只吃本租户事件，审计与日志不含敏感内容。"""
    _upload(owner_workspace, make_invoice_pdf(SERIAL))
    before = _pending_events()
    assert before > 0

    outsider = identity("outsider-automation@example.com", "外部空间", "owner")
    outsider_summary = _dispatch(outsider)
    assert outsider_summary["consumed"] == 0
    assert _pending_events() == before
    assert _runs(outsider) == []

    _create_rule(
        owner_workspace,
        name="外部不可动",
        event_type="job.succeeded",
        action={"type": "tag_document", "params": {"label": "属主标签"}},
    )
    assert _dispatch(owner_workspace)["applied"] == 1

    with SessionLocal() as db:
        audit = (
            db.query(AuditEvent)
            .filter(AuditEvent.action == "automation.dispatched")
            .order_by(AuditEvent.created_at.desc())
            .first()
        )
        assert set(audit.metadata_) == {"consumed", "applied", "skipped", "failed"}
        assert audit.tenant_id == owner_workspace["tenant_id"]
        run = db.scalars(select(RuleRun)).one()
        assert str(sandbox) not in (run.detail or "")
        assert run.tenant_id == owner_workspace["tenant_id"]
        # 他租户既看不到运行日志也看不到通知
        assert (
            outsider["client"]
            .get(f"{_base(outsider)}/automation/runs")
            .json()["runs"]
            == []
        )


def _upload_two(workspace: dict, pdf_a: Path, pdf_b: Path) -> str:
    response = workspace["client"].post(
        f"/api/v1/tenants/{workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": workspace["csrf"]},
        files=[
            ("files", (pdf_a.name, pdf_a.read_bytes(), "application/pdf")),
            ("files", (pdf_b.name, pdf_b.read_bytes(), "application/pdf")),
        ],
        data={"fields": FIELDS},
    )
    assert response.status_code == 201, response.text
    return response.json()["job_id"]


def _run_rule(workspace: dict, rule_id: str) -> dict:
    response = workspace["client"].post(
        f"{_base(workspace)}/automation/rules/{rule_id}/run",
        headers={"X-CSRF-Token": workspace["csrf"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_manual_rule_run_leaves_other_events_pending(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """评审 P0：手动跑一条规则不得把整个 outbox 标成已投递。"""
    job_id = _upload(owner_workspace, make_invoice_pdf(SERIAL))
    tagger = _create_rule(
        owner_workspace,
        name="只标注成功任务",
        event_type="job.succeeded",
        action={"type": "tag_document", "params": {"label": "手动跑"}},
    ).json()
    notifier = _create_rule(
        owner_workspace,
        name="确认后通知",
        event_type="result.confirmed",
        action={"type": "create_notification", "params": {"subject": "已确认", "body": "去归档"}},
    ).json()

    tried = _run_rule(owner_workspace, tagger["id"])
    assert tried["applied"] == 1 and tried["published"] == 0
    assert "手动跑" in _labels(owner_workspace)
    assert _pending_events() > 0  # 其他事件必须留在队列里

    result = owner_workspace["client"].get(
        f"{_base(owner_workspace)}/results?job_id={job_id}"
    ).json()["results"][0]
    owner_workspace["client"].post(
        f"{_base(owner_workspace)}/results/{result['id']}/confirm",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
    )
    assert _dispatch(owner_workspace)["applied"] == 1
    with SessionLocal() as db:
        assert db.query(NotificationRequest).filter_by(rule_id=notifier["id"]).count() == 1
        # 每条 (事件, 规则, 版本) 至多一条运行记录
        keys = [row.delivery_key for row in db.query(RuleRun).all()]
        assert len(keys) == len(set(keys)) == 2

    paused = _patch_rule(owner_workspace, tagger["id"], status="paused").json()
    assert _run_rule(owner_workspace, paused["id"]) == {
        "consumed": 0,
        "applied": 0,
        "skipped": 0,
        "failed": 0,
        "published": 0,
    }


def test_failed_action_rolls_back_partial_tags(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, monkeypatch
) -> None:
    """评审 P0：多文档动作中途失败时，已打上的标签必须一起回滚。"""
    _upload_two(owner_workspace, make_invoice_pdf(SERIAL), make_invoice_pdf("01100200011100045240"))
    _create_rule(
        owner_workspace,
        name="双文档标注",
        event_type="job.succeeded",
        action={"type": "tag_document", "params": {"label": "整批标签"}},
    )

    original = documents_service.add_label
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise ValueError("第二份文档标注失败")
        return original(*args, **kwargs)

    monkeypatch.setattr(documents_service, "add_label", flaky)
    assert _dispatch(owner_workspace)["failed"] == 1
    assert _pending_events() > 0
    assert "整批标签" not in _labels(owner_workspace)  # 第一份的写入被回滚

    monkeypatch.setattr(documents_service, "add_label", original)
    assert _dispatch(owner_workspace)["applied"] == 1
    labels = owner_workspace["client"].get(f"{_base(owner_workspace)}/documents").json()["items"]
    assert [sorted(item["labels"]) for item in labels] == [["整批标签"], ["整批标签"]]
    with SessionLocal() as db:
        assert db.query(RuleRun).count() == 1
        assert db.query(RuleRun).one().attempt_count == 2


def test_unknown_condition_key_is_rejected(owner_workspace: dict) -> None:
    """评审 P1：拼错的条件键会让规则永不匹配且无从发现，因此直接拒于库外。"""
    rejected = _create_rule(
        owner_workspace,
        name="拼错键",
        event_type="job.succeeded",
        condition={"statuss": "succeeded"},
        action={"type": "tag_document", "params": {"label": "x"}},
    )
    assert rejected.status_code == 422
    assert "没有可比较的字段" in rejected.json()["detail"]

    unknown_type = _create_rule(
        owner_workspace,
        name="订阅未发布事件",
        event_type="job.queued",
        action={"type": "tag_document", "params": {"label": "x"}},
    )
    assert unknown_type.status_code == 422

    allowed = _create_rule(
        owner_workspace,
        name="合法条件",
        event_type="job.succeeded",
        condition={"engine": "mock"},
        action={"type": "tag_document", "params": {"label": "x"}},
    )
    assert allowed.status_code == 201, allowed.text
    with SessionLocal() as db:
        assert db.query(EventRule).count() == 1


def test_all_write_endpoints_reject_non_admins(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, member_of
) -> None:
    """TC-SEC-017 强化：四个写端点逐一过角色矩阵，而不只是创建。"""
    _upload(owner_workspace, make_invoice_pdf(SERIAL))
    admin = member_of("auto-admin2@example.com", owner_workspace["tenant_id"], "admin")
    rule = _create_rule(
        admin,
        name="矩阵规则",
        event_type="job.succeeded",
        action={"type": "tag_document", "params": {"label": "矩阵标签"}},
    ).json()

    for role in ("member", "reviewer"):
        actor = member_of(f"auto-matrix-{role}@example.com", owner_workspace["tenant_id"], role)
        csrf = {"X-CSRF-Token": actor["csrf"]}
        tenant = f"/api/v1/tenants/{actor['tenant_id']}"
        before = _labels(actor)
        attempts = [
            actor["client"].post(
                f"{tenant}/automation/rules",
                headers=csrf,
                json={
                    "name": f"{role} 建规则",
                    "event_type": "job.succeeded",
                    "action": {"type": "tag_document", "params": {"label": "越权"}},
                },
            ),
            actor["client"].patch(
                f"{tenant}/automation/rules/{rule['id']}", headers=csrf, json={"name": "越权改名"}
            ),
            actor["client"].post(f"{tenant}/automation/rules/{rule['id']}/run", headers=csrf),
            actor["client"].post(f"{tenant}/automation/dispatch", headers=csrf),
        ]
        assert [response.status_code for response in attempts] == [403, 403, 403, 403], [
            response.text for response in attempts
        ]
        assert _labels(actor) == before
        assert actor["client"].get(f"{tenant}/automation/notifications").status_code == 403
    with SessionLocal() as db:
        assert db.query(EventRule).count() == 1
        assert db.query(RuleRun).count() == 0


def test_run_details_and_notifications_carry_no_secrets(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, monkeypatch
) -> None:
    """TC-SEC-018 强化：失败详情与通知正文都必须不含路径、令牌与正文。"""
    job_id = _upload(owner_workspace, make_invoice_pdf(SERIAL))
    _create_rule(
        owner_workspace,
        name="失败并通知",
        event_type="result.confirmed",
        action={"type": "create_notification", "params": {"subject": "确认通知", "body": "含敏感词检查"}},
    )
    original = documents_service.add_label

    def boom(*args, **kwargs):
        raise ValueError(f"标注失败：{kwargs.get('document_id')} 在 {sandbox}")

    monkeypatch.setattr(documents_service, "add_label", boom)
    _create_rule(
        owner_workspace,
        name="失败标注",
        event_type="job.succeeded",
        action={"type": "tag_document", "params": {"label": "会失败"}},
    )
    result = owner_workspace["client"].get(
        f"{_base(owner_workspace)}/results?job_id={job_id}"
    ).json()["results"][0]
    owner_workspace["client"].post(
        f"{_base(owner_workspace)}/results/{result['id']}/confirm",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
    )
    assert _dispatch(owner_workspace)["failed"] == 1
    monkeypatch.setattr(documents_service, "add_label", original)

    runs = _runs(owner_workspace)
    failed = [run for run in runs if run["status"] == "failed"]
    assert failed and all(str(sandbox) not in (run["detail"] or "") for run in failed), runs
    listed = owner_workspace["client"].get(
        f"{_base(owner_workspace)}/automation/notifications"
    ).json()["notifications"]
    assert len(listed) == 1 and listed[0]["status"] == "pending"
    assert str(sandbox) not in json.dumps(listed)
    assert job_id not in (failed[0]["detail"] or "")
