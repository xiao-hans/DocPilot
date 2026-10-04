"""阶段 G2 队列派发与执行租约的故障恢复用例（TC-REC-009/010/011）。

覆盖"消息可能重复、可能丢、Worker 可能死在半路"的分支：判定权全部在数据库的
条件更新上。传输层用记录型假实现注入，真实 broker 行为由 `test_queue_broker.py` 验证。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select, update

from app.contexts.processing import dispatch as dispatch_store
from app.contexts.processing import service as processing
from app.contexts.processing.broker import TransportError
from app.contexts.processing.dispatcher import Dispatcher
from app.contexts.processing.domain import JobAttempt
from app.db import SessionLocal
from app.models.account import Tenant
from app.models.dispatch import JobDispatch
from app.models.job import JobStatus
from app.models.persistent_job import PersistentJob
from app.models.schema import INVOICE_TEMPLATE
from app.worker import handle

FIELDS = json.dumps(INVOICE_TEMPLATE)


class RecordingTransport:
    """记录型传输：留下投递证据，并可按需模拟 broker 故障。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[dict] = []
        self.fail = fail

    def publish(self, payload: dict) -> None:
        if self.fail:
            raise TransportError("broker 不可用")
        self.published.append(payload)


@pytest.fixture()
def queue_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """让上传端点不再进程内执行，任务停在 queued 等派发。"""
    monkeypatch.setattr("app.api.workspace_jobs.QUEUE_ENABLED", True)


def _upload(workspace: dict, pdf: Path):
    return workspace["client"].post(
        f"/api/v1/tenants/{workspace['tenant_id']}/jobs",
        headers={"X-CSRF-Token": workspace["csrf"]},
        files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))],
        data={"fields": FIELDS},
    )


def _submit(owner_workspace: dict, make_invoice_pdf, sandbox: Path) -> str:
    response = _upload(owner_workspace, make_invoice_pdf())
    assert response.status_code == 201, response.text
    return response.json()["job_id"]


def _rows_for(job_id: str) -> list[JobDispatch]:
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(JobDispatch)
                .where(JobDispatch.job_id == job_id)
                .order_by(JobDispatch.attempt_no)
            ).all()
        )


def _payload_for(job_id: str) -> dict:
    row = _rows_for(job_id)[0]
    return {
        "dispatch_id": row.id,
        "job_id": row.job_id,
        "tenant_id": row.tenant_id,
        "attempt_no": row.attempt_no,
        "queue": row.queue_name,
    }


def _status_of(dispatch_id: str) -> str:
    with SessionLocal() as db:
        return db.get(JobDispatch, dispatch_id).status


def _job_status(job_id: str) -> str:
    with SessionLocal() as db:
        return db.get(PersistentJob, job_id).status


def _attempt_count(job_id: str) -> int:
    with SessionLocal() as db:
        return int(
            db.scalar(select(func.count()).select_from(JobAttempt).where(JobAttempt.job_id == job_id))
            or 0
        )


def _force_attempt_stale(job_id: str) -> None:
    """把最新尝试伪装成"Worker 已死在执行的半路上"。"""
    with SessionLocal() as db:
        db.execute(
            update(JobAttempt)
            .where(JobAttempt.job_id == job_id)
            .values(
                status="running",
                started_at=datetime.now(timezone.utc) - timedelta(seconds=600),
                finished_at=None,
            )
        )
        db.execute(
            update(PersistentJob)
            .where(PersistentJob.id == job_id)
            .values(status="processing", finished_at=None)
        )
        db.commit()


def test_submit_writes_dispatch_row_in_same_transaction(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, queue_mode
) -> None:
    job_id = _submit(owner_workspace, make_invoice_pdf, sandbox)
    rows = _rows_for(job_id)
    assert len(rows) == 1
    assert rows[0].status == "ready" and rows[0].attempt_no == 1
    assert rows[0].publish_confirmed_at is None
    assert _job_status(job_id) == JobStatus.QUEUED.value


def test_dispatcher_publishes_only_with_confirm(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, queue_mode
) -> None:
    """拿到发布确认才推进 published；载荷只带标识，不含路径或凭据。"""
    job_id = _submit(owner_workspace, make_invoice_pdf, sandbox)
    transport = RecordingTransport()
    assert Dispatcher(transport).run_once()["published"] == 1

    payload = _payload_for(job_id)
    assert payload["dispatch_id"] in [item["dispatch_id"] for item in transport.published]
    assert _status_of(payload["dispatch_id"]) == "published"
    with SessionLocal() as db:
        confirmed = db.get(JobDispatch, payload["dispatch_id"]).publish_confirmed_at
    assert confirmed is not None
    dumped = json.dumps(transport.published[0])
    assert "\\" not in dumped and "/" not in dumped


def test_worker_beating_publish_confirmation_still_executes(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, queue_mode
) -> None:
    """Worker 比派发器的状态确认更早看到消息时，任务仍必须被执行。

    派发器是"先投消息、拿到 confirm 才写 published"，所以这个窗口真实存在：G8 把端到端
    搬到 Compose 形态后第一次抓到它——Worker 比 mark_published 早 3 毫秒领租约，领不到就
    把消息确认掉，任务停在 queued 直到 300 秒的重投窗口。回归点因此是"ready 状态可领租约"。
    """

    class ImmediateDelivery(RecordingTransport):
        def publish(self, payload: dict) -> None:
            super().publish(payload)
            # 同一条消息在派发器写下 published 之前就被取走并处理。
            assert handle(payload) is True

    job_id = _submit(owner_workspace, make_invoice_pdf, sandbox)
    counters = Dispatcher(ImmediateDelivery()).run_once()

    assert _job_status(job_id) == JobStatus.SUCCEEDED.value
    # 行被 Worker 抢先领走，mark_published 不再命中：这是预期分支，不是丢任务。
    assert counters == {"published": 0, "failed": 0, "skipped": 1, "republished": 0}
    assert _status_of(_payload_for(job_id)["dispatch_id"]) != "ready"


def test_publish_failure_keeps_row_ready(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, queue_mode
) -> None:
    """broker 故障时记录留在 ready 并累计次数，不能伪装成已投递。"""
    job_id = _submit(owner_workspace, make_invoice_pdf, sandbox)
    assert Dispatcher(RecordingTransport(fail=True)).run_once()["failed"] == 1

    payload = _payload_for(job_id)
    assert _status_of(payload["dispatch_id"]) == "ready"
    with SessionLocal() as db:
        attempts = db.get(JobDispatch, payload["dispatch_id"]).publish_attempts
    assert attempts == 1
    assert _job_status(job_id) == JobStatus.QUEUED.value


def test_stalled_dispatch_is_republished_after_window(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, queue_mode, monkeypatch
) -> None:
    """已发布却长时间无人认领的行退回 ready，消息丢失不会静默。"""
    monkeypatch.setattr(dispatch_store, "DISPATCH_REPUBLISH_SECONDS", 60)
    job_id = _submit(owner_workspace, make_invoice_pdf, sandbox)
    Dispatcher(RecordingTransport()).run_once()
    dispatch_id = _payload_for(job_id)["dispatch_id"]
    with SessionLocal() as db:
        db.execute(
            update(JobDispatch)
            .where(JobDispatch.id == dispatch_id)
            .values(updated_at=datetime.now(timezone.utc) - timedelta(seconds=600))
        )
        db.commit()

    assert dispatch_store.republish_stalled() == [dispatch_id]
    assert _status_of(dispatch_id) == "ready"


def test_worker_claims_lease_once(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, queue_mode
) -> None:
    """同一派发的重复消息只能有一个执行者拿到租约。"""
    job_id = _submit(owner_workspace, make_invoice_pdf, sandbox)
    Dispatcher(RecordingTransport()).run_once()
    payload = _payload_for(job_id)
    args = (
        payload["dispatch_id"],
    )
    kwargs = {
        "tenant_id": payload["tenant_id"],
        "job_id": payload["job_id"],
        "attempt_no": payload["attempt_no"],
    }
    assert dispatch_store.claim_lease(*args, **kwargs)
    assert dispatch_store.claim_lease(*args, **kwargs) is None
    assert _status_of(payload["dispatch_id"]) == "claimed"


def test_expired_lease_can_be_taken_over(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, queue_mode
) -> None:
    """租约过期后可被接管；旧执行者既不能续租，也不能再写结果。"""
    job_id = _submit(owner_workspace, make_invoice_pdf, sandbox)
    Dispatcher(RecordingTransport()).run_once()
    payload = _payload_for(job_id)
    kwargs = {
        "tenant_id": payload["tenant_id"],
        "job_id": payload["job_id"],
        "attempt_no": payload["attempt_no"],
    }
    stale = dispatch_store.Lease(
        dispatch_id=payload["dispatch_id"],
        token=dispatch_store.claim_lease(payload["dispatch_id"], **kwargs),
    )
    with SessionLocal() as db:
        db.execute(
            update(JobDispatch)
            .where(JobDispatch.id == stale.dispatch_id)
            .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        db.commit()

    fresh_token = dispatch_store.claim_lease(stale.dispatch_id, **kwargs)
    assert fresh_token and fresh_token != stale.token
    assert dispatch_store.renew_lease(stale.dispatch_id, stale.token) is False

    attempt = processing.begin_attempt(job_id, payload["tenant_id"])
    # 旧执行者带着过期租约收口：必须整体放弃，不能覆盖新执行者的状态。
    assert (
        processing.finish_attempt(
            attempt, status="succeeded", job_status="succeeded", lease=stale
        )
        is False
    )
    assert _job_status(job_id) == JobStatus.QUEUED.value


def test_handle_executes_and_settles_lease(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, queue_mode
) -> None:
    """完整链路：领租约 → 执行 → 结果与租约同事务收口。"""
    job_id = _submit(owner_workspace, make_invoice_pdf, sandbox)
    Dispatcher(RecordingTransport()).run_once()
    payload = _payload_for(job_id)

    assert handle(payload) is True
    assert _status_of(payload["dispatch_id"]) == "settled"
    assert _job_status(job_id) == "succeeded"
    assert _attempt_count(job_id) == 1


def test_handle_deduplicates_repeated_delivery(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, queue_mode
) -> None:
    """重复投递不产生第二次尝试，也不重复写业务结果。"""
    job_id = _submit(owner_workspace, make_invoice_pdf, sandbox)
    Dispatcher(RecordingTransport()).run_once()
    payload = _payload_for(job_id)

    assert handle(payload) is True
    assert _attempt_count(job_id) == 1
    assert handle(payload) is True
    assert _attempt_count(job_id) == 1
    assert _job_status(job_id) == "succeeded"


def test_handle_ignores_malformed_payload(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, queue_mode
) -> None:
    """结构不完整的消息直接确认，不动任何状态。"""
    job_id = _submit(owner_workspace, make_invoice_pdf, sandbox)
    payload = _payload_for(job_id)
    for broken in ({"dispatch_id": payload["dispatch_id"]}, "不是字典", [], None):
        assert handle(broken) is True
    assert _status_of(payload["dispatch_id"]) == "ready"
    assert _attempt_count(job_id) == 0


def test_two_dispatchers_do_not_double_advance(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, queue_mode
) -> None:
    """第一个派发器推进后，第二个没有 ready 记录可领；重复投递由租约去重。"""
    job_id = _submit(owner_workspace, make_invoice_pdf, sandbox)
    assert Dispatcher(RecordingTransport()).run_once()["published"] == 1
    assert Dispatcher(RecordingTransport()).run_once()["published"] == 0


def test_recovery_requeues_with_new_dispatch_row(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, queue_mode, monkeypatch
) -> None:
    """Worker 死在执行中：超时回收关闭旧派发，并为下一次尝试登记新派发行。"""
    monkeypatch.setattr(processing, "JOB_ATTEMPT_TIMEOUT_SECONDS", 30)
    job_id = _submit(owner_workspace, make_invoice_pdf, sandbox)
    Dispatcher(RecordingTransport()).run_once()
    assert handle(_payload_for(job_id)) is True

    _force_attempt_stale(job_id)
    with SessionLocal() as db:
        outcome = processing.reclaim_timeouts(db)
    assert job_id in outcome["requeued"]

    rows = _rows_for(job_id)
    assert [row.status for row in rows[:-1]] == ["settled"] * (len(rows) - 1)
    assert rows[-1].status == "ready"
    assert rows[-1].attempt_no == len(rows)
    assert _job_status(job_id) == JobStatus.QUEUED.value


def test_dispatch_row_commits_with_brand_new_job() -> None:
    """同一事务内"新增任务 + 登记派发"必须可提交。

    派发行以任务行为外键，但两者之间没有 ORM relationship，单元工作不会因此
    保证父行先插入；没有这条用例时，非 API 路径（脚本、恢复器、以后的批量导入）
    会在 SQLite 与 PostgreSQL 上同时撞外键。
    """
    now = datetime.now(timezone.utc)
    tenant_id, job_id = uuid.uuid4().hex, uuid.uuid4().hex
    with SessionLocal() as db:
        db.add(Tenant(id=tenant_id, name="顺序空间", status="active", created_at=now))
        db.add(
            PersistentJob(
                id=job_id,
                tenant_id=tenant_id,
                status="queued",
                created_at=now,
                attempt_count=0,
                snapshot={"status": "queued"},
            )
        )
        row = dispatch_store.create_pending(
            db, tenant_id=tenant_id, job_id=job_id, attempt_no=1
        )
        db.commit()
        dispatch_id = row.id

    assert _status_of(dispatch_id) == "ready"
    assert _job_status(job_id) == "queued"


def test_settled_slot_reopens_instead_of_stranding_job(
    owner_workspace: dict, sandbox: Path, make_invoice_pdf, queue_mode
) -> None:
    """重试计数尚未推进就回收时，已收口的槽位必须重新打开。

    否则任务退回 queued 却没有可派发的记录，会永久静默卡住。
    """
    job_id = _submit(owner_workspace, make_invoice_pdf, sandbox)
    payload = _payload_for(job_id)
    with SessionLocal() as db:
        db.execute(
            update(JobDispatch)
            .where(JobDispatch.id == payload["dispatch_id"])
            .values(status="settled", publish_confirmed_at=datetime.now(timezone.utc))
        )
        db.commit()

    with SessionLocal() as db:
        dispatch_store.create_pending(
            db,
            tenant_id=payload["tenant_id"],
            job_id=job_id,
            attempt_no=payload["attempt_no"],
        )
        db.commit()

    row = _rows_for(job_id)[0]
    assert row.status == "ready"
    assert row.publish_confirmed_at is None
