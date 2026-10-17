# -*- coding: utf-8 -*-
"""投递明细的查询契约（FR-041，维护轮 M23）。

`tests/test_webhooks.py` 里原本只有一条用例同时覆盖"租户边界 + status 过滤 + 422"，
`endpoint_id` 过滤、分页、`counts` 聚合三处契约没有独立判据。本文件补上。

三处契约里各有一条容易写错的地方，用例就针对它们：

1. **`endpoint_id` 过滤要先做存在性检查**。服务先 `get_endpoint(db, tenant_id, id)`，
   跨租户或不存在都走同一条 404——不能退化成"过滤出空列表"，否则等于把
   "这个端点不属于你"和"这个端点没有投递"混成一件事。
2. **`total` 是过滤后的全量计数，不是当页条数**。分页时它保持不变，`items` 才变短。
3. **`counts` 按状态跨全量聚合，不受 `status` 过滤影响**。它答的是"这个租户各状态各多少"，
   若跟着过滤走，界面上的状态计数会在筛选后自己变小。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app import config
from app.contexts.webhooks import delivery
from app.contexts.webhooks.domain import WebhookDelivery, WebhookEndpoint
from app.db import SessionLocal

from tests.test_webhooks import MASTER, _base, _create_endpoint, _record_event


@pytest.fixture(autouse=True)
def master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 `test_webhooks.py` 的同一份配置搬进来。

    那个 autouse fixture 是模块级的，不跨模块生效，所以本文件要自己声明一次。
    这里只复制**必要的那一项**（主密钥），不放宽私网/明文目标策略——
    本文件不做真实投递，放宽了反而让判据更弱。`MASTER` 从那边导入，避免两处各写一份密钥。
    """
    monkeypatch.setattr(config, "WEBHOOK_MASTER_KEY", MASTER, raising=False)


class FakeTransport:
    """与 `tests/test_webhooks.py` 里的同名替身一致：按预设状态码应答。"""

    def __init__(self, status: int = 200) -> None:
        self.status = status

    def post(self, url, body, headers, timeout):  # noqa: ANN001 - 契约由调用方定义
        return self.status, "{}"


def _deliveries(client, workspace, query: str = "") -> dict:
    response = client.get(
        f"{_base(workspace)}/webhooks/deliveries{query}",
        headers={"X-CSRF-Token": workspace["csrf"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _endpoint_id(workspace: dict, url: str | None = None) -> str:
    """建一个端点并取出它的 id（创建响应是 `{endpoint, secret}`）。"""
    created = _create_endpoint(workspace) if url is None else _create_endpoint(workspace, url=url)
    assert created.status_code == 201, created.text
    return created.json()["endpoint"]["id"]


def _add_delivery(tenant_id: str, endpoint_id: str, status: str, created_at: datetime, tag: str) -> str:
    """直接落一行投递记录：造"不同状态并存"的局面比真实投递快且稳。

    `tag` 只用来拼出可读的 id，同时规避 (endpoint_id, event_id) 的唯一约束。
    """
    row_id = f"{tag:<32}"[:32].replace(" ", "-")
    with SessionLocal() as db:
        db.add(
            WebhookDelivery(
                id=row_id,
                tenant_id=tenant_id,
                endpoint_id=endpoint_id,
                event_id=tag.rjust(32, "0")[:32],
                event_type="job.succeeded",
                payload={},
                status=status,
                attempts=1,
                next_attempt_at=created_at,
                created_at=created_at,
            )
        )
        db.commit()
    return row_id


def test_endpoint_filter_rejects_foreign_and_unknown_endpoints(owner_workspace, other_workspace):
    """`endpoint_id` 先做存在性检查：别人的端点与不存在的端点都必须 404。

    这条防的退化写法是"把 endpoint_id 当普通过滤条件"——那会让跨租户查询返回空列表，
    于是调用方无法区分"无权看"与"没有投递"。

    "别人的端点"直接落库造，不走对方的 API：`other_workspace` 的身份在该租户里不是
    owner/admin，建端点会被 403 拦住（既有跨租户用例也都只在对方租户里读、不在那里建）。
    """
    mine = _endpoint_id(owner_workspace)
    foreign = "f" * 32
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        db.add(
            WebhookEndpoint(
                id=foreign,
                tenant_id=other_workspace["tenant_id"],
                url="https://example.com/foreign",
                description="",
                event_types=["job.succeeded"],
                status="active",
                secret_version=1,
                created_at=now,
                updated_at=now,
                grace_seconds=3600,
            )
        )
        db.commit()

    for endpoint_id in (foreign, "0" * 32):
        response = owner_workspace["client"].get(
            f"{_base(owner_workspace)}/webhooks/deliveries?endpoint_id={endpoint_id}",
            headers={"X-CSRF-Token": owner_workspace["csrf"]},
        )
        assert response.status_code == 404, f"{endpoint_id} 应 404，实际 {response.status_code}"

    # 自己的端点即便没有任何投递，也是 200 + 空列表，而不是 404
    body = _deliveries(owner_workspace["client"], owner_workspace, f"?endpoint_id={mine}")
    assert body["total"] == 0 and body["items"] == []


def test_endpoint_filter_narrows_items_but_not_counts(owner_workspace):
    """`endpoint_id` 只影响 `items`/`total`，`counts` 仍报该租户全量。"""
    first = _endpoint_id(owner_workspace)
    second = _endpoint_id(owner_workspace, url=f"https://example.com/second-{first}")
    now = datetime.now(timezone.utc)
    _add_delivery(owner_workspace["tenant_id"], first, "succeeded", now, tag="a1")
    _add_delivery(owner_workspace["tenant_id"], second, "failed", now, tag="a2")

    body = _deliveries(owner_workspace["client"], owner_workspace, f"?endpoint_id={first}")
    assert body["total"] == 1
    assert [row["endpoint_id"] for row in body["items"]] == [first]
    # counts 是跨全量的：两个端点各一条，都要算进来
    assert body["counts"].get("succeeded") == 1 and body["counts"].get("failed") == 1


def test_pagination_keeps_total_and_orders_newest_first(owner_workspace):
    """`total` 是过滤后的全量计数；分页只改 `items`。排序按创建时间倒序。"""
    endpoint = _endpoint_id(owner_workspace)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ids = [
        _add_delivery(
            owner_workspace["tenant_id"], endpoint, "succeeded", base.replace(minute=i), tag=f"p{i}"
        )
        for i in range(3)
    ]

    page1 = _deliveries(owner_workspace["client"], owner_workspace, "?limit=2&offset=0")
    page2 = _deliveries(owner_workspace["client"], owner_workspace, "?limit=2&offset=2")

    assert page1["total"] == 3 and page2["total"] == 3, "total 必须与当页条数无关"
    assert len(page1["items"]) == 2 and len(page2["items"]) == 1
    assert page1["items"][0]["id"] == ids[-1], "最新一条应排在最前"
    assert [row["id"] for row in page1["items"]] == list(reversed(ids))[:2]
    assert [row["id"] for row in page2["items"]] == [ids[0]]
    # 两页不重叠
    assert not ({row["id"] for row in page1["items"]} & {row["id"] for row in page2["items"]})


def test_counts_aggregate_across_statuses_and_ignore_status_filter(owner_workspace):
    """`counts` 答的是"各状态各多少"，与 `status` 过滤无关。"""
    endpoint = _endpoint_id(owner_workspace)
    now = datetime.now(timezone.utc)
    _add_delivery(owner_workspace["tenant_id"], endpoint, "succeeded", now, tag="c1")
    _add_delivery(owner_workspace["tenant_id"], endpoint, "pending", now, tag="c2")

    filtered = _deliveries(owner_workspace["client"], owner_workspace, "?status=succeeded")
    assert filtered["total"] == 1 and len(filtered["items"]) == 1
    assert filtered["counts"].get("succeeded") == 1, "筛选后 counts 不应缩小"
    assert filtered["counts"].get("pending") == 1, "counts 要包含被筛掉的状态"


def test_delivery_rows_carry_no_receiver_body(owner_workspace):
    """明细只到失败类别这一层：接收方的响应正文不入库。

    界面上那一列展示的是 `error_kind` 与 `last_error`（我们自己的判定），不是对方的
    响应体。这条用例把"不存对方数据"钉成契约，而不是只写在注释里。
    """
    endpoint = _endpoint_id(owner_workspace)
    secret = json.dumps({"hello": "receiver-payload"})
    _record_event(owner_workspace["tenant_id"])
    delivery.deliver_round(type("T", (), {"post": lambda *a, **k: (500, secret)})())

    body = _deliveries(owner_workspace["client"], owner_workspace)
    assert body["total"] >= 1
    row = body["items"][0]
    assert "receiver-payload" not in json.dumps(body, ensure_ascii=False)
    assert row["last_error"] is None or isinstance(row["last_error"], str)
    assert endpoint
