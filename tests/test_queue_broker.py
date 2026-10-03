"""阶段 G2 真实 broker 集成用例（TC-REC-011 的本机部分）。

需要本机 Compose 的 RabbitMQ：设置 `BROKER_URL` 后运行；未设置则整组跳过，并且
按覆盖缺口记录（跳过不等于通过）。每条用例使用独立队列名，避免互相污染。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

import pika
import pytest
from sqlalchemy import select

from app.config import BROKER_URL
from app.contexts.processing import dispatch as dispatch_store
from app.contexts.processing import service as processing
from app.contexts.processing.broker import AmqpTransport
from app.contexts.processing.dispatcher import Dispatcher
from app.contexts.processing.domain import JobAttempt
from app.db import SessionLocal
from app.models.dispatch import JobDispatch
from app.models.persistent_job import PersistentJob
from app.models.schema import INVOICE_TEMPLATE
from app.worker import handle

pytestmark = pytest.mark.broker

FIELDS = json.dumps(INVOICE_TEMPLATE)

if not BROKER_URL:
    pytest.skip(
        "未设置 BROKER_URL：真实 broker 用例未执行（覆盖缺口，不算通过）",
        allow_module_level=True,
    )


@pytest.fixture()
def queue_mode(monkeypatch: pytest.MonkeyPatch) -> str:
    """队列模式 + 本用例专用队列名（在登记派发行之前生效）。"""
    queue = f"docpilot.it.{uuid.uuid4().hex[:12]}"
    monkeypatch.setattr("app.api.workspace_jobs.QUEUE_ENABLED", True)
    monkeypatch.setattr(dispatch_store, "QUEUE_NAME", queue)
    return queue


def _submit(owner_workspace: dict, make_invoice_pdf, sandbox: Path) -> tuple[str, dict]:
    response = owner_workspace["client"].post(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
        files=[("files", (make_invoice_pdf().name, make_invoice_pdf().read_bytes(), "application/pdf"))],
        data={"fields": FIELDS},
    )
    assert response.status_code == 201, response.text
    job_id = response.json()["job_id"]
    with SessionLocal() as db:
        row = db.scalars(
            select(JobDispatch).where(JobDispatch.job_id == job_id).order_by(JobDispatch.attempt_no)
        ).first()
        assert row is not None
        payload = {
            "dispatch_id": row.id,
            "job_id": row.job_id,
            "tenant_id": row.tenant_id,
            "attempt_no": row.attempt_no,
            "queue": row.queue_name,
        }
    return job_id, payload


def _dispatch_state(dispatch_id: str) -> str:
    with SessionLocal() as db:
        return db.get(JobDispatch, dispatch_id).status


def _queued_count(queue: str) -> int:
    """用独立连接确认 broker 侧真的留下了消息。"""
    connection = pika.BlockingConnection(pika.URLParameters(BROKER_URL))
    try:
        return int(connection.channel().queue_declare(queue=queue, passive=True).method.message_count)
    finally:
        connection.close()


def _drain(queue: str, limit: int = 5) -> list[dict]:
    connection = pika.BlockingConnection(pika.URLParameters(BROKER_URL))
    channel = connection.channel()
    bodies: list[dict] = []
    for _ in range(limit):
        method, _properties, body = channel.basic_get(queue=queue, auto_ack=True)
        if method is None:
            break
        bodies.append(json.loads(body.decode("utf-8")))
    connection.close()
    return bodies


def _job_outcome(job_id: str) -> tuple[str, list[str]]:
    with SessionLocal() as db:
        status = db.get(PersistentJob, job_id).status
        attempts = [
            attempt.status
            for attempt in db.scalars(
                select(JobAttempt).where(JobAttempt.job_id == job_id).order_by(JobAttempt.attempt_no)
            ).all()
        ]
    return status, attempts


def test_confirmed_publish_lands_on_real_broker(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, queue_mode
) -> None:
    """发布确认后消息在 broker 侧可见，且载荷只含任务标识。"""
    job_id, payload = _submit(owner_workspace, make_invoice_pdf, sandbox)
    transport = AmqpTransport(queue=queue_mode)
    try:
        assert Dispatcher(transport).run_once()["published"] == 1
        assert _queued_count(queue_mode) == 1
        assert _dispatch_state(payload["dispatch_id"]) == "published"

        received = _drain(queue_mode)
        assert [item["job_id"] for item in received] == [job_id]
        assert set(received[0]) == {"dispatch_id", "job_id", "tenant_id", "attempt_no", "queue"}
    finally:
        transport.close()


def _consume_until(transport: AmqpTransport, predicate, timeout: float = 30.0) -> bool:
    """在消费线程里跑 handle，直到条件满足；停止走 threadsafe 回调。"""
    thread = threading.Thread(target=transport.consume, args=(handle,), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.5)
        return predicate()
    finally:
        transport.stop()
        thread.join(10)


def test_redelivery_executes_only_once(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, queue_mode
) -> None:
    """重复投递：第二条消息拿不到租约，任务只执行一次、只留一次尝试。"""
    job_id, payload = _submit(owner_workspace, make_invoice_pdf, sandbox)
    transport = AmqpTransport(queue=queue_mode)
    assert Dispatcher(transport).run_once()["published"] == 1
    transport.publish(payload)  # 同一派发的重复投递
    assert _queued_count(queue_mode) == 2

    try:
        assert _consume_until(
            transport,
            lambda: _dispatch_state(payload["dispatch_id"]) == "settled"
            and _queued_count(queue_mode) == 0,
        )
    finally:
        transport.close()

    status, attempts = _job_outcome(job_id)
    assert status == "succeeded"
    assert attempts == ["succeeded"]


def test_message_survives_connection_loss(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, queue_mode
) -> None:
    """投递后连接断开：持久消息仍在队列里，重连后仍能消费并执行。"""
    job_id, payload = _submit(owner_workspace, make_invoice_pdf, sandbox)
    first = AmqpTransport(queue=queue_mode)
    assert Dispatcher(first).run_once()["published"] == 1
    first.close()  # 模拟网络中断

    assert _queued_count(queue_mode) == 1

    second = AmqpTransport(queue=queue_mode)
    try:
        assert _consume_until(
            second,
            lambda: _dispatch_state(payload["dispatch_id"]) == "settled"
            and _queued_count(queue_mode) == 0,
        )
        assert _queued_count(queue_mode) == 0
    finally:
        second.close()

    status, attempts = _job_outcome(job_id)
    assert status == "succeeded" and attempts == ["succeeded"]
