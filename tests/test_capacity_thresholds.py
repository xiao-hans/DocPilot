"""已批链路阈值判定（`docs/19 §1.2`，2026-10-07 负责人批准默认值）。

判定必须能失败，所以这里同时喂"满足"和"超限"两组数据；第三组故意缺样本——
**没有读数不能读成通过**，那正是 G6 之前那份报告把阈值留空的原因。
"""

from __future__ import annotations

from scripts.capacity_thresholds import (
    LIST_P95_MS,
    SCHEDULE_P95_MS,
    evaluate,
    render_section,
)


def _payload(list_p95=12.8, schedule_p95=985.0, stream_rss=0.0, with_reads=True):
    reads = (
        {
            "latency_ms": {
                "list_documents": {"count": 100, "p50": 9.0, "p95": list_p95, "max": list_p95 * 2},
                "search_hit": {"count": 100, "p50": 8.0, "p95": list_p95 - 1, "max": list_p95},
            }
        }
        if with_reads
        else {"latency_ms": {}}
    )
    return {
        "counts": {"documents": 1000, "results": 1000},
        "reads": reads,
        "queue": {
            "workers": 8,
            "schedule_delay_ms": {"p50": 831.0, "p95": schedule_p95, "max": schedule_p95 * 1.5},
            "attempt_seconds": {"p50": 0.102, "p95": 0.145, "max": 0.3},
        },
        "large": {
            "stream_read_mb": 18.4,
            "stream_rss_delta_mb": stream_rss,
            "read_bytes_rss_delta_mb": 18.4,
        },
    }


def test_measured_run_satisfies_every_approved_link_threshold():
    verdict = evaluate(_payload())
    assert verdict["breaches"] == []
    by_name = {row["阈值"]: row["判定"] for row in verdict["rows"]}
    assert by_name["租户内列表/检索接口 P95（≤10 万文档量级）"] == "满足"
    assert by_name["调度延迟 P95（发布确认 → 开始执行）"] == "满足"
    assert by_name["单请求进程内存增量（禁止在请求路径整对象读）"] == "满足"


def test_each_threshold_can_fail_and_names_its_number():
    breaches = evaluate(_payload(list_p95=LIST_P95_MS + 40, schedule_p95=985.0, stream_rss=0.0))["breaches"]
    assert len(breaches) == 1 and "240.0 ms" in breaches[0]

    breaches = evaluate(_payload(list_p95=12.8, schedule_p95=SCHEDULE_P95_MS + 1000, stream_rss=0.0))["breaches"]
    assert len(breaches) == 1 and "调度延迟" in breaches[0]

    breaches = evaluate(_payload(list_p95=12.8, schedule_p95=985.0, stream_rss=5.0))["breaches"]
    assert len(breaches) == 1 and "流式读" in breaches[0]


def test_missing_samples_are_reported_as_unjudged_not_passed():
    verdict = evaluate({"counts": {"documents": 0}, "reads": {"latency_ms": {}}, "queue": {}, "large": {}})
    rows = {row["阈值"]: row["判定"] for row in verdict["rows"]}
    assert rows["租户内列表/检索接口 P95（≤10 万文档量级）"] == "未判定"
  # 没有读数就不该产生"满足"，否则缺数据会被写成通过。
    assert "满足" not in rows.values()


def test_processing_thresholds_stay_out_of_the_verdict():
    row = next(row for row in evaluate(_payload())["rows"] if row["阈值"].startswith("单任务端到端"))
    assert row["判定"] == "不判定"
    assert row["建议值"] == "暂不设定"


def test_rendered_section_shows_the_failure_in_plain_sight():
    passing = "\n".join(render_section(_payload()))
    assert "链路阈值判定" in passing and "全部满足" in passing

    failing = "\n".join(render_section(_payload(list_p95=400.0, schedule_p95=4000.0, stream_rss=3.0)))
    assert "不满足 3 项" in failing
    assert "退出码为 1" in failing
