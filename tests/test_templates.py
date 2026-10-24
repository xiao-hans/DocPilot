"""模板与不可变版本（DOC-DD-004 §4/§6/§7.1，用例 TC-FUNC-014..016、TC-SEC-011/012）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.platform.db import SessionLocal
from app.main import app
from app.models.persistent_job import PersistentJob
from app.models.schema import INVOICE_TEMPLATE

TWO_FIELDS = [
    {"name": "发票号码", "type": "text", "description": "20 位发票号码"},
    {"name": "开票日期", "type": "date", "description": "开票日期 YYYY-MM-DD"},
]
THREE_FIELDS = TWO_FIELDS + [
    {"name": "金额", "type": "number", "description": "价税合计金额"}
]


def _base(workspace: dict) -> str:
    return f"/api/v1/tenants/{workspace['tenant_id']}/templates"


def _call(workspace: dict, method: str, path: str, **kwargs):
    client = workspace["client"]
    headers = kwargs.pop("headers", {})
    if method.upper() not in ("GET", "HEAD", "OPTIONS"):
        headers = {"X-CSRF-Token": workspace["csrf"], **headers}
    return getattr(client, method.lower())(path, headers=headers, **kwargs)


def _create(workspace: dict, name: str = "发票模板", fields: list[dict] | None = None):
    return _call(
        workspace,
        "post",
        _base(workspace),
        json={"name": name, "fields": TWO_FIELDS if fields is None else fields},
    )


def _publish(workspace: dict, template_id: str):
    return _call(workspace, "post", f"{_base(workspace)}/{template_id}/publish")


def test_template_lifecycle_is_tenant_scoped(owner_workspace: dict) -> None:
    created = _create(owner_workspace)
    assert created.status_code == 201, created.text
    template_id = created.json()["id"]
    assert created.json()["status"] == "draft"
    assert created.json()["latest_revision"] == 0

    listing = _call(owner_workspace, "get", _base(owner_workspace)).json()["templates"]
    assert [item["id"] for item in listing] == [template_id]

    other = _create(owner_workspace, name="第二个模板")
    assert other.status_code == 201
    duplicate = _create(owner_workspace, name="第二个模板")
    assert duplicate.status_code == 409


def test_publishing_snapshots_fields_and_keeps_history(owner_workspace: dict, member_of) -> None:
    template_id = _create(owner_workspace).json()["id"]

    first = _publish(owner_workspace, template_id)
    assert first.status_code == 201, first.text
    assert first.json()["revision"] == 1
    assert first.json()["fields"] == TWO_FIELDS

    patched = _call(
        owner_workspace,
        "patch",
        f"{_base(owner_workspace)}/{template_id}",
        json={"fields": THREE_FIELDS},
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "active"

    second = _publish(owner_workspace, template_id)
    assert second.json()["revision"] == 2
    assert second.json()["fields"] == THREE_FIELDS

    detail = _call(owner_workspace, "get", f"{_base(owner_workspace)}/{template_id}").json()
    revisions = {item["revision"]: item["fields"] for item in detail["versions"]}
    assert revisions[1] == TWO_FIELDS and revisions[2] == THREE_FIELDS
    assert {item["id"] for item in detail["versions"]} == {
        first.json()["template_version_id"],
        second.json()["template_version_id"],
    }


def test_invalid_template_payloads_are_rejected(owner_workspace: dict) -> None:
    assert _create(owner_workspace, fields=[]).status_code == 422
    assert _call(
        owner_workspace,
        "post",
        _base(owner_workspace),
        json={"name": "坏字段", "fields": [{"name": "只有名字"}]},
    ).status_code == 422
    missing = _call(owner_workspace, "get", f"{_base(owner_workspace)}/{'0' * 32}")
    assert missing.status_code == 404


def test_job_created_from_template_version_freezes_fields(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    template_id = _create(owner_workspace).json()["id"]
    version_id = _publish(owner_workspace, template_id).json()["template_version_id"]

    created = owner_workspace["client"].post(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        files=[("files", (make_invoice_pdf().name, make_invoice_pdf().read_bytes(), "application/pdf"))],
        data={"template_version_id": version_id},
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["job_id"]

    detail = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{job_id}"
    ).json()
    assert detail["status"] == "succeeded"
    assert detail["template_version_id"] == version_id
    assert {field["name"] for field in detail["fields"]} == {"发票号码", "开票日期"}

    result = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{job_id}/result"
    ).json()
    assert set(result["result"][0]["values"]) == {"发票号码", "开票日期"}
    assert result["attempt_count"] == 1
    assert result["finished_at"] is not None

    attempts = detail["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["engine"] == "mock"
    assert attempts[0]["status"] == "succeeded"
    assert attempts[0]["parse_modes"] == ["text"]

    # 模板后续发布新版本不得改变历史任务的字段集。
    _call(owner_workspace, "patch", f"{_base(owner_workspace)}/{template_id}", json={"fields": THREE_FIELDS})
    _publish(owner_workspace, template_id)
    again = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{job_id}"
    ).json()
    assert {field["name"] for field in again["fields"]} == {"发票号码", "开票日期"}


def test_conflicting_inline_fields_and_template_are_rejected(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    template_id = _create(owner_workspace).json()["id"]
    version_id = _publish(owner_workspace, template_id).json()["template_version_id"]

    response = owner_workspace["client"].post(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        files=[("files", (make_invoice_pdf().name, make_invoice_pdf().read_bytes(), "application/pdf"))],
        data={"template_version_id": version_id, "fields": json.dumps(INVOICE_TEMPLATE)},
    )
    assert response.status_code == 422
    with SessionLocal() as db:
        assert db.query(PersistentJob).count() == 0


def test_retired_template_blocks_new_jobs_but_history_stays_readable(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf
) -> None:
    template_id = _create(owner_workspace).json()["id"]
    version_id = _publish(owner_workspace, template_id).json()["template_version_id"]
    job_id = owner_workspace["client"].post(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        files=[("files", (make_invoice_pdf().name, make_invoice_pdf().read_bytes(), "application/pdf"))],
        data={"template_version_id": version_id},
    ).json()["job_id"]

    copied = _call(owner_workspace, "post", f"{_base(owner_workspace)}/{template_id}/copy", json={})
    assert copied.status_code == 201
    assert copied.json()["status"] == "draft"
    assert copied.json()["name"] == "发票模板 副本"
    assert copied.json()["draft_fields"] == TWO_FIELDS

    retired = _call(owner_workspace, "post", f"{_base(owner_workspace)}/{template_id}/retire")
    assert retired.status_code == 200

    blocked = owner_workspace["client"].post(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        files=[("files", ("second.pdf", make_invoice_pdf("01100200011100045240").read_bytes(), "application/pdf"))],
        data={"template_version_id": version_id},
    )
    assert blocked.status_code == 409

    assert owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{job_id}"
    ).status_code == 200
    assert owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs/{job_id}/excel"
    ).status_code == 200


@pytest.mark.parametrize("role, allowed", [("reviewer", False), ("member", False), ("admin", True), ("owner", True)])
def test_template_write_roles(owner_workspace: dict, member_of, role: str, allowed: bool) -> None:
    member = member_of(f"tpl-{role}@example.com", owner_workspace["tenant_id"], role)
    assert _call(member, "get", _base(member)).status_code == 200

    created = _create(member)
    assert (created.status_code == 201) is allowed
    if not allowed:
        assert created.status_code == 403
        with SessionLocal() as db:
            from app.contexts.templates.domain import Template

            assert db.query(Template).count() == 0


def test_cross_tenant_template_references_are_denied(
    owner_workspace: dict, identity, sandbox: Path, make_invoice_pdf
) -> None:
    template_id = _create(owner_workspace).json()["id"]
    version_id = _publish(owner_workspace, template_id).json()["template_version_id"]

    # 另一个租户的所有者：角色满足，但资源不属于该租户，必须按存在性拒绝。
    other = identity("other-owner@example.com", "丁方空间", "owner")
    assert _call(other, "get", f"{_base(other)}/{template_id}").status_code == 404
    denied = _call(other, "post", f"{_base(other)}/{template_id}/retire")
    assert denied.status_code == 404, denied.text

    response = other["client"].post(
        f"/api/v1/tenants/{other['tenant_id']}/jobs",
        headers={"X-CSRF-Token": other["csrf"]},
        files=[("files", (make_invoice_pdf().name, make_invoice_pdf().read_bytes(), "application/pdf"))],
        data={"template_version_id": version_id},
    )
    assert response.status_code == 404
    with SessionLocal() as db:
        assert db.query(PersistentJob).count() == 0


def test_referenced_template_version_cannot_be_deleted(owner_workspace: dict, sandbox: Path, make_invoice_pdf) -> None:
    template_id = _create(owner_workspace).json()["id"]
    version_id = _publish(owner_workspace, template_id).json()["template_version_id"]
    owner_workspace["client"].post(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        files=[("files", (make_invoice_pdf().name, make_invoice_pdf().read_bytes(), "application/pdf"))],
        data={"template_version_id": version_id},
    )

    with SessionLocal() as db:
        from app.contexts.templates.domain import TemplateVersion

        with pytest.raises(IntegrityError):
            db.execute(
                TemplateVersion.__table__.delete().where(TemplateVersion.id == version_id)
            )
        db.rollback()

    schema = app.openapi()
    template_paths = [path for path in schema["paths"] if path.endswith("/templates/{template_id}")]
    assert "delete" not in schema["paths"][template_paths[0]]


@pytest.mark.parametrize(
    "role, allowed",
    [("reviewer", False), ("member", False), ("admin", True), ("owner", True)],
)
def test_all_template_write_endpoints_enforce_roles(
    owner_workspace: dict, member_of, role: str, allowed: bool
) -> None:
    """DOC-DD-004 §6：模板写面只允许 admin/owner，逐端点断言（TC-SEC-011）。"""
    member = member_of(f"role-{role}@example.com", owner_workspace["tenant_id"], role)
    created = _create(owner_workspace, name=f"角色矩阵{role}")
    assert created.status_code == 201
    template_id = created.json()["id"]

    patch = _call(member, "patch", f"{_base(member)}/{template_id}", json={"name": "改名"})
    publish = _call(member, "post", f"{_base(member)}/{template_id}/publish")
    copy = _call(member, "post", f"{_base(member)}/{template_id}/copy", json={})
    retire = _call(member, "post", f"{_base(member)}/{template_id}/retire")

    if allowed:
        assert (patch.status_code, publish.status_code, copy.status_code) == (
            200,
            201,
            201,
        ), (patch.text, publish.text, copy.text)
        assert retire.status_code == 200
    else:
        assert {item.status_code for item in (patch, publish, copy, retire)} == {403}
        with SessionLocal() as db:
            from app.contexts.templates.domain import TemplateVersion

            assert db.query(TemplateVersion).count() == 0
