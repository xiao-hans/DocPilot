"""运营视图与就绪探针（TC-FUNC-038..040、TC-SEC-019/020、TC-REC-007）。"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from app.contexts.operations import api as operations_api
from app.db import SessionLocal
from app.main import app
from app.models.access import AuditEvent
from app.models.schema import INVOICE_TEMPLATE

from fastapi.testclient import TestClient

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


def test_health_and_readiness_probes(owner_workspace: dict) -> None:
    """TC-FUNC-038：探针匿名可读，就绪要求迁移已到 head。"""
    anonymous = TestClient(app)
    assert anonymous.get("/healthz").json() == {"status": "ok"}

    ready = anonymous.get("/readyz")
    assert ready.status_code == 200, ready.text
    body = ready.json()
    assert body["status"] == "ready" and body["database"] is True
    assert body["migration"]["in_sync"] is True
    assert body["migration"]["current"] == body["migration"]["head"]


def test_readiness_degrades_without_raising(
    owner_workspace: dict, sandbox: Path, monkeypatch
) -> None:
    """TC-REC-007：数据库不可用时探针给 503 与分类原因，不抛未捕获异常、不泄露路径。"""
    class Broken:
        def __call__(self):
            raise SQLAlchemyError("模拟数据库不可用")

        def __enter__(self):  # pragma: no cover - 只用来暴露错误路径
            raise SQLAlchemyError("模拟数据库不可用")

        def __exit__(self, *args):  # pragma: no cover
            return False

    monkeypatch.setattr(operations_api, "SessionLocal", Broken())
    response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable" and body["database"] is False
    assert body["migration"]["in_sync"] is False and body["migration"]["current"] is None
    assert str(sandbox) not in response.text


def test_tenant_usage_matches_stored_facts(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    """TC-FUNC-039：用量数字与库内实际一致，窗口参数受校验。"""
    job_id = _upload(owner_workspace, make_invoice_pdf(SERIAL))
    base = f"/api/v1/tenants/{owner_workspace['tenant_id']}"
    result = owner_workspace["client"].get(f"{base}/results?job_id={job_id}").json()["results"][0]
    owner_workspace["client"].patch(
        f"{base}/results/{result['id']}",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        json={"values": {**result["values"], "销售方名称": "用量公司"}, "expected_revision": 1},
    )
    owner_workspace["client"].post(
        f"{base}/results/{result['id']}/confirm", headers={"X-CSRF-Token": owner_workspace["csrf"]}
    )

    usage = owner_workspace["client"].get(f"{base}/usage?days=7").json()
    assert usage["tenant_id"] == owner_workspace["tenant_id"]
    assert usage["window_days"] == 7
    assert usage["documents"]["total"] == 1
    assert usage["documents"]["recent_created"] == 1
    assert usage["documents"]["retention"] == {"active": 1}
    assert usage["file_versions"]["total"] == 1
    assert usage["file_versions"]["storage_bytes"] > 0
    assert usage["jobs"]["total"] == 1
    assert usage["jobs"]["by_status"]["succeeded"] == 1
    assert usage["jobs"]["attempts"] == 1
    assert usage["results"]["by_state"]["confirmed"] == 1
    assert usage["results"]["human_revisions"] == 1
    assert usage["search_entries"] >= 3
    assert usage["automation"]["pending_events"] >= 1

    with SessionLocal() as db:
        assert db.query(AuditEvent).count() == usage["results"]["human_revisions"] + 2  # +job.created +confirm

    assert owner_workspace["client"].get(f"{base}/usage?days=0").status_code == 422
    assert owner_workspace["client"].get(f"{base}/usage?days=999").status_code == 422
    assert "storage_key" not in json.dumps(usage)


def test_platform_usage_is_platform_only_and_content_free(
    owner_workspace: dict,
    other_workspace: dict,
    make_invoice_pdf,
    identity,
    platform_admin_without_membership,
) -> None:
    """TC-SEC-019：平台视图只向平台身份开放，且不携带任何客户内容。"""
    _upload(owner_workspace, make_invoice_pdf(SERIAL))
    _upload(other_workspace, make_invoice_pdf("01100200011100045240"))

    assert TestClient(app).get("/api/v1/platform/usage").status_code == 401
    # 只有租户成员身份、没有平台身份的 owner 也读不到跨租户聚合
    outsider_owner = other_workspace["client"].get("/api/v1/platform/usage")
    assert outsider_owner.status_code == 403, outsider_owner.text
    plain = identity("plain-member-ops@example.com", "丁方空间", "member")
    assert plain["client"].get("/api/v1/platform/usage").status_code == 403

    ops = platform_admin_without_membership()
    response = ops["client"].get("/api/v1/platform/usage?days=30")
    assert response.status_code == 200, response.text
    body = response.json()
    # bootstrap 出来的租户所有者同时是平台管理员，因此这里按"有数据的租户"断言
    assert body["total_tenants"] == 3 and len(body["tenants"]) == 3
    assert {row["tenant_id"] for row in body["tenants"]} >= {
        owner_workspace["tenant_id"],
        other_workspace["tenant_id"],
    }
    by_tenant = {row["tenant_id"]: row for row in body["tenants"]}
    assert by_tenant[owner_workspace["tenant_id"]]["documents"] == 1
    assert by_tenant[other_workspace["tenant_id"]]["documents"] == 1
    assert by_tenant[plain["tenant_id"]]["documents"] == 0
    assert by_tenant[owner_workspace["tenant_id"]]["storage_bytes"] > 0
    dumped = json.dumps(body, ensure_ascii=False)
    assert SERIAL not in dumped  # 文件名、正文、字段值一律不出现在平台视图

    # 平台身份不授予租户数据读取权
    leakage = ops["client"].get(f"/api/v1/tenants/{owner_workspace['tenant_id']}/usage")
    assert leakage.status_code == 403, leakage.text
    jobs = ops["client"].get(f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs")
    assert jobs.status_code == 403, jobs.text


def test_platform_usage_audits_every_listed_tenant(
    owner_workspace: dict, other_workspace: dict, make_invoice_pdf, platform_admin_without_membership
) -> None:
    """TC-SEC-020：平台每读一个租户的聚合，该租户留下一条可读回的审计。"""
    _upload(owner_workspace, make_invoice_pdf(SERIAL))
    _upload(other_workspace, make_invoice_pdf("01100200011100045240"))
    ops = platform_admin_without_membership()

    assert ops["client"].get("/api/v1/platform/usage?days=14").status_code == 200

    with SessionLocal() as db:
        events = (
            db.query(AuditEvent).filter(AuditEvent.action == "platform.usage_viewed").all()
        )
        assert {event.tenant_id for event in events} == {
            owner_workspace["tenant_id"],
            other_workspace["tenant_id"],
        }
        assert all(event.actor_user_id == ops["user_id"] for event in events)
        assert all(set(event.metadata_) == {"scope", "window_days", "rows"} for event in events)
        assert all(event.metadata_["window_days"] == 14 for event in events)

    # 租户管理员在自己的审计流里能看到平台读过自己
    listed = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/audit-events"
    ).json()["events"]
    assert any(event["action"] == "platform.usage_viewed" for event in listed)
