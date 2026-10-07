"""跨租户泄漏探针的判据本身（`scripts/capacity_leak_probe.py`）。

G28 复跑观测时这条探针报过 3 处"未知标记泄漏"，查下来是 SHA-256 校验和里恰好有一段
20 位数字。假阳性比漏报更贵：它会把一次正常的观测读成一次越权。所以这里同时钉住
两件事——**真越权必须仍然被抓到**，以及**不属于任何人的数字不再算泄漏**。
"""

from __future__ import annotations

from scripts.capacity_leak_probe import find_leaks

MARKER_A = f"9{0:019d}"  # 90000000000000000000
MARKER_B = f"9{1:019d}"  # 90000000000000000001
OWNERS = {MARKER_A: "tenant-a", MARKER_B: "tenant-b"}


def test_another_tenants_marker_is_still_reported_as_a_leak():
    payload = '{"values": {"发票号码": "%s"}, "document_id": "abc"}' % MARKER_B
    leaks = find_leaks(payload, OWNERS, "tenant-a", "review_queue")
    assert leaks == ["review_queue:marker_of_tenant-b"]


def test_own_marker_is_not_a_leak():
    payload = '{"values": {"发票号码": "%s"}}' % MARKER_A
    assert find_leaks(payload, OWNERS, "tenant-a", "review_queue") == []


def test_digit_run_inside_a_checksum_is_not_read_as_a_foreign_marker():
    # G28 那次假阳性的形状：校验和里的一段 20 位数字，不属于任何播种租户。
    checksum = "94230950822845780695" + "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    payload = '{"checksum": "%s"}' % checksum
    assert find_leaks(payload, OWNERS, "tenant-a", "review_queue") == []


def test_probe_reports_every_foreign_marker_on_a_shared_page():
    # 三个租户、逐个查别人的标记：只要响应体里出现非自己的标记就必须报两次的量级。
    owners = {MARKER_A: "tenant-a", MARKER_B: "tenant-b", "9" + "0" * 18 + "2": "tenant-c"}
    body = '{"items": ["%s", "%s"]}' % (MARKER_B, "9" + "0" * 18 + "2")
    leaks = find_leaks(body, owners, "tenant-a", "search_hit")
    assert sorted(leaks) == ["search_hit:marker_of_tenant-b", "search_hit:marker_of_tenant-c"]
