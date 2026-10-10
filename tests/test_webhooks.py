"""出站 Webhook：签名、轮换、幂等扇出、退避与目标校验（FR-041，DOC-DD-021 §8）。

每条正向都配一条能失败的判据：签名要能因为改一个字节而验不过、宽限窗口要能因为把窗口设为
0 而少一个头、私网目标要能因为打开开关而放行。
"""

from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import config
from app.contexts.webhooks import delivery, service, signing, targets
from app.contexts.webhooks.domain import WebhookDelivery, WebhookEndpoint
from app.db import SessionLocal
from app.services import outbox

MASTER = "test-master-key-please-rotate"

# 默认端点用 IP 字面量，不用域名。
#
# 出站策略会在投递前解析主机名，命中回环/内网即拒（DOC-DD-021 §5，这是设计而非缺陷）。
# 但"解析结果"属于运行环境：实测本机 DNS 把 `example.com` 解析到 `127.0.0.1`，
# 于是所有走真实投递的用例都变成 `error_kind=blocked_target`、`counters["sent"] == 0`，
# 报出来的却是"签名/退避坏了"。用公网 IP 字面量，判定就不再看本机 DNS。
PUBLIC_HOST = "93.184.216.34"


@pytest.fixture(autouse=True)
def master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "WEBHOOK_MASTER_KEY", MASTER, raising=False)
    monkeypatch.setattr(config, "WEBHOOK_ALLOW_PRIVATE_TARGETS", False, raising=False)
    monkeypatch.setattr(config, "WEBHOOK_ALLOW_INSECURE_TARGETS", False, raising=False)
    monkeypatch.setattr(config, "WEBHOOK_MAX_ATTEMPTS", 3, raising=False)
    monkeypatch.setattr(config, "WEBHOOK_BACKOFF_SECONDS", 60, raising=False)


@pytest.fixture(autouse=True)
def public_default_host() -> None:
    """`PUBLIC_HOST` 必须真的被判为放行。

    这条断言把"环境变了"和"策略坏了"分开：如果哪天出站策略把公网地址也拦下，
    用例会在这里红，而不是在五条投递用例里各自红一次。
    """
    assert targets.blocked_address(PUBLIC_HOST) is None
    assert not ipaddress.ip_address(PUBLIC_HOST).is_private


def _base(workspace: dict) -> str:
    return f"/api/v1/tenants/{workspace['tenant_id']}"


def _create_endpoint(workspace: dict, url: str = f"https://{PUBLIC_HOST}/hooks/docpilot", **payload):
    body = {"url": url, "event_types": ["job.succeeded"], "description": "账务系统"}
    body.update(payload)
    return workspace["client"].post(
        f"{_base(workspace)}/webhooks", headers={"X-CSRF-Token": workspace["csrf"]}, json=body
    )


def _record_event(tenant_id: str, event_type: str = "job.succeeded", *, occurred_at=None) -> str:
    with SessionLocal() as db:
        event = outbox.record(
            db,
            tenant_id=tenant_id,
            event_type=event_type,
            aggregate_type="job",
            aggregate_id="job-" + event_type.replace(".", "-"),
            payload={"job_id": "job-1", "document_count": 1},
        )
        if occurred_at is not None:
            event.occurred_at = occurred_at
        db.commit()
        return event.id


def _make_due_now() -> None:
    # 把待投行的到期时间拉回当下，等价于"时间过去了一个退避窗口"——判据仍走真实退避计算。
    with SessionLocal() as db:
        for row in db.scalars(select(WebhookDelivery)).all():
            row.next_attempt_at = datetime.now(timezone.utc)
        db.commit()


class FakeTransport:
    """记录真正发出的字节与头，并可编程返回状态。"""

    def __init__(self, status: int | None = 200, error_kind: str | None = None) -> None:
        self.calls: list[dict] = []
        self.status = status
        self.error_kind = error_kind

    def post(self, url, body, headers):
        self.calls.append({"url": url, "body": body, "headers": dict(headers)})
        if self.status is None:
            return None, self.error_kind or "connect"
        return self.status, None


def test_secret_shown_once_then_only_fingerprint(owner_workspace):
    created = _create_endpoint(owner_workspace)
    assert created.status_code == 201, created.text
    body = created.json()
    secret = body["secret"]
    assert secret.startswith("whsec_")
    assert body["endpoint"]["secret_fingerprint"] == f"…{secret[-6:]}"

    listed = owner_workspace["client"].get(
        f"{_base(owner_workspace)}/webhooks", headers={"X-CSRF-Token": owner_workspace["csrf"]}
    ).json()
    # 列表里带的是"当前密钥的可验证明文"（后端自己算得出），但响应里绝不出现整把密钥字段。
    assert "secret" not in listed["items"][0]
    assert listed["items"][0]["secret_version"] == 1

    rotated = owner_workspace["client"].post(
        f"{_base(owner_workspace)}/webhooks/{body['endpoint']['id']}/rotate",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        json={},
    )
    assert rotated.status_code == 200
    new_secret = rotated.json()["secret"]
    assert new_secret != secret
    assert rotated.json()["endpoint"]["secret_version"] == 2


def test_no_master_key_refuses_creation(owner_workspace, monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_MASTER_KEY", "", raising=False)
    response = _create_endpoint(owner_workspace)
    assert response.status_code == 409
    with SessionLocal() as db:
        assert db.scalar(select(WebhookEndpoint).limit(1)) is None


def test_signature_covers_the_exact_bytes_sent(owner_workspace):
    created = _create_endpoint(owner_workspace).json()
    endpoint_id = created["endpoint"]["id"]
    secret = created["secret"]
    _record_event(owner_workspace["tenant_id"])

    transport = FakeTransport()
    counters = delivery.deliver_round(transport)
    assert counters["succeeded"] == 1 and counters["sent"] == 1
    call = transport.calls[0]
    timestamp = int(call["headers"][signing.TIMESTAMP_HEADER])
    assert signing.verify(secret, timestamp, call["body"], call["headers"][signing.SIGNATURE_HEADER])
    # 负向：动一个字节就必须验不过——否则"验签通过"什么都证明不了。
    tampered = call["body"][:-2] + b"xx}"
    assert not signing.verify(secret, timestamp, tampered, call["headers"][signing.SIGNATURE_HEADER])
    # 负向：拿上一版密钥也验不过（版本号在头里，接收方按它选密钥）。
    previous = signing.derive_secret(MASTER, endpoint_id, 0)
    assert not signing.verify(previous, timestamp, call["body"], call["headers"][signing.SIGNATURE_HEADER])

    with SessionLocal() as db:
        row = db.scalar(select(WebhookDelivery))
        assert row.status == "succeeded" and row.response_status == 200 and row.completed_at is not None


def test_rotation_grace_emits_previous_signature_during_window(owner_workspace):
    created = _create_endpoint(owner_workspace).json()
    endpoint_id = created["endpoint"]["id"]
    with SessionLocal() as db:
        service.rotate_secret(
            db,
            tenant_id=owner_workspace["tenant_id"],
            endpoint_id=endpoint_id,
            actor_user_id=owner_workspace["user_id"],
            grace_seconds=3600,
        )
    _record_event(owner_workspace["tenant_id"])
    transport = FakeTransport()
    delivery.deliver_round(transport)
    headers = transport.calls[0]["headers"]
    body = transport.calls[0]["body"]
    timestamp = int(headers[signing.TIMESTAMP_HEADER])
    current = signing.derive_secret(MASTER, endpoint_id, 2)
    previous = signing.derive_secret(MASTER, endpoint_id, 1)
    assert signing.verify(current, timestamp, body, headers[signing.SIGNATURE_HEADER])
    assert signing.verify(
        previous, timestamp, body, headers[f"{signing.SIGNATURE_HEADER}-Previous"]
    )

    # 负向：窗口设成 0，多出来的那个头就该消失——证明这个头真的是窗口决定的，不是恒发。
    with SessionLocal() as db:
        service.rotate_secret(
            db,
            tenant_id=owner_workspace["tenant_id"],
            endpoint_id=endpoint_id,
            actor_user_id=owner_workspace["user_id"],
            grace_seconds=0,
        )
    _record_event(owner_workspace["tenant_id"])
    transport_second = FakeTransport()
    delivery.deliver_round(transport_second)
    assert len(transport_second.calls) == 1
    assert f"{signing.SIGNATURE_HEADER}-Previous" not in transport_second.calls[0]["headers"]


def test_fan_out_is_idempotent_and_claim_has_single_winner(owner_workspace):
    endpoint_id = _create_endpoint(owner_workspace).json()["endpoint"]["id"]
    _record_event(owner_workspace["tenant_id"])
    with SessionLocal() as db:
        first = service.fan_out(db)
        second = service.fan_out(db)
    assert first == 1 and second == 0

    with SessionLocal() as db:
        claimed = service.claim_due(db, limit=5)
    assert len(claimed) == 1
    item = claimed[0]
    # 第二次认领同一行应该什么都拿不到（条件更新只有一个胜者）
    with SessionLocal() as db:
        again = service.claim_due(db, limit=5)
    assert again == []
    with SessionLocal() as db:
        # 拿错的租约令牌收尾必须无效，绝不能覆盖持有者的状态
        assert service.finish(
            db, delivery_id=item["delivery_id"], lease_token="wrong", outcome="succeeded"
        ) == "lost"
        row = db.get(WebhookDelivery, item["delivery_id"])
        assert row.status == "inflight"
        assert service.finish(
            db,
            delivery_id=item["delivery_id"],
            lease_token=item["lease_token"],
            outcome="succeeded",
            response_status=200,
        ) == "succeeded"


def test_expired_lease_returns_to_pending(owner_workspace):
    _create_endpoint(owner_workspace)
    _record_event(owner_workspace["tenant_id"])
    with SessionLocal() as db:
        assert service.fan_out(db) == 1
    with SessionLocal() as db:
        claimed = service.claim_due(db, limit=1)[0]
        row = db.get(WebhookDelivery, claimed["delivery_id"])
        row.claimed_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    with SessionLocal() as db:
        assert service.reclaim_expired(db) == 1
        fresh = service.claim_due(db, limit=1)
    assert len(fresh) == 1 and fresh[0]["lease_token"] != claimed["lease_token"]


def test_backoff_then_terminal_and_410_shortcut(owner_workspace):
    _create_endpoint(owner_workspace)
    _record_event(owner_workspace["tenant_id"])

    for expected_attempt in (1, 2):
        _make_due_now()
        transport = FakeTransport(status=500)
        counters = delivery.deliver_round(transport)
        assert counters["retry"] == 1
        with SessionLocal() as db:
            row = db.scalar(select(WebhookDelivery))
            assert row.status == "pending" and row.attempts == expected_attempt
            assert row.next_attempt_at > row.created_at

    _make_due_now()
    transport = FakeTransport(status=503)
    counters = delivery.deliver_round(transport)
    assert counters["failed"] == 1
    with SessionLocal() as db:
        row = db.scalar(select(WebhookDelivery))
        assert row.status == "failed" and row.error_kind == "http_status"

    # 410 是接收方明说"别再发了"，不该把重试预算花光
    _record_event(owner_workspace["tenant_id"])
    gone = FakeTransport(status=410)
    counters = delivery.deliver_round(gone)
    assert counters["failed"] == 1
    with SessionLocal() as db:
        rows = db.scalars(
            select(WebhookDelivery).order_by(WebhookDelivery.created_at.desc())
        ).all()
        assert rows[0].attempts == 1 and rows[0].status == "failed"
        assert "410" in (rows[0].last_error or "")


def test_network_error_is_retriable_not_terminal(owner_workspace):
    _create_endpoint(owner_workspace)
    _record_event(owner_workspace["tenant_id"])
    transport = FakeTransport(status=None, error_kind="timeout")
    counters = delivery.deliver_round(transport)
    assert counters["retry"] == 1 and counters["failed"] == 0
    with SessionLocal() as db:
        row = db.scalar(select(WebhookDelivery))
        assert row.status == "pending" and row.error_kind == "timeout"


def test_private_targets_blocked_by_default(owner_workspace, monkeypatch):
    response = _create_endpoint(owner_workspace, url="https://10.0.0.1/hooks")
    assert response.status_code == 201, response.text
    _record_event(owner_workspace["tenant_id"])
    transport = FakeTransport(status=200)
    counters = delivery.deliver_round(transport)
    assert transport.calls == []  # 一个字节都没发出去
    assert counters["failed"] == 1
    with SessionLocal() as db:
        row = db.scalar(select(WebhookDelivery))
        assert row.error_kind == "blocked_target"

    # 负向：开关打开后同一条就该放行——证明拦它的是配置而不是写死的分支
    monkeypatch.setattr(config, "WEBHOOK_ALLOW_PRIVATE_TARGETS", True, raising=False)
    with SessionLocal() as db:
        row = db.scalar(select(WebhookDelivery))
        # 上一轮已终态，这里把同一行还原成待投：要验的是"开关决定放行"，不是重新扇出
        row.status = "pending"
        row.attempts = 0
        row.next_attempt_at = datetime.now(timezone.utc)
        row.error_kind = None
        db.commit()
    allowed = FakeTransport(status=200)
    counters = delivery.deliver_round(allowed)
    assert counters["succeeded"] == 1 and len(allowed.calls) == 1


def test_url_policy_rejects_insecure_and_credentials(owner_workspace):
    assert _create_endpoint(owner_workspace, url="http://127.0.0.1:9/hook").status_code == 422
    assert _create_endpoint(owner_workspace, url="https://u:pw@example.com/hook").status_code == 422
    assert _create_endpoint(owner_workspace, url="ftp://example.com/hook").status_code == 422
    # 打开本机开关后回环 http 才允许
    from app import config as cfg

    cfg.WEBHOOK_ALLOW_INSECURE_TARGETS = True
    try:
        assert _create_endpoint(
            owner_workspace, url="http://localhost:9/hook", event_types=["job.failed"]
        ).status_code == 201
    finally:
        cfg.WEBHOOK_ALLOW_INSECURE_TARGETS = False
    assert targets.check_target("https://example.com/a") == "https://example.com/a"


def test_unknown_event_type_and_duplicate_url_rejected(owner_workspace):
    assert _create_endpoint(owner_workspace, event_types=["nope.happened"]).status_code == 422
    assert _create_endpoint(owner_workspace, event_types=[]).status_code == 422
    assert _create_endpoint(owner_workspace).status_code == 201
    assert _create_endpoint(owner_workspace).status_code == 409


def test_role_matrix_and_cross_tenant_invisibility(owner_workspace, identity, member_of):
    endpoint_id = _create_endpoint(owner_workspace).json()["endpoint"]["id"]

    member = member_of("hook-member@example.com", owner_workspace["tenant_id"], role="member")
    assert (
        member["client"].get(
            f"{_base(owner_workspace)}/webhooks", headers={"X-CSRF-Token": member["csrf"]}
        ).status_code
        == 200
    )
    assert (
        member["client"].post(
            f"{_base(owner_workspace)}/webhooks",
            headers={"X-CSRF-Token": member["csrf"]},
            json={"url": "https://example.com/x", "event_types": ["job.succeeded"]},
        ).status_code
        == 403
    )

    reviewer = member_of("hook-reviewer@example.com", owner_workspace["tenant_id"], role="reviewer")
    assert (
        reviewer["client"].get(
            f"{_base(owner_workspace)}/webhooks", headers={"X-CSRF-Token": reviewer["csrf"]}
        ).status_code
        == 403
    )

    other = identity(email="hook-other@example.com", tenant_name="别人的空间", role="owner")
    # 不是这个租户的成员，先在成员关系那一层被挡住——对象归属根本不会被读到。
    assert (
        other["client"].get(
            f"{_base(owner_workspace)}/webhooks", headers={"X-CSRF-Token": other["csrf"]}
        ).status_code
        == 403
    )
    listed = other["client"].get(
        f"{_base(other)}/webhooks", headers={"X-CSRF-Token": other["csrf"]}
    ).json()
    assert listed["total"] == 0

    # 跨租户的"看不见"在服务层判：别人的端点 ID 在本租户里查就是 404，不是 403，
    # 也不带任何"这个 ID 存在"的信息。
    from app.errors import AccessError

    foreign = _create_endpoint(other).json()["endpoint"]["id"]
    with SessionLocal() as db:
        with pytest.raises(AccessError) as raised:
            service.get_endpoint(db, owner_workspace["tenant_id"], foreign)
        assert raised.value.status_code == 404
    assert (
        owner_workspace["client"].get(
            f"{_base(owner_workspace)}/webhooks/{foreign}",
            headers={"X-CSRF-Token": owner_workspace["csrf"]},
        ).status_code
        == 404
    )


def test_paused_endpoint_stops_delivery_and_audits_status_change(owner_workspace):
    endpoint_id = _create_endpoint(owner_workspace).json()["endpoint"]["id"]
    # 先在活动期把行扇出来，再暂停：`disabled` 终态这条分支走的正是"行已存在但端点不再活动"。
    _record_event(owner_workspace["tenant_id"])
    with SessionLocal() as db:
        assert service.fan_out(db) == 1
    response = owner_workspace["client"].post(
        f"{_base(owner_workspace)}/webhooks/{endpoint_id}/status",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        json={"status": "paused"},
    )
    assert response.status_code == 200 and response.json()["status"] == "paused"
    _record_event(owner_workspace["tenant_id"])
    transport = FakeTransport(status=200)
    counters = delivery.deliver_round(transport)
    assert transport.calls == []
    with SessionLocal() as db:
        row = db.scalar(select(WebhookDelivery))
        assert row.status == "failed" and row.error_kind == "disabled"

    from app.models.access import AuditEvent

    with SessionLocal() as db:
        actions = list(
            db.scalars(
                select(AuditEvent.action).where(AuditEvent.object_type == "webhook_endpoint")
            ).all()
        )
    assert "webhook.endpoint_created" in actions
    assert "webhook.endpoint_status_changed" in actions


def test_meta_reports_posture_without_copying_constants(owner_workspace):
    meta = owner_workspace["client"].get(
        f"{_base(owner_workspace)}/webhooks/meta", headers={"X-CSRF-Token": owner_workspace["csrf"]}
    ).json()
    assert "job.succeeded" in meta["event_types"]
    assert meta["enabled"] is True and meta["allow_private_targets"] is False
    assert meta["signing"]["algorithm"] == "HMAC-SHA256"
    assert meta["limits"]["max_attempts"] == 3


def test_delivery_list_scopes_to_tenant_and_filters_status(owner_workspace):
    _create_endpoint(owner_workspace)
    _record_event(owner_workspace["tenant_id"])
    delivery.deliver_round(FakeTransport(status=200))
    body = owner_workspace["client"].get(
        f"{_base(owner_workspace)}/webhooks/deliveries?status=succeeded",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
    ).json()
    assert body["total"] == 1 and body["counts"]["succeeded"] == 1
    assert body["items"][0]["event_type"] == "job.succeeded"
    assert (
        owner_workspace["client"].get(
            f"{_base(owner_workspace)}/webhooks/deliveries?status=nope",
            headers={"X-CSRF-Token": owner_workspace["csrf"]},
        ).status_code
        == 422
    )


def test_events_older_than_endpoint_are_not_backfilled(owner_workspace):
    old_event = _record_event(owner_workspace["tenant_id"], occurred_at=datetime.now(timezone.utc) - timedelta(days=2))
    with SessionLocal() as db:
        endpoint = WebhookEndpoint(
            id="e" * 32,
            tenant_id=owner_workspace["tenant_id"],
            url="https://example.com/late",
            description="",
            event_types=["job.succeeded"],
            status="active",
            secret_version=1,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            grace_seconds=3600,
        )
        db.add(endpoint)
        db.commit()
    with SessionLocal() as db:
        assert service.fan_out(db) == 0
    assert old_event
