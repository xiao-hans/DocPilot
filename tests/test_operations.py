"""运营视图与就绪探针（TC-FUNC-038..040、TC-SEC-019/020、TC-REC-007、TC-SEC-028）。"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from app.contexts.operations import api as operations_api
from app.platform.db import SessionLocal
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


def test_readyz_storage_segment_follows_backend(owner_workspace: dict, monkeypatch) -> None:
    """TC-OPS-002：对象存储进就绪判定，但只在真的声明走 s3 时才参与。

    local 后端没有外部依赖可问，声明 s3 就必须真能列出对象——否则上传/下载都会失败，
    不能只因为数据库健康就把服务标成 ready。
    """
    from app.platform import config
    from app.contexts.operations import service as operations_service

    # 判据必须只由存储段决定：迁移不同步本身就会 503，所以这里传库里的真实 head。
    from sqlalchemy import text

    with SessionLocal() as db:
        head = db.scalar(text("SELECT version_num FROM alembic_version"))
    assert head, "测试库没登记 alembic 版本，503 的来源会分不清"

    monkeypatch.setattr(config, "STORAGE_BACKEND", "local", raising=False)
    code, body = operations_service.readiness(SessionLocal, head, storage_probe=lambda: {
        "enabled": False, "backend": "local", "checked": False
    })
    assert code == 200 and body["storage"]["checked"] is False

    code, body = operations_service.readiness(SessionLocal, head, storage_probe=lambda: {
        "enabled": True, "backend": "s3", "bucket": "docpilot", "reachable": True, "checked": True
    })
    assert code == 200 and body["storage"]["reachable"] is True

    # 负向：桶问不到就必须是 503，且原因写在段里而不是靠 error 字段猜
    code, body = operations_service.readiness(SessionLocal, head, storage_probe=lambda: {
        "enabled": True, "backend": "s3", "reachable": False, "error": "EndpointConnectionError"
    })
    assert code == 503 and body["status"] == "unavailable"
    assert body["storage"]["error"] == "EndpointConnectionError"

    # 负向：探针自己炸了也不能变成 500
    def explode() -> dict:
        raise RuntimeError("探针内部故障")

    code, body = operations_service.readiness(SessionLocal, head, storage_probe=explode)
    assert code == 503 and "storage_probe" in body["storage"]["error"]


def test_storage_health_reads_config_shape(owner_workspace: dict, monkeypatch) -> None:
    """`storage_health()` 本身的正反向：local 不去问，s3 问不到就报类名。"""
    from app.platform import config
    from app.contexts.operations import service as operations_service

    monkeypatch.setattr(config, "STORAGE_BACKEND", "local", raising=False)
    assert operations_service.storage_health() == {
        "enabled": False, "backend": "local", "checked": False
    }

    monkeypatch.setattr(config, "STORAGE_BACKEND", "s3", raising=False)
    monkeypatch.setattr(config, "S3_BUCKET", "docpilot", raising=False)

    class Listing:
        def list_keys(self, prefix: str = ""):
            yield "some/key.pdf"

    import app.adapters.storage.object_storage as object_storage

    monkeypatch.setattr(object_storage, "get_object_storage", lambda: Listing())
    health = operations_service.storage_health()
    assert health["reachable"] is True and health["listed_sample"] is True

    class Broken:
        def list_keys(self, prefix: str = ""):
            raise object_storage.StorageError("桶不可达")

    monkeypatch.setattr(object_storage, "get_object_storage", lambda: Broken())
    health = operations_service.storage_health()
    assert health["reachable"] is False and health["error"] == "StorageError"


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

# ---- 队列就绪判定（G5 / docs/17 G-DEC-09、风险 R-017）----


def _queue_mode(monkeypatch, consumers, backlog):
    from app.adapters.broker import amqp as broker
    from app.contexts.processing import dispatch

    monkeypatch.setattr("app.contexts.processing.service.QUEUE_ENABLED", True)
    monkeypatch.setattr(broker, "consumer_count", lambda queue=None: consumers)
    monkeypatch.setattr(dispatch, "queue_backlog", lambda: backlog)


def test_readyz_is_unavailable_when_backlogged_queue_has_no_consumer(
    owner_workspace: dict, monkeypatch
) -> None:
    """队列模式 + 有积压 + 没有消费者 = 任务会无限停在队列里，必须报不可用。"""
    _queue_mode(monkeypatch, 0, {"pending": 2, "oldest_pending_seconds": 45.0})

    response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    queue = response.json()["queue"]
    assert queue["enabled"] is True and queue["stuck"] is True
    assert queue["consumers"] == 0 and queue["pending"] == 2
    assert queue["oldest_pending_seconds"] == 45.0


def test_readyz_allows_idle_queue_without_consumers(owner_workspace: dict, monkeypatch) -> None:
    """没活干时 0 个消费者是正常缩容，不能被读成服务不可用。"""
    _queue_mode(monkeypatch, 0, {"pending": 0, "oldest_pending_seconds": None})

    response = TestClient(app).get("/readyz")
    assert response.status_code == 200, response.text
    queue = response.json()["queue"]
    assert queue["enabled"] is True and queue["stuck"] is False
    assert queue["consumers"] == 0 and queue["pending"] == 0
    assert queue["oldest_pending_seconds"] is None


def test_readyz_treats_unreachable_broker_as_failure(owner_workspace: dict, monkeypatch) -> None:
    """"问不到消费者"与"问到 0 个"是两种故障：配置声称走队列却连状态都取不到。"""
    _queue_mode(monkeypatch, None, {"pending": 0, "oldest_pending_seconds": None})

    response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    queue = response.json()["queue"]
    assert queue["consumers"] is None and queue["stuck"] is True


def test_readyz_keeps_queue_mode_out_of_the_judgement_when_disabled(
    owner_workspace: dict,
) -> None:
    """未设置 BROKER_URL 时 API 进程内执行任务，队列状态不参与就绪判定。"""
    body = TestClient(app).get("/readyz").json()
    assert body["status"] == "ready"
    assert body["queue"] == {"enabled": False, "reason": "queue_disabled"}


def test_readiness_probe_failure_cannot_escalate_to_500() -> None:
    """探针自己出问题时仍要给出 503 与分类原因，而不是把 /readyz 变成未捕获异常。"""
    from app.contexts.operations import service as operations_service

    def explode() -> dict:
        raise RuntimeError("模拟探针故障")

    code, body = operations_service.readiness(SessionLocal, "head", queue_probe=explode)
    assert code == 503
    assert body["queue"]["stuck"] is True
    assert body["error"] == "queue_probe:RuntimeError"


# ---------- 失败明细下钻（FR-042）----------

SENTINEL_NAME = "华鑫电子发票-2026-07.pdf"


def _force_failed_job(tenant_id: str, *, error: str | None, status: str = "failed") -> str:
    """直接落一条失败任务与它的两次尝试。

    下钻要断言的是"错误被呈现给谁、以什么形状"，不该依赖某条真实故障链路的措辞；真实链路
    本身由 `test_processing_reliability.py` 覆盖。
    """
    from datetime import datetime, timezone
    from uuid import uuid4

    from app.contexts.processing.domain import JobAttempt
    from app.models.persistent_job import PersistentJob

    job_id = uuid4().hex
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        db.add(
            PersistentJob(
                id=job_id,
                tenant_id=tenant_id,
                status=status,
                created_at=now,
                finished_at=now,
                attempt_count=2,
            )
        )
        # 必须先落父行：`JobAttempt` 只声明到 `processing_jobs` 的外键、没有 ORM relationship，
        # 同一次 flush 不保证父行先插入（`docs/06 §5.9` 记过同一个坑）。
        db.flush()
        db.add(
            JobAttempt(
                id=uuid4().hex, job_id=job_id, tenant_id=tenant_id, attempt_no=1, status="failed",
                started_at=now, finished_at=now, engine="mock", parse_modes=[], error="第一次也炸",
            )
        )
        db.add(
            JobAttempt(
                id=uuid4().hex, job_id=job_id, tenant_id=tenant_id, attempt_no=2, status="failed",
                started_at=now, finished_at=now, engine="mock", parse_modes=[], error=error,
            )
        )
        db.commit()
    return job_id


def test_tenant_failure_details_show_own_error_text_from_newest_attempt(
    owner_workspace: dict, other_workspace: dict
) -> None:
    """租户明细给本租户自己的原文，并且取最后一次尝试的错误，不含别的租户的任务。"""
    mine = _force_failed_job(owner_workspace["tenant_id"], error=f"解析 {SENTINEL_NAME} 失败：{SERIAL}")
    theirs = _force_failed_job(other_workspace["tenant_id"], error="队列不可用：amqp 连接被拒")

    response = owner_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/usage/failures?days=30"
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    items = {row["job_id"]: row for row in payload["jobs"]["items"]}

    assert mine in items
    assert SENTINEL_NAME in items[mine]["error"]
    assert items[mine]["code"] == "document_unreadable"
    assert "第一次也炸" not in json.dumps(payload, ensure_ascii=False)
    assert theirs not in items
    assert payload["jobs"]["total"] == 1


def test_tenant_failure_details_require_membership(owner_workspace: dict, other_workspace: dict) -> None:
    _force_failed_job(owner_workspace["tenant_id"], error="任意错误")
    response = other_workspace["client"].get(
        f"/api/v1/tenants/{owner_workspace['tenant_id']}/usage/failures"
    )
    assert response.status_code == 403, response.text


def test_platform_failure_distribution_is_content_free(
    owner_workspace: dict, other_workspace: dict, platform_admin_without_membership
) -> None:
    """跨租户的失败分布只有代号与计数：原文、文件名、发票号一律不出去。

    这条断言是 DOC-DD-007 §3 那条不变式的现场版本——平台身份不授予客户数据读取权，
    下钻也不行；要看原文必须回到租户端点，那里有成员关系与角色矩阵。
    """
    _force_failed_job(owner_workspace["tenant_id"], error=f"解析 {SENTINEL_NAME} 失败：{SERIAL}")
    _force_failed_job(other_workspace["tenant_id"], error="执行超时且已达上限")

    ops = platform_admin_without_membership()
    response = ops["client"].get("/api/v1/platform/usage/failures?days=30")
    assert response.status_code == 200, response.text
    payload = response.json()

    codes = {row["code"]: row for row in payload["codes"]}
    assert {"document_unreadable", "timeout"} <= set(codes)
    assert codes["document_unreadable"]["jobs"] >= 1
    assert payload["tenants_with_failures"] >= 2
    assert payload["truncated"] is False

    dumped = json.dumps(payload, ensure_ascii=False)
    for leak in (SERIAL, SENTINEL_NAME, "解析", "第一次也炸"):
        assert leak not in dumped


def test_platform_failure_view_audits_each_listed_tenant(
    owner_workspace: dict, other_workspace: dict, platform_admin_without_membership
) -> None:
    from sqlalchemy import select

    _force_failed_job(owner_workspace["tenant_id"], error="提取引擎不可用（429）")
    _force_failed_job(other_workspace["tenant_id"], error="没有可读文本，无结果")

    ops = platform_admin_without_membership()
    assert ops["client"].get("/api/v1/platform/usage/failures?days=30").status_code == 200

    with SessionLocal() as db:
        rows = list(
            db.scalars(
                select(AuditEvent).where(AuditEvent.action == "platform.usage_failures_viewed")
            ).all()
        )
    assert {row.tenant_id for row in rows} >= {
        owner_workspace["tenant_id"],
        other_workspace["tenant_id"],
    }
    assert all(row.metadata_["scope"] == "platform" for row in rows)


# ---------- 恶意文件检测分布（FR-010 / DOC-DD-024，维护轮 M31）----------


def _set_malware_status(tenant_id: str, *, status: str | None) -> None:
    """把该租户全部文档版本标成指定状态，或标回 NULL（没扫过）。"""
    from sqlalchemy import update

    from app.models.document import DocumentVersion

    with SessionLocal() as db:
        db.execute(
            update(DocumentVersion)
            .where(DocumentVersion.tenant_id == tenant_id)
            .values(malware_status=status)
        )
        db.commit()


def test_tenant_usage_reports_unscanned_separately_from_clean(
    owner_workspace: dict, make_invoice_pdf
) -> None:
    """TC-SEC-028：没扫过不能被读成"扫过且干净"。

    恶意文件检测默认关闭（DOC-DD-024 §3.2），所以生产库里默认形态是"全部没扫过"。
    若把 NULL 折进 `clean`，运维看到的就是"100% 干净"——正是这条口径要防的误读。
    """
    _upload(owner_workspace, make_invoice_pdf(SERIAL))
    base = f"/api/v1/tenants/{owner_workspace['tenant_id']}"

    usage = owner_workspace["client"].get(f"{base}/usage?days=7").json()
    assert usage["malware"]["total"] == 1
    assert usage["malware"]["by_status"] == {"clean": 0, "infected": 0, "error": 0, "unscanned": 1}

    # 扫过且干净：四个键都在，缺的补 0，界面不必自己判断"缺键是什么意思"
    _set_malware_status(owner_workspace["tenant_id"], status="clean")
    usage = owner_workspace["client"].get(f"{base}/usage?days=7").json()
    assert usage["malware"]["by_status"]["clean"] == 1
    assert usage["malware"]["by_status"]["unscanned"] == 0

    # 引擎不可达：结论记 error，不算"干净"也不算"感染"
    _set_malware_status(owner_workspace["tenant_id"], status="error")
    usage = owner_workspace["client"].get(f"{base}/usage?days=7").json()
    assert usage["malware"]["by_status"] == {"clean": 0, "infected": 0, "error": 1, "unscanned": 0}


def test_platform_malware_total_counts_only_actionable_statuses(
    owner_workspace: dict, other_workspace: dict, make_invoice_pdf, platform_admin_without_membership
) -> None:
    """跨租户只聚合要动手的状态：感染与引擎故障。

    `unscanned` 不进告警——能力默认关闭时它就该是全部，把默认形态算进告警会让运维
    每次刷新都收到一条本该怎么有的通知，最后谁都不过这一列。
    """
    _upload(owner_workspace, make_invoice_pdf(SERIAL))
    _upload(other_workspace, make_invoice_pdf("01100200011100045240"))
    _set_malware_status(owner_workspace["tenant_id"], status="infected")
    _set_malware_status(other_workspace["tenant_id"], status="clean")

    ops = platform_admin_without_membership()
    response = ops["client"].get("/api/v1/platform/usage?days=30")
    assert response.status_code == 200, response.text
    body = response.json()
    by_tenant = {row["tenant_id"]: row for row in body["tenants"]}

    assert by_tenant[owner_workspace["tenant_id"]]["malware"]["infected"] == 1
    assert by_tenant[owner_workspace["tenant_id"]]["malware"]["unscanned"] == 0
    assert by_tenant[other_workspace["tenant_id"]]["malware"]["clean"] == 1
    assert body["totals"]["malware_flagged"] == 1

    dumped = json.dumps(body, ensure_ascii=False)
    assert SERIAL not in dumped
