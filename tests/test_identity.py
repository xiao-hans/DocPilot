"""身份、会话与租户授权边界测试（FR-001 / FR-002 / FR-003 / NFR-001）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models.account import Membership, Tenant, User
from conftest import BOOTSTRAP_TOKEN, PASSWORD

NO_SUCH_JOB = "0" * 32


def _bootstrap(client: TestClient, email="owner@example.com", tenant_name="甲方空间"):
    return client.post(
        "/api/v1/auth/bootstrap",
        headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
        json={"email": email, "password": PASSWORD, "tenant_name": tenant_name},
    )


# ---------- 匿名访问 ----------


def test_anonymous_requests_are_rejected(client: TestClient) -> None:
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/tenants").status_code == 401
    assert client.post("/api/v1/tenants", json={"name": "越权空间"}).status_code == 401


# ---------- 首次初始化 ----------


def test_bootstrap_rejects_wrong_deployment_token(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/bootstrap",
        headers={"X-Bootstrap-Token": "guessed-token"},
        json={"email": "owner@example.com", "password": PASSWORD, "tenant_name": "甲方空间"},
    )
    assert response.status_code == 403
    assert "docpilot_session" not in client.cookies
    with SessionLocal() as db:
        assert db.query(User).count() == 0


def test_bootstrap_creates_admin_session_and_tenant(client: TestClient) -> None:
    response = _bootstrap(client)
    assert response.status_code == 201, response.text
    body = response.json()
    assert len(body["csrf_token"]) >= 16

    payload = client.get("/api/v1/auth/me").json()
    assert payload["user"]["email"] == "owner@example.com"
    assert payload["user"]["is_platform_admin"] is True
    assert payload["tenants"] == [{"id": body["tenant_id"], "name": "甲方空间", "role": "owner"}]


def test_bootstrap_is_single_use(client: TestClient) -> None:
    assert _bootstrap(client).status_code == 201
    assert _bootstrap(client, email="second@example.com", tenant_name="乙方空间").status_code == 409


def test_bootstrap_rejects_foreign_origin(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/bootstrap",
        headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN, "Origin": "https://evil.example"},
        json={"email": "owner@example.com", "password": PASSWORD, "tenant_name": "甲方空间"},
    )
    assert response.status_code == 403


def test_bootstrap_rejects_weak_password(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/bootstrap",
        headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
        json={"email": "owner@example.com", "password": "short", "tenant_name": "甲方空间"},
    )
    assert response.status_code == 422


# ---------- 登录与会话 ----------


def test_login_rejects_wrong_password(identity) -> None:
    identity("member@example.com", "乙方空间")
    response = TestClient(app).post(
        "/api/v1/auth/login",
        json={"email": "member@example.com", "password": "wrong-password-1"},
    )
    assert response.status_code == 401
    assert "docpilot_session" not in response.cookies


def test_login_rejects_deactivated_user(identity) -> None:
    member = identity("member@example.com", "乙方空间")
    with SessionLocal() as db:
        db.get(User, member["user_id"]).is_active = False
        db.commit()
    response = TestClient(app).post(
        "/api/v1/auth/login", json={"email": "member@example.com", "password": PASSWORD}
    )
    assert response.status_code == 401


def test_login_issues_session_for_existing_member(identity) -> None:
    member = identity("member@example.com", "乙方空间")
    payload = member["client"].get("/api/v1/auth/me").json()
    assert payload["user"]["is_platform_admin"] is False
    assert payload["tenants"] == [{"id": member["tenant_id"], "name": "乙方空间", "role": "member"}]


def test_logout_revokes_session(owner_workspace: dict) -> None:
    client, csrf = owner_workspace["client"], owner_workspace["csrf"]
    assert client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401


def test_csrf_refresh_invalidates_previous_token(owner_workspace: dict) -> None:
    client, csrf = owner_workspace["client"], owner_workspace["csrf"]
    refreshed = client.get("/api/v1/auth/csrf")
    assert refreshed.status_code == 200

    stale = client.post("/api/v1/tenants", json={"name": "新空间"}, headers={"X-CSRF-Token": csrf})
    assert stale.status_code == 403

    fresh = client.post(
        "/api/v1/tenants",
        json={"name": "新空间"},
        headers={"X-CSRF-Token": refreshed.json()["csrf_token"]},
    )
    assert fresh.status_code == 201


# ---------- 写操作 CSRF ----------


def test_write_without_csrf_header_is_rejected(owner_workspace: dict) -> None:
    response = owner_workspace["client"].post("/api/v1/tenants", json={"name": "缺 CSRF"})
    assert response.status_code == 403


def test_member_can_create_second_workspace(owner_workspace: dict) -> None:
    client, csrf = owner_workspace["client"], owner_workspace["csrf"]
    created = client.post(
        "/api/v1/tenants", json={"name": "乙方空间"}, headers={"X-CSRF-Token": csrf}
    )
    assert created.status_code == 201
    names = [item["name"] for item in client.get("/api/v1/auth/me").json()["tenants"]]
    assert names == sorted(["甲方空间", "乙方空间"])


# ---------- 租户边界 ----------


def test_non_member_cannot_open_other_tenant(owner_workspace: dict, other_workspace: dict) -> None:
    response = other_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{NO_SUCH_JOB}"
    )
    assert response.status_code == 403


def test_removed_membership_is_rejected(owner_workspace: dict) -> None:
    tenant_id = owner_workspace["tenant_id"]
    with SessionLocal() as db:
        db.query(Membership).filter(Membership.tenant_id == tenant_id).one().status = "removed"
        db.commit()
    assert (
        owner_workspace["client"].get(f"/api/v1/tenants/{tenant_id}/jobs/{NO_SUCH_JOB}").status_code
        == 403
    )


def test_suspended_tenant_is_rejected(owner_workspace: dict) -> None:
    tenant_id = owner_workspace["tenant_id"]
    with SessionLocal() as db:
        db.get(Tenant, tenant_id).status = "suspended"
        db.commit()
    assert (
        owner_workspace["client"].get(f"/api/v1/tenants/{tenant_id}/jobs/{NO_SUCH_JOB}").status_code
        == 403
    )


# ---------- 旧演示接口 ----------


def test_index_serves_built_frontend_or_reports_unbuilt(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """两条分支都要断言到：有构建产物就服务它，没有就 503 说清下一步。

    原来这里没有分支覆盖，只有一条"根路径返回旧演示页"的断言；那个页面依赖已退役的
    `/api/jobs`，留着它就是留一个点开即 404 的界面。用 skip 躲开"本机恰好已构建"会让
    这条用例在这台机器上什么都不验证，所以改成把 `_FRONTEND_DIST` 指到临时目录。
    """
    monkeypatch.setattr("app.main._FRONTEND_DIST", tmp_path / "missing")
    unbuilt = client.get("/")
    assert unbuilt.status_code == 503
    assert "前端尚未构建" in unbuilt.json()["detail"]

    built = tmp_path / "built"
    built.mkdir()
    (built / "index.html").write_text("<!doctype html><title>DocPilot</title>", encoding="utf-8")
    monkeypatch.setattr("app.main._FRONTEND_DIST", built)
    served = client.get("/")
    assert served.status_code == 200
    assert "doctype" in served.text.lower()


def test_tenantless_job_api_is_gone_in_every_environment(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无租户任务接口已退役：不再依赖"只在开发环境暴露"的门禁，而是根本不存在。

    原来这里测的是 `ensure_legacy_demo_enabled` 那道环境门禁（开发环境 200、生产 404）。
    门禁挡不住误配的部署，退役后接口在所有环境下都是 404。
    """
    upload = {"files": ("a.pdf", b"%PDF-1.4", "application/pdf")}
    form = {"fields": "[]"}

    for env in ("development", "production"):
        monkeypatch.setattr("app.config.APP_ENV", env)
        assert client.post("/api/jobs", files=upload, data=form).status_code == 404
        assert client.get("/api/jobs/" + NO_SUCH_JOB).status_code == 404
        assert client.get(f"/api/jobs/{NO_SUCH_JOB}/excel").status_code == 404
