"""投递执行（DOC-DD-021 §4）：把认领到的行真正发出去。

传输层是一个只有 `post` 方法的窄接口，用例里换成假实现就能断言"签名覆盖的正是发出去的那串
字节"、"410 直接终态"、"5xx 按退避重试"，不必起真端口。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from app.platform.config import WEBHOOK_TIMEOUT_SECONDS
from app.contexts.webhooks import service, signing, targets
from app.contexts.webhooks.domain import WebhookEndpoint
from app.platform.db import SessionLocal

logger = logging.getLogger("docpilot.webhooks")


class Transport(Protocol):
    def post(self, url: str, body: bytes, headers: dict[str, str]) -> tuple[int | None, str | None]:
        """返回 (状态码, 错误类别)。连不上/超时给 (None, 'connect'|'timeout')。"""


class HttpxTransport:
    def __init__(self, timeout: int = WEBHOOK_TIMEOUT_SECONDS) -> None:
        self._client = httpx.Client(timeout=timeout, follow_redirects=False)

    def post(self, url: str, body: bytes, headers: dict[str, str]) -> tuple[int | None, str | None]:
        try:
            response = self._client.post(
                url,
                content=body,
                headers={**headers, "Content-Type": "application/json; charset=utf-8"},
            )
        except httpx.TimeoutException:
            return None, "timeout"
        except httpx.HTTPError:
            return None, "connect"
        return response.status_code, None

    def close(self) -> None:
        self._client.close()


def deliver_round(
    transport: Transport, *, fan_out_limit: int = 200, claim_limit: int = 20
) -> dict[str, Any]:
    counters: dict[str, Any] = {
        "reclaimed": 0,
        "fanned": 0,
        "sent": 0,
        "succeeded": 0,
        "retry": 0,
        "failed": 0,
        "lost": 0,
    }
    with SessionLocal() as db:
        counters["reclaimed"] = service.reclaim_expired(db)
        counters["fanned"] = service.fan_out(db, limit=fan_out_limit)
        claimed = service.claim_due(db, limit=claim_limit)
    # service.finish 的词表在这里翻译成计数：terminal 就是"这条不再投了"，记进 failed。
    translation = {"succeeded": "succeeded", "retry": "retry", "terminal": "failed", "lost": "lost"}
    for item in claimed:
        counters["sent"] += 1
        outcome = translation.get(_deliver_one(transport, item), "retry")
        counters[outcome] += 1
    if counters["sent"]:
        logger.info("webhook 轮次 %s", {k: v for k, v in counters.items() if v})
    return counters


def _deliver_one(transport: Transport, item: dict[str, Any]) -> str:
    body = signing.canonical_bytes(item["payload"])
    host = urlparse(item["url"]).hostname or ""
    with SessionLocal() as db:
        endpoint = db.get(WebhookEndpoint, item["endpoint_id"])
        if endpoint is None or endpoint.status != "active":
            # 端点被删或被暂停：不是网络故障，重试一万次也不会变，直接终态。
            return service.finish(
                db,
                delivery_id=item["delivery_id"],
                lease_token=item["lease_token"],
                outcome="terminal",
                error_kind="disabled",
                last_error="端点已删除或处于暂停状态",
            )
        blocked = targets.blocked_address(host)
        if blocked is not None:
            return service.finish(
                db,
                delivery_id=item["delivery_id"],
                lease_token=item["lease_token"],
                outcome="terminal",
                error_kind="blocked_target",
                last_error=blocked,
            )
        headers = dict(service.signature_headers(endpoint, body, int(time.time())))
        headers[signing.DELIVERY_HEADER] = item["delivery_id"]
        headers[signing.EVENT_HEADER] = str(item["payload"].get("type", ""))
        headers[signing.SECRET_VERSION_HEADER] = str(endpoint.secret_version)

    status, error_kind = transport.post(item["url"], body, headers)

    with SessionLocal() as db:
        if status is None:
            return service.finish(
                db,
                delivery_id=item["delivery_id"],
                lease_token=item["lease_token"],
                outcome="retry",
                error_kind=error_kind or "connect",
                last_error=f"传输失败：{error_kind or 'connect'}",
            )
        if 200 <= status < 300:
            return service.finish(
                db,
                delivery_id=item["delivery_id"],
                lease_token=item["lease_token"],
                outcome="succeeded",
                response_status=status,
            )
        if status == 410:
            return service.finish(
                db,
                delivery_id=item["delivery_id"],
                lease_token=item["lease_token"],
                outcome="terminal",
                response_status=status,
                error_kind="http_status",
                last_error="接收方返回 410：端点已注销",
            )
        return service.finish(
            db,
            delivery_id=item["delivery_id"],
            lease_token=item["lease_token"],
            outcome="retry",
            response_status=status,
            error_kind="http_status",
            last_error=f"接收方返回 HTTP {status}",
        )
