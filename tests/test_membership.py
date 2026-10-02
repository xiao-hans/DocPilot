"""角色矩阵、成员管理与邀请生命周期（DOC-DD-003 §3/§5/§6，用例 TC-SEC-006..010、TC-FUNC-007..013）。"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import SessionLocal
from app.main import app
from app.models.access import AuditEvent, Invitation
from app.models.account import Membership, User
from app.models.persistent_job import PersistentJob
from app.models.schema import INVOICE_TEMPLATE
from app.services import membership

FIELDS = json.dumps(INVOICE_TEMPLATE)


def _upload(workspace: dict, pdf: Path, tenant_id: str | None = None):
    return workspace["client"].post(
        f"/api/v1/tenants/{tenant_id or workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": workspace["csrf"]},
        files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))],
        data={"fields": FIELDS},
    )


def _tenant_path(workspace: dict, suffix: str) -> str:
    return f"/api/v1/tenants/{workspace['tenant_id']}{suffix}"


def _set_role(user_id: str, tenant_id: str, role: str) -> None:
    with SessionLocal() as db:
        row = db.scalar(
            select(Membership).where(
                Membership.tenant_id == tenant_id, Membership.user_id == user_id
            )
        )
        row.role = role
        db.commit()


# ---------- TC-SEC-006：上传动作的角色矩阵 ----------


@pytest.mark.parametrize(
    "role, allowed",
    [("reviewer", False), ("member", True), ("admin", True), ("owner", True)],
)
def test_only_member_and_above_may_create_jobs(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, member_of, role: str, allowed: bool
) -> None:
    member = member_of(f"member-{role}@example.com", owner_workspace["tenant_id"], role)
    response = _upload(member, make_invoice_pdf())

    if allowed:
        assert response.status_code == 201, response.text
        with SessionLocal() as db:
            assert db.get(PersistentJob, response.json()["job_id"]) is not None
    else:
        assert response.status_code == 403
        assert "权限" in response.json()["detail"]
        with SessionLocal() as db:
            assert db.query(PersistentJob).count() == 0


@pytest.mark.parametrize("role", ["reviewer", "member", "admin", "owner"])
def test_any_active_role_may_read_and_export(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, member_of, role: str
) -> None:
    job_id = _upload(owner_workspace, make_invoice_pdf()).json()["job_id"]
    member = member_of(f"member-{role}@example.com", owner_workspace["tenant_id"], role)

    assert member["client"].get(_tenant_path(member, f"/jobs/{job_id}")).status_code == 200
    assert member["client"].get(_tenant_path(member, f"/jobs/{job_id}/excel")).status_code == 200


# ---------- TC-SEC-007：平台管理员不产生租户权限 ----------


def test_platform_admin_without_membership_is_denied_everywhere(
    owner_workspace: dict,
    sandbox: Path,
    make_invoice_pdf,
    platform_admin_without_membership,
) -> None:
    ops = platform_admin_without_membership()
    tenant_id = owner_workspace["tenant_id"]
    job_id = _upload(owner_workspace, make_invoice_pdf()).json()["job_id"]

    assert ops["client"].get(f"/api/v1/tenants/{tenant_id}/jobs/{job_id}").status_code == 403
    assert _upload(ops, make_invoice_pdf(), tenant_id).status_code == 403
    assert ops["client"].get("/api/v1/tenants").json()["tenants"] == []


def test_platform_admin_can_create_and_use_own_workspace(
    platform_admin_without_membership, sandbox: Path
) -> None:
    ops = platform_admin_without_membership("ops2@example.com")
    created = ops["client"].post(
        "/api/v1/tenants", json={"name": "运营空间"}, headers={"X-CSRF-Token": ops["csrf"]}
    )
    assert created.status_code == 201
    assert ops["client"].get("/api/v1/auth/me").json()["user"]["is_platform_admin"] is True


# ---------- 成员列表与管理面可见性（TC-SEC-010） ----------


def test_members_are_visible_to_every_active_role(owner_workspace: dict, member_of) -> None:
    reviewer = member_of("member-reviewer@example.com", owner_workspace["tenant_id"], "reviewer")
    listing = reviewer["client"].get(_tenant_path(reviewer, "/members"))
    assert listing.status_code == 200
    emails = {item["email"] for item in listing.json()["members"]}
    assert {"member-reviewer@example.com", "owner@example.com"} <= emails


@pytest.mark.parametrize("role", ["reviewer", "member"])
def test_management_surfaces_are_hidden_to_low_roles(
    owner_workspace: dict, member_of, role: str
) -> None:
    low = member_of(f"low-{role}@example.com", owner_workspace["tenant_id"], role)
    assert low["client"].get(_tenant_path(low, "/invitations")).status_code == 403
    assert low["client"].get(_tenant_path(low, "/audit-events")).status_code == 403
    assert (
        low["client"].post(
            _tenant_path(low, "/invitations"), json={"email": "x@y.co", "role": "member"}
        ).status_code
        == 403
    )


# ---------- TC-FUNC-007 / TC-FUNC-008：邀请生命周期 ----------


def _invite(workspace: dict, email: str, role: str = "member"):
    return workspace["client"].post(
        _tenant_path(workspace, "/invitations"),
        headers={"X-CSRF-Token": workspace["csrf"]},
        json={"email": email, "role": role},
    )


def _accept(client: TestClient, csrf: str, token: str):
    return client.post(
        "/api/v1/invitations/accept", headers={"X-CSRF-Token": csrf}, json={"token": token}
    )


def _force_expire(invitation_id: str) -> None:
    with SessionLocal() as db:
        db.get(Invitation, invitation_id).expires_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        db.commit()


def test_invited_user_joins_tenant_and_is_audited(
    owner_workspace: dict, member_of, identity
) -> None:
    admin = member_of("member-admin@example.com", owner_workspace["tenant_id"], "admin")
    created = _invite(admin, "Newcomer@Example.com ")
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["email"] == "newcomer@example.com"
    assert len(body["token"]) >= 32

    newcomer = identity("newcomer@example.com", "自有空间")
    accepted = _accept(newcomer["client"], newcomer["csrf"], body["token"])
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {
        "tenant_id": owner_workspace["tenant_id"],
        "tenant_name": "甲方空间",
        "role": "member",
    }

    members = admin["client"].get(_tenant_path(admin, "/members")).json()["members"]
    assert "newcomer@example.com" in {item["email"] for item in members}
    assert len(newcomer["client"].get("/api/v1/auth/me").json()["tenants"]) == 2

    listing = admin["client"].get(_tenant_path(admin, "/invitations")).json()["invitations"]
    mine = [item for item in listing if item["id"] == body["invitation_id"]]
    assert [item["status"] for item in mine] == ["accepted"]
    assert all("token" not in item and "token_hash" not in item for item in listing)

    actions = [
        event["action"]
        for event in admin["client"].get(_tenant_path(admin, "/audit-events")).json()["events"]
    ]
    assert {"member.invited", "member.invitation_accepted"} <= set(actions)


def test_invitation_negative_paths(
    owner_workspace: dict, identity, member_of, client: TestClient
) -> None:
    tenant_id = owner_workspace["tenant_id"]

    duplicate = _invite(owner_workspace, "dup@example.com")
    assert duplicate.status_code == 201
    assert _invite(owner_workspace, "DUP@example.com").status_code == 409

    wrong = identity("someone-else@example.com", "自有空间")
    token = duplicate.json()["token"]
    assert _accept(wrong["client"], wrong["csrf"], token).status_code == 403
    with SessionLocal() as db:
        assert db.get(Invitation, duplicate.json()["invitation_id"]).status == "pending"

    replay = _invite(owner_workspace, "replay@example.com").json()
    replay_member = identity("replay@example.com", "自有空间")
    assert _accept(replay_member["client"], replay_member["csrf"], replay["token"]).status_code == 200
    assert _accept(replay_member["client"], replay_member["csrf"], replay["token"]).status_code == 410

    revoked = _invite(owner_workspace, "revoked@example.com").json()
    revoke = owner_workspace["client"].delete(
        f"/api/v1/tenants/{tenant_id}/invitations/{revoked['invitation_id']}",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
    )
    assert revoke.status_code == 200
    revoked_member = identity("revoked@example.com", "自有空间")
    assert _accept(revoked_member["client"], revoked_member["csrf"], revoked["token"]).status_code == 410

    expired = _invite(owner_workspace, "expired@example.com").json()
    _force_expire(expired["invitation_id"])
    expired_member = identity("expired@example.com", "自有空间")
    assert _accept(expired_member["client"], expired_member["csrf"], expired["token"]).status_code == 410
    with SessionLocal() as db:
        assert db.get(Invitation, expired["invitation_id"]).status == "expired"
    late_revoke = owner_workspace["client"].delete(
        f"/api/v1/tenants/{tenant_id}/invitations/{expired['invitation_id']}",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
    )
    assert late_revoke.status_code == 409

    anonymous = TestClient(app)
    assert anonymous.post("/api/v1/invitations/accept", json={"token": "x" * 40}).status_code == 401
    assert (
        owner_workspace["client"].post(
            "/api/v1/invitations/accept", json={"token": "x" * 40}
        ).status_code
        == 403
    )  # 已登录但缺少 CSRF 头
    auditor = member_of("auditor@example.com", tenant_id, "admin")
    unknown = _accept(auditor["client"], auditor["csrf"], "not-a-real-token-1234567890")
    assert unknown.status_code == 410
    assert unknown.json()["detail"] == membership.INVALID_INVITATION


def test_removed_member_rejoins_via_new_invitation(
    owner_workspace: dict, member_of
) -> None:
    tenant_id = owner_workspace["tenant_id"]
    member = member_of("bounced@example.com", tenant_id, "member")
    owner_workspace["client"].delete(
        f"/api/v1/tenants/{tenant_id}/members/{member['user_id']}",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
    )

    invitation = _invite(owner_workspace, "bounced@example.com", "admin").json()
    accepted = _accept(member["client"], member["csrf"], invitation["token"])
    assert accepted.status_code == 200, accepted.text
    # 复活沿用原角色：邀请不得成为 admin 提升或降低既有成员权限的通道。
    assert accepted.json()["role"] == "member"
    with SessionLocal() as db:
        row = db.scalar(
            select(Membership).where(
                Membership.tenant_id == tenant_id, Membership.user_id == member["user_id"]
            )
        )
        assert (row.status, row.role) == ("active", "member")
        assert db.get(Invitation, invitation["invitation_id"]).status == "accepted"


# ---------- 邀请与审计的输入契约 ----------


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"email": "a@b.co", "role": "owner"}, 422),
        ({"email": "a@b.co", "role": "superuser"}, 422),
        ({"email": "not-an-email", "role": "member"}, 422),
    ],
)
def test_invitation_request_validation(owner_workspace: dict, payload: dict, expected: int) -> None:
    response = _invite(owner_workspace, payload["email"], payload.get("role", "member"))
    assert response.status_code == expected
    with SessionLocal() as db:
        assert db.query(Invitation).count() == 0


def test_active_member_cannot_reaccept(owner_workspace: dict, member_of) -> None:
    member = member_of("already@example.com", owner_workspace["tenant_id"], "member")
    invitation = _invite(owner_workspace, "already@example.com").json()
    refused = _accept(member["client"], member["csrf"], invitation["token"])
    assert refused.status_code == 409
    with SessionLocal() as db:
        assert db.get(Invitation, invitation["invitation_id"]).status == "pending"


# ---------- TC-FUNC-012：并发接受只生效一次 ----------


def test_concurrent_accept_creates_a_single_membership(
    owner_workspace: dict, identity
) -> None:
    tenant_id = owner_workspace["tenant_id"]
    invitation = _invite(owner_workspace, "race@example.com").json()
    joined = identity("race@example.com", "自有空间")
    outcomes: list[object] = []
    barrier = threading.Barrier(2)

    def attempt() -> None:
        with SessionLocal() as db:
            user = db.get(User, joined["user_id"])
            barrier.wait()
            try:
                outcomes.append(
                    membership.accept_invitation(db, user=user, token=invitation["token"])
                )
            except membership.AccessError as exc:
                outcomes.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    successes = [item for item in outcomes if isinstance(item, dict)]
    assert len(successes) == 1, outcomes
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Membership)
                .where(Membership.tenant_id == tenant_id, Membership.user_id == joined["user_id"])
            )
            == 1
        )
        accepted_audits = db.scalars(
            select(AuditEvent.id).where(AuditEvent.action == "member.invitation_accepted")
        ).all()
        assert len(accepted_audits) == 1


# ---------- TC-SEC-008 / TC-FUNC-013：移除成员立即失效且归属不变 ----------


def test_removed_member_loses_access_with_same_session(
    owner_workspace: dict,
    sandbox: Path,
    make_invoice_pdf,
    member_of,
    other_workspace: dict,
    add_membership,
) -> None:
    tenant_id = owner_workspace["tenant_id"]
    member = member_of("leaver@example.com", tenant_id, "member")
    job_id = _upload(owner_workspace, make_invoice_pdf()).json()["job_id"]
    assert member["client"].get(_tenant_path(member, f"/jobs/{job_id}")).status_code == 200

    # 同一用户在另一个租户也有成员关系：移除只影响被移除的那个租户。
    foreign_job = _upload(other_workspace, make_invoice_pdf()).json()["job_id"]
    add_membership(member["user_id"], other_workspace["tenant_id"], "member")
    other_path = f"/api/v1/tenants/{other_workspace['tenant_id']}/jobs/{foreign_job}"
    assert member["client"].get(other_path).status_code == 200

    removed = owner_workspace["client"].delete(
        f"/api/v1/tenants/{tenant_id}/members/{member['user_id']}",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
    )
    assert removed.status_code == 200, removed.text

    # 会话没有被撤销，但授权判定即时失败。
    assert member["client"].get(_tenant_path(member, f"/jobs/{job_id}")).status_code == 403
    assert _upload(member, make_invoice_pdf()).status_code == 403
    assert member["client"].get("/api/v1/auth/me").status_code == 200
    assert member["client"].get(other_path).status_code == 200

    with SessionLocal() as db:
        assert (
            db.scalar(
                select(Membership).where(
                    Membership.tenant_id == tenant_id, Membership.user_id == member["user_id"]
                )
            ).status
            == "removed"
        )
        assert db.get(PersistentJob, job_id).owner_id == owner_workspace["user_id"]


# ---------- P0 回归：旧演示接口不得触达租户任务 ----------


def test_legacy_demo_api_cannot_reach_tenant_scoped_jobs(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, client: TestClient
) -> None:
    job_id = _upload(owner_workspace, make_invoice_pdf()).json()["job_id"]
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    assert client.get(f"/api/jobs/{job_id}/result").status_code == 404
    assert client.get(f"/api/jobs/{job_id}/excel").status_code == 404


# ---------- TC-FUNC-010：移除保护规则 ----------


@pytest.mark.parametrize(
    "actor_role, target_role, expected",
    [
        ("admin", "admin", 403),
        ("admin", "owner", 403),
        ("admin", "member", 200),
        ("owner", "member", 200),
    ],
)
def test_removal_protection_rules(
    owner_workspace: dict, member_of, actor_role: str, target_role: str, expected: int
) -> None:
    tenant_id = owner_workspace["tenant_id"]
    target = member_of(f"target-{target_role}@example.com", tenant_id, target_role)
    if target_role == "owner":
        _set_role(target["user_id"], tenant_id, "owner")
    actor = owner_workspace if actor_role == "owner" else member_of(
        f"actor-{actor_role}@example.com", tenant_id, actor_role
    )

    response = actor["client"].delete(
        f"/api/v1/tenants/{tenant_id}/members/{target['user_id']}",
        headers={"X-CSRF-Token": actor["csrf"]},
    )
    assert response.status_code == expected, response.text


def test_owner_cannot_remove_self_or_last_owner(owner_workspace: dict) -> None:
    blocked = owner_workspace["client"].delete(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/members/{owner_workspace['user_id']}",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
    )
    assert blocked.status_code == 409


def test_second_owner_may_be_removed_while_one_remains(owner_workspace: dict, member_of) -> None:
    tenant_id = owner_workspace["tenant_id"]
    second = member_of("second-owner@example.com", tenant_id, "admin")
    _set_role(second["user_id"], tenant_id, "owner")

    response = owner_workspace["client"].delete(
        f"/api/v1/tenants/{tenant_id}/members/{second['user_id']}",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
    )
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        owners = db.scalars(
            select(Membership.user_id).where(
                Membership.tenant_id == tenant_id,
                Membership.role == "owner",
                Membership.status == "active",
            )
        ).all()
    assert list(owners) == [owner_workspace["user_id"]]


# ---------- TC-FUNC-009：角色变更 ----------


@pytest.mark.parametrize(
    "actor_role, target_role, expected",
    [("owner", "member", 200), ("admin", "member", 403), ("owner", "admin", 409)],
)
def test_role_change_rules(
    owner_workspace: dict, member_of, actor_role: str, target_role: str, expected: int
) -> None:
    tenant_id = owner_workspace["tenant_id"]
    target = member_of(f"target-{target_role}@example.com", tenant_id, target_role)
    if target_role == "admin":
        _set_role(target["user_id"], tenant_id, "owner")
    actor = owner_workspace if actor_role == "owner" else member_of(
        f"changer-{actor_role}@example.com", tenant_id, actor_role
    )

    response = actor["client"].patch(
        f"/api/v1/tenants/{tenant_id}/members/{target['user_id']}",
        headers={"X-CSRF-Token": actor["csrf"]},
        json={"role": "admin"},
    )
    assert response.status_code == expected, response.text
    if expected == 200:
        assert response.json() == {"user_id": target["user_id"], "role": "admin"}
        actions = [
            event["action"]
            for event in owner_workspace["client"].get(
                f"/api/v1/tenants/{tenant_id}/audit-events"
            ).json()["events"]
        ]
        assert "member.role_changed" in actions


def test_owner_cannot_change_own_role_and_invalid_role_is_rejected(
    owner_workspace: dict, member_of
) -> None:
    tenant_id = owner_workspace["tenant_id"]
    target = member_of("target-self@example.com", tenant_id, "member")

    self_change = owner_workspace["client"].patch(
        f"/api/v1/tenants/{tenant_id}/members/{owner_workspace['user_id']}",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        json={"role": "member"},
    )
    assert self_change.status_code == 409

    invalid = owner_workspace["client"].patch(
        f"/api/v1/tenants/{tenant_id}/members/{target['user_id']}",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        json={"role": "superuser"},
    )
    assert invalid.status_code == 422

    missing = owner_workspace["client"].patch(
        f"/api/v1/tenants/{tenant_id}/members/{'0' * 32}",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        json={"role": "member"},
    )
    assert missing.status_code == 404
