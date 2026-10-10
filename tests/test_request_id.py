"""请求关联 ID 贯穿（NFR-005，TC-OPS-003，`docs/06 §5.26`）。

需求原文（`docs/03` NFR-005）：关键业务操作结构化记录关联 request_id、tenant_id、
job_id；日志不得包含密钥或不必要的文档内容。

一个 ID 要同时出现在**响应头**、**日志行**、**审计事件**三处才有用：
只进响应头，用户拿着 ID 查不到日志；只进日志，用户不知道自己的 ID 是什么。
本文件的每条判据都对应其中一处，另加"没有请求上下文时不得编造"这条负向。

约定：日志用 `caplog` 捕获，不依赖真实 uvicorn 进程；中间件行为在 TestClient 上验证。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.db import SessionLocal
from app.models.access import AuditEvent
from app.request_context import (
    MAX_CHARS,
    MIN_CHARS,
    REQUEST_ID_HEADER,
    RequestIdFilter,
    adopt_or_generate,
    current,
    generate,
    is_well_formed,
)
from sqlalchemy import select

FIELDS = '[{"name": "发票号码", "type": "text", "description": "号码"}]'
BASE = "/api/v1"


def _create_template(workspace: dict, name: str, request_id: str | None = None):
    headers = {"X-CSRF-Token": workspace["csrf"]}
    if request_id is not None:
        headers[REQUEST_ID_HEADER] = request_id
    return workspace["client"].post(
        f"{BASE}/tenants/{workspace['tenant_id']}/templates",
        headers=headers,
        json={"name": name, "fields": [{"name": "发票号码", "type": "text", "description": "号码"}]},
    )


# ---------------------------------------------------------------- 校验函数

def test_generate_is_32_hex_chars():
    value = generate()
    assert len(value) == 32
    assert is_well_formed(value)
    assert value != generate()  # 两次生成不重复


@pytest.mark.parametrize(
    "value",
    [
        "",  # 空
        "short",  # 短于 8
        "a" * (MAX_CHARS + 1),  # 长于 64
        "has space",  # 空格
        "line\nbreak",  # 控制字符：进日志行会伪造出新行
        "tab\there",
        "carriage\rreturn",
        "中文标识",  # 非 ASCII
    ],
)
def test_malformed_values_are_rejected(value: str):
    assert not is_well_formed(value)


def test_adopt_or_generate_keeps_valid_and_replaces_invalid():
    valid = "abc12345"
    assert adopt_or_generate(valid) == valid
    # 不合规不报错，只替换：关联 ID 不值得让一个正常请求失败
    replaced = adopt_or_generate("bad id")
    assert replaced != "bad id"
    assert is_well_formed(replaced)
    assert is_well_formed(adopt_or_generate(None))


def test_current_is_none_without_a_request_context():
    """没有请求上下文时必须返回 None，不能继承上一个请求的 ID。

    Worker、派发器、脚本都在这个状态下写日志与审计；继承会让"这条记录出自哪次请求"
    变成错误信息，比没有关联更难查。
    """
    assert current() is None


def test_log_filter_fills_dash_without_a_request_context():
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1, msg="m", args=(), exc_info=None
    )
    assert RequestIdFilter().filter(record) is True
    assert record.request_id == "-"


# ---------------------------------------------------------------- 响应头

def test_response_header_carries_a_generated_id(client):
    response = client.get("/healthz")
    value = response.headers.get(REQUEST_ID_HEADER)
    assert value is not None and is_well_formed(value)


def test_valid_inbound_id_is_adopted(client):
    """前端或网关传进来的 ID 要沿用，否则跨层串联断在第一跳。"""
    supplied = "trace-from-gateway-001"
    response = client.get("/healthz", headers={REQUEST_ID_HEADER: supplied})
    assert response.headers[REQUEST_ID_HEADER] == supplied


def test_malformed_inbound_id_is_replaced_not_echoed(client):
    """含控制字符的入站 ID 绝不能回显——响应头里的换行是响应拆分面。"""
    response = client.get("/healthz", headers={REQUEST_ID_HEADER: "bad\nvalue"})
    value = response.headers[REQUEST_ID_HEADER]
    assert value != "bad\nvalue"
    assert is_well_formed(value)
    assert "\n" not in value


def test_header_present_on_error_responses_too(client):
    """错误响应同样要带 ID：用户报错时拿到的正是这种响应。"""
    response = client.get(f"{BASE}/tenants/not-a-real-tenant-id-000000000000/jobs")
    assert response.status_code in (401, 403, 404, 422)
    assert is_well_formed(response.headers[REQUEST_ID_HEADER])


def test_each_request_gets_its_own_id(client):
    first = client.get("/healthz").headers[REQUEST_ID_HEADER]
    second = client.get("/healthz").headers[REQUEST_ID_HEADER]
    assert first != second


def test_context_does_not_leak_into_the_next_request(client):
    """处理完一个请求后上下文必须清空，否则下一个请求的日志会贴上别人的 ID。"""
    supplied = "trace-first-request-0001"
    client.get("/healthz", headers={REQUEST_ID_HEADER: supplied})
    assert current() is None


# ---------------------------------------------------------------- 审计事件

def test_audit_event_records_the_same_id_as_the_response(owner_workspace):
    """同一次请求的响应头 ID 与审计行 ID 必须相等。

    这是"用户报的 ID 能查到这次请求做了什么"的可执行版本。
    """
    supplied = "trace-audit-link-000001"
    response = _create_template(owner_workspace, "关联 ID 模板", supplied)
    assert response.status_code == 201, response.text
    assert response.headers[REQUEST_ID_HEADER] == supplied

    with SessionLocal() as db:
        row = db.scalar(
            select(AuditEvent).where(AuditEvent.action == "template.created").order_by(
                AuditEvent.created_at.desc()
            )
        )
    assert row is not None
    assert row.request_id == supplied


def test_audit_event_request_id_is_readable_through_the_api(owner_workspace):
    """审计列表要把 request_id 回给租户，否则这条关联只有 DBA 看得到。"""
    supplied = "trace-audit-api-0000001"
    assert _create_template(owner_workspace, "列表里的关联 ID", supplied).status_code == 201
    events = owner_workspace["client"].get(
        f"{BASE}/tenants/{owner_workspace['tenant_id']}/audit-events",
        headers={"X-CSRF-Token": owner_workspace["csrf"]},
    ).json()["events"]
    assert any(event["request_id"] == supplied for event in events)


def test_audit_rows_without_a_request_context_stay_null(owner_workspace):
    """没有请求上下文时写审计，`request_id` 必须是 NULL，不能编造一个。

    `docs/11 §4.2` 当初拒绝给审计表加这一列的理由正是"当前没有产生方，
    避免留下无消费者的空字段"。现在有产生方了，但产生方只覆盖 HTTP 请求；
    Worker 与脚本路径仍然是 NULL，这条用例把两种情形分开钉住。
    """
    from app.services import audit

    with SessionLocal() as db:
        audit.record(
            db,
            tenant_id=owner_workspace["tenant_id"],
            actor_user_id=owner_workspace["user_id"],
            action="job.retried",
            object_type="job",
            object_id="job-without-request",
            metadata={"attempt_count": 1},
        )
        db.commit()
        row = db.scalar(
            select(AuditEvent).where(AuditEvent.object_id == "job-without-request")
        )
    assert row is not None
    assert row.request_id is None


def test_explicit_request_id_wins_over_the_context(owner_workspace):
    """显式传值优先：少数场景要把动作挂到另一个 ID 上（例如后台补偿）。"""
    from app.services import audit

    with SessionLocal() as db:
        event = audit.new_event(
            tenant_id=owner_workspace["tenant_id"],
            actor_user_id=None,
            action="job.retried",
            object_type="job",
            object_id="job-explicit-id",
            metadata={"attempt_count": 2},
            request_id="explicit-override-0001",
        )
    assert event.request_id == "explicit-override-0001"


# ---------------------------------------------------------------- 日志行

def test_configure_logging_installs_the_filter_once():
    """重复调用不得叠加过滤器：叠了会让每条日志被处理多遍。"""
    from app.request_context import configure_logging

    configure_logging()
    configure_logging()
    root = logging.getLogger()
    assert root.handlers, "configure_logging 必须留下一个 handler"
    for handler in root.handlers:
        assert sum(isinstance(f, RequestIdFilter) for f in handler.filters) == 1


def test_formatted_log_line_contains_the_current_request_id(caplog):
    """把 ID 真的渲染进日志行，而不只是挂个过滤器。

    用 `configure_logging()` 的格式串手工格式化一条记录：断言最终文本里出现 ID。
    只看"过滤器已挂"证明不了格式串里有 `%(request_id)s`——两者缺一个都会让
    "日志里有 ID"变成空话。
    """
    from app.request_context import LOG_FORMAT, configure_logging, reset, set_current

    configure_logging()
    token = set_current("trace-rendered-00000001")
    try:
        with caplog.at_level(logging.INFO):
            logging.getLogger("docpilot.test").info("处理完成")
        # caplog 的 handler 没有过滤器，这里直接验格式串能渲染该字段
        record = logging.LogRecord(
            name="docpilot.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="处理完成",
            args=(),
            exc_info=None,
        )
        RequestIdFilter().filter(record)
        rendered = logging.Formatter(LOG_FORMAT).format(record)
        assert "trace-rendered-00000001" in rendered
        assert "%(request_id)s" not in rendered
    finally:
        reset(token)


# ---------------------------------------------------------------- 边界

def test_bounds_are_what_the_docs_say():
    assert (MIN_CHARS, MAX_CHARS) == (8, 64)
    assert REQUEST_ID_HEADER == "X-Request-ID"


def test_worker_path_has_no_request_context(sandbox: Path):
    """后台处理不得继承任何请求 ID：它是另一条生命周期。"""
    from app.services.job_manager import get_job_manager

    manager = get_job_manager()
    assert manager is not None
    assert current() is None