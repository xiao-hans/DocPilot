"""审计事件的写入、边界与不可变性（TC-SEC-009、TC-FUNC-011、TC-REC-003）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models.access import AuditEvent, Invitation
from app.models.schema import INVOICE_TEMPLATE
from app.services import audit

FIELDS = json.dumps(INVOICE_TEMPLATE)


def _audit_events(workspace: dict):
    return workspace["client"].get(
        f"/api/v1/tenants/{workspace['tenant_id']}/audit-events"
    )


def _upload(workspace: dict, pdf: Path):
    return workspace["client"].post(
        f"/api/v1/tenants/{workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": workspace["csrf"]},
        files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))],
        data={"fields": FIELDS},
    )


def test_audit_pagination_is_descending_and_stable(owner_workspace: dict, sandbox: Path, make_invoice_pdf):
    for index in range(2):
        assert _upload(owner_workspace, make_invoice_pdf(f"011002000111000452{index:02d}")).status_code == 201
    base = f"/api/v1/tenants/{owner_workspace['tenant_id']}/audit-events"

    first_page = owner_workspace["client"].get(f"{base}?limit=1")
    assert first_page.status_code == 200
    events = first_page.json()["events"]
    assert len(events) == 1

    second = owner_workspace["client"].get(f"{base}?limit=1&before={events[0]['created_at']}")
    later = second.json()["events"]
    assert len(later) == 1
    assert later[0]["id"] != events[0]["id"]
    assert later[0]["created_at"] <= events[0]["created_at"]

    assert owner_workspace["client"].get(f"{base}?limit=500").status_code == 422


def test_upload_and_export_write_audit_events(owner_workspace: dict, sandbox: Path, make_invoice_pdf):
    job_id = _upload(owner_workspace, make_invoice_pdf()).json()["job_id"]
    actions = [event["action"] for event in _audit_events(owner_workspace).json()["events"]]
    assert "job.created" in actions

    owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{job_id}/excel"
    )
    events = _audit_events(owner_workspace).json()["events"]
    created = next(event for event in events if event["action"] == "job.created")
    assert created["object_id"] == job_id
    assert created["metadata"] == {"file_count": 1}
    assert created["actor_user_id"] == owner_workspace["user_id"]
    assert "job.excel_exported" in [event["action"] for event in events]

    dumped = json.dumps(events, ensure_ascii=False)
    assert str(sandbox) not in dumped and ".sqlite3" not in dumped


def test_unregistered_metadata_keys_are_dropped():
    event = audit.new_event(
        tenant_id="t" * 32,
        actor_user_id=None,
        action="job.created",
        object_type="job",
        object_id="j" * 32,
        metadata={"file_count": 3, "absolute_path": "/srv/uploads/x.pdf", "token": "secret"},
    )
    assert event.metadata_ == {"file_count": 3}


def test_unregistered_action_is_rejected():
    with pytest.raises(ValueError):
        audit.new_event(
            tenant_id="t" * 32,
            actor_user_id=None,
            action="job.destroyed",
            object_type="job",
            object_id="j" * 32,
        )


def test_audit_events_are_not_writable_through_the_api(
    owner_workspace: dict, other_workspace: dict
):
    tenant_id = owner_workspace["tenant_id"]
    foreign = other_workspace["client"].get(f"/api/v1/tenants/{tenant_id}/audit-events")
    assert foreign.status_code == 403

    schema = app.openapi()
    path = f"/api/v1/tenants/{{tenant_id}}/audit-events"
    assert set(schema["paths"][path].keys()) == {"get"}


def test_audit_and_invitation_state_survive_restart(owner_workspace: dict, sandbox: Path):
    created = owner_workspace["client"].post(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/invitations",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        json={"email": "persistence@example.com", "role": "admin"},
    )
    assert created.status_code == 201
    invitation_id = created.json()["invitation_id"]

    with TestClient(app) as restarted:
        restarted.cookies.clear()
        # 用同一账号重新登录，验证事件与邀请状态在重启后仍可读。
        login = restarted.post(
            "/api/v1/auth/login",
            json={"email": owner_workspace["email"], "password": "test-password-1234"},
        )
        assert login.status_code == 200
        events = restarted.get(
            f"/api/v1/tenants/{owner_workspace['tenant_id']}/audit-events",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
        ).json()["events"]
        assert "member.invited" in [event["action"] for event in events]

    with SessionLocal() as db:
        invitation = db.get(Invitation, invitation_id)
        assert invitation.status == "pending"
        assert len(invitation.token_hash) == 64
        assert created.json()["token"] not in str(invitation.token_hash)
        assert db.query(AuditEvent).count() == len(events)
